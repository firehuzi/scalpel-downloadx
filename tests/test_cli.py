"""CLI 测试：真的用命令行下载文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from downloadx.cli import main
from tests.support.server import make_payload


@pytest.fixture()
def cli_args(tmp_path, server):
    state = tmp_path / "state"
    downloads = tmp_path / "downloads"
    return [
        "--local",
        "--state-dir",
        str(state),
        "--download-dir",
        str(downloads),
    ], state, downloads, server


def test_cli_add_downloads_file(capsys, cli_args):
    common, _, downloads, server = cli_args
    size = 128 * 1024
    name = "cli.bin"
    code = main([*common, "add", server.url(f"/file/{name}?size={size}")])
    assert code == 0
    out = capsys.readouterr().out
    assert "已加入" in out
    assert (downloads / name).read_bytes() == make_payload(name, size)


def test_cli_add_json_output(capsys, cli_args):
    common, _, downloads, server = cli_args
    size = 8192
    name = "clijson.bin"
    code = main(
        [*common, "--json", "add", server.url(f"/file/{name}?size={size}"), "--sha256", "0" * 64]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert payload["filename"] == name
    assert Path(payload["save_path"]).parent == downloads
    assert payload["expected_sha256"] == "0" * 64
    # --wait 是默认行为（进程内模式）：输出的应当是"最终状态"这一个文档
    assert payload["status"] == "failed"
    assert payload["error_code"] == "checksum_mismatch"


def test_cli_add_json_wait_reports_final_state(capsys, cli_args):
    """--json --wait 必须只输出一个文档，且是完成后的状态（脚本最常踩的坑）。"""
    common, _, downloads, server = cli_args
    size = 8192
    name = "clijsonfinal.bin"
    code = main([*common, "--json", "add", server.url(f"/file/{name}?size={size}")])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # 整段输出必须是一个合法 JSON 文档
    assert payload["status"] == "completed"
    assert payload["total_size"] == size
    assert payload["sha256"] and len(payload["sha256"]) == 64
    assert (downloads / name).read_bytes() == make_payload(name, size)


def test_cli_list_and_get_and_stats(capsys, cli_args):
    common, _, _, server = cli_args
    main([*common, "add", server.url("/file/clilist.bin?size=4096")])
    capsys.readouterr()

    assert main([*common, "list"]) == 0
    listing = capsys.readouterr().out
    assert "clilist.bin" in listing
    assert "完成" in listing

    assert main([*common, "--json", "list"]) == 0
    tasks = json.loads(capsys.readouterr().out)
    task_id = tasks[0]["id"]

    assert main([*common, "get", task_id[:8] if False else task_id]) == 0
    assert "clilist.bin" in capsys.readouterr().out

    assert main([*common, "stats"]) == 0
    stats_out = capsys.readouterr().out
    assert "总速度" in stats_out and "已完成" in stats_out


def test_cli_pause_resume_cancel_retry(capsys, cli_args):
    common, _, downloads, server = cli_args
    size = 512 * 1024
    name = "clicontrol.bin"
    url = server.url(f"/file/{name}?size={size}&delay=0.02&chunk=8192")

    # 后台入队（不等），然后暂停 -> 继续 -> 等待完成
    assert main([*common, "--json", "add", url, "--no-wait"]) == 0
    task = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    task_id = task["id"]

    assert main([*common, "pause", task_id]) == 0
    assert "暂停" in capsys.readouterr().out

    assert main([*common, "--json", "get", task_id]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused"

    assert main([*common, "retry", task_id, "--wait"]) == 0
    capsys.readouterr()
    assert (downloads / name).read_bytes() == make_payload(name, size)


def test_cli_cancel_then_remove(capsys, cli_args):
    common, _, downloads, server = cli_args
    name = "clirm.bin"
    url = server.url(f"/file/{name}?size=262144")
    assert main([*common, "--json", "add", url, "--no-wait"]) == 0
    task_id = json.loads(capsys.readouterr().out.strip().splitlines()[0])["id"]

    # 进程内模式只入队：取消应该把它从队列里摘掉
    assert main([*common, "cancel", task_id]) == 0
    assert "取消" in capsys.readouterr().out

    # 再下次真的下载完，然后带文件一起移除
    assert main([*common, "retry", task_id, "--wait"]) == 0
    capsys.readouterr()
    assert (downloads / name).exists()

    assert main([*common, "rm", task_id, "--delete-file"]) == 0
    assert "已移除" in capsys.readouterr().out
    assert not (downloads / name).exists()


def test_cli_probe(capsys, cli_args):
    common, _, _, server = cli_args
    assert main([*common, "probe", server.url("/file/probe.bin?size=2048")]) == 0
    out = capsys.readouterr().out
    assert "probe.bin" in out
    assert "支持" in out


def test_cli_tools_and_manifest(capsys, cli_args):
    common, _, _, _ = cli_args
    assert main([*common, "tools"]) == 0
    out = capsys.readouterr().out
    assert "add_download" in out and "pause_download" in out

    assert main([*common, "--json", "tools"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert len(manifest["tools"]) >= 15


def test_cli_natural_language(capsys, cli_args):
    common, _, downloads, server = cli_args
    name = "clinl.bin"
    url = server.url(f"/file/{name}?size=4096")

    assert main([*common, "nl", f"帮我下载 {url}"]) == 0
    out = capsys.readouterr().out
    assert "add_download" in out
    assert "已加入下载" in out

    # 等它下完，再用自然语言查询
    for _ in range(100):
        if (downloads / name).exists():
            break
        import time

        time.sleep(0.05)
    assert (downloads / name).exists()

    assert main([*common, "nl", "现在有哪些任务"]) == 0
    assert "list_downloads" in capsys.readouterr().out


def test_cli_config_show_and_set(capsys, tmp_path, cli_args):
    common, _, _, _ = cli_args
    assert main([*common, "config"]) == 0
    assert "download_dir" in capsys.readouterr().out

    new_dir = tmp_path / "another"
    assert main(
        [
            *common,
            "config",
            "--set-download-dir",
            str(new_dir),
            "--set-max-concurrent",
            "6",
            "--set-global-limit",
            "2",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert str(new_dir) in out
    assert "6" in out
    assert str(2 * 1024 * 1024) in out


def test_cli_rejects_bad_url(capsys, cli_args):
    common, _, _, _ = cli_args
    assert main([*common, "add", "ftp://example.com/x"]) == 1
    assert "http" in capsys.readouterr().err


def test_cli_unknown_task_id(capsys, cli_args):
    common, _, _, _ = cli_args
    with pytest.raises(SystemExit) as info:
        main([*common, "get", "deadbeefdeadbeef"])
    assert info.value.code == 1


def test_cli_help_and_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "DownloadX" in capsys.readouterr().out


def test_cli_token_lifecycle(capsys, cli_args):
    common, state, _, _ = cli_args
    assert main([*common, "token"]) == 0
    first = capsys.readouterr().out.strip().splitlines()[-1]
    assert len(first) >= 20, first  # 自动生成

    assert main([*common, "token", "--rotate"]) == 0
    second = capsys.readouterr().out.strip().splitlines()[-1]
    assert second != first

    # 写进了配置
    import json

    saved = json.loads((state / "config.json").read_text(encoding="utf-8"))
    assert saved["api_token"] == second

    assert main([*common, "token", "--clear"]) == 0
    assert "已关闭" in capsys.readouterr().out
    saved = json.loads((state / "config.json").read_text(encoding="utf-8"))
    assert saved["api_token"] == ""
