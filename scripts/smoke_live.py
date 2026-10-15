"""联调冒烟：拉起真实的 DownloadX 服务进程，驱动它完成一次真实下载。

用法::

    cd outputs/DownloadX
    python scripts/smoke_live.py

它验证的是"装出来的服务真的能用"：独立进程 -> 健康检查 -> 探测 -> 分片下载 ->
SHA-256 -> 落盘内容比对 -> 统计 -> 历史 -> 暂停/移除。全程只访问本机。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import httpx  # noqa: E402

from tests.support.server import TestServer, make_payload  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8751
BASE = f"http://127.0.0.1:{PORT}"
WORK = PROJECT / ".smoke"
STATE = WORK / "state"
DOWNLOADS = WORK / "downloads"


def wait_health(timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{BASE}/api/health", timeout=2.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:  # 服务还没起来
            last_error = exc
        time.sleep(0.3)
    raise RuntimeError(f"服务未在 {timeout}s 内启动：{last_error}")


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "downloadx",
            "serve",
            "--port",
            str(PORT),
            "--state-dir",
            str(STATE),
            "--download-dir",
            str(DOWNLOADS),
        ],
        cwd=str(PROJECT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    ok = False
    try:
        health = wait_health()
        print(f"[1] 服务进程 pid={server.pid}  健康检查: {health['app']} {health['version']}")
        print(f"    下载目录: {health['download_dir']}")

        with TestServer() as origin, httpx.Client(base_url=BASE, timeout=30) as client:
            size = 4 * 1024 * 1024
            name = "demo-package.bin"
            url = origin.url(f"/file/{name}?size={size}&delay=0.004&chunk=32768")

            ui = client.get("/")
            print(
                f"[2] Web UI: HTTP {ui.status_code}, {len(ui.text)} 字节, "
                f"标题包含 DownloadX: {'DownloadX' in ui.text}"
            )

            probe = client.post("/api/probe", json={"url": url}).json()
            print(f"[3] 探测: {probe['filename']}  {probe['size']} 字节  可续传={probe['resumable']}")

            task = client.post(
                "/api/tasks", json={"url": url, "connections": 6, "priority": 1}
            ).json()
            print(f"[4] 创建任务 {task['id'][:8]}  mode={task['mode']}")

            samples: list[tuple[float, float]] = []
            deadline = time.time() + 90
            current = task
            while time.time() < deadline:
                current = client.get(f"/api/tasks/{task['id']}").json()
                if current["status"] in ("completed", "failed", "canceled"):
                    break
                if current["status"] == "downloading":
                    samples.append((current["percent"], current["speed"]))
                time.sleep(0.2)
            peak = max((s for _, s in samples), default=0.0)
            print(
                f"[5] 进度采样 {len(samples)} 次, 峰值速度 {peak / 1024 / 1024:.2f} MB/s, "
                f"最终 {current['status']} {current['percent']}%  "
                f"分片 {current['segments_done']}/{current['segments_total']}"
            )
            print(f"    SHA-256 = {(current['sha256'] or '')[:32]}…")

            saved = Path(current["save_path"])
            ok = saved.exists() and saved.read_bytes() == make_payload(name, size)
            print(
                f"[6] 落盘文件 {saved.name}  "
                f"字节={saved.stat().st_size if saved.exists() else 0}  "
                f"内容校验={'通过' if ok else '不通过'}"
            )

            nl = client.post(
                "/agent/nl", json={"text": "现在有哪些任务", "planner": "rules"}
            ).json()
            print(
                f"[7] 自然语言指令 -> 规划器={nl['planner']}  "
                f"工具={nl['tool_calls'][0]['name']}  回复: {nl['reply']}"
            )

            manifest = client.get("/agent/manifest").json()
            print(f"[8] Agent 工具清单: {len(manifest['tools'])} 个（OpenAI 格式同源）")

            stats = client.get("/api/stats").json()
            print(
                f"[9] 统计: 完成={stats['completed_tasks']} "
                f"连接池={stats['connections']['in_use']}/{stats['connections']['limit']} "
                f"峰值连接={stats['connections']['peak_in_use']}"
            )

            history = client.get("/api/history").json()
            print(
                f"[10] 历史记录 {history['count']} 条, "
                f"首条={history['records'][0]['filename']} {history['records'][0]['status']}"
            )

            second = client.post(
                "/api/tasks",
                json={"url": origin.url(f"/file/second.bin?size={256 * 1024}"), "start": False},
            ).json()
            paused = client.post(f"/api/tasks/{second['id']}/pause").json()
            removed = client.delete(f"/api/tasks/{second['id']}").json()
            print(f"[11] 建任务 -> 暂停({paused['status']}) -> 移除({removed['removed']})")
    finally:
        server.terminate()
        try:
            out = server.communicate(timeout=10)[0].decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            server.kill()
            out = server.communicate()[0].decode("utf-8", errors="replace")
        tail = [line for line in out.splitlines() if line.strip()][-6:]
        print("\n服务端日志（末 6 行）：")
        for line in tail:
            print("   " + line)
    return 0 if ok and current["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
