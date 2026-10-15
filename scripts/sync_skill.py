r"""把 skill 从项目源同步到各个副本，并逐文件校验哈希。

为什么需要它：这个 skill 同时存在三份——项目源、Codex 加载目录、归档目录。
手工 `Copy-Item -Recurse` 一个文件夹到同名已存在目录会套出一层 downloadx/downloadx/
（真踩过：导致 skills 列表里出现两条同名 skill）。这里按**相对路径逐文件复制**，
天然不会套层，并且复制后立刻用 sha256 验证。

用法::

    python scripts/sync_skill.py                # 同步到默认目标（Codex + D:/Projects/skill）
    python scripts/sync_skill.py --check        # 只报告差异，不写任何文件
    python scripts/sync_skill.py --target <dir> # 只同步到指定目录（可重复）
    python scripts/sync_skill.py --prune        # 额外删除目标里多余的文件（精确镜像）
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "skill" / "downloadx"
DEFAULT_TARGETS = (
    Path.home() / ".codex" / "skills" / "downloadx",
    Path("D:/Projects/skill/downloadx"),
)
INSTALLER = Path(__file__).resolve().parent / "install_to_assistant.py"


def load_installer():
    """加载同目录的 install_to_assistant.py（文件名带下划线，可直接当模块用）。"""
    spec = importlib.util.spec_from_file_location("downloadx_install_to_assistant", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): digest(p)
        for p in root.rglob("*")
        if p.is_file()
    }


def sync_one(source: Path, target: Path, *, check: bool, prune: bool) -> int:
    print(f"\n目标: {target}")
    if not source.is_dir():
        print(f"  ✗ 源目录不存在: {source}")
        return 1
    src = snapshot(source)
    dst = snapshot(target) if target.is_dir() else {}

    missing = sorted(set(src) - set(dst))
    changed = sorted(k for k in set(src) & set(dst) if src[k] != dst[k])
    extra = sorted(set(dst) - set(src))

    if not (missing or changed or extra):
        print(f"  ✓ 已是最新（{len(src)} 个文件，哈希全部一致）")
        return 0
    for name in missing:
        print(f"  + 新增 {name}")
    for name in changed:
        print(f"  ~ 更新 {name}")
    for name in extra:
        print(f"  ! 目标多出 {name}" + ("（--prune 会删除）" if not prune else "（删除）"))
    if check:
        print("  （--check：未写入任何文件）")
        return 0

    for name in missing + changed:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
    if prune:
        for name in extra:
            (target / name).unlink()

    after = snapshot(target)
    bad = [
        name
        for name in src
        if after.get(name) != src[name]
    ]
    if bad:
        print(f"  ✗ 复制后校验失败: {bad}")
        return 1
    print(f"  ✓ 同步完成并校验通过（{len(src)} 个文件）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 downloadx skill 到各副本")
    parser.add_argument("--target", action="append", default=None, help="目标目录，可重复")
    parser.add_argument("--check", action="store_true", help="只报告差异")
    parser.add_argument("--prune", action="store_true", help="删除目标中多余的文件")
    parser.add_argument("--no-assistant", action="store_true",
                        help="不同步到「我的AI助手」中台")
    parser.add_argument("--assistant-password", default=None, help="中台密码（走 API 时用）")
    parser.add_argument("--assistant-db", action="store_true", help="强制直接写中台 hub.db")
    args = parser.parse_args()

    targets = [Path(t) for t in args.target] if args.target else list(DEFAULT_TARGETS)
    print(f"源: {SOURCE}")
    failed = [t for t in targets if sync_one(SOURCE, t, check=args.check, prune=args.prune)]
    if failed:
        print(f"\n有 {len(failed)} 个目标未同步成功")
        return 1
    print("\n文件副本已同步。")

    if args.no_assistant:
        print("（--no-assistant：跳过中台）")
        return 0

    # 文件同步完之后，把同一份内容推给「我的AI助手」中台（技能库 + 应用广场）。
    # 内容没变就不会写库，也就不会堆备份。
    print(f"\n=== 同步到「我的AI助手」 ===")
    try:
        installer = load_installer()
        summary = installer.run_sync(check=args.check, update=True,
                                     use_db=args.assistant_db,
                                     password=args.assistant_password)
    except SystemExit as exc:
        print(f"中台同步失败：{exc}")
        return 1
    if not summary.get("ok"):
        if args.check:
            print(f"（--check）中台不可达或读取失败：{summary.get('error')}")
            return 0
        print(f"中台同步失败：{summary.get('error')}")
        return 1
    if args.check:
        return 0
    print("\n" + summary_line(summary))
    print("skill 改动需重启 Codex 才会重新加载；中台页面刷新即可见。")
    return 0


def summary_line(summary: dict) -> str:
    names = {"created": "新建", "updated": "已更新", "unchanged": "一致（未写）",
             "skipped": "跳过", "absent": "不存在", "stale": "待更新", "none": "未处理"}
    skill = summary.get("skill", {})
    app = summary.get("app", {})
    text = (f"中台结果：技能 {names.get(skill.get('action'), skill.get('action'))}"
            f"（id={skill.get('id')}，{skill.get('chars', '?')} 字符）"
            f"；应用 {names.get(app.get('action'), app.get('action'))}")
    if summary.get("backup"):
        text += f"；数据库备份 {summary['backup']}"
    return text


if __name__ == "__main__":
    raise SystemExit(main())
