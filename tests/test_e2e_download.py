"""端到端：真的从本地 HTTP 服务把文件下载下来。"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import run
from tests.support.server import make_payload, payload_sha256

from downloadx import DownloadManager
from downloadx.core.state_machine import TaskStatus


def test_segmented_download_with_hash(config, server, tmp_path):
    """多连接分片下载 + SHA-256 校验 + 最终文件就位。"""
    size = 512 * 1024
    name = "segmented.bin"
    url = server.url(f"/file/{name}?size={size}")

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(
                url,
                expected_sha256=payload_sha256(name, size),
                connections=4,
            )
            status = await manager.wait_for(task.id, timeout=60)
            return manager, task, status

    manager, task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert task.mode == "segmented"
    assert task.filename == name
    assert task.total_size == size
    assert task.downloaded_size == size
    assert task.verified is True
    assert task.sha256 == payload_sha256(name, size)
    assert len(task.segments) > 1

    saved = Path(task.save_path)
    assert saved.exists()
    assert saved.read_bytes() == make_payload(name, size)
    assert not Path(task.part_path).exists()


def test_small_file_single_segment(config, server):
    size = 1024
    name = "tiny.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url(f"/file/{name}?size={size}"))
            status = await manager.wait_for(task.id, timeout=30)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert len(task.segments) == 1
    assert Path(task.save_path).read_bytes() == make_payload(name, size)


def test_no_range_falls_back_to_streaming(config, server):
    """服务端忽略 Range：必须降级为整段下载，且结果正确。"""
    size = 200 * 1024
    name = "norange.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url(f"/norange/{name}?size={size}"))
            status = await manager.wait_for(task.id, timeout=30)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert task.resumable is False
    assert task.mode in ("single", "streaming")
    assert Path(task.save_path).read_bytes() == make_payload(name, size)


def test_unknown_size_streaming(config, server):
    size = 100 * 1024
    name = "unknown.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url(f"/unknown/{name}?size={size}"))
            status = await manager.wait_for(task.id, timeout=30)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert task.mode == "streaming"
    assert Path(task.save_path).read_bytes() == make_payload(name, size)


def test_content_disposition_filename(config, server):
    size = 4096
    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url(f"/disposition/report.bin?size={size}"))
            status = await manager.wait_for(task.id, timeout=30)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert task.filename == "中文文件.bin"
    assert Path(task.save_path).name == "中文文件.bin"


def test_retry_on_transient_503(config, server):
    """前两次请求 503，重试后必须成功。"""
    size = 128 * 1024
    name = "flaky.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url(f"/flaky/{name}?size={size}&fails=2"))
            status = await manager.wait_for(task.id, timeout=60)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert task.attempts >= 2
    assert Path(task.save_path).read_bytes() == make_payload(name, size)


def test_checksum_mismatch_fails(config, server):
    size = 64 * 1024
    name = "badhash.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(
                server.url(f"/file/{name}?size={size}"),
                expected_sha256="0" * 64,
            )
            status = await manager.wait_for(task.id, timeout=30)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.FAILED
    assert task.error_code == "checksum_mismatch"
    assert not Path(task.save_path).exists()


def test_404_fails_without_retry_exhaustion(config, server):
    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url("/missing/none.bin"))
            status = await manager.wait_for(task.id, timeout=30)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.FAILED
    assert task.error and "404" in task.error


def test_truncated_body_is_resumed(config, server):
    """服务端报全长但只发一半：客户端应判为中断，重试后从断点续传并完成。"""
    size = 256 * 1024
    name = "truncated.bin"

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(server.url(f"/truncate/{name}?size={size}"))
            status = await manager.wait_for(task.id, timeout=60)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED
    assert Path(task.save_path).read_bytes() == make_payload(name, size)
