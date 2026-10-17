"""DownloadX 命令行。

两种工作方式，自动选择：

1. **连服务**：本机已有 DownloadX 服务在跑 -> 所有命令走 HTTP，多端共享同一个引擎。
2. **前台下载**：没有服务 -> 命令自己起引擎，``add`` 像 wget/curl 一样边下边显示进度，
   下完就退出（适合脚本里一条命令搞定）。

``--server`` 可强制走 HTTP；``--state-dir`` 可指定独立的状态目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config
from .core.download_manager import DownloadManager, InvalidOperation, TaskNotFound
from .storage.lock import LockError, SingleInstanceLock
from .version import APP_DISPLAY_NAME, APP_NAME, __version__

STATUS_CN = {
    "queued": "排队",
    "downloading": "下载中",
    "paused": "暂停",
    "verifying": "校验",
    "completed": "完成",
    "failed": "失败",
    "canceled": "取消",
}


# ----------------------------------------------------------------------
# 显示工具
# ----------------------------------------------------------------------
def human_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_time(sec: float | None) -> str:
    if sec is None or sec < 0 or sec != sec:
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def progress_bar(percent: float, width: int = 24) -> str:
    filled = int(max(0, min(100, percent)) / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def task_line(task: dict[str, Any], *, index: int | None = None) -> str:
    prefix = f"{index:>2}. " if index is not None else ""
    status = STATUS_CN.get(task["status"], task["status"])
    total = task.get("total_size")
    size_text = f"{human_bytes(task.get('downloaded_size'))}/{human_bytes(total)}" if total \
        else human_bytes(task.get("downloaded_size"))
    percent = f"{task.get('percent', 0):5.1f}%"
    speed = f"{human_bytes(task.get('speed', 0))}/s" if task.get("speed") else "—"
    eta = human_time(task.get("eta")) if task.get("eta") else "—"
    return (
        f"{prefix}{task['id'][:8]}  {status:<4}  {percent}  {size_text:>22}  "
        f"{speed:>12}  ETA {eta:>7}  {task['filename']}"
    )


def print_tasks(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("（没有任务）")
        return
    for i, task in enumerate(tasks, 1):
        print(task_line(task, index=i))
        if task.get("error"):
            print(f"      ⚠ {task['error']}")


def error_exit(message: str, code: int = 1) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(code)


# ----------------------------------------------------------------------
# 客户端：HTTP 或进程内
# ----------------------------------------------------------------------
class HttpBackend:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        import httpx

        async with httpx.AsyncClient(base_url=self.base, timeout=30.0) as client:
            resp = await client.request(method, path, **kwargs)
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail")
                except Exception:
                    detail = resp.text
                raise RuntimeError(detail or f"HTTP {resp.status_code}")
            return resp.json()

    async def add(self, url: str, **kwargs) -> dict:
        return await self._request("POST", "/api/tasks", json={"url": url, **kwargs})

    async def list(self, **kwargs) -> list[dict]:
        params = {k: v for k, v in kwargs.items() if v is not None}
        return (await self._request("GET", "/api/tasks", params=params))["tasks"]

    async def get(self, task_id: str) -> dict:
        return await self._request("GET", f"/api/tasks/{task_id}")

    async def action(self, task_id: str, action: str, **params) -> dict:
        return await self._request("POST", f"/api/tasks/{task_id}/{action}", params=params)

    async def delete(self, task_id: str, delete_file: bool = False) -> dict:
        return await self._request(
            "DELETE", f"/api/tasks/{task_id}", params={"delete_file": delete_file}
        )

    async def stats(self) -> dict:
        return await self._request("GET", "/api/stats")

    async def history(self, limit: int = 20) -> list[dict]:
        return (await self._request("GET", "/api/history", params={"limit": limit}))["records"]

    async def get_config(self) -> dict:
        return await self._request("GET", "/api/config")

    async def patch_config(self, payload: dict) -> dict:
        return await self._request("PATCH", "/api/config", json=payload)

    async def probe(self, url: str) -> dict:
        return await self._request("POST", "/api/probe", json={"url": url})

    async def nl(self, text: str, execute: bool = True) -> dict:
        return await self._request("POST", "/agent/nl", json={"text": text, "execute": execute})

    async def manifest(self) -> dict:
        return await self._request("GET", "/agent/manifest")

    async def aclose(self) -> None:
        return None


class LocalBackend:
    """进程内直接使用引擎：不需要服务，但会独占状态目录。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.manager: DownloadManager | None = None
        self.lock = SingleInstanceLock(config.state_dir)

    async def open(self) -> "LocalBackend":
        if not self.lock.acquire():
            holder = self.lock.holder() or {}
            raise RuntimeError(
                f"状态目录被占用（pid={holder.get('pid')}）。"
                "请改用正在运行的服务：downloadx --server http://127.0.0.1:8720 ..."
            )
        self.manager = DownloadManager(self.config)
        await self.manager.start()
        return self

    async def aclose(self) -> None:
        if self.manager is not None:
            await self.manager.stop()
            self.manager = None
        self.lock.release()

    def _m(self) -> DownloadManager:
        assert self.manager is not None, "backend 未打开"
        return self.manager

    async def add(self, url: str, **kwargs) -> dict:
        # 外部接口用 sha256 命名，引擎内部用 expected_sha256
        if "sha256" in kwargs:
            kwargs["expected_sha256"] = kwargs.pop("sha256")
        task = await self._m().add(url, **kwargs)
        return self._m().public(task)

    async def list(self, **kwargs) -> list[dict]:
        return self._m().snapshot(
            status=kwargs.get("status"), order=kwargs.get("order") or "created"
        )

    async def get(self, task_id: str) -> dict:
        return self._m().public(self._m().require(task_id))

    async def action(self, task_id: str, action: str, **params) -> dict:
        manager = self._m()
        if action == "pause":
            await manager.pause(task_id)
        elif action == "resume":
            await manager.resume(task_id)
        elif action == "cancel":
            await manager.cancel(task_id)
        elif action == "retry":
            await manager.retry(task_id, reset=bool(params.get("reset")))
        elif action == "pause-all":
            await manager.pause_all()
        elif action == "resume-all":
            await manager.resume_all()
        return manager.public(manager.require(task_id))

    async def delete(self, task_id: str, delete_file: bool = False) -> dict:
        removed = await self._m().remove(task_id, delete_file=delete_file)
        return {"removed": removed, "id": task_id}

    async def stats(self) -> dict:
        return self._m().stats()

    async def history(self, limit: int = 20) -> list[dict]:
        return self._m().history(limit=limit)

    async def get_config(self) -> dict:
        return self.config.to_dict()

    async def patch_config(self, payload: dict) -> dict:
        manager = self._m()
        if payload.get("download_dir"):
            await manager.set_download_dir(payload["download_dir"])
        if payload.get("max_concurrent_tasks") is not None:
            await manager.set_max_concurrent(int(payload["max_concurrent_tasks"]))
        if payload.get("global_speed_limit") is not None:
            await manager.set_global_speed_limit(int(payload["global_speed_limit"]))
        return self.config.to_dict()

    async def probe(self, url: str) -> dict:
        info = await self._m().probe(url)
        return info.to_dict()

    async def nl(self, text: str, execute: bool = True) -> dict:
        from .api.agent_api import handle_natural_language

        return await handle_natural_language(text, self._m(), execute=execute)

    async def manifest(self) -> dict:
        from .api.agent_api import build_manifest

        return build_manifest()


# ----------------------------------------------------------------------
# 后端选择
# ----------------------------------------------------------------------
async def _server_alive(url: str, timeout: float = 0.5) -> bool:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url.rstrip("/") + "/api/health")
            return resp.status_code == 200
    except Exception:
        return False


def build_config(args: argparse.Namespace) -> Config:
    config = Config.load(state_dir=Path(args.state_dir) if args.state_dir else None)
    if getattr(args, "download_dir", None):
        config.download_dir = Path(args.download_dir).expanduser()
    config.ensure_dirs()
    return config


async def open_backend(args: argparse.Namespace, config: Config):
    """返回 (backend, is_remote)。"""
    explicit = getattr(args, "server", None)
    force_local = getattr(args, "local", False)
    candidate = explicit or f"http://{config.host}:{config.port}"
    if (explicit or await _server_alive(candidate)) and not force_local:
        if not await _server_alive(candidate, timeout=2.0):
            error_exit(f"连不上 DownloadX 服务：{candidate}")
        return HttpBackend(candidate), True
    local = LocalBackend(config)
    try:
        await local.open()
    except (RuntimeError, LockError) as exc:
        error_exit(str(exc))
    return local, False


# ----------------------------------------------------------------------
# 命令实现
# ----------------------------------------------------------------------
async def _watch(backend, task_id: str, *, quiet: bool = False) -> dict:
    """前台进度显示，直到任务停下（完成/失败/暂停/取消）。

    ``quiet=True``（配合 ``--json``）时不打印任何东西，只把最终状态交回调用方 ——
    这样一次调用只产出一个 JSON 文档，脚本可以直接把整段输出丢给 json.loads。
    """
    interactive = sys.stdout.isatty()
    if quiet:
        interactive = False
    last_len = 0
    while True:
        task = await backend.get(task_id)
        status = task["status"]
        if status in ("completed", "failed", "paused", "canceled"):
            if not quiet:
                if interactive and last_len:
                    sys.stdout.write("\r" + " " * last_len + "\r")
                print(task_line(task))
                if task.get("error"):
                    print(f"⚠ {task['error']}", file=sys.stderr)
            return task
        if interactive:
            line = (
                f"{progress_bar(task.get('percent', 0))} "
                f"{task.get('percent', 0):5.1f}%  "
                f"{human_bytes(task.get('downloaded_size'))}/{human_bytes(task.get('total_size'))}  "
                f"{human_bytes(task.get('speed', 0))}/s  "
                f"ETA {human_time(task.get('eta'))}  "
            )
            sys.stdout.write("\r" + line + " " * max(0, last_len - len(line)))
            sys.stdout.flush()
            last_len = len(line)
        await asyncio.sleep(0.3)


async def cmd_add(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, remote = await open_backend(args, config)
    try:
        wait = args.wait
        if wait is None:
            wait = not remote  # 前台模式默认等到下完；连服务时默认交给服务
        results = []
        for url in args.urls:
            payload: dict[str, Any] = {}
            if args.dir:
                payload["save_dir"] = args.dir
            if args.name and len(args.urls) == 1:
                payload["filename"] = args.name
            if args.priority is not None:
                payload["priority"] = args.priority
            if args.connections is not None:
                payload["connections"] = args.connections
            if args.limit:
                payload["speed_limit"] = int(args.limit * 1024 * 1024)
            if args.sha256 and len(args.urls) == 1:
                payload["sha256"] = args.sha256
            payload["start"] = True
            try:
                task = await backend.add(url, **payload)
            except Exception as exc:
                print(f"添加失败 {url}：{exc}", file=sys.stderr)
                continue
            results.append(task)
            if not args.json:
                print(
                    f"已加入：{task['filename']}  (id={task['id'][:8]}, "
                    f"{'多连接' if task.get('mode') == 'segmented' else '单连接'})"
                )
            if wait:
                # --json + --wait：只输出"最终状态"这一个文档；不带 --wait 才输出入队快照
                final = await _watch(backend, task["id"], quiet=args.json)
                if args.json:
                    print(json.dumps(final, ensure_ascii=False))
            elif args.json:
                print(json.dumps(task, ensure_ascii=False))
        if not wait and not remote and results and not args.json:
            print(
                "（进程内模式：任务已入队，但没有常驻服务，"
                "需运行 downloadx serve 或再执行一次 downloadx list 才会继续下载）"
            )
        if args.json:
            return 0
        return 0 if results else 1
    finally:
        await backend.aclose()


async def cmd_list(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        tasks = await backend.list(status=args.status, order=args.sort)
        if args.json:
            print(json.dumps(tasks, ensure_ascii=False, indent=2))
        else:
            print_tasks(tasks)
        return 0
    finally:
        await backend.aclose()


async def cmd_get(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        task = await backend.get(args.task_id)
        if args.json:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            print(task_line(task))
            for key in ("url", "save_path", "mode", "plan_reason", "sha256"):
                if task.get(key):
                    print(f"  {key}: {task[key]}")
        return 0
    finally:
        await backend.aclose()


async def cmd_action(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        task = await backend.action(args.task_id, args.action)
        print(f"{args.action} -> {STATUS_CN.get(task['status'], task['status'])}  {task['filename']}")
        return 0
    finally:
        await backend.aclose()


async def cmd_retry(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, remote = await open_backend(args, config)
    try:
        task = await backend.action(args.task_id, "retry", reset=args.reset)
        if not args.json:
            print(f"已重新入队：{task['filename']}")
        if args.wait or not remote:
            final = await _watch(backend, args.task_id, quiet=args.json)
            if args.json:
                print(json.dumps(final, ensure_ascii=False))
        elif args.json:
            print(json.dumps(task, ensure_ascii=False))
        return 0
    finally:
        await backend.aclose()


async def cmd_remove(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        result = await backend.delete(args.task_id, delete_file=args.delete_file)
        print("已移除" if result.get("removed") else "任务不存在")
        return 0
    finally:
        await backend.aclose()


async def cmd_bulk(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        if args.bulk_action == "pause-all":
            active = [t for t in await backend.list() if t["status"] in ("downloading", "queued")]
            for task in active:
                await backend.action(task["id"], "pause")
            print(f"已暂停 {len(active)} 个任务")
        else:
            paused = [t for t in await backend.list() if t["status"] == "paused"]
            for task in paused:
                await backend.action(task["id"], "resume")
            print(f"已恢复 {len(paused)} 个任务")
        return 0
    finally:
        await backend.aclose()


async def cmd_stats(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        stats = await backend.stats()
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        print(f"{APP_DISPLAY_NAME} {stats.get('version', __version__)}")
        print(f"  总速度      : {human_bytes(stats.get('download_speed', 0))}/s")
        print(f"  下载中/排队 : {stats.get('active_tasks', 0)} / {stats.get('queued_tasks', 0)}")
        print(f"  暂停/失败   : {stats.get('paused_tasks', 0)} / {stats.get('failed_tasks', 0)}")
        print(f"  已完成      : {stats.get('completed_tasks', 0)}")
        print(f"  剩余总量    : {human_bytes(stats.get('total_remaining_bytes', 0))}")
        conn = stats.get("connections", {})
        print(f"  连接池      : {conn.get('in_use', 0)}/{conn.get('limit', 0)}（峰值 {conn.get('peak_in_use', 0)}）")
        queue = stats.get("queue", {})
        print(f"  并发上限    : {queue.get('max_concurrent', 0)}")
        bw = stats.get("bandwidth", {})
        rate = bw.get("global_rate", 0)
        print(f"  全局限速    : {'不限' if not rate else human_bytes(rate) + '/s'}")
        return 0
    finally:
        await backend.aclose()


async def cmd_history(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        records = await backend.history(limit=args.limit)
        if args.json:
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return 0
        if not records:
            print("（没有历史记录）")
            return 0
        for record in records:
            finished = time.strftime("%m-%d %H:%M", time.localtime(record.get("finished_at", 0)))
            status = STATUS_CN.get(record.get("status"), record.get("status"))
            print(
                f"{finished}  {status:<4}  {human_bytes(record.get('total_size')):>10}  "
                f"{record.get('filename')}"
            )
            if record.get("error"):
                print(f"            ⚠ {record['error']}")
        return 0
    finally:
        await backend.aclose()


async def cmd_probe(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        info = await backend.probe(args.url)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        print(f"文件名    : {info['filename']}")
        print(f"大小      : {human_bytes(info['size'])}（{info['size']} 字节）")
        print(f"断点续传  : {'支持' if info['resumable'] else '不支持'}")
        print(f"类型      : {info.get('content_type') or '未知'}")
        print(f"最终地址  : {info['final_url']}")
        return 0
    finally:
        await backend.aclose()


async def cmd_config(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, _ = await open_backend(args, config)
    try:
        payload: dict[str, Any] = {}
        if args.set_download_dir:
            payload["download_dir"] = args.set_download_dir
        if args.set_max_concurrent is not None:
            payload["max_concurrent_tasks"] = args.set_max_concurrent
        if args.set_global_limit is not None:
            payload["global_speed_limit"] = int(args.set_global_limit * 1024 * 1024)
        data = await backend.patch_config(payload) if payload else await backend.get_config()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for key in (
            "download_dir",
            "state_dir",
            "max_concurrent_tasks",
            "max_connections_per_task",
            "max_global_connections",
            "global_speed_limit",
            "default_priority",
            "auto_resume",
            "verify_sha256",
            "host",
            "port",
        ):
            if key in data:
                print(f"{key:<26}: {data[key]}")
        return 0
    finally:
        await backend.aclose()


async def cmd_nl(args: argparse.Namespace) -> int:
    config = build_config(args)
    backend, remote = await open_backend(args, config)
    try:
        result = await backend.nl(args.text, execute=not args.plan_only)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[规划器: {result['planner']}] {result['reply']}")
            for call in result.get("tool_calls", []):
                mark = "✓" if call.get("ok") else "✗"
                print(f"  {mark} {call['name']} {json.dumps(call.get('arguments'), ensure_ascii=False)}")
        # 进程内模式：指令新建的下载要接着下完，否则命令退出后没人推进它
        if not remote and not args.plan_only:
            for call, outcome in zip(result.get("tool_calls", []), result.get("results", [])):
                if call.get("name") == "add_download" and outcome.get("ok"):
                    task_id = outcome.get("result", {}).get("id")
                    if task_id:
                        await _watch(backend, task_id)
        return 0
    finally:
        await backend.aclose()


async def cmd_tools(args: argparse.Namespace) -> int:
    from .api.agent_api import build_manifest

    manifest = build_manifest()
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    print(f"{APP_DISPLAY_NAME} {manifest['version']} —— 可用工具（{len(manifest['tools'])} 个）")
    for tool in manifest["tools"]:
        params = ", ".join(tool["parameters"].get("properties", {}))
        flag = "  [有副作用]" if tool.get("side_effects") else ""
        print(f"  {tool['name']:<18} {tool['description']}{flag}")
        if params:
            print(f"      参数: {params}")
    return 0


async def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.local_api import create_app

    config = build_config(args)
    if args.port:
        config.port = args.port
    if args.host:
        config.host = args.host
    lock = SingleInstanceLock(config.state_dir)
    if not lock.acquire():
        holder = lock.holder() or {}
        error_exit(f"已有 DownloadX 服务在运行（pid={holder.get('pid')}）")
    manager = DownloadManager(config)
    app = create_app(manager)
    banner = (
        f"\n  {APP_DISPLAY_NAME} {__version__}\n"
        f"  Web UI   : http://{config.host}:{config.port}/\n"
        f"  本地 API : http://{config.host}:{config.port}/api/tasks\n"
        f"  Agent API: http://{config.host}:{config.port}/agent/manifest\n"
        f"  下载目录 : {config.download_dir}\n"
        f"  状态目录 : {config.state_dir}\n"
    )
    print(banner)
    try:
        server = uvicorn.Server(
            uvicorn.Config(app, host=config.host, port=config.port, log_level=args.log_level.lower())
        )
        await server.serve()
    finally:
        lock.release()
    return 0


async def cmd_token(args: argparse.Namespace) -> int:
    """查看 / 轮换 / 关闭本地 API 的访问令牌。"""
    config = build_config(args)
    if args.clear:
        config.api_token = ""
        path = config.save()
        print(f"已关闭访问令牌（{path}）。API 恢复为仅凭回环地址保护。")
        return 0
    if args.rotate or not config.api_token:
        config.api_token = secrets.token_urlsafe(24)
        path = config.save()
        print(f"已生成新令牌并写入 {path}")
        print("提示：正在运行的服务需重启后生效；面板第一次请求时会提示输入。")
    print(config.api_token)
    return 0


# ----------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------
def add_common_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """全局选项。

    ``suppress=True`` 用在子命令上：子命令没写这个选项时 **不要覆盖**
    主解析器已经解析出来的值（默认值会悄悄把 ``--state-dir`` 清成 None）。
    """
    nothing = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--server",
        default=nothing,
        help="强制连接已有的 DownloadX 服务，如 http://127.0.0.1:8720",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="强制进程内运行，即使本机已有服务",
    )
    parser.add_argument(
        "--state-dir",
        default=nothing,
        help="状态目录（默认 %LOCALAPPDATA%/DownloadX）",
    )
    parser.add_argument("--download-dir", default=nothing, help="本次使用的下载目录")
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="以 JSON 输出，便于脚本消费",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="downloadx",
        description=f"{APP_DISPLAY_NAME} —— 本地优先、可被 Agent 调用的下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  downloadx serve                        启动服务与 Web UI\n"
            "  downloadx add https://x/y.zip          直接下载（前台显示进度）\n"
            "  downloadx add <url> --priority 0 --conn 16 --limit 5\n"
            "  downloadx list --status downloading\n"
            "  downloadx pause 3f9a1c2b\n"
            "  downloadx nl \"把那个大文件暂停，其他继续\"\n"
            "  downloadx tools                        查看可被 Agent 调用的工具\n"
        ),
    )
    parser.add_argument("--version", action="version",
                        version=f"{APP_DISPLAY_NAME} {__version__}（标识 {APP_NAME}）")
    add_common_options(parser, suppress=False)

    sub = parser.add_subparsers(dest="command", metavar="命令")

    def new_parser(*args, **kwargs) -> argparse.ArgumentParser:
        """子命令也要能接全局选项（``downloadx serve --port 1 --state-dir X``）。"""
        child = sub.add_parser(*args, **kwargs)
        add_common_options(child, suppress=True)
        return child

    p_serve = new_parser("serve", help="启动本地服务 + Web UI")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=cmd_serve)

    p_add = new_parser("add", help="新建下载任务")
    p_add.add_argument("urls", nargs="+", help="一个或多个下载链接")
    p_add.add_argument("--dir", help="保存目录")
    p_add.add_argument("--name", help="另存文件名（仅单个链接时有效）")
    p_add.add_argument("--priority", type=int, help="优先级，0 最高")
    p_add.add_argument("--conn", type=int, dest="connections", help="分片连接数")
    p_add.add_argument("--limit", type=float, help="限速 MB/s，0 不限速")
    p_add.add_argument("--sha256", help="完成后校验的 SHA-256")
    p_add.add_argument("--wait", action="store_true", default=None, help="等到下载结束")
    p_add.add_argument("--no-wait", action="store_false", dest="wait", help="只入队不等待")
    p_add.set_defaults(func=cmd_add)

    p_list = new_parser("list", aliases=["ls"], help="列出任务")
    p_list.add_argument("--status", help="按状态过滤")
    p_list.add_argument("--sort", default="created", choices=["created", "priority", "updated"])
    p_list.set_defaults(func=cmd_list)

    p_get = new_parser("get", help="查看单个任务详情")
    p_get.add_argument("task_id")
    p_get.set_defaults(func=cmd_get)

    for name, action in (("pause", "pause"), ("resume", "resume"), ("cancel", "cancel")):
        p = new_parser(name, help=f"{action} 任务")
        p.add_argument("task_id")
        p.set_defaults(func=cmd_action, action=action)

    p_retry = new_parser("retry", help="重试任务")
    p_retry.add_argument("task_id")
    p_retry.add_argument("--reset", action="store_true", help="丢弃已下载进度，从零开始")
    p_retry.add_argument("--wait", action="store_true", default=None)
    p_retry.set_defaults(func=cmd_retry)

    p_rm = new_parser("rm", help="移除任务")
    p_rm.add_argument("task_id")
    p_rm.add_argument("--delete-file", action="store_true", help="连同已下载文件一起删除")
    p_rm.set_defaults(func=cmd_remove)

    for name in ("pause-all", "resume-all"):
        p = new_parser(name, help="批量暂停 / 恢复")
        p.set_defaults(func=cmd_bulk, bulk_action=name)

    p_stats = new_parser("stats", help="查看全局统计")
    p_stats.set_defaults(func=cmd_stats)

    p_history = new_parser("history", help="查看历史记录")
    p_history.add_argument("--limit", type=int, default=20)
    p_history.set_defaults(func=cmd_history)

    p_probe = new_parser("probe", help="只探测链接，不下载")
    p_probe.add_argument("url")
    p_probe.set_defaults(func=cmd_probe)

    p_config = new_parser("config", help="查看 / 修改配置")
    p_config.add_argument("--set-download-dir")
    p_config.add_argument("--set-max-concurrent", type=int)
    p_config.add_argument("--set-global-limit", type=float, help="MB/s")
    p_config.set_defaults(func=cmd_config)

    p_nl = new_parser("nl", help="用自然语言下达指令（可选 LLM，默认规则解析）")
    p_nl.add_argument("text")
    p_nl.add_argument("--plan-only", action="store_true", help="只出计划不执行")
    p_nl.set_defaults(func=cmd_nl)

    p_tools = new_parser("tools", help="列出可被 Agent 调用的工具")
    p_tools.set_defaults(func=cmd_tools)

    p_token = new_parser("token", help="查看 / 轮换本地 API 访问令牌")
    p_token.add_argument("--rotate", action="store_true", help="生成新令牌（旧的立即失效）")
    p_token.add_argument("--clear", action="store_true", help="关闭令牌鉴权")
    p_token.set_defaults(func=cmd_token)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断：下载进度已保存到磁盘，下次会从断点继续。", file=sys.stderr)
        return 130
    except (TaskNotFound, InvalidOperation, ValueError, RuntimeError) as exc:
        error_exit(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
