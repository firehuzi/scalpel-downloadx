#!/usr/bin/env python3
"""DownloadX skill 的薄封装：定位引擎 + 挑解释器 + 原样转发参数。

这个脚本**不做任何下载逻辑**。它解决的问题只有一个：skill 里不能写死一个绝对路径，
而引擎可能在项目目录、可能被 pip 装过、也可能被用户挪走。

用法::

    python dx.py doctor                     # 打印解析到的引擎/解释器/版本
    python dx.py add <url> --wait --json    # 其余参数原样交给 downloadx CLI

可用环境变量覆盖解析：
    DOWNLOADX_HOME    引擎项目根目录（里面应有 downloadx/ 包）
    DOWNLOADX_PYTHON  指定解释器
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 创建这个 skill 时引擎所在的位置。机器相关，只是候选之一：
# 换机器/挪目录时用 DOWNLOADX_HOME 覆盖，或者把 skill 放在引擎仓库里（见下面的自相对候选）。
RECORDED_ENGINE = Path(
    r"C:\Users\xheih\Documents\Codex\2026-10-07\codex-v0-1-0-windows-ai\outputs\DownloadX"
)
RECORDED_PYTHON = r"C:\Program Files\Python312\python.exe"

#: 引擎跑起来需要的全部导入（少一个都不算可用）
REQUIRED_IMPORTS = "import downloadx, aiohttp, fastapi, uvicorn, httpx, pydantic"
ENGINE_ENTRY = Path("downloadx") / "cli.py"


def _is_engine(root: Path) -> bool:
    return (root / ENGINE_ENTRY).exists()


def _self_relative_engine() -> Path | None:
    """本文件位于 <repo>/skill/downloadx/scripts/dx.py 时，仓库根就是引擎根。

    这条让 skill 跟着仓库走：clone 到任何位置都能自己找到引擎，不依赖本机路径。
    """
    candidate = Path(__file__).resolve().parents[3]
    return candidate if _is_engine(candidate) else None


def candidate_engines() -> list[Path | None]:
    """按可能性排序的引擎候选；None 表示"靠已安装的包导入"。"""
    found: list[Path | None] = []
    env_home = os.environ.get("DOWNLOADX_HOME")
    if env_home:
        found.append(Path(env_home).expanduser())
    inside_repo = _self_relative_engine()
    if inside_repo is not None:
        found.append(inside_repo)
    if _is_engine(RECORDED_ENGINE):
        found.append(RECORDED_ENGINE)

    # 项目less 会话的目录形如 <Documents>/Codex/<date>/<slug>/outputs/DownloadX
    documents = Path.home() / "Documents" / "Codex"
    if documents.is_dir():
        matches = sorted(
            (p for p in documents.glob("*/*/outputs/DownloadX") if _is_engine(p)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        found.extend(matches)

    # 去重且保序，最后补一个"已安装包"的兜底
    unique: list[Path | None] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    unique.append(None)
    return unique


def candidate_pythons() -> list[str]:
    found: list[str] = []
    env_python = os.environ.get("DOWNLOADX_PYTHON")
    if env_python:
        found.append(env_python)
    found.append(sys.executable)
    if Path(RECORDED_PYTHON).exists():
        found.append(RECORDED_PYTHON)
    for name in ("python", "python3"):
        which = shutil.which(name)
        if which:
            found.append(which)
    return [p for p in found if p]


def probe(python: str, engine: Path | None) -> tuple[bool, str, str]:
    """在给定解释器 + 引擎路径下试跑一次导入。"""
    env = os.environ.copy()
    if engine is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            [str(engine)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
    code = f"{REQUIRED_IMPORTS}; print(downloadx.__version__)"
    try:
        result = subprocess.run(
            [python, "-c", code], env=env, capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return True, result.stdout.decode("utf-8", "replace").strip(), ""
    detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
    return False, "", detail.splitlines()[-1] if detail else "导入失败"


def resolve() -> dict:
    """找到第一个可用的 (解释器, 引擎) 组合。"""
    attempts: list[str] = []
    for python in candidate_pythons():
        for engine in candidate_engines():
            ok, version, error = probe(python, engine)
            label = str(engine) if engine else "<已安装的 downloadx 包>"
            if ok:
                return {
                    "python": python,
                    "engine": str(engine) if engine else None,
                    "version": version,
                    "source": label,
                    "attempts": attempts,
                }
            attempts.append(f"{python} + {label}: {error}")
    raise SystemExit(
        "找不到可用的 DownloadX 引擎。\n"
        "  1) 确认引擎目录存在，然后用 DOWNLOADX_HOME=<路径> 指过去；\n"
        "  2) 或安装依赖：pip install -r <引擎目录>/requirements.txt\n"
        "已尝试：\n  " + "\n  ".join(attempts[-6:])
    )


def run_engine(resolution: dict, args: list[str]) -> int:
    env = os.environ.copy()
    if resolution["engine"]:
        env["PYTHONPATH"] = os.pathsep.join(
            [resolution["engine"]] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
    # Windows 控制台默认不是 UTF-8，中文进度/错误会变乱码甚至抛 UnicodeEncodeError
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    command = [resolution["python"], "-m", "downloadx", *args]
    try:
        return subprocess.run(command, env=env).returncode
    except KeyboardInterrupt:
        # 子进程已收到中断并自行收尾（进度落盘），父进程安静退出即可
        return 130


def doctor(resolution: dict) -> int:
    print("DownloadX skill 解析结果")
    print(f"  引擎      : {resolution['source']}")
    print(f"  解释器    : {resolution['python']}")
    print(f"  版本      : {resolution['version']}")
    if resolution["attempts"]:
        print(f"  先试失败  : {len(resolution['attempts'])} 种组合（不影响使用）")
    code = run_engine(resolution, ["stats"])
    print(f"  stats 退出码: {code}")
    return code


def main(argv: list[str]) -> int:
    resolution = resolve()
    if not argv or argv[0] in ("doctor", "env"):
        return doctor(resolution)
    return run_engine(resolution, argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
