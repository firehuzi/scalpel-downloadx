"""DASH（.mpd）：解析、选轨、双轨下载与合并。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run

from downloadx import DownloadManager
from downloadx.core.state_machine import TaskStatus
from downloadx.network import dash


def _durations(text: str) -> list[float]:
    """ffprobe CSV 输出里，duration 通常在 format_name 同一行的最后一列。"""
    values: list[float] = []
    for line in text.splitlines():
        try:
            values.append(float(line.rsplit(",", 1)[-1].strip()))
        except ValueError:
            continue
    return values

MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT8S" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v360" bandwidth="800000" width="640" height="360">
        <SegmentTemplate media="v360/$Number$.m4s" initialization="v360/init.mp4"
                         duration="2" timescale="1" startNumber="1"/>
      </Representation>
      <Representation id="v1080" bandwidth="2500000" width="1920" height="1080">
        <SegmentTemplate media="v1080/$Number$.m4s" initialization="v1080/init.mp4"
                         duration="2" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a0" bandwidth="128000">
        <SegmentTemplate media="a0/$Number$.m4s" initialization="a0/init.mp4"
                         duration="2" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_parse_template_picks_best_video_and_audio():
    tracks = dash.parse(MPD, "https://cdn.example.com/dash/manifest.mpd")
    assert [t.kind for t in tracks] == ["video", "audio"]
    video, audio = tracks
    assert video.height == 1080
    assert video.init == "https://cdn.example.com/dash/v1080/init.mp4"
    assert video.urls[0] == "https://cdn.example.com/dash/v1080/1.m4s"
    assert len(video.urls) == 4  # 8s / 2s
    assert audio.urls[0] == "https://cdn.example.com/dash/a0/1.m4s"


def test_parse_respects_max_height():
    tracks = dash.parse(MPD, "https://cdn.example.com/dash/manifest.mpd", max_height=720)
    assert tracks[0].height == 360


def test_parse_segment_timeline():
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT6S">
      <Period><AdaptationSet mimeType="video/mp4">
        <Representation id="v" bandwidth="100" width="320" height="240">
          <SegmentTemplate media="s/$Time$.m4s" initialization="s/init.mp4" timescale="1000">
            <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
          </SegmentTemplate>
        </Representation>
      </AdaptationSet></Period></MPD>"""
    track = dash.parse(mpd, "https://x/v/manifest.mpd")[0]
    assert [u.rsplit("/", 1)[-1] for u in track.urls] == ["0.m4s", "2000.m4s", "4000.m4s"]


def test_parse_segment_list():
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT4S">
      <Period><AdaptationSet mimeType="video/mp4">
        <Representation id="v" bandwidth="100" width="320" height="240">
          <SegmentList><Initialization sourceURL="i.mp4"/>
            <SegmentURL media="a1.m4s"/><SegmentURL media="a2.m4s"/>
          </SegmentList>
        </Representation>
      </AdaptationSet></Period></MPD>"""
    track = dash.parse(mpd, "https://x/v/manifest.mpd")[0]
    assert track.init == "https://x/v/i.mp4"
    assert [u.rsplit("/", 1)[-1] for u in track.urls] == ["a1.m4s", "a2.m4s"]


def test_parse_rejects_drm():
    mpd = MPD.replace(
        "<AdaptationSet mimeType=\"video/mp4\">",
        "<AdaptationSet mimeType=\"video/mp4\"><ContentProtection schemeIdUri=\"urn:mpeg:dash:mp4protection:2011\"/>",
    )
    with pytest.raises(dash.DashError) as info:
        dash.parse(mpd, "https://x/manifest.mpd")
    assert "DRM" in str(info.value)


def _period_mpd(init1: str, init2: str) -> str:
    def block(index: int, init: str) -> str:
        return f"""
    <Period duration="PT4S">
      <AdaptationSet mimeType="video/mp4">
        <Representation id="v" bandwidth="800000" width="640" height="360">
          <SegmentTemplate media="p{index}/$Number$.m4s" initialization="{init}"
                           duration="2" timescale="1" startNumber="1"/>
        </Representation>
      </AdaptationSet>
    </Period>"""

    return (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT8S">'
        + block(1, init1)
        + block(2, init2)
        + "\n</MPD>"
    )


def test_parse_multi_period_merges_segments_in_order():
    tracks = dash.parse(_period_mpd("init.mp4", "init.mp4"), "https://x/manifest.mpd")
    assert len(tracks) == 1
    names = [url.rsplit("/", 1)[-1] for url in tracks[0].urls]
    assert names == ["1.m4s", "2.m4s", "1.m4s", "2.m4s"]
    assert tracks[0].urls[0].endswith("p1/1.m4s")
    assert tracks[0].urls[2].endswith("p2/1.m4s")


def test_parse_multi_period_with_different_inits_is_rejected():
    with pytest.raises(dash.DashError) as info:
        dash.parse(_period_mpd("init1.mp4", "init2.mp4"), "https://x/manifest.mpd")
    assert "多 Period" in str(info.value)


def test_parse_bandwidth_template():
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT4S">
      <Period><AdaptationSet mimeType="video/mp4">
        <Representation id="v" bandwidth="500000" width="320" height="240">
          <SegmentTemplate media="seg-$Bandwidth$-$Number$.m4s"
                           initialization="init-$Bandwidth$.mp4"
                           duration="2" timescale="1" startNumber="1"/>
        </Representation>
      </AdaptationSet></Period></MPD>"""
    track = dash.parse(mpd, "https://x/manifest.mpd")[0]
    assert track.init.endswith("init-500000.mp4")
    assert [url.rsplit("/", 1)[-1] for url in track.urls] == ["seg-500000-1.m4s", "seg-500000-2.m4s"]


def test_parse_rejects_dynamic_mpd():
    mpd = MPD.replace('type="static"', 'type="dynamic"')
    with pytest.raises(dash.DashError) as info:
        dash.parse(mpd, "https://x/manifest.mpd")
    assert "动态" in str(info.value)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_dash_real_ffmpeg_download_and_mux(config, server, tmp_path):
    """用 ffmpeg 生成真 DASH（视频轨 + 音频轨分开），下载后合并成 mp4。"""
    src = tmp_path / "dash"
    src.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-f", "dash", "-seg_duration", "1", "-use_template", "1", "-use_timeline", "0",
            "-init_seg_name", "init-$RepresentationID$.m4s",
            "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
            str(src / "manifest.mpd"),
        ],
        check=True,
        capture_output=True,
        timeout=180,
        cwd=str(src),  # ffmpeg 把分片名按 cwd 解析，必须待在这里
    )
    token = server.mount(src)
    url = server.url(f"/disk/{token}/manifest.mpd")

    async def scenario():
        async with DownloadManager(config) as manager:
            task = await manager.add(url, connections=2)
            status = await manager.wait_for(task.id, timeout=120)
            return task, status

    task, status = run(scenario())
    assert status is TaskStatus.COMPLETED, task.error
    assert task.mode == "dash"
    assert task.filename.endswith(".mp4")
    assert [t["kind"] for t in task.stream_tracks] == ["video", "audio"]
    assert task.stream_count > 0
    assert task.percent == 100.0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type",
         "-of", "csv=p=0", task.save_path],
        capture_output=True, text=True, timeout=60,
    )
    out = probe.stdout
    assert "video" in out and "audio" in out, out
    assert "mp4" in out, f"容器应该真的被 remux 成 mp4：{out}"
    durations = _durations(out)
    assert durations and max(durations) > 1.0, out
