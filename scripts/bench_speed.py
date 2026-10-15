"""用 DownloadX 本体测量下载速度（回环上限 + 外网多连接对比）。

两部分：
  A. 本机回环：隔离"引擎 + 磁盘"上限（网络不参与）
  B. 真实外网：CacheFly 100MB，对比 1 / 4 / 16 连接

用法::

    python scripts/bench_speed.py             # A + B，约 60 秒
    python scripts/bench_speed.py --only-a    # 只测本机上限，约 10 秒
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
from tests.support.server import TestServer

BASE = ROOT / "work" / "speed"
REAL_URL = "https://cachefly.cachefly.net/100mb.test"
REAL_SIZE = 100 * 1024 * 1024


def mb(value: float) -> float:
    return value / 1024 / 1024


async def measure(url: str, *, name: str, connections: int, tag: str) -> dict:
    cfg = Config(
        download_dir=BASE / tag / "downloads",
        state_dir=BASE / tag / "state",
    )
    cfg.connections_per_task = connections
    cfg.max_connections_per_task = connections
    cfg.max_concurrent_tasks = 1
    cfg.min_segment_size = 4 * 1024 * 1024
    cfg.chunk_size = 64 * 1024
    cfg.checkpoint_interval = 1.0
    cfg.verify_sha256 = False          # 不把 CPU 花在哈希上，专测吞吐
    cfg.ensure_dirs()

    async with DownloadManager(cfg) as manager:
        task = await manager.add(url, filename=name, connections=connections, save_dir=cfg.download_dir)
        samples: list[float] = []
        deadline = time.monotonic() + 180
        started = None
        while time.monotonic() < deadline:
            if task.started_at and started is None:
                started = time.monotonic()
            if task.status.value in ("completed", "failed", "canceled"):
                break
            if task.is_active and task.downloaded_size:
                samples.append(manager.speed_of(task.id))
            await asyncio.sleep(0.2)
        elapsed = (time.monotonic() - started) if started else 0.0
        size = task.downloaded_size
        saved = Path(task.save_path)
        return {
            "tag": tag,
            "connections": connections,
            "segments": len(task.segments),
            "mode": task.mode,
            "status": task.status.value,
            "bytes": size,
            "elapsed": elapsed,
            "avg": size / elapsed if elapsed else 0.0,
            "peak": max(samples) if samples else 0.0,
            "on_disk": saved.stat().st_size if saved.exists() else 0,
            "error": task.error,
        }


def report(rows: list[dict]) -> None:
    print(f"  {'连接':<4} {'分片':<4} {'耗时':>8} {'平均':>12} {'峰值':>12} {'落盘':>10}  状态")
    for r in rows:
        print(
            f"  {r['connections']:<4} {r['segments']:<4} {r['elapsed']:>7.2f}s "
            f"{mb(r['avg']):>9.2f} MB/s {mb(r['peak']):>9.2f} MB/s "
            f"{mb(r['on_disk']):>7.1f} MB  {r['status']}"
            + (f"  ({r['error']})" if r["error"] else "")
        )


def main() -> int:
    import shutil

    shutil.rmtree(BASE, ignore_errors=True)

    only_loopback = "--only-a" in sys.argv

    print("=" * 78)
    print("A. 本机回环（隔离引擎与磁盘上限，网络不参与）")
    print("=" * 78)
    print("   磁盘裸写基线：", end="")
    probe_disk = BASE / "disk-baseline.bin"
    probe_disk.parent.mkdir(parents=True, exist_ok=True)
    blob = b"\0" * (8 * 1024 * 1024)
    started = time.monotonic()
    with open(probe_disk, "wb", buffering=0) as fh:
        for _ in range(16):          # 128 MB
            fh.write(blob)
        fh.flush()
        import os
        os.fsync(fh.fileno())
    disk_elapsed = time.monotonic() - started
    print(f"{mb(128 * 1024 * 1024) / disk_elapsed:.1f} MB/s")

    loop_rows = []
    with TestServer() as srv:
        size = 128 * 1024 * 1024
        url = srv.url(f"/file/loopback.bin?size={size}&chunk=262144")
        for conn in (1, 8):
            loop_rows.append(
                asyncio.run(
                    measure(url, name=f"loopback-{conn}.bin", connections=conn, tag=f"loop{conn}")
                )
            )
    report(loop_rows)

    if only_loopback:
        return 0

    print()
    print("=" * 78)
    print(f"B. 真实外网：{REAL_URL}（{mb(REAL_SIZE):.0f} MB）")
    print("=" * 78)
    real_rows = []
    for conn in (1, 4, 16):
        row = asyncio.run(
            measure(REAL_URL, name=f"cachefly-{conn}.bin", connections=conn, tag=f"real{conn}")
        )
        real_rows.append(row)
        print(f"  -> {conn} 连接完成：{mb(row['avg']):.2f} MB/s 平均")
    print()
    report(real_rows)

    best = max(real_rows, key=lambda r: r["avg"])
    print(f"\n结论：{best['connections']} 连接最快，平均 {mb(best['avg']):.2f} MB/s，"
          f"峰值 {mb(best['peak']):.2f} MB/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
