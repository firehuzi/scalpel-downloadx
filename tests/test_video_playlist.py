"""播放列表批量：逐条执行、状态文件续跑、只补失败项。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill" / "downloadx" / "scripts" / "fetch_video.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_video_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_args(**overrides):
    base = dict(
        url="https://example.com/playlist",
        quality="best",
        audio=False,
        inspect=False,
        cookies=None,
        cookies_from_browser=None,
        json=True,
        redo=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


ENTRIES = [
    {"id": "a", "title": "第一集", "url": "https://example.com/a"},
    {"id": "b", "title": "第二集", "url": "https://example.com/b"},
    {"id": "c", "title": "第三集", "url": "https://example.com/c"},
]


def test_playlist_resumes_and_retries_only_failures(tmp_path, monkeypatch, capsys):
    module = load_module()
    monkeypatch.setattr(module, "enumerate_playlist", lambda *a, **k: list(ENTRIES))

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        url = command[2]
        # 第一轮：b 失败
        failing = module._fail_url == url
        return SimpleNamespace(returncode=1 if failing else 0)

    module.subprocess.run = fake_run
    module._fail_url = "https://example.com/b"

    out_dir = tmp_path / "videos"
    out_dir.mkdir()

    # 第一轮：3 条都跑，b 失败
    code = module.run_playlist(make_args(), "python", [], [], out_dir, "1.0", "example.com")
    assert code == 1, "有失败条目时退出码应为 1"
    assert len(calls) == 3
    state = json.loads((out_dir / ".downloadx-playlist.json").read_text(encoding="utf-8"))
    statuses = {key: value["status"] for key, value in state["entries"].items()}
    assert statuses == {"a": "done", "b": "failed", "c": "done"}

    # 第二轮：只补失败的那条
    module._fail_url = None
    calls.clear()
    code = module.run_playlist(make_args(), "python", [], [], out_dir, "1.0", "example.com")
    assert code == 0
    assert [cmd[2] for cmd in calls] == ["https://example.com/b"], "已完成的条目不应重下"

    # 第三轮：--redo 全重下
    calls.clear()
    module.run_playlist(make_args(redo=True), "python", [], [], out_dir, "1.0", "example.com")
    assert len(calls) == 3
    capsys.readouterr()  # 吃掉输出，避免污染


def test_playlist_inspect_lists_entries(tmp_path, monkeypatch, capsys):
    module = load_module()
    monkeypatch.setattr(module, "enumerate_playlist", lambda *a, **k: list(ENTRIES))
    module.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载"))

    code = module.run_playlist(make_args(inspect=True), "python", [], [], tmp_path, "1.0", "x")
    assert code == 0
    out = capsys.readouterr().out
    assert "第一集" in out and "第三集" in out
    assert not (tmp_path / ".downloadx-playlist.json").exists()


def test_playlist_empty_reports_error(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "enumerate_playlist", lambda *a, **k: [])
    assert module.run_playlist(make_args(), "python", [], [], tmp_path, "1.0", "x") == 1
