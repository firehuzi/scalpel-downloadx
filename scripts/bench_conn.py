"""A/B 测"这个站点吃几根连接"。

为什么必须交替跑：同一台机器上，同一份配置的吞吐会在一次会话里漂移几十倍
（实测 conn=1 从 2.89s 漂到 112s 传同一个 10MB 文件）。顺序跑各配置，量到的是
网络的脾气，不是配置的效果。所以这里按轮次交替：1,2,4, 1,2,4, ...

用法::

    python scripts/bench_conn.py <url> --connections 1,2,4 --rounds 3
    python scripts/bench_conn.py <url> --connections 1,4 --window 8 --rounds 2

每个组合在固定观测窗口内下载，然后暂停（保住已下字节）、量吞吐、换下一种。
结论看中位数与"相对单连接倍数"——倍数 < 1.2 就说明加连接没换来吞吐。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from downloadx import DownloadManager
from downloadx.config import Config

BASE = ROOT / "work" / "bench_conn"


def mb(value: float) -> float:
    return value / 1024 / 1024


async def one_run(url: str, connections: int, window: float, tag: str) -> dict:
    cfg = Config(download_dir=BASE / tag / "d", state_dir=BASE / tag / "s")
    cfg.max_connections_per_task = connections
    cfg.max_concurrent_tasks = 1
    cfg.min_segment_size = 1 * 1024 * 1024  # 小文件也能切出分片，便于对比
    cfg.chunk_size = 64 * 1024
    cfg.verify_sha256 = False
    cfg.ensure_dirs()

    async with DownloadManager(cfg) as manager:
        task = await manager.add(
            url, connections=connections, filename=f"{tag}.bin", save_dir=cfg.download_dir
        )
        started: float | None = None
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            if started is None and task.started_at:
                started = time.monotonic()
            if task.status.value in ("completed", "failed", "canceled"):
                break
            await asyncio.sleep(0.1)

        elapsed = (time.monotonic() - started) if started else 0.0
        finished = task.status.value == "completed"
        if task.is_active:
            await manager.pause(task.id)  # 保住进度、停掉连接

        import shutil

        shutil.rmtree(BASE / tag, ignore_errors=True)
        return {
            "connections": connections,
            "bytes": task.downloaded_size,
            "elapsed": elapsed,
            "throughput": task.downloaded_size / elapsed if elapsed else 0.0,
            "finished": finished,
            "retries": task.retry_count,
            "error": task.error,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="同一 URL 上交替对比不同连接数")
    parser.add_argument("url")
    parser.add_argument("--connections", default="1,2,4", help="逗号分隔，默认 1,2,4")
    parser.add_argument("--rounds", type=int, default=3, help="交替轮数，默认 3")
    parser.add_argument("--window", type=float, default=10.0, help="每组观测秒数，默认 10")
    args = parser.parse_args()

    combos = [int(x) for x in args.connections.split(",") if x.strip()]
    if 1 not in combos:
        combos = [1, *combos]  # 没有基准就没法判断

    import shutil

    shutil.rmtree(BASE, ignore_errors=True)
    print(f"URL        : {args.url}")
    print(f"组合       : {combos}   轮数: {args.rounds}   每组窗口: {args.window:g}s")
    print("（按轮次交替跑，避免把网络漂移当成配置差异）\n")

    samples: dict[int, list[float]] = {c: [] for c in combos}
    instant: list[str] = []
    for round_index in range(1, args.rounds + 1):
        for conn in combos:
            tag = f"c{conn}-r{round_index}"
            result = asyncio.run(one_run(args.url, conn, args.window, tag))
            samples[conn].append(result["throughput"])
            if result["finished"] and result["elapsed"] < 2.0:
                # 秒内下完：吞吐由启动开销支配，不能拿来比较配置
                instant.append(f"conn={conn}")
            note = "已下完" if result["finished"] else f"窗口内下 {mb(result['bytes']):.1f} MB"
            print(
                f"  round {round_index}  conn={conn:<2} "
                f"{result['elapsed']:>5.1f}s  {mb(result['throughput']):>6.2f} MB/s  {note}"
                + (f"  重试={result['retries']}" if result["retries"] else "")
                + (f"  错误={result['error']}" if result["error"] else "")
            )

    baseline = statistics.median(samples[1]) if samples[1] else 0.0
    print("\n中位数汇总：")
    if instant:
        print(
            f"  ⚠ 有 {len(instant)} 组在 2 秒内就下完了（{', '.join(sorted(set(instant)))}），"
            "这些数字受启动开销支配，不能用来比较配置。"
        )
        print("     换更大的文件，或把 --window 调小到能截断传输，再测一次。\n")
    for conn in combos:
        values = samples[conn]
        if not values:
            continue
        median = statistics.median(values)
        ratio = (median / baseline) if baseline else 0.0
        verdict = ""
        if conn != 1 and not instant:
            verdict = "并行有收益" if ratio >= 1.2 else "并行没换来吞吐（建议用 1）"
        print(f"  conn={conn:<2} {mb(median):>6.2f} MB/s  相对单连接 {ratio:>4.2f}x  {verdict}")
    print(
        "\n判读：倍数 ≈1 说明服务端按连接限速，加连接只是把同一份带宽切得更碎；"
        ">1.5 才说明并行真的有用。低于 1 说明加连接反而更慢。"
    )
    shutil.rmtree(BASE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
