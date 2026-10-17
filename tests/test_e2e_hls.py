"""HLS（.m3u8）：分片下载、合并、断点续传。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run
from tests.support.server import make_payload, range_starts, recorded_requests

from downloadx import DownloadManager
from downloadx.core.state_machine import TaskStatus
from downloadx.network import hls


def _durations(text: str) -> list[float]:
    """ffprobe CSV 输出里，duration 通常在 format_name 同一行的最后一列。"""
    values: list[float] = []
    for line in text.splitlines():
        try:
            values.append(float(line.rsplit(",", 1)[-1].strip()))
        except ValueError:
            continue
    return values


def test_parse_master_and_media():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=854x480
low/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080
high/playlist.m3u8
"""
    variants = hls.parse_master(master, "https://cdn.example.com/vod/master.m3u8")
    assert [v.resolution for v in variants] == [(854, 480), (1920, 1080)]
    assert variants[1].uri == "https://cdn.example.com/vod/high/playlist.m3u8"
    assert hls.pick_variant(variants).resolution == (1920, 1080)
    assert hls.pick_variant(variants, max_height=720).resolution == (854, 480)

    media = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
seg0.m4s
#EXTINF:3.5,
seg1.m4s
#EXT-X-ENDLIST
"""
    playlist = hls.parse_media(media, "https://cdn.example.com/vod/playlist.m3u8")
    assert len(playlist.segments) == 2
    assert playlist.segments[0].uri == "https://cdn.example.com/vod/seg0.m4s"
    assert playlist.init_uri == "https://cdn.example.com/vod/init.mp4"
    assert playlist.is_live is False
    assert abs(playlist.duration - 7.5) < 1e-6


def test_parse_rejects_encrypted_and_live():
    encrypted = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:4.0,
a.ts
#EXT-X-ENDLIST
"""
    try:
        hls.parse_media(encrypted, "https://x/p.m3u8")
    except hls.HlsError as exc:
        assert "加密" in str(exc)
    else:
        raise AssertionError("加密流应当被拒绝")

    live = """#EXTM3U
#EXTINF:4.0,
a.ts
"""
    assert hls.parse_media(live, "https://x/p.m3u8").is_live is True


def test_hls_download_concatenates_segments(config, server):
    """主播放列表 -> 选变体 -> 下全部分片 -> 按顺序拼成一个文件。"""
    config.stream_remux = False  # 关掉 ffmpeg remux，直接比对拼接后的字节
    count, size = 6, 16 * 1024
    url = server.url(f"/hls/demo/master.m3u8?segments={count}&size={size}")

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(url, connections=4)
            status = await manager.wait_for(task.id, timeout=60)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED, task.error
    assert task.mode == "hls"
    assert task.stream_count == count
    assert sorted(task.stream_tracks[0]["done"]) == list(range(count))
    assert task.percent == 100.0
    assert task.filename.endswith(".ts")
    expected = b"".join(make_payload(f"demo-{i}", size) for i in range(count))
    assert Path(task.save_path).read_bytes() == expected
    assert not Path(task.part_path + ".segs").exists(), "分片临时目录应该被清掉"


def test_hls_resume_skips_finished_segments(config, server):
    """暂停后继续：已经下完的分片不能重新请求。"""
    config.stream_remux = False
    count, size = 6, 16 * 1024
    url = server.url(f"/hls/resume/master.m3u8?segments={count}&size={size}&delay=0.25")

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(url, connections=2)
            for _ in range(200):
                if task.stream_done_count >= 2:
                    break
                await asyncio.sleep(0.05)
            assert 0 < task.stream_done_count < count, f"暂停时机不对：{task.stream_done_count}"

            status = await manager.pause(task.id)
            assert status is TaskStatus.PAUSED
            done_at_pause = set(task.stream_tracks[0]["done"])

            requests_before = len(recorded_requests("resume"))
            await manager.resume(task.id)
            final = await manager.wait_for(task.id, timeout=90)
            after = recorded_requests("resume")[requests_before:]
            return task, final, done_at_pause, after

    task, final, done_at_pause, after = run(scenario())
    assert final is TaskStatus.COMPLETED, task.error
    expected = b"".join(make_payload(f"resume-{i}", size) for i in range(count))
    assert Path(task.save_path).read_bytes() == expected

    refetched = set()
    for item in after:
        name = item["path"].rsplit("/", 1)[-1]
        if name.startswith("seg") and name.endswith(".ts"):
            refetched.add(int(name[3:-3]))
    assert not (refetched & done_at_pause), (
        f"恢复后重复下载了已完成分片：{sorted(refetched & done_at_pause)}"
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_hls_real_ffmpeg_remux_to_mp4(config, server, tmp_path):
    """用 ffmpeg 现场生成真 HLS（1 秒一段），下载后 remux 成可播放的 mp4。"""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
            str(src / "playlist.m3u8"),
        ],
        check=True,
        capture_output=True,
        timeout=120,
        cwd=str(src),  # 分片名按 cwd 解析，避免散落到仓库根目录
    )
    assert (src / "playlist.m3u8").exists()

    config.stream_remux = True
    token = server.mount(src)
    url = server.url(f"/disk/{token}/playlist.m3u8")

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(url, connections=2)
            status = await manager.wait_for(task.id, timeout=90)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED, task.error
    assert task.filename.endswith(".mp4")
    saved = Path(task.save_path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type",
         "-of", "csv=p=0", str(saved)],
        capture_output=True, text=True, timeout=60,
    )
    out = probe.stdout
    assert "video" in out and "audio" in out, out
    assert "mp4" in out, f"容器应该真的被 remux 成 mp4：{out}"
    durations = _durations(out)
    assert durations and max(durations) > 1.0, out
