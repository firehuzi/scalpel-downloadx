"""Agent API —— 让 LLM / 自动化脚本把下载器当成一套工具来用。

设计红线（对应任务书 §1.1）：

* 这里只是**控制层**。所有工具都是对 ``DownloadManager`` 的确定性封装，
  不经过任何模型推理；模型只是"可选的自然语言翻译器"。
* 没有 LLM、没有网络、没有 API Key，全部工具依然可用。
* ``handle_natural_language`` 在没有可用 LLM 时自动退化为规则解析，
  规则解析同样能正确处理"下载这个链接""把 xxx 暂停"这类指令。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.download_manager import DownloadManager, InvalidOperation, TaskNotFound
from ..version import APP_NAME, __version__

log = logging.getLogger("downloadx.agent")

ToolHandler = Callable[[DownloadManager, dict[str, Any]], Awaitable[Any]]


# ----------------------------------------------------------------------
# 工具定义
# ----------------------------------------------------------------------
class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
        returns: str,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        self.returns = returns

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "returns": self.returns,
            "side_effects": self.name
            in {"add_download", "pause_download", "resume_download", "cancel_download",
                "retry_download", "remove_download", "set_priority", "set_speed_limit",
                "pause_all", "resume_all"},
        }

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_TASK_ID = {"type": "string", "description": "任务 ID"}


async def _add(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    task = await manager.add(
        args["url"],
        save_dir=args.get("save_dir"),
        filename=args.get("filename"),
        priority=args.get("priority"),
        connections=args.get("connections"),
        speed_limit=int(args.get("speed_limit") or 0),
        expected_sha256=args.get("sha256"),
        note=args.get("note") or "agent",
        start=bool(args.get("start", True)),
    )
    return manager.public(task)


async def _list(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    tasks = manager.snapshot(status=args.get("status"), order=args.get("order") or "created")
    if args.get("limit"):
        tasks = tasks[: int(args["limit"])]
    return {"count": len(tasks), "tasks": tasks}


async def _get(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    return manager.public(manager.require(args["task_id"]))


async def _pause(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    await manager.pause(args["task_id"])
    return manager.public(manager.require(args["task_id"]))


async def _resume(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    await manager.resume(args["task_id"])
    return manager.public(manager.require(args["task_id"]))


async def _cancel(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    await manager.cancel(args["task_id"])
    return manager.public(manager.require(args["task_id"]))


async def _retry(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    await manager.retry(args["task_id"], reset=bool(args.get("reset", False)))
    return manager.public(manager.require(args["task_id"]))


async def _remove(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    removed = await manager.remove(
        args["task_id"], delete_file=bool(args.get("delete_file", False))
    )
    return {"removed": removed, "task_id": args["task_id"]}


async def _set_priority(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    value = await manager.set_priority(args["task_id"], int(args["priority"]))
    return {"task_id": args["task_id"], "priority": value}


async def _set_speed(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    value = await manager.set_speed_limit(args["task_id"], int(args["speed_limit"]))
    return {"task_id": args["task_id"], "speed_limit": value}


async def _pause_all(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    return {"paused": await manager.pause_all()}


async def _resume_all(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    return {"resumed": await manager.resume_all()}


async def _stats(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    return manager.stats()


async def _history(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 20)
    records = manager.history(limit=limit)
    return {"count": len(records), "records": records}


async def _probe(manager: DownloadManager, args: dict[str, Any]) -> dict[str, Any]:
    info = await manager.probe(args["url"], args.get("headers"))
    return info.to_dict()


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool(
            "add_download",
            "新建一个 HTTP/HTTPS 下载任务并按优先级排队。返回任务 ID 与初始状态。",
            _obj(
                {
                    "url": {"type": "string", "description": "http/https 下载链接"},
                    "save_dir": {"type": "string", "description": "保存目录，缺省用全局下载目录"},
                    "filename": {"type": "string", "description": "另存文件名"},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 99,
                                 "description": "数字越小越优先，默认 5"},
                    "connections": {"type": "integer", "minimum": 1, "maximum": 32,
                                    "description": "分片连接数"},
                    "speed_limit": {"type": "integer", "minimum": 0,
                                    "description": "单任务限速 bytes/s，0 表示不限速"},
                    "sha256": {"type": "string", "description": "完成后校验的 SHA-256"},
                    "start": {"type": "boolean", "description": "是否立即开始，默认 true"},
                },
                ["url"],
            ),
            _add,
            "下载任务对象",
        ),
        Tool(
            "list_downloads",
            "列出下载任务（可按状态过滤）。状态取值：queued/downloading/paused/verifying/completed/failed/canceled。",
            _obj(
                {
                    "status": {"type": "string", "description": "状态过滤"},
                    "order": {"type": "string", "enum": ["created", "priority", "updated"]},
                    "limit": {"type": "integer", "minimum": 1},
                }
            ),
            _list,
            "{count, tasks[]}",
        ),
        Tool("get_download", "查询单个任务的完整状态（进度、速度、分片、错误）。",
             _obj({"task_id": _TASK_ID}, ["task_id"]), _get, "下载任务对象"),
        Tool("pause_download", "暂停任务，保留已下载进度以便续传。",
             _obj({"task_id": _TASK_ID}, ["task_id"]), _pause, "下载任务对象"),
        Tool("resume_download", "恢复任务，从断点继续下载。",
             _obj({"task_id": _TASK_ID}, ["task_id"]), _resume, "下载任务对象"),
        Tool("cancel_download", "取消任务，保留 .dxpart 临时文件，可再次重试续传。",
             _obj({"task_id": _TASK_ID}, ["task_id"]), _cancel, "下载任务对象"),
        Tool("retry_download", "重试失败/取消的任务；reset=true 表示丢弃进度从零开始。",
             _obj(
                 {"task_id": _TASK_ID, "reset": {"type": "boolean", "default": False}},
                 ["task_id"],
             ),
             _retry,
             "下载任务对象",
        ),
        Tool("remove_download", "从列表移除任务；delete_file=true 时连同已下载文件一起删除。",
             _obj(
                 {"task_id": _TASK_ID, "delete_file": {"type": "boolean", "default": False}},
                 ["task_id"],
             ),
             _remove,
             "{removed, task_id}",
        ),
        Tool("set_priority", "调整任务优先级（0 最高，默认 5）。",
             _obj(
                 {"task_id": _TASK_ID, "priority": {"type": "integer", "minimum": 0, "maximum": 99}},
                 ["task_id", "priority"],
             ),
             _set_priority,
             "{task_id, priority}",
        ),
        Tool("set_speed_limit", "设置单任务限速（bytes/s，0 取消限速）。",
             _obj(
                 {"task_id": _TASK_ID, "speed_limit": {"type": "integer", "minimum": 0}},
                 ["task_id", "speed_limit"],
             ),
             _set_speed,
             "{task_id, speed_limit}",
        ),
        Tool("pause_all", "暂停所有任务（队列级）。", _obj({}), _pause_all, "{paused}"),
        Tool("resume_all", "恢复所有已暂停的任务。", _obj({}), _resume_all, "{resumed}"),
        Tool("get_stats", "全局统计：速度、队列、连接池、各状态任务数。", _obj({}), _stats, "统计对象"),
        Tool("get_history", "读取完成/失败/取消的历史记录。",
             _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 500}}), _history,
             "{count, records[]}"),
        Tool("probe_url", "只探测链接（大小、是否支持断点续传、文件名），不下载。",
             _obj({"url": {"type": "string"}, "headers": {"type": "object"}}, ["url"]),
             _probe, "资源信息"),
    ]
}


def build_manifest() -> dict[str, Any]:
    return {
        "app": APP_NAME,
        "version": __version__,
        "principles": [
            "下载引擎独立运行，LLM 不是必需依赖",
            "所有工具调用都是确定性的，可脱机执行",
            "任务状态机严格校验，非法流转会被拒绝",
        ],
        "tools": [tool.schema() for tool in TOOLS.values()],
        "openai_tools": [tool.openai_schema() for tool in TOOLS.values()],
    }


async def invoke_tool(name: str, arguments: dict[str, Any], manager: DownloadManager) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"未知工具：{name}（可用：{', '.join(sorted(TOOLS))}）")
    args = arguments or {}
    missing = [key for key in tool.parameters.get("required", []) if key not in args]
    if missing:
        raise ValueError(f"缺少必填参数：{', '.join(missing)}")
    result = await tool.handler(manager, args)
    return {"ok": True, "tool": name, "result": result}


# ----------------------------------------------------------------------
# 自然语言层（可选 LLM + 规则兜底）
# ----------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s，。；）)】\"'<>]+", re.IGNORECASE)
_ID_RE = re.compile(r"\b([0-9a-f]{8,32})\b")
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(b|kb|k|mb|m|gb|g)?\s*/?\s*s\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(\d{1,2})")

_SPEED_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
}

#: 意图 -> (关键词列表, 命中后的动作)
_INTENTS: list[tuple[str, tuple[str, ...]]] = [
    ("pause_all", ("全部暂停", "所有暂停", "都暂停", "全部停止")),
    ("resume_all", ("全部继续", "全部恢复", "都继续", "全部开始")),
    ("pause", ("暂停", "停一下", "先停", "pause")),
    ("resume", ("继续", "恢复", "接着下", "接着", "resume")),
    ("cancel", ("取消", "中止", "stop", "cancel")),
    ("retry", ("重试", "再试", "重新下", "retry")),
    ("remove", ("删除", "移除", "清掉", "不要了", "remove")),
    ("probe", ("探测", "看看大小", "能不能下", "probe")),
    ("history", ("历史", "记录", "history")),
    ("stats", ("统计", "总速度", "整体情况", "stats")),
    ("list", ("列表", "有哪些", "状态", "进度", "情况", "list")),
    ("add", ("下载", "下一下", "拉取", "抓取", "下个", "帮我下", "download", "抓下来")),
]


def parse_speed(text: str) -> int | None:
    match = _SPEED_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    return int(value * _SPEED_UNITS.get(unit, 1))


def detect_intent(text: str) -> str | None:
    lowered = text.lower()
    for intent, keywords in _INTENTS:
        for keyword in keywords:
            if keyword in lowered:
                return intent
    return None


def resolve_task_id(text: str, manager: DownloadManager) -> str | None:
    """从文本里找到目标任务：先精确 ID，再按文件名模糊匹配。"""
    for candidate in _ID_RE.findall(text):
        if manager.get(candidate) is not None:
            return candidate
        for task in manager.list():
            if task.id.startswith(candidate):
                return task.id
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for task in manager.list():
        stem = task.filename.lower()
        stem_no_ext = stem.rsplit(".", 1)[0]
        for key in {stem, stem_no_ext}:
            if key and key in lowered:
                score = len(key)
                if best is None or score > best[0]:
                    best = (score, task.id)
    return best[1] if best else None


def plan_with_rules(text: str, manager: DownloadManager) -> list[dict[str, Any]]:
    """纯规则规划：无需任何模型，离线可用。"""
    calls: list[dict[str, Any]] = []
    intent = detect_intent(text)
    urls = _URL_RE.findall(text)
    task_id = resolve_task_id(text, manager)

    if intent in ("pause_all", "resume_all"):
        return [{"name": intent, "arguments": {}}]

    if urls and intent in (None, "add", "probe"):
        name = "probe_url" if intent == "probe" else "add_download"
        for url in urls:
            arguments: dict[str, Any] = {"url": url}
            if name == "add_download":
                speed = parse_speed(text)
                if speed:
                    arguments["speed_limit"] = speed
                priority = re.search(r"优先级\s*(\d{1,2})", text)
                if priority:
                    arguments["priority"] = int(priority.group(1))
                conns = re.search(r"(\d{1,2})\s*(?:个)?(?:连接|线程|分片)", text)
                if conns:
                    arguments["connections"] = int(conns.group(1))
            calls.append({"name": name, "arguments": arguments})
        return calls

    if intent in ("list", "stats", "history", None):
        if intent == "history":
            return [{"name": "get_history", "arguments": {"limit": 20}}]
        if intent == "stats":
            return [{"name": "get_stats", "arguments": {}}]
        if intent is None:
            # 什么都没听出来：能对上一个具体任务就查询它，否则明确表示没理解
            return [{"name": "get_download", "arguments": {"task_id": task_id}}] if task_id else []
        status = None
        for candidate in ("downloading", "paused", "failed", "completed", "queued", "canceled"):
            if candidate in text.lower():
                status = candidate
                break
        arguments = {"order": "created"}
        if status:
            arguments["status"] = status
        return [{"name": "list_downloads", "arguments": arguments}]

    if intent in ("pause", "resume", "cancel", "retry", "remove"):
        if task_id is None:
            return []
        arguments = {"task_id": task_id}
        if intent == "retry":
            arguments["reset"] = bool(re.search(r"(重新|从头|从零|删了重下)", text))
        if intent == "remove":
            arguments["delete_file"] = bool(re.search(r"(删文件|把文件也删|连文件)", text))
        return [{"name": f"{intent}_download", "arguments": arguments}]

    return calls


# ---- 可选 LLM 规划 ----------------------------------------------------
def _llm_settings() -> tuple[str, str, str] | None:
    key = os.environ.get("DOWNLOADX_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    base = os.environ.get("DOWNLOADX_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DOWNLOADX_LLM_MODEL", "deepseek-chat")
    return base, model, key


async def plan_with_llm(text: str, manager: DownloadManager) -> list[dict[str, Any]] | None:
    """用 LLM 把自然语言翻成工具调用；任何失败都返回 None 让上层退化到规则。"""
    settings = _llm_settings()
    if settings is None:
        return None
    base, model, key = settings
    try:
        import httpx

        context = [
            {"id": t.id, "filename": t.filename, "status": str(t.status)}
            for t in manager.list()
        ][:30]
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 DownloadX 的指令解析器。把用户的话翻译成一次或多次工具调用。"
                        "只输出 JSON，形如 {\"tool_calls\":[{\"name\":\"...\",\"arguments\":{...}}]}。"
                        f"当前任务列表：{json.dumps(context, ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": text},
            ],
            "tools": [tool.openai_schema() for tool in TOOLS.values()],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        message = data["choices"][0]["message"]
        raw_calls = message.get("tool_calls") or []
        calls: list[dict[str, Any]] = []
        if raw_calls:
            for call in raw_calls:
                function = call.get("function", {})
                arguments = function.get("arguments") or "{}"
                calls.append(
                    {
                        "name": function.get("name"),
                        "arguments": json.loads(arguments) if isinstance(arguments, str) else arguments,
                    }
                )
        else:
            content = (message.get("content") or "").strip()
            if content:
                parsed = json.loads(content)
                calls = parsed.get("tool_calls", [])
        return [c for c in calls if c.get("name") in TOOLS] or None
    except Exception as exc:
        log.info("LLM 规划不可用，退回规则解析：%s", exc)
        return None


def _summarize(calls: list[dict[str, Any]], results: list[dict[str, Any]]) -> str:
    if not calls:
        return "没听懂要做什么。可以说：「下载 https://…」「暂停 xxx」「看看现在的情况」。"
    parts: list[str] = []
    for call, result in zip(calls, results):
        name = call["name"]
        payload = result.get("result") if isinstance(result, dict) else None
        if not isinstance(payload, dict):
            parts.append(f"{name} 已执行")
            continue
        if name == "add_download":
            parts.append(f"已加入下载：{payload.get('filename')}（ID {payload.get('id')}）")
        elif name == "list_downloads":
            parts.append(f"共有 {payload.get('count', 0)} 个任务")
        elif name == "get_stats":
            speed = payload.get("download_speed", 0)
            parts.append(
                f"当前速度 {speed / 1024:.0f} KB/s，下载中 {payload.get('active_tasks', 0)} 个，"
                f"排队 {payload.get('queued_tasks', 0)} 个，暂停 {payload.get('paused_tasks', 0)} 个"
            )
        elif name in ("pause_download", "resume_download", "cancel_download", "retry_download"):
            parts.append(f"{payload.get('filename')} 现在是 {payload.get('status')}")
        elif name == "remove_download":
            parts.append(f"已移除任务 {payload.get('task_id')}")
        elif name == "get_history":
            parts.append(f"历史记录 {payload.get('count', 0)} 条")
        elif name == "probe_url":
            size = payload.get("size")
            size_text = f"{size / 1024 / 1024:.1f} MB" if size else "未知大小"
            parts.append(
                f"{payload.get('filename')}：{size_text}，"
                f"{'支持' if payload.get('resumable') else '不支持'}断点续传"
            )
        else:
            parts.append(f"{name} 已执行")
    return "；".join(parts)


async def handle_natural_language(
    text: str,
    manager: DownloadManager,
    *,
    execute: bool = True,
    planner: str = "auto",
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("指令为空")

    calls: list[dict[str, Any]] | None = None
    used = "rules"
    if planner in ("auto", "llm"):
        calls = await plan_with_llm(text, manager)
        if calls:
            used = "llm"
    if not calls:
        calls = plan_with_rules(text, manager)
        used = "rules"

    results: list[dict[str, Any]] = []
    if execute:
        for call in calls:
            try:
                results.append(await invoke_tool(call["name"], call.get("arguments") or {}, manager))
            except (TaskNotFound, InvalidOperation, ValueError) as exc:
                results.append({"ok": False, "tool": call["name"], "error": str(exc)})
    else:
        results = [
            {"ok": True, "tool": call["name"], "result": {"planned": True, "arguments": call.get("arguments")}}
            for call in calls
        ]

    return {
        "input": text,
        "planner": used,
        "executed": execute,
        "tool_calls": [dict(call, ok=result.get("ok", False)) for call, result in zip(calls, results)],
        "results": results,
        "reply": _summarize(calls, results) if execute else f"计划执行 {len(calls)} 个操作",
    }


# ----------------------------------------------------------------------
# FastAPI 路由
# ----------------------------------------------------------------------
class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class NaturalLanguageRequest(BaseModel):
    text: str
    execute: bool = True
    planner: str = Field("auto", pattern="^(auto|rules|llm)$")


def build_agent_router(manager: DownloadManager) -> APIRouter:
    router = APIRouter(prefix="/agent", tags=["agent"])

    @router.get("/manifest")
    async def manifest() -> dict[str, Any]:
        return build_manifest()

    @router.get("/openai-tools")
    async def openai_tools() -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in TOOLS.values()]

    @router.post("/tool")
    async def call_tool(payload: ToolCallRequest) -> dict[str, Any]:
        try:
            return await invoke_tool(payload.name, payload.arguments, manager)
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail=f"任务不存在：{exc.args[0]}") from exc
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/nl")
    async def natural_language(payload: NaturalLanguageRequest) -> dict[str, Any]:
        try:
            return await handle_natural_language(
                payload.text, manager, execute=payload.execute, planner=payload.planner
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
