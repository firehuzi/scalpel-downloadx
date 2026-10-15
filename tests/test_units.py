"""单元测试：状态机、分片、解析、重试、存储、限速、锁、计量。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from downloadx.config import Config
from downloadx.core.download_task import DownloadTask
from downloadx.core.metrics import SpeedMeter, format_bytes, format_eta
from downloadx.core.segment import (
    Segment,
    SegmentStatus,
    build_plan,
    plan_segments,
    total_received,
)
from downloadx.core.state_machine import (
    InvalidTransitionError,
    StateMachine,
    TaskStatus,
    can_transition,
    is_terminal,
)
from downloadx.network.range_request import (
    build_range_header,
    content_range_total,
    filename_from_url,
    guess_filename,
    parse_content_range,
    sanitize_filename,
    supports_ranges,
)
from downloadx.network.retry import (
    HttpStatusError,
    RetryExhausted,
    RetryPolicy,
    is_retryable,
    retry_async,
)
from downloadx.queue.bandwidth_controller import BandwidthController, TokenBucket
from downloadx.queue.priority_queue import PriorityQueue
from downloadx.storage.checksum import IncrementalHasher, sha256_file, verify_file
from downloadx.storage.checkpoint import CheckpointManager
from downloadx.storage.file_writer import FileWriter, finalize_part
from downloadx.storage.lock import SingleInstanceLock
from downloadx.storage.metadata_store import MetadataStore

from tests.conftest import run


# ----------------------------------------------------------------------
# 状态机
# ----------------------------------------------------------------------
def test_state_machine_happy_path():
    machine = StateMachine()
    assert machine.status is TaskStatus.QUEUED
    machine.to(TaskStatus.DOWNLOADING)
    machine.to(TaskStatus.PAUSED)
    machine.to(TaskStatus.QUEUED)
    machine.to(TaskStatus.DOWNLOADING)
    machine.to(TaskStatus.VERIFYING)
    assert machine.to(TaskStatus.COMPLETED) is TaskStatus.COMPLETED
    assert machine.is_terminal


@pytest.mark.parametrize(
    "start,target",
    [
        (TaskStatus.QUEUED, TaskStatus.COMPLETED),
        (TaskStatus.QUEUED, TaskStatus.VERIFYING),
        (TaskStatus.PAUSED, TaskStatus.DOWNLOADING),
        (TaskStatus.VERIFYING, TaskStatus.PAUSED),
        (TaskStatus.CANCELED, TaskStatus.DOWNLOADING),
    ],
)
def test_state_machine_rejects_illegal(start, target):
    assert can_transition(start, target) is False
    with pytest.raises(InvalidTransitionError):
        StateMachine(start).to(target)


def test_state_machine_listeners():
    machine = StateMachine()
    seen = []
    machine.on_change(lambda old, new: seen.append((str(old), str(new))))
    machine.to(TaskStatus.DOWNLOADING)
    machine.to(TaskStatus.FAILED)
    assert seen == [("queued", "downloading"), ("downloading", "failed")]


def test_terminal_status_includes_failed():
    assert is_terminal(TaskStatus.FAILED)
    assert is_terminal(TaskStatus.COMPLETED)
    assert is_terminal(TaskStatus.CANCELED)
    assert not is_terminal(TaskStatus.PAUSED)


# ----------------------------------------------------------------------
# 分片规划
# ----------------------------------------------------------------------
@pytest.mark.parametrize("total,conn,minimum", [
    (100, 4, 32), (1024, 8, 64), (1000, 3, 100), (10, 4, 4096), (1, 8, 1),
])
def test_plan_segments_cover_without_overlap(total, conn, minimum):
    segments = plan_segments(total, conn, minimum)
    assert segments, "必须至少切出一片"
    assert sum(s.size for s in segments) == total
    assert segments[0].start == 0
    assert segments[-1].end == total - 1
    for prev, nxt in zip(segments, segments[1:]):
        assert prev.end + 1 == nxt.start, "分片必须连续且不重叠"
    assert [s.index for s in segments] == list(range(len(segments)))


def test_build_plan_modes():
    assert build_plan(total_size=None, connections=4, min_segment_size=1, resumable=False).mode == "streaming"
    single = build_plan(total_size=100, connections=4, min_segment_size=1024, resumable=False)
    assert single.mode == "single" and len(single.segments) == 1
    multi = build_plan(total_size=10 ** 6, connections=4, min_segment_size=10 ** 5, resumable=True)
    assert multi.mode == "segmented" and len(multi.segments) == 4


def test_segment_progress_tracking():
    segment = Segment(index=0, start=0, end=99)
    assert segment.size == 100 and segment.next_offset == 0
    segment.received = 30
    assert segment.next_offset == 30 and segment.remaining == 70
    assert not segment.is_done
    segment.mark_done()
    assert segment.is_done and segment.received == 100
    assert total_received([segment]) == 100


# ----------------------------------------------------------------------
# Range 解析
# ----------------------------------------------------------------------
def test_parse_content_range():
    assert parse_content_range("bytes 0-1023/4096") == (0, 1023, 4096)
    assert parse_content_range("bytes 5-9/*") == (5, 9, None)
    assert parse_content_range("nonsense") is None
    assert content_range_total("bytes 0-1/999") == 999
    assert build_range_header(10, 20) == "bytes=10-20"
    with pytest.raises(ValueError):
        build_range_header(20, 10)


def test_supports_ranges_header():
    assert supports_ranges({"Accept-Ranges": "bytes"})
    assert supports_ranges({"accept-ranges": "BYTES"})
    assert not supports_ranges({"Accept-Ranges": "none"})
    assert not supports_ranges({})


def test_filename_detection():
    assert filename_from_url("https://x.com/a/b/report%20final.pdf") == "report final.pdf"
    assert filename_from_url("https://x.com/") is None
    assert guess_filename("https://x.com/a.bin") == "a.bin"
    assert (
        guess_filename(
            "https://x.com/dl",
            {"Content-Disposition": "attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95.zip"},
        )
        == "测试.zip"
    )


def test_sanitize_filename_windows_rules():
    assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_filename("CON") == "CON_"
    assert sanitize_filename("nul.txt") == "nul_.txt"
    assert sanitize_filename("  trailing.  ") == "trailing"
    assert sanitize_filename("") == "download"
    long_name = "x" * 300 + ".bin"
    assert len(sanitize_filename(long_name)) <= 180


# ----------------------------------------------------------------------
# 重试
# ----------------------------------------------------------------------
def test_retry_policy_backoff():
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=10.0, jitter=0)
    assert [policy.delay_for(i) for i in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 10.0]


def test_retryable_classification():
    assert is_retryable(HttpStatusError(503, "u"))
    assert not is_retryable(HttpStatusError(404, "u"))
    assert not is_retryable(HttpStatusError(416, "u"))
    assert is_retryable(ConnectionError("boom"))
    assert not is_retryable(asyncio.CancelledError())


def test_retry_async_succeeds_after_failures():
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("暂时性错误")
        return "ok"

    sleeper = []

    async def fake_sleep(delay):
        sleeper.append(delay)

    result = run(retry_async(flaky, RetryPolicy(max_attempts=5, base_delay=0.01, jitter=0), sleep=fake_sleep))
    assert result == "ok"
    assert len(sleeper) == 2


def test_retry_async_exhausts_and_stops_on_permanent():
    async def always_fail():
        raise ConnectionError("永远失败")

    with pytest.raises(RetryExhausted) as info:
        run(retry_async(always_fail, RetryPolicy(max_attempts=2, jitter=0), sleep=lambda d: asyncio.sleep(0)))
    assert info.value.attempts == 2

    async def permanent():
        raise HttpStatusError(403, "u")

    with pytest.raises(RetryExhausted) as info2:
        run(retry_async(permanent, RetryPolicy(max_attempts=5, jitter=0), sleep=lambda d: asyncio.sleep(0)))
    assert info2.value.attempts == 1


# ----------------------------------------------------------------------
# 存储
# ----------------------------------------------------------------------
def test_metadata_store_roundtrip_and_index(tmp_path):
    store = MetadataStore(tmp_path)
    store.ensure()
    store.save_task({"id": "a" * 32, "url": "http://x/1", "status": "queued"})
    store.save_task({"id": "b" * 32, "url": "http://x/2", "status": "queued"})
    loaded = store.load_all_tasks()
    assert [t["id"] for t in loaded] == ["a" * 32, "b" * 32]
    assert store.load_task("a" * 32)["url"] == "http://x/1"
    assert store.delete_task("a" * 32) is True
    assert store.load_task("a" * 32) is None


def test_metadata_store_recovers_orphans(tmp_path):
    store = MetadataStore(tmp_path)
    store.ensure()
    store.save_task({"id": "c" * 32, "url": "http://x/3", "status": "queued"})
    # 手工删掉 index，模拟索引丢失
    (tmp_path / "tasks" / "index.json").unlink()
    reloaded = MetadataStore(tmp_path)
    reloaded.ensure()
    assert [t["id"] for t in reloaded.load_all_tasks()] == ["c" * 32]


def test_metadata_store_migration_and_corruption(tmp_path):
    store = MetadataStore(tmp_path)
    store.ensure()
    # v0 格式（没有 schema_version / segments / priority）
    (tmp_path / "tasks" / "old.json").write_text(
        '{"id": "old", "url": "http://x/old", "status": "queued"}', encoding="utf-8"
    )
    (tmp_path / "tasks" / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    tasks = store.load_all_tasks()
    ids = {t["id"] for t in tasks}
    assert "old" in ids and "broken" not in ids
    old = store.load_task("old")
    assert old["schema_version"] >= 1 and old["segments"] == []


def test_metadata_store_history(tmp_path):
    store = MetadataStore(tmp_path)
    store.ensure()
    for i in range(3):
        store.append_history({"id": f"h{i}", "status": "completed"})
    records = store.read_history(limit=2)
    assert len(records) == 2
    assert records[0]["id"] == "h2"  # 最新在前


def test_file_writer_ranged_and_append(tmp_path):
    path = tmp_path / "ranged.bin"

    async def scenario():
        writer = FileWriter(path, mode="ranged", size=10)
        writer.open()
        await writer.write_at(5, b"abcde")
        await writer.write_at(0, b"12345")
        await writer.close()

        appender = FileWriter(tmp_path / "appended.bin", mode="append")
        appender.open()
        await appender.append(b"hello ")
        await appender.append(b"world")
        await appender.close()

    run(scenario())
    assert path.read_bytes() == b"12345abcde"
    assert (tmp_path / "appended.bin").read_bytes() == b"hello world"


def test_file_writer_mode_guards(tmp_path):
    writer = FileWriter(tmp_path / "x.bin", mode="append")
    writer.open()
    assert writer.mode == "append"
    with pytest.raises(RuntimeError):
        run(writer.write_at(0, b"x"))
    run(writer.close())
    with pytest.raises(ValueError):
        FileWriter(tmp_path / "y.bin", mode="ranged", size=None)
    with pytest.raises(ValueError):
        FileWriter(tmp_path / "y.bin", mode="nonsense")


def test_finalize_part_replaces_target(tmp_path):
    part = tmp_path / "a.bin.dxpart"
    target = tmp_path / "a.bin"
    part.write_bytes(b"new")
    target.write_bytes(b"old")
    finalize_part(part, target)
    assert target.read_bytes() == b"new"
    assert not part.exists()


def test_checksum_helpers(tmp_path):
    payload = b"hello downloadx" * 1000
    path = tmp_path / "c.bin"
    path.write_bytes(payload)
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    assert sha256_file(path) == digest
    assert verify_file(path, digest) is True
    assert verify_file(path, "0" * 64) is False
    assert verify_file(path, "") is True

    hasher = IncrementalHasher()
    hasher.update(payload[:100])
    hasher.update(payload[100:])
    assert hasher.hexdigest() == digest
    assert hasher.byte_count == len(payload)


# ----------------------------------------------------------------------
# 队列与限速
# ----------------------------------------------------------------------
def test_priority_queue_order_and_mutation():
    queue = PriorityQueue()
    queue.push("low", 9)
    queue.push("high", 0)
    queue.push("mid", 5)
    assert queue.pop() == "high"
    assert queue.set_priority("low", 0) is True
    assert queue.pop() == "low"
    assert queue.remove("mid") is True
    assert queue.pop() is None
    assert queue.peek() is None


def test_priority_queue_fifo_within_priority():
    queue = PriorityQueue()
    for i in range(3):
        queue.push(f"t{i}", 5)
    assert [queue.pop() for _ in range(3)] == ["t0", "t1", "t2"]


def test_priority_queue_rejects_duplicates():
    queue = PriorityQueue()
    assert queue.push("a", 5) is True
    assert queue.push("a", 5) is False
    assert len(queue) == 1


def test_token_bucket_unlimited_is_free():
    async def scenario():
        bucket = TokenBucket(0)
        start = time.monotonic()
        await bucket.consume(10 ** 7)
        return time.monotonic() - start

    assert run(scenario()) < 0.05


def test_token_bucket_rate_limits():
    async def scenario():
        bucket = TokenBucket(64 * 1024)  # 64 KiB/s
        start = time.monotonic()
        await bucket.consume(64 * 1024)
        return time.monotonic() - start

    elapsed = run(scenario())
    assert elapsed >= 0.5, f"限速未生效：{elapsed:.3f}s"


def test_bandwidth_controller_task_and_global():
    async def scenario():
        controller = BandwidthController(0)
        start = time.monotonic()
        await controller.acquire(1024, "task")
        fast = time.monotonic() - start
        controller.set_task_rate("task", 32 * 1024)
        start = time.monotonic()
        await controller.acquire(32 * 1024, "task")
        slow = time.monotonic() - start
        controller.clear_task("task")
        return fast, slow, controller.snapshot()

    fast, slow, snapshot = run(scenario())
    assert fast < 0.05
    assert slow >= 0.4
    assert snapshot["global_rate"] == 0


# ----------------------------------------------------------------------
# 检查点 / 锁 / 计量
# ----------------------------------------------------------------------
def test_checkpoint_flush_persists_snapshots(tmp_path):
    store = MetadataStore(tmp_path)
    store.ensure()
    checkpointer = CheckpointManager(store, interval=0.05)
    state = {"progress": 0}
    checkpointer.attach("t1", lambda: {"id": "t1", "progress": state["progress"]})

    async def scenario():
        await checkpointer.flush()
        assert store.load_task("t1")["progress"] == 0
        state["progress"] = 42
        checkpointer.touch("t1")
        await asyncio.sleep(0.2)
        await checkpointer.flush()
        return store.load_task("t1")["progress"]

    assert run(scenario()) == 42


def test_single_instance_lock(tmp_path):
    first = SingleInstanceLock(tmp_path)
    second = SingleInstanceLock(tmp_path)
    assert first.acquire() is True
    assert second.acquire() is False
    assert (second.holder() or {}).get("pid")
    first.release()
    assert second.acquire() is True
    second.release()


def test_single_instance_lock_takes_over_stale(tmp_path):
    lock = SingleInstanceLock(tmp_path, stale_after=0.05)
    lock._try_create()  # 伪造一个"死进程"留下的锁
    time.sleep(0.1)
    assert lock.acquire() is True
    lock.release()


def test_speed_meter():
    meter = SpeedMeter(window=5.0)
    now = 1000.0
    meter.add(0, now)
    for i in range(1, 11):
        meter.add(10_000, now + i * 0.1)
    assert 95_000 < meter.speed(now + 1.0) < 105_000
    assert meter.total == 100_000
    meter.reset()
    assert meter.speed(now + 2) == 0.0


def test_format_helpers():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536).startswith("1.50 KB")
    assert format_eta(None) == "—"
    assert format_eta(45) == "45s"
    assert format_eta(90) == "1m 30s"
    assert format_eta(3700).startswith("1h")


# ----------------------------------------------------------------------
# 任务模型
# ----------------------------------------------------------------------
def test_download_task_roundtrip(tmp_path):
    task = DownloadTask.create(
        "https://example.com/dir/file.zip?v=1",
        save_dir=tmp_path,
        priority=2,
        connections=6,
    )
    task.total_size = 1000
    task.segments = [Segment(index=0, start=0, end=999, received=250)]
    task.sync_progress()
    data = task.to_dict()
    restored = DownloadTask.from_dict(data)
    assert restored.id == task.id
    assert restored.filename == task.filename
    assert restored.priority == 2
    assert restored.downloaded_size == 250
    assert restored.percent == 25.0
    assert restored.part_path.endswith(".dxpart")
    assert restored.allowed()


def test_download_task_sanitizes_server_filename(tmp_path):
    task = DownloadTask.create("https://example.com/a/pack.tar.gz", save_dir=tmp_path)
    assert task.filename == "pack.tar.gz"
    assert task.filename_locked is False
    locked = DownloadTask.create("https://example.com/a/pack.tar.gz", save_dir=tmp_path, filename="自定义.bin")
    assert locked.filename == "自定义.bin"
    assert locked.filename_locked is True


def test_download_task_transition_updates_timestamps(tmp_path):
    task = DownloadTask.create("https://example.com/x.bin", save_dir=tmp_path)
    task.transition(TaskStatus.DOWNLOADING)
    assert task.started_at is not None
    task.transition(TaskStatus.VERIFYING)
    task.transition(TaskStatus.COMPLETED)
    assert task.completed_at is not None
    with pytest.raises(InvalidTransitionError):
        task.transition(TaskStatus.PAUSED)


def test_config_roundtrip_and_env(tmp_path, monkeypatch):
    config = Config(download_dir=tmp_path / "d", state_dir=tmp_path / "s")
    config.max_concurrent_tasks = 7
    config.ensure_dirs()
    path = config.save()
    assert path.exists()
    reloaded = Config.load(state_dir=tmp_path / "s")
    assert reloaded.max_concurrent_tasks == 7
    assert Path(reloaded.download_dir) == tmp_path / "d"

    monkeypatch.setenv("DOWNLOADX_MAX_CONCURRENT_TASKS", "11")
    monkeypatch.setenv("DOWNLOADX_VERIFY_SHA256", "false")
    reloaded.apply_env()
    assert reloaded.max_concurrent_tasks == 11
    assert reloaded.verify_sha256 is False


def test_config_tolerates_broken_file(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "config.json").write_text("{ 坏掉的配置", encoding="utf-8")
    config = Config.load(state_dir=state)
    assert config.max_concurrent_tasks >= 1
