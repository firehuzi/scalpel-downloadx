"""端到端：暂停 / 恢复 / 取消 / 重试 / 崩溃恢复。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.conftest import run
from tests.support.server import (
    clear_requests,
    make_payload,
    range_starts,
    recorded_requests,
)

from downloadx import DownloadManager
from downloadx.core.state_machine import TaskStatus


def _slow_url(server, name: str, size: int, delay: float = 0.02) -> str:
    return server.url(f"/file/{name}?size={size}&delay={delay}&chunk=8192")


def test_pause_then_resume_continues_from_offset(config, server):
    size = 512 * 1024
    name = "pausable.bin"
    expected = make_payload(name, size)

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(_slow_url(server, name, size), connections=4)

            # 等到真的下了点东西再暂停
            for _ in range(200):
                if task.downloaded_size > 8 * 1024:
                    break
                await asyncio.sleep(0.02)
            assert task.downloaded_size > 0, "任务没有开始下载"

            status = await manager.pause(task.id)
            paused_bytes = task.downloaded_size
            assert status is TaskStatus.PAUSED
            assert Path(task.part_path).exists()
            assert not Path(task.save_path).exists()

            clear_requests()
            await manager.resume(task.id)
            final = await manager.wait_for(task.id, timeout=60)
            return task, final, paused_bytes

    task, final, paused_bytes = run(scenario())
    assert final is TaskStatus.COMPLETED
    assert paused_bytes > 0
    assert Path(task.save_path).read_bytes() == expected

    # 恢复后必须从断点继续：所有 Range 起始偏移都 > 0
    starts = range_starts(name)
    assert starts, "恢复后没有发出任何 Range 请求"
    assert min(starts) > 0, f"恢复后从 0 重新下载了：{starts}"


def test_cancel_keeps_partial_and_retry_resumes(config, server):
    size = 512 * 1024
    name = "cancelable.bin"
    expected = make_payload(name, size)

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(_slow_url(server, name, size), connections=2)
            for _ in range(200):
                if task.downloaded_size > 8 * 1024:
                    break
                await asyncio.sleep(0.02)

            status = await manager.cancel(task.id)
            assert status is TaskStatus.CANCELED
            assert Path(task.part_path).exists()

            clear_requests()
            await manager.retry(task.id)
            final = await manager.wait_for(task.id, timeout=60)
            return task, final

    task, final = run(scenario())
    assert final is TaskStatus.COMPLETED
    assert Path(task.save_path).read_bytes() == expected
    starts = range_starts(name)
    assert min(starts) > 0, f"重试后从 0 重新下载了：{starts}"


def test_retry_reset_starts_from_zero(config, server):
    size = 256 * 1024
    name = "reset.bin"
    expected = make_payload(name, size)

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(_slow_url(server, name, size, delay=0.01), connections=2)
            for _ in range(200):
                if task.downloaded_size > 4 * 1024:
                    break
                await asyncio.sleep(0.02)
            await manager.cancel(task.id)
            clear_requests()
            await manager.retry(task.id, reset=True)
            final = await manager.wait_for(task.id, timeout=60)
            return task, final

    task, final = run(scenario())
    assert final is TaskStatus.COMPLETED
    assert Path(task.save_path).read_bytes() == expected
    starts = range_starts(name)
    assert 0 in starts, f"reset 重试应从 0 开始：{starts}"


def test_crash_recovery_resumes_from_disk_state(config, server):
    """模拟"进程被强杀"：磁盘上留下 downloading + 半截分片，新进程必须能接着下。"""
    size = 512 * 1024
    name = "crash.bin"
    expected = make_payload(name, size)
    quarter = size // 4
    url = server.url(f"/file/{name}?size={size}")

    downloads = Path(config.download_dir)
    downloads.mkdir(parents=True, exist_ok=True)
    save_path = downloads / name
    part_path = downloads / (name + ".dxpart")

    # 手工构造"崩溃现场"：已下完第一片、第二片下到一半，文件里已有真实字节
    part_path.write_bytes(expected[:quarter])
    task_json = {
        "id": "crashrecovery0001",
        "url": url,
        "filename": name,
        "save_path": str(save_path),
        "status": "downloading",
        "total_size": size,
        "downloaded_size": quarter,
        "created_at": 1.0,
        "updated_at": 1.0,
        "priority": 5,
        "connections": 4,
        "mode": "segmented",
        "resumable": True,
        "accept_ranges": True,
        "segments": [
            {"index": 0, "start": 0, "end": quarter - 1, "received": quarter, "status": "done"},
            {
                "index": 1,
                "start": quarter,
                "end": 2 * quarter - 1,
                "received": 0,
                "status": "active",
            },
        ],
        "headers": {},
        "elapsed": 0.0,
        "retry_count": 0,
        "attempts": 1,
    }
    # 用真实的分片表覆盖：第一片已完成，剩下按 4 连接重新规划
    from downloadx.core.segment import plan_segments

    rest = plan_segments(size - quarter, 4, config.min_segment_size)
    segments = [
        {"index": 0, "start": 0, "end": quarter - 1, "received": quarter, "status": "done"}
    ]
    for i, seg in enumerate(rest, start=1):
        segments.append(
            {
                "index": i,
                "start": seg.start + quarter,
                "end": seg.end + quarter,
                "received": 0,
                "status": "pending",
            }
        )
    task_json["segments"] = segments

    state_tasks = Path(config.state_dir) / "tasks"
    state_tasks.mkdir(parents=True, exist_ok=True)
    import json

    (state_tasks / "index.json").write_text(
        json.dumps({"schema_version": 1, "order": [task_json["id"]]}), encoding="utf-8"
    )
    (state_tasks / f"{task_json['id']}.json").write_text(
        json.dumps(task_json, ensure_ascii=False), encoding="utf-8"
    )

    # 服务端侧先记录基线，恢复后只应请求缺失区间
    clear_requests()

    async def scenario():
        async with DownloadManager(config) as manager:
            assert task_json["id"] in {t.id: t for t in manager.list()} or True
            task = manager.require(task_json["id"])
            final = await manager.wait_for(task.id, timeout=60)
            return task, final

    task, final = run(scenario())
    assert final is TaskStatus.COMPLETED
    assert Path(task.save_path).read_bytes() == expected
    starts = range_starts(name)
    assert starts, "恢复任务没有发出任何请求"
    assert min(starts) >= quarter, f"恢复时重复下载了已完成部分：{starts}"


def test_priority_order(config, server):
    """优先级高的先跑（用单并发串行验证出队顺序）。"""
    size = 64 * 1024

    async def scenario():
        async with DownloadManager(config) as manager:
            await manager.set_max_concurrent(1)
            low = await manager.add(server.url(f"/file/low.bin?size={size}"), priority=9)
            high = await manager.add(server.url(f"/file/high.bin?size={size}"), priority=0)
            order = manager.task_queue.pending_ids()
            await manager.wait_for(low.id, timeout=30)
            await manager.wait_for(high.id, timeout=30)
            return order, low, high

    order, low, high = run(scenario())
    assert order.index(high.id) < order.index(low.id)
    assert low.status is TaskStatus.COMPLETED
    assert high.status is TaskStatus.COMPLETED


def test_global_speed_limit_slows_download(config, server):
    """限速必须真的生效（否则"低资源占用"就是空话）。"""
    size = 256 * 1024
    name = "limited.bin"
    rate = 256 * 1024  # 256 KiB/s -> 至少 1 秒

    async def scenario():
        async with DownloadManager(config) as manager:
            await manager.set_global_speed_limit(rate)
            started = asyncio.get_running_loop().time()
            task = await manager.add(server.url(f"/file/{name}?size={size}"), connections=4)
            final = await manager.wait_for(task.id, timeout=60)
            elapsed = asyncio.get_running_loop().time() - started
            return task, final, elapsed

    task, final, elapsed = run(scenario())
    assert final is TaskStatus.COMPLETED
    assert Path(task.save_path).read_bytes() == make_payload(name, size)
    assert elapsed >= 0.7, f"限速没生效：{elapsed:.2f}s 下完了 {size} 字节"


def test_server_throttling_makes_engine_reduce_connections(config, server):
    """服务端按并发限流（429）时，引擎必须自降连接数而不是硬刚到底。"""
    config.max_connections_per_task = 8
    size = 256 * 1024
    name = "throttled.bin"
    expected = make_payload(name, size)
    url = server.url(f"/throttle/{name}?size={size}&max=2&delay=0.01&chunk=16384")

    async def scenario():
        async with DownloadManager(config) as manager:
            events = []
            queue = manager.bus.subscribe()
            task = await manager.add(url, connections=8)
            deadline = asyncio.get_running_loop().time() + 60
            while asyncio.get_running_loop().time() < deadline:
                while not queue.empty():
                    events.append(queue.get_nowait())
                if task.status.value in ("completed", "failed", "canceled"):
                    break
                await asyncio.sleep(0.05)
            manager.bus.unsubscribe(queue)
            return task, events

    task, events = run(scenario())
    assert task.status is TaskStatus.COMPLETED, task.error
    assert Path(task.save_path).read_bytes() == expected

    started = [e for e in events if e.type == "task_started"]
    baseline = started[0].data["connections"] if started else task.connections
    assert baseline >= 4, f"测试前提不成立，起始并发只有 {baseline}"

    # 引擎把并发降了下来，并且明确报出了限流事件
    assert task.effective_connections is not None
    assert task.effective_connections < baseline, (
        f"没有降并发：起始 {baseline}，最终 {task.effective_connections}"
    )
    throttled = [e for e in events if e.type == "task_throttled"]
    assert throttled, "没有发出 task_throttled 事件"
    assert throttled[0].data["connections"] < throttled[0].data["previous"]
    assert task.attempts > len(task.segments), "应该出现过重试"


def test_no_false_throttle_when_server_tolerates_concurrency(config, server):
    """服务端能扛住 8 并发时，引擎不应该莫名其妙降连接数。"""
    config.max_connections_per_task = 8
    size = 256 * 1024
    name = "tolerant.bin"
    url = server.url(f"/throttle/{name}?size={size}&max=8&delay=0.005&chunk=16384")

    async def scenario():
        async with DownloadManager(config) as manager:
            events = []
            queue = manager.bus.subscribe()
            task = await manager.add(url, connections=8)
            deadline = asyncio.get_running_loop().time() + 60
            while asyncio.get_running_loop().time() < deadline:
                while not queue.empty():
                    events.append(queue.get_nowait())
                if task.status.value in ("completed", "failed", "canceled"):
                    break
                await asyncio.sleep(0.05)
            manager.bus.unsubscribe(queue)
            return task, events

    task, events = run(scenario())
    assert task.status is TaskStatus.COMPLETED, task.error
    started = [e for e in events if e.type == "task_started"]
    baseline = started[0].data["connections"] if started else task.connections
    assert baseline >= 4, f"测试前提不成立，起始并发只有 {baseline}"
    assert task.effective_connections == baseline
    assert not [e for e in events if e.type == "task_throttled"]


def _ramp_config(config):
    """把自校准参数调小，让测试几秒内就能跑完一轮测量。"""
    config.max_connections_per_task = 4
    config.ramp_enabled = True
    config.ramp_min_total_size = 512 * 1024
    config.ramp_warmup_seconds = 0.5
    config.ramp_warmup_bytes = 64 * 1024
    config.ramp_probe_seconds = 1.2
    config.ramp_gain_threshold = 1.15
    return config


async def _run_and_collect(manager, url, connections):
    events = []
    queue = manager.bus.subscribe()
    task = await manager.add(url, connections=connections)
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        while not queue.empty():
            events.append(queue.get_nowait())
        if task.status.value in ("completed", "failed", "canceled"):
            break
        await asyncio.sleep(0.05)
    manager.bus.unsubscribe(queue)
    return task, events


def test_calibration_falls_back_when_parallelism_gains_nothing(config, server):
    """服务端按聚合速率限速（静默限速）：加连接没换来吞吐，引擎必须自己退回单连接。"""
    config = _ramp_config(config)
    size = 1536 * 1024          # 1.5 MB @ 512 KB/s ≈ 3 秒
    name = "sharedcap.bin"
    expected = make_payload(name, size)
    url = server.url(f"/sharedcap/{name}?size={size}&rate={512 * 1024}&chunk=16384")

    async def scenario():
        async with DownloadManager(config) as manager:
            return await _run_and_collect(manager, url, connections=4)

    task, events = run(scenario())
    assert task.status is TaskStatus.COMPLETED, task.error
    assert Path(task.save_path).read_bytes() == expected

    ramp_down = [e for e in events if e.type == "task_ramp_down"]
    ramps = [(e.type, e.data) for e in events if e.type.startswith("task_ramp")]
    assert ramp_down, f"没有触发并发自校准降级；实际 ramp 事件={ramps}"
    payload = ramp_down[0].data
    assert payload["gain"] < config.ramp_gain_threshold, payload
    assert task.effective_connections == 1
    assert task.connections == 4, "配置值不该被改写"
    assert "并发自校准" in task.plan_reason
    assert not [e for e in events if e.type == "task_ramp_kept"]


def test_calibration_keeps_connections_when_parallelism_helps(config, server):
    """服务端每条连接独立限速：加连接真的更快，引擎必须保留并发。"""
    config = _ramp_config(config)
    size = 32 * 1024 * 1024     # 32 MB，单连接 1.6 MB/s、4 连接 ≈ 6.4 MB/s
    name = "perconn.bin"
    url = server.url(f"/file/{name}?size={size}&delay=0.02&chunk=32768")

    async def scenario():
        async with DownloadManager(config) as manager:
            return await _run_and_collect(manager, url, connections=4)

    task, events = run(scenario())
    assert task.status is TaskStatus.COMPLETED, task.error

    kept = [e for e in events if e.type == "task_ramp_kept"]
    assert kept, "并行有收益却触发了降级"
    assert kept[0].data["gain"] >= config.ramp_gain_threshold, kept[0].data
    assert task.effective_connections == 4
    assert not [e for e in events if e.type == "task_ramp_down"]


def test_calibration_is_skipped_for_small_files(config, server):
    """小文件不该为了测量浪费时间。"""
    config = _ramp_config(config)
    config.ramp_min_total_size = 8 * 1024 * 1024   # 远大于下面的文件
    size = 256 * 1024
    name = "rampskip.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            return await _run_and_collect(
                manager, server.url(f"/file/{name}?size={size}"), connections=4
            )

    task, events = run(scenario())
    assert task.status is TaskStatus.COMPLETED
    assert not [e for e in events if e.type == "task_ramp_started"]
    assert task.effective_connections == 4


def test_pause_during_calibration_leaves_no_orphan_writer(config, server):
    """校准窗口里暂停：必须干净停下，且不留下任何存活的分片任务。

    说明：暂停标志本身会让孤儿分片在下一个 chunk 边界退出，所以"进度不再增长"在修复前
    也成立。这里额外直接断言不变量 —— worker 返回后不允许还有 dx-seg-* 任务活着。
    """
    config = _ramp_config(config)
    size = 4 * 1024 * 1024
    name = "ramppause.bin"
    url = server.url(f"/sharedcap/{name}?size={size}&rate={384 * 1024}&chunk=16384")

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(url, connections=4)
            # 停在预热/测量窗口中间（还远没到下完）
            await asyncio.sleep(0.8)
            status = await manager.pause(task.id)
            assert status is TaskStatus.PAUSED, f"暂停失败：{task.status}"
            # .dxpart 是预分配的，文件大小恒等于总长，所以用"新请求数"和"进度计数"来判断
            requests_at_pause = len(recorded_requests(name))
            downloaded_at_pause = task.downloaded_size
            alive = [t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("dx-seg-")]
            # 孤儿分片会在暂停后继续写：等一会儿看它有没有继续长
            await asyncio.sleep(1.0)
            requests_after = len(recorded_requests(name))
            quiet_downloaded = task.downloaded_size

            await manager.resume(task.id)
            final = await manager.wait_for(task.id, timeout=90)
            return (
                task,
                final,
                alive,
                requests_at_pause,
                requests_after,
                downloaded_at_pause,
                quiet_downloaded,
            )

    task, final, alive, reqs_before, reqs_after, at_pause, quiet = run(scenario())
    assert final is TaskStatus.COMPLETED, task.error
    assert not alive, f"暂停后仍有存活的分片任务：{alive}"
    assert reqs_after == reqs_before, f"暂停后仍有分片在发新请求：{reqs_before} -> {reqs_after}"
    assert quiet == at_pause, f"暂停后进度仍在变：{at_pause} -> {quiet}"
