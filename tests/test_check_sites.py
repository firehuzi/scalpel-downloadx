"""站点回归脚本的离线部分（不联网）：画质提取与清单判定。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "downloadx" / "scripts" / "check_sites.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_sites_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_quality_picks_max_height_and_counts_formats():
    module = load_module()
    info = {
        "title": "demo",
        "duration": 10,
        "formats": [
            {"height": 360, "vcodec": "avc1"},
            {"height": 1080, "vcodec": "avc1"},
            {"height": None, "acodec": "aac"},
            "not-a-dict",
        ],
    }
    out = module._quality(info)
    assert out["max_height"] == 1080
    assert out["formats"] == 3, "非法条目应被忽略"
    assert out["title"] == "demo"


def test_hls_and_dash_checks_reject_bad_payloads(monkeypatch):
    module = load_module()

    monkeypatch.setattr(module, "_http", lambda url, timeout=20.0: (200, b"<html>nope</html>"))
    assert module.check_hls("python", 5)["ok"] is False
    assert module.check_dash("python", 5)["ok"] is False

    monkeypatch.setattr(
        module, "_http",
        lambda url, timeout=20.0: (200, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n"),
    )
    ok = module.check_hls("python", 5)
    assert ok["ok"] is True and ok["variants"] == 1

    monkeypatch.setattr(
        module, "_http", lambda url, timeout=20.0: (200, b'<?xml version="1.0"?><MPD></MPD>')
    )
    assert module.check_dash("python", 5)["ok"] is True
