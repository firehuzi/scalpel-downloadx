"""终验：在被限流的真实镜像上，用激进的 16 连接下完一整个 ISO 并校验官方 SHA-256。

目的：证明"服务端限流 -> 引擎自降并发 -> 数据依然正确"这条链路在真实网络里成立。

用法::  python scripts/bench_verify.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from downloadx import DownloadManager
from downloadx.config import Config

BASE = ROOT / "work" / "verify"
MIRROR = "https://mirrors.ustc.edu.cn/debian-cd/current/amd64/iso-cd"


def mb(v: float) -> float:
    return v / 1024 / 1024


def resolve_target() -> tuple[str, str, int]:
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        sums = client.get(f"{MIRROR}/SHA256SUMS")
        sums.raise_for_status()
        for line in sums.text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].endswith("amd64-netinst.iso"):
                digest, name = parts[0], parts[1]
                head = client.head(f"{MIRROR}/{name}")
                head.raise_for_status()
                size = int(head.headers.get("Content-Length") or 0)
                ranges = head.headers.get("Accept-Ranges", "").lower() == "bytes"
                print(f"目标：{name}")
                print(f"  官方 SHA-256 : {digest}")
                print(f"  大小         : {mb(size):.0f} MB   断点续传={ranges}")
                return f"{MIRROR}/{name}", digest, size
    raise RuntimeError("没找到 amd64-netinst.iso")


async def main_async(url: str, digest: str, size: int, connections: int) -> dict:
    cfg = Config(download_dir=BASE / "downloads", state_dir=BASE / "state")
    cfg.max_connections_per_task = connections
    cfg.max_concurrent_tasks = 1
    cfg.min_segment_size = 8 * 1024 * 1024
    cfg.chunk_size = 128 * 1024
    cfg.verify_sha256 = True
    cfg.max_retries = 6
    cfg.ensure_dirs()

    async with DownloadManager(cfg) as manager:
        events = []
        queue = manager.bus.subscribe()
        started = time.monotonic()
        task = await manager.add(url, connections=connections, expected_sha256=digest,
                                 save_dir=cfg.download_dir)
        samples: list[float] = []
        last_report = 0.0
        deadline = started + 900
        while time.monotonic() < deadline:
            while not queue.empty():
                events.append(queue.get_nowait())
            if task.status.value in ("completed", "failed", "canceled"):
                break
            if task.is_active:
                if task.downloaded_size:
                    samples.append(manager.speed_of(task.id))
                now = time.monotonic() - started
                if now - last_report >= 20:
                    last_report = now
                    print(f"    [{now:5.0f}s] {mb(task.downloaded_size):6.0f}/{mb(size):.0f} MB  "
                          f"{mb(manager.speed_of(task.id)):5.2f} MB/s  "
                          f"并发={task.effective_connections}  重试事件="
                          f"{sum(1 for e in events if e.type == 'segment_retry')}")
            await asyncio.sleep(0.2)
        manager.bus.unsubscribe(queue)
        elapsed = time.monotonic() - started
        return {
            "status": task.status.value,
            "elapsed": elapsed,
            "bytes": task.downloaded_size,
            "avg": task.downloaded_size / elapsed if elapsed else 0.0,
            "peak": max(samples) if samples else 0.0,
            "effective": task.effective_connections,
            "configured": connections,
            "segments": len(task.segments),
            "sha256": task.sha256,
            "verified": task.verified,
            "retry_events": sum(1 for e in events if e.type == "segment_retry"),
            "throttle_events": sum(1 for e in events if e.type == "task_throttled"),
            "error": task.error,
            "path": task.save_path,
        }


def main() -> int:
    import shutil

    shutil.rmtree(BASE, ignore_errors=True)
    url, digest, size = resolve_target()
    print("\n用 16 连接下载（故意激进，看引擎会不会自己收敛）...")
    result = asyncio.run(main_async(url, digest, size, 16))

    print()
    print("=" * 70)
    print(f"状态           : {result['status']}")
    print(f"耗时           : {result['elapsed']:.1f}s   平均 {mb(result['avg']):.2f} MB/s"
          f"   峰值 {mb(result['peak']):.2f} MB/s")
    print(f"并发           : 配置 {result['configured']} -> 实际 {result['effective']}"
          f"（限流事件 {result['throttle_events']} 次，重试 {result['retry_events']} 次）")
    print(f"分片           : {result['segments']}")
    print(f"SHA-256 校验   : {result['verified']}   {result['sha256']}")
    print(f"与官方一致     : {result['sha256'] == digest}")
    if result["error"]:
        print(f"错误           : {result['error']}")
    saved = Path(result["path"])
    print(f"落盘           : {saved.name}  {mb(saved.stat().st_size) if saved.exists() else 0:.0f} MB")

    ok = result["status"] == "completed" and result["sha256"] == digest
    print("\n结论：" + ("通过 —— 激进并发被限流后自动收敛，整包哈希与官方一致"
                       if ok else "未通过，需要继续排查"))
    shutil.rmtree(BASE, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
