"""诊断：国内镜像上多连接到底有没有收益，失败原因是什么。

做法：同一个大文件，各跑固定时长（默认 40s），比较这段时间内真实落盘字节数。
不等到整个文件下完，所以既快又公平。

用法::  python scripts/bench_mirror.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from downloadx import DownloadManager
from downloadx.config import Config

BASE = ROOT / "work" / "speed3"
URL = ("https://mirrors.ustc.edu.cn/ubuntu-releases/22.04/"
       "ubuntu-22.04.5-desktop-amd64.iso")
WINDOW = 40.0


def mb(v: float) -> float:
    return v / 1024 / 1024


async def run(connections: int) -> dict:
    tag = f"cn{connections}"
    cfg = Config(download_dir=BASE / tag / "d", state_dir=BASE / tag / "s")
    cfg.max_connections_per_task = connections
    cfg.max_concurrent_tasks = 1
    cfg.min_segment_size = 4 * 1024 * 1024
    cfg.chunk_size = 64 * 1024
    cfg.verify_sha256 = False
    cfg.max_retries = 3
    cfg.retry_base_delay = 0.3
    cfg.ensure_dirs()

    async with DownloadManager(cfg) as manager:
        task = await manager.add(URL, connections=connections, filename=f"iso-{connections}.bin",
                                 save_dir=cfg.download_dir)
        samples: list[float] = []
        started = None
        errors: list[str] = []
        queue = manager.bus.subscribe()
        deadline = time.monotonic() + WINDOW
        while time.monotonic() < deadline:
            if task.started_at and started is None:
                started = time.monotonic()
            while not queue.empty():
                event = queue.get_nowait()
                if event.type in ("segment_retry", "probe_retry", "task_failed", "task_invalidated"):
                    errors.append(f"{event.type}:{event.data.get('error') or event.data.get('reason')}")
            if task.status.value in ("completed", "failed", "canceled"):
                break
            if task.is_active and task.downloaded_size:
                samples.append(manager.speed_of(task.id))
            await asyncio.sleep(0.2)
        manager.bus.unsubscribe(queue)

        elapsed = (time.monotonic() - started) if started else 0.0
        downloaded = task.downloaded_size
        return {
            "connections": connections,
            "elapsed": elapsed,
            "bytes": downloaded,
            "throughput": downloaded / elapsed if elapsed else 0.0,
            "avg": sum(samples) / len(samples) if samples else 0.0,
            "peak": max(samples) if samples else 0.0,
            "segments": len(task.segments),
            "status": task.status.value,
            "error": task.error,
            "attempts": task.attempts,
            "events": errors[:6],
        }


def main() -> int:
    import shutil

    shutil.rmtree(BASE, ignore_errors=True)
    print(f"文件：{URL}")
    print(f"每轮固定观测 {WINDOW:.0f} 秒，比较真实落盘吞吐\n")
    rows = []
    for conn in (1, 4, 16):
        row = asyncio.run(run(conn))
        rows.append(row)
        print(f"  {conn:>2} 连接  分片={row['segments']:<3} "
              f"{row['elapsed']:>5.1f}s 内下了 {mb(row['bytes']):>7.1f} MB  "
              f"实测 {mb(row['throughput']):>6.2f} MB/s  "
              f"采样均值 {mb(row['avg']):>6.2f}  峰值 {mb(row['peak']):>6.2f}  "
              f"状态={row['status']} 尝试={row['attempts']}")
        if row["error"]:
            print(f"        错误：{row['error']}")
        for ev in row["events"]:
            print(f"        事件：{ev}")
        try:
            shutil.rmtree(BASE / f"cn{conn}", ignore_errors=True)
        except OSError:
            pass

    base = rows[0]["throughput"]
    best = max(rows, key=lambda r: r["throughput"])
    print(f"\n  最快：{best['connections']} 连接，{mb(best['throughput']):.2f} MB/s，"
          f"相对单连接 {best['throughput'] / base:.2f}x")
    shutil.rmtree(BASE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
