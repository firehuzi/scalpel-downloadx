"""站点档案：域名匹配、保存/列出/删除。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "downloadx" / "scripts" / "fetch_video.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_video_sites", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def args(**overrides):
    base = dict(site_list=False, site_set=None, site_remove=None,
                cookies=None, proxy=None, site_args=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_match_site_prefers_longest_suffix():
    module = load_module()
    sites = {
        "bilibili.com": {"cookies": "a.txt"},
        "api.bilibili.com": {"cookies": "b.txt"},
        "x.com": {},
    }
    assert module.match_site("www.bilibili.com", sites)[0] == "bilibili.com"
    assert module.match_site("api.bilibili.com", sites)[0] == "api.bilibili.com"
    assert module.match_site("x.com", sites)[0] == "x.com"
    assert module.match_site("notbilibili.com", sites) is None
    assert module.match_site("bilibili.com.evil.test", sites) is None


def test_site_set_list_remove(tmp_path, monkeypatch, capsys):
    module = load_module()
    monkeypatch.setattr(module, "sites_path", lambda: tmp_path / "sites.json")

    assert module.site_command(args(
        site_set="bilibili.com",
        cookies="C:/tmp/cookies.txt",
        proxy="http://127.0.0.1:7890",
        site_args="--write-subs --write-auto-subs",
    )) == 0

    saved = json.loads((tmp_path / "sites.json").read_text(encoding="utf-8"))
    assert saved["bilibili.com"]["cookies"].endswith("cookies.txt")
    assert saved["bilibili.com"]["proxy"] == "http://127.0.0.1:7890"
    assert saved["bilibili.com"]["args"] == ["--write-subs", "--write-auto-subs"]

    assert module.site_command(args(site_list=True)) == 0
    out = capsys.readouterr().out
    assert "bilibili.com" in out and "7890" in out

    assert module.site_command(args(site_remove="bilibili.com")) == 0
    assert json.loads((tmp_path / "sites.json").read_text(encoding="utf-8")) == {}
    assert module.site_command(args(site_remove="bilibili.com")) == 1


def test_site_set_requires_at_least_one_field(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "sites_path", lambda: tmp_path / "sites.json")
    assert module.site_command(args(site_set="example.com")) == 1
    assert not (tmp_path / "sites.json").exists()


def test_profile_is_matched_from_url_host(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "sites_path", lambda: tmp_path / "sites.json")
    module.site_command(args(site_set="bilibili.com", cookies="C:/tmp/bili.txt"))
    pattern, profile = module.site_profile(module.host_of("https://www.bilibili.com/video/BV1"))
    assert pattern == "bilibili.com"
    assert profile["cookies"].endswith("bili.txt")
