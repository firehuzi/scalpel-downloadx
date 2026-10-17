"""BT 子进程：持有一个 libtorrent 会话，用 stdin/stdout 的 JSON lines 通信。

命令（每行一个 JSON，带 ``req_id``）：``add`` / ``pause`` / ``resume`` / ``remove`` /
``status`` / ``shutdown``。进度事件（``event=progress``）不带 ``req_id``，由客户端推入事件队列。

本进程不认识 HTTP 引擎：它只会加种子、暂停、继续、删任务、报进度。
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROGRESS_INTERVAL = 0.5
_BTIH = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _resume_path(save_path: Path, info_hash: str) -> Path:
    return save_path / ".downloadx-bt" / f"{info_hash}.resume"


def _snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    status = entry["handle"].status()
    errc = getattr(status, "errc", None)
    return {
        "id": entry["id"],
        "name": status.name or entry.get("name") or "",
        "percent": round(status.progress * 100, 2),
        "rate": status.download_rate,
        "peers": status.num_peers,
        "done": status.total_done,
        "total": status.total_wanted,
        "pieces": status.num_pieces,
        "is_seeding": status.is_seeding,
        "state": str(status.state),
        "error": errc.message() if errc else "",
    }


def _add(lt: Any, session: Any, torrents: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    url = req.get("url") or ""
    torrent_path = req.get("torrent_path")
    save_path = Path(req["save_path"]).expanduser().resolve()
    save_path.mkdir(parents=True, exist_ok=True)

    ti = lt.torrent_info(str(torrent_path)) if torrent_path else None
    if ti is not None:
        info_hash = str(ti.info_hash())
    else:
        match = _BTIH.search(url)
        if not match:
            raise ValueError("不是合法的 magnet 链接（找不到 btih）")
        info_hash = match.group(1).lower()

    resume_file = _resume_path(save_path, info_hash)
    restored = False
    if resume_file.is_file():
        # 从 resume data 建 params；注意不要给结果赋 ti —— 会把分片状态冲掉
        atp = lt.read_resume_data(resume_file.read_bytes())
        restored = True
    elif ti is not None:
        atp = lt.add_torrent_params()
        atp.ti = ti
    else:
        atp = lt.parse_magnet_uri(url)

    atp.save_path = str(save_path)
    peers = [tuple(peer) for peer in (req.get("peers") or [])]
    if peers:
        atp.peers = peers

    handle = session.add_torrent(atp)
    limit = int(req.get("download_limit") or 0)
    if limit > 0:
        handle.set_download_limit(limit)

    torrents[info_hash] = {
        "id": info_hash,
        "handle": handle,
        "save_path": save_path,
        "resume_file": resume_file,
        "name": ti.name() if ti is not None else "",
    }
    _write({
        "event": "added",
        "id": info_hash,
        "restored": restored,
        "save_path": str(save_path),
        "resume_file": str(resume_file),
    })
    return {"id": info_hash, "restored": restored, "resume_file": str(resume_file)}


def _pause(lt: Any, session: Any, torrents: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    entry = torrents.get(req["id"])
    if entry is None:
        raise ValueError(f"没有这个任务：{req['id']}")
    handle = entry["handle"]
    handle.pause()
    # 硬性要求：不 flush 会把"还在缓存里"的块记成已完成，换进程后文件就坏了
    handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
    data = b""
    deadline = time.time() + 15
    while time.time() < deadline and not data:
        for alert in session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                data = bytes(lt.write_resume_data_buf(alert.params))
        if not data:
            time.sleep(0.05)
    if not data:
        raise RuntimeError("暂停失败：拿不到 resume data")
    entry["resume_file"].parent.mkdir(parents=True, exist_ok=True)
    entry["resume_file"].write_bytes(data)
    snapshot = _snapshot(entry)
    _write({"event": "paused", **snapshot})
    return {"id": entry["id"], "resume_bytes": len(data), "pieces": snapshot["pieces"]}


def _resume(torrents: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    entry = torrents.get(req["id"])
    if entry is None:
        raise ValueError(f"没有这个任务：{req['id']}")
    entry["handle"].resume()
    return {"id": entry["id"]}


def _remove(session: Any, torrents: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    entry = torrents.pop(req["id"], None)
    if entry is None:
        raise ValueError(f"没有这个任务：{req['id']}")
    session.remove_torrent(entry["handle"])
    return {"id": req["id"]}


def main() -> int:
    try:
        import libtorrent as lt
    except ImportError as exc:  # 依赖缺失时明确告知，不要静默失败
        _write({"event": "fatal", "message": f"缺少 libtorrent：{exc}"})
        return 2

    session = lt.session({"listen_interfaces": "0.0.0.0:0"})
    torrents: dict[str, Any] = {}
    commands: "queue.Queue[dict[str, Any]]" = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                commands.put(json.loads(line))
            except ValueError:
                commands.put({"cmd": "__bad__"})
        commands.put({"cmd": "shutdown"})

    threading.Thread(target=reader, daemon=True).start()
    _write({"event": "ready", "libtorrent": lt.version})

    last_emit = 0.0
    while True:
        try:
            req = commands.get(timeout=PROGRESS_INTERVAL / 2)
        except queue.Empty:
            req = None

        if req is not None:
            req_id = req.get("req_id")
            cmd = req.get("cmd")
            try:
                if cmd == "add":
                    data = _add(lt, session, torrents, req)
                elif cmd == "pause":
                    data = _pause(lt, session, torrents, req)
                elif cmd == "resume":
                    data = _resume(torrents, req)
                elif cmd == "remove":
                    data = _remove(session, torrents, req)
                elif cmd == "status":
                    data = {"torrents": [_snapshot(entry) for entry in torrents.values()]}
                elif cmd == "shutdown":
                    for entry in list(torrents.values()):
                        try:
                            _pause(lt, session, torrents, {"id": entry["id"]})
                        except Exception:  # 退出时尽力保存，失败不阻塞
                            pass
                    _write({"event": "bye"})
                    return 0
                else:
                    raise ValueError(f"未知命令：{cmd}")
                _write({"req_id": req_id, "event": "ok", "data": data})
            except Exception as exc:  # 单个命令失败不该杀掉子进程
                _write({"req_id": req_id, "event": "error",
                        "message": f"{type(exc).__name__}: {exc}"})

        now = time.monotonic()
        if now - last_emit >= PROGRESS_INTERVAL and torrents:
            last_emit = now
            for entry in list(torrents.values()):
                try:
                    _write({"event": "progress", **_snapshot(entry)})
                except Exception:
                    continue


if __name__ == "__main__":
    raise SystemExit(main())
