"""附加产物（字幕/章节/弹幕）里的纯逻辑部分。"""

from __future__ import annotations

import importlib.util
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "downloadx" / "scripts" / "fetch_video.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_video_extras", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_chapters_text_formats_times():
    module = load_module()
    info = {
        "chapters": [
            {"start_time": 0, "title": "开场"},
            {"start_time": 65.9, "title": "第二段"},
            {"start_time": 3661, "title": ""},
        ]
    }
    assert module.chapters_text(info).splitlines() == [
        "00:00:00  开场",
        "00:01:05  第二段",
        "01:01:01",
    ]
    assert module.chapters_text({}) == ""


def test_decompress_danmaku_accepts_all_wrappers():
    module = load_module()
    xml = b'<?xml version="1.0" encoding="UTF-8"?><i><d p="1">hi</d></i>'
    zlib_wrapped = zlib.compress(xml)
    raw_deflate = zlib_wrapped[2:-4]  # 去掉 zlib 头与 adler32
    assert module.decompress_danmaku(xml) == xml
    assert module.decompress_danmaku(zlib_wrapped) == xml
    assert module.decompress_danmaku(raw_deflate) == xml
    with pytest.raises(ValueError):
        module.decompress_danmaku(b"\x00\x01\x02\x03")


def test_bilibili_bvid_extraction():
    module = load_module()
    assert module.bilibili_bvid("https://www.bilibili.com/video/BV1YXJ46nEY9?p=2") == "BV1YXJ46nEY9"
    assert module.bilibili_bvid("https://b23.tv/abc") is None
    assert module.bilibili_bvid("") is None


def test_collect_subs_handles_bracketed_names(tmp_path):
    """回归：文件名里的 [id] 不能被 glob 当字符类。"""
    module = load_module()
    video = tmp_path / "demo [abc12345678].mp4"
    video.write_bytes(b"")
    (tmp_path / "demo [abc12345678].en.srt").write_text("1", encoding="utf-8")
    (tmp_path / "demo [abc12345678].zh-Hans.srt").write_text("1", encoding="utf-8")
    (tmp_path / "unrelated.srt").write_text("1", encoding="utf-8")
    got = module.collect_subs(video)
    assert len(got) == 2
    assert all("[abc12345678]" in path for path in got)
