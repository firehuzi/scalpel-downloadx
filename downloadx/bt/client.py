"""BT 子进程的异步客户端：启停进程、发命令、收进度事件。

父进程（引擎）只认这个类，不直接碰 libtorrent —— GPL 依赖止步于子进程边界。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator


class BtError(RuntimeError):
    """子进程报错、崩溃或协议异常。"""


def libtorrent_available() -> bool:
    try:
        import libtorrent  # noqa: F401
    except ImportError:
        return False
    return True


class BtClient:
    def __init__(
        self,
        *,
        python: str | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.python = python or sys.executable
        self.env = env
        self.cwd = cwd or Path(__file__).resolve().parents[2]  # 仓库根，-m 才能找到包
        self.proc: asyncio.subprocess.Process | None = None
        self.version = ""
        self._seq = 0
        self._futures: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []

    # ---- 生命周期 -----------------------------------------------------
    async def start(self, timeout: float = 30.0) -> "BtClient":
        env = dict(os.environ if self.env is None else self.env)
        self.proc = await asyncio.create_subprocess_exec(
            self.python,
            "-m",
            "downloadx.bt.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=env,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        ready = await self._wait_event("ready", timeout)
        if ready.get("event") == "fatal":
            raise BtError(ready.get("message") or "子进程启动失败")
        self.version = str(ready.get("libtorrent") or "")
        return self

    async def close(self, timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        if self.proc.returncode is None:
            try:
                await self.request("shutdown", timeout=timeout)
            except Exception:
                pass
        if self._reader:
            self._reader.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.proc.kill()
        self.proc = None

    # ---- 协议 ---------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            req_id = payload.get("req_id")
            if isinstance(req_id, int) and req_id in self._futures:
                future = self._futures[req_id]
                if future.done():
                    continue
                if payload.get("event") == "error":
                    future.set_exception(BtError(payload.get("message") or "命令失败"))
                else:
                    future.set_result(payload.get("data") or {})
            else:
                await self._events.put(payload)
        await self._events.put({"event": "eof"})

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            if len(self._stderr_tail) > 20:
                self._stderr_tail.pop(0)

    async def _wait_event(self, kind: str, timeout: float) -> dict[str, Any]:
        async def loop() -> dict[str, Any]:
            while True:
                event = await self._events.get()
                if event.get("event") == "eof":
                    tail = " / ".join(self._stderr_tail[-3:])
                    raise BtError(f"BT 子进程提前退出：{tail or '无输出'}")
                if event.get("event") == kind:
                    return event

        return await asyncio.wait_for(loop(), timeout=timeout)

    async def request(self, cmd: str, timeout: float = 60.0, **kwargs: Any) -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None:
            raise BtError("子进程未启动")
        self._seq += 1
        req_id = self._seq
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._futures[req_id] = future
        payload = {"cmd": cmd, "req_id": req_id, **kwargs}
        try:
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._futures.pop(req_id, None)

    # ---- 命令 ---------------------------------------------------------
    async def add(
        self,
        *,
        url: str | None = None,
        torrent_path: str | Path | None = None,
        save_path: str | Path,
        peers: list[tuple[str, int]] | None = None,
        download_limit: int = 0,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        if not url and not torrent_path:
            raise BtError("要么给 url（磁力链），要么给 torrent_path")
        return await self.request(
            "add",
            timeout=timeout,
            url=url or "",
            torrent_path=str(torrent_path) if torrent_path else None,
            save_path=str(save_path),
            peers=[list(peer) for peer in (peers or [])],
            download_limit=int(download_limit),
        )

    async def pause(self, info_hash: str) -> dict[str, Any]:
        return await self.request("pause", id=info_hash)

    async def resume(self, info_hash: str) -> dict[str, Any]:
        return await self.request("resume", id=info_hash)

    async def remove(self, info_hash: str) -> dict[str, Any]:
        return await self.request("remove", id=info_hash)

    async def status(self) -> dict[str, dict[str, Any]]:
        data = await self.request("status")
        return {item["id"]: item for item in data.get("torrents") or []}

    async def wait(
        self,
        info_hash: str,
        *,
        until: str = "completed",
        timeout: float = 300.0,
        poll: float = 0.3,
    ) -> dict[str, Any]:
        """轮询到满足条件；``until`` 取 ``completed`` 或具体百分比（如 ``"50"``）。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            current = (await self.status()).get(info_hash)
            if current is None:
                raise BtError(f"任务消失了：{info_hash}")
            if until == "completed" and (current["is_seeding"] or current["percent"] >= 100.0):
                return current
            if until.replace(".", "", 1).isdigit() and current["percent"] >= float(until):
                return current
            await asyncio.sleep(poll)
        raise BtError(f"等待超时：{until}")

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            event = await self._events.get()
            if event.get("event") == "eof":
                return
            yield event
