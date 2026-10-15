"""把 DownloadX 同步进「我的AI助手」中台：技能库里加一条，应用广场加一个可一键启停的面板。

沿用这台机器上已有的两套约定：

* 优先走中台自己的 HTTP API（`POST /api/skills`、`POST /api/apps`），**不直接改数据库**；
* 中台开了登录之后 API 需要密码（`api_login` 用 PBKDF2 校验，脚本拿不到），
  这时退回 motion-studio 用过的做法：直接写 `data/hub.db`，**写前自动备份**，
  并同步写好 `data/skills/<id>.md` 镜像。

技能同名默认跳过（`--update` 覆盖），应用按 slug 覆盖写入，重复执行安全。

用法::

    python scripts/install_to_assistant.py --check                 # 只报告
    python scripts/install_to_assistant.py                         # 自动选路并写入
    python scripts/install_to_assistant.py --password <中台密码>    # 走 API
    python scripts/install_to_assistant.py --db                    # 强制直接写库
    python scripts/install_to_assistant.py --update --no-app
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKILL_DIR = ENGINE / "skill" / "downloadx"
#: 本机的中台位置；换机器用环境变量覆盖，不必改代码。
ASSISTANT_DIR = Path(os.environ.get("DOWNLOADX_ASSISTANT_DIR") or r"D:\Projects\我的AI助手")

SKILL_NAME = "下载工具 DownloadX"
SKILL_DESC = (
    "用本机 DownloadX 引擎下载文件与视频：多连接断点续传、队列与优先级、限速、"
    "SHA-256 校验；YouTube / B站 / 推特视频按站点自动分流（yt-dlp / download-x-video）。"
)
APP_SLUG = "downloadx"
APP_NAME = "下载器面板 DownloadX"
APP_DESC = "DownloadX 的本地 Web 面板：任务队列、进度、限速、历史，端口 8720"
APP_ICON = "⬇️"
APP_PORT = "8720"


# ----------------------------------------------------------------------
# 内容组装
# ----------------------------------------------------------------------
def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text


def build_skill_content() -> str:
    header = "\n".join([
        f"> 来源：本机 Codex skill `downloadx`。引擎项目：{ENGINE}",
        f"> 命令行入口：`python \"{ENGINE / 'scripts' / 'dx.py'}\" ...`（解析引擎 + 挑解释器 + 转发参数）",
        f"> 视频分流：`python \"{SKILL_DIR / 'scripts' / 'fetch_video.py'}\" <url>`",
        f"> Web 面板 / 本地 API：http://127.0.0.1:{APP_PORT}（应用广场可一键启停）",
        "> 引擎是单实例：同一状态目录同时只能有一个进程在写；面板起不来多半是锁被别的进程占了。",
        "",
    ])
    body = strip_frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")).rstrip()
    video = (SKILL_DIR / "references" / "video-sites.md").read_text(encoding="utf-8").rstrip()
    return "\n".join([header, body, "", "---", "", video, ""])


def python_that_runs_engine() -> str:
    """应用广场的启动命令里要写死一个真能跑起引擎的解释器。"""
    for python in (sys.executable, r"C:\Program Files\Python312\python.exe"):
        if not python or not Path(python).exists():
            continue
        try:
            result = subprocess.run([python, "-m", "downloadx", "--version"],
                                    cwd=str(ENGINE), capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return python
    raise SystemExit("找不到能跑起 downloadx 的解释器；先确认引擎依赖装好了")


def app_payload(python: str) -> dict:
    return {
        "slug": APP_SLUG,
        "name": APP_NAME,
        "description": APP_DESC,
        "icon": APP_ICON,
        "kind": "local",
        "target": f"http://127.0.0.1:{APP_PORT}",
        "cwd": str(ENGINE),
        "start_cmd": f'"{python}" -m downloadx serve --port {APP_PORT}',
        "port": APP_PORT,
        "permissions": "仅自己",
    }


# ----------------------------------------------------------------------
# 传输层 A：中台 HTTP API
# ----------------------------------------------------------------------
class ApiTransport:
    label = "中台 HTTP API"

    def __init__(self, base: str, password: str | None = None) -> None:
        self.base = base.rstrip("/")
        self.cookie = ""
        if password:
            result = self._send("/api/login", {"password": password}, "POST", expect_cookie=True)
            if not result.get("ok"):
                raise SystemExit(f"中台登录失败：{result.get('error')}")
            print("已用密码登录中台，使用会话 cookie 调用 API")

    def _send(self, path: str, payload: dict | None = None, method: str = "GET",
              expect_cookie: bool = False):
        data = None
        headers = {"Accept": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            if expect_cookie:
                raw_cookie = resp.headers.get("Set-Cookie", "")
                if raw_cookie:
                    self.cookie = raw_cookie.split(";", 1)[0]
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}

    def skills(self) -> dict[str, dict]:
        return {s["name"]: s for s in self._send("/api/skills").get("skills", [])}

    def apps(self) -> dict[str, dict]:
        return {a.get("slug"): a for a in self._send("/api/apps").get("apps", [])}

    def put_skill(self, name: str, description: str, content: str, old_id: int | None) -> str:
        if old_id is not None:
            self._send("/api/skills/delete", {"id": old_id}, method="POST")
        result = self._send("/api/skills",
                            {"name": name, "description": description, "content": content},
                            method="POST")
        return f"id={result.get('id')} error={result.get('error')}"

    def put_app(self, payload: dict) -> str:
        return str(self._send("/api/apps", payload, method="POST").get("ok", "?"))


# ----------------------------------------------------------------------
# 传输层 B：直接写 hub.db（motion-studio 用过的做法，写前备份）
# ----------------------------------------------------------------------
class DbTransport:
    def __init__(self, assistant_dir: Path, *, write: bool) -> None:
        self.dir = assistant_dir
        self.db_path = assistant_dir / "data" / "hub.db"
        self.skills_dir = assistant_dir / "data" / "skills"
        if not self.db_path.is_file():
            raise SystemExit(f"找不到中台数据库：{self.db_path}")
        self.writable = write
        self.backup: Path | None = None

    @property
    def label(self) -> str:
        if self.backup is not None:
            return f"直接写 hub.db（已备份 {self.backup.name}）"
        return "直接写 hub.db" if self.writable else "直接读 hub.db（只读）"

    def _ensure_backup(self) -> None:
        """第一次真正要写之前才备份 —— 内容没变就一次写都不发生，也就不会堆备份。"""
        if self.backup is not None or not self.writable:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.backup = self.db_path.with_name(f"hub.db.bak-{stamp}-downloadx")
        shutil.copy2(self.db_path, self.backup)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        return conn

    def skills(self) -> dict[str, dict]:
        with self._connect() as conn:
            return {r["name"]: dict(r) for r in conn.execute("SELECT * FROM skills")}

    def apps(self) -> dict[str, dict]:
        with self._connect() as conn:
            return {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM apps")}

    def put_skill(self, name: str, description: str, content: str, old_id: int | None) -> str:
        self._ensure_backup()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            if old_id is None:
                cur = conn.execute(
                    "INSERT INTO skills(name, description, content, created_at) VALUES(?,?,?,?)",
                    (name, description, content, stamp))
                skill_id = cur.lastrowid
            else:
                conn.execute("UPDATE skills SET description=?, content=? WHERE id=?",
                             (description, content, old_id))
                skill_id = old_id
            conn.commit()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        (self.skills_dir / f"{skill_id}.md").write_text(content, encoding="utf-8")
        return f"id={skill_id}（镜像已写 data/skills/{skill_id}.md）"

    def put_app(self, payload: dict) -> str:
        self._ensure_backup()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("DELETE FROM apps WHERE slug=?", (payload["slug"],))
            conn.execute(
                "INSERT INTO apps(slug, name, description, icon, kind, target, permissions,"
                " cwd, start_cmd, port, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (payload["slug"], payload["name"], payload["description"], payload["icon"],
                 payload["kind"], payload["target"], payload["permissions"], payload["cwd"],
                 payload["start_cmd"], payload["port"], stamp))
            conn.commit()
        return "ok"


# ----------------------------------------------------------------------
def pick_transport(*, api: str, password: str | None, use_db: bool,
                   assistant_dir: Path, write: bool, log=print):
    if use_db:
        return DbTransport(assistant_dir, write=write)
    if password:
        return ApiTransport(api, password)
    try:
        transport = ApiTransport(api)
        transport.skills()          # 先探一下：401 会在这里抛出来
        return transport
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        if write:
            log("中台 API 需要登录（401）且未提供 --password，改走直接写库（写前会先备份 hub.db）")
        else:
            log("中台 API 需要登录（401）且未提供 --password，改走直接读库（只读，不写）")
        return DbTransport(assistant_dir, write=write)
    except urllib.error.URLError as exc:
        raise SystemExit(f"连不上中台 {api}：{exc}。先启动它：{assistant_dir / '启动.bat'}")


APP_FIELDS = ("name", "description", "icon", "kind", "target", "cwd", "start_cmd", "port")


def skill_matches(existing: dict, content: str, description: str) -> bool:
    """判断中台那条技能是否已经是最新的。

    DB 路径下能拿到完整 content；API 的列表只给 `LENGTH(content) AS size`
    （SQLite 的 LENGTH 对 TEXT 返回字符数），所以两种形状都要认。
    """
    if (existing.get("description") or "") != description:
        return False
    if existing.get("content") is not None:
        return existing["content"] == content
    size = existing.get("size")
    return size is not None and int(size) == len(content)


def run_sync(*, api: str = "http://127.0.0.1:8765", password: str | None = None,
             use_db: bool = False, assistant_dir: Path = ASSISTANT_DIR,
             check: bool = False, update: bool = False, force: bool = False,
             with_app: bool = True, log=print) -> dict:
    """把技能（+应用）同步到中台。返回结构化结果，供 CLI 与 sync_skill.py 共用。"""
    summary: dict = {"ok": False, "transport": "", "backup": None,
                     "skill": {"action": "none"}, "app": {"action": "none"}, "error": None}
    try:
        transport = pick_transport(api=api, password=password, use_db=use_db,
                                  assistant_dir=assistant_dir, write=not check, log=log)
    except SystemExit as exc:
        summary["error"] = str(exc)
        log(str(exc))
        return summary

    skills = transport.skills()
    apps = transport.apps()
    summary["transport"] = transport.label
    log(f"传输方式：{transport.label}")
    log(f"中台技能数：{len(skills)}   应用数：{len(apps)}")

    python = python_that_runs_engine()
    content = build_skill_content()
    old = skills.get(SKILL_NAME)
    log(f"技能「{SKILL_NAME}」：{'已存在 id=%s' % old['id'] if old else '不存在'}；"
        f"正文 {len(content)} 字符；解释器 {python}")

    if check:
        if old:
            same = skill_matches(old, content, SKILL_DESC)
            summary["skill"] = {"action": "unchanged" if same else "stale", "id": old["id"]}
            log("中台技能内容：" + ("一致" if same else "与本机不一致（跑一次同步即可）"))
        else:
            summary["skill"] = {"action": "absent"}
            log("中台还没有这条技能")
        wanted = app_payload(python)
        current = apps.get(APP_SLUG)
        same_app = bool(current) and all(str(current.get(k)) == str(wanted[k]) for k in APP_FIELDS)
        summary["app"] = {"action": "unchanged" if same_app else ("stale" if current else "absent")}
        log("中台应用：" + ("一致" if same_app else ("存在但配置不同" if current else "不存在")))
        summary["ok"] = True
        return summary

    if old is None:
        summary["skill"] = {"action": "created",
                            "id": None, "chars": len(content),
                            "detail": transport.put_skill(SKILL_NAME, SKILL_DESC, content, None)}
    elif not update:
        summary["skill"] = {"action": "skipped", "id": old["id"], "chars": len(content)}
        log(f"跳过技能（已存在；--update 可覆盖）")
    else:
        same = skill_matches(old, content, SKILL_DESC)
        if same and not force:
            summary["skill"] = {"action": "unchanged", "id": old["id"], "chars": len(content)}
            log("技能内容与本机一致，跳过写入（也就不会产生新的数据库备份）")
        else:
            summary["skill"] = {"action": "updated", "id": old["id"], "chars": len(content),
                                "detail": transport.put_skill(SKILL_NAME, SKILL_DESC, content,
                                                              old["id"])}

    if with_app:
        wanted = app_payload(python)
        current = apps.get(APP_SLUG)
        if current and all(str(current.get(k)) == str(wanted[k]) for k in APP_FIELDS) and not force:
            summary["app"] = {"action": "unchanged", "slug": APP_SLUG}
            log("应用配置一致，跳过写入")
        else:
            summary["app"] = {"action": "created" if current is None else "updated",
                              "slug": APP_SLUG, "detail": transport.put_app(wanted)}

    summary["backup"] = getattr(transport, "backup", None)
    if summary["backup"]:
        summary["backup"] = Path(summary["backup"]).name
        log(f"数据库备份：{summary['backup']}")
    summary["ok"] = True
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="把 DownloadX 同步进「我的AI助手」")
    parser.add_argument("--api", default="http://127.0.0.1:8765", help="中台地址")
    parser.add_argument("--password", default=None, help="中台密码（走 API 时用）")
    parser.add_argument("--db", action="store_true", help="强制直接写 hub.db")
    parser.add_argument("--assistant-dir", default=str(ASSISTANT_DIR), help="中台目录")
    parser.add_argument("--check", action="store_true", help="只报告现状，不写任何东西")
    parser.add_argument("--update", action="store_true", help="同名技能已存在时覆盖（内容没变则跳过）")
    parser.add_argument("--force", action="store_true", help="即使内容一致也重写一遍")
    parser.add_argument("--no-app", action="store_true", help="只同步技能，不写应用")
    args = parser.parse_args()

    summary = run_sync(api=args.api, password=args.password, use_db=args.db,
                       assistant_dir=Path(args.assistant_dir), check=args.check,
                       update=args.update, force=args.force, with_app=not args.no_app)
    if not summary["ok"]:
        return 1
    if args.check:
        print("\n（--check：未写入任何东西）")
        return 0
    print(f"\n技能：{summary['skill']['action']}   应用：{summary['app']['action']}"
          + (f"   备份：{summary['backup']}" if summary["backup"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
