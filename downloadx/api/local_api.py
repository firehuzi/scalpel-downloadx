"""本地 REST API + WebSocket 事件流 + 内置 Web UI。

服务只监听本地回环地址（默认 127.0.0.1:8720），不对外暴露。
没有鉴权体系 —— 这是 v0.1 有意的取舍：它就是一个本地工具。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Config
from ..core.download_manager import (
    DownloadManager,
    InvalidOperation,
    TaskNotFound,
)
from ..core.state_machine import TaskStatus
from ..version import APP_DISPLAY_NAME, APP_NAME, __version__

log = logging.getLogger("downloadx.api")

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------
class AddTaskRequest(BaseModel):
    url: str = Field(..., description="http/https 链接")
    save_dir: str | None = None
    filename: str | None = None
    priority: int | None = Field(None, ge=0, le=99)
    connections: int | None = Field(None, ge=1, le=32)
    speed_limit: int = Field(0, ge=0, description="bytes/s，0 表示不限速")
    headers: dict[str, str] | None = None
    sha256: str | None = None
    note: str = ""
    start: bool = True


class UpdateTaskRequest(BaseModel):
    priority: int | None = Field(None, ge=0, le=99)
    connections: int | None = Field(None, ge=1, le=32)
    speed_limit: int | None = Field(None, ge=0)


class UpdateConfigRequest(BaseModel):
    download_dir: str | None = None
    max_concurrent_tasks: int | None = Field(None, ge=1, le=32)
    global_speed_limit: int | None = Field(None, ge=0)


# ----------------------------------------------------------------------
# 错误映射：把领域异常翻译成 HTTP 语义
# ----------------------------------------------------------------------
def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, TaskNotFound):
        return HTTPException(status_code=404, detail=f"任务不存在：{exc.args[0]}")
    if isinstance(exc, InvalidOperation):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def build_router(manager: DownloadManager) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["downloadx"])
    started_at = time.time()

    # ---- 健康检查 -----------------------------------------------------
    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app": APP_NAME,
            "display_name": APP_DISPLAY_NAME,
            "version": __version__,
            "uptime": round(time.time() - started_at, 2),
            "download_dir": str(manager.config.download_dir),
            "state_dir": str(manager.config.state_dir),
        }

    # ---- 任务 ---------------------------------------------------------
    @router.get("/tasks")
    async def list_tasks(
        status: str | None = Query(None, description="按状态过滤"),
        order: str = Query("created", pattern="^(created|priority|updated)$"),
    ) -> dict[str, Any]:
        if status is not None and status not in {str(s) for s in TaskStatus}:
            raise HTTPException(status_code=400, detail=f"未知状态：{status}")
        tasks = manager.snapshot(status=status, order=order)
        return {"tasks": tasks, "count": len(tasks)}

    @router.post("/tasks", status_code=201)
    async def add_task(payload: AddTaskRequest) -> dict[str, Any]:
        try:
            task = await manager.add(
                payload.url,
                save_dir=payload.save_dir,
                filename=payload.filename,
                priority=payload.priority,
                connections=payload.connections,
                speed_limit=payload.speed_limit,
                headers=payload.headers,
                expected_sha256=payload.sha256,
                note=payload.note,
                start=payload.start,
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return manager.public(task)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        try:
            return manager.public(manager.require(task_id))
        except Exception as exc:
            raise _translate(exc) from exc

    @router.patch("/tasks/{task_id}")
    async def update_task(task_id: str, payload: UpdateTaskRequest) -> dict[str, Any]:
        try:
            manager.require(task_id)
            if payload.priority is not None:
                await manager.set_priority(task_id, payload.priority)
            if payload.connections is not None:
                await manager.set_connections(task_id, payload.connections)
            if payload.speed_limit is not None:
                await manager.set_speed_limit(task_id, payload.speed_limit)
            return manager.public(manager.require(task_id))
        except Exception as exc:
            raise _translate(exc) from exc

    @router.delete("/tasks/{task_id}")
    async def delete_task(
        task_id: str, delete_file: bool = Query(False, description="同时删除已下载文件")
    ) -> dict[str, Any]:
        try:
            removed = await manager.remove(task_id, delete_file=delete_file)
        except Exception as exc:
            raise _translate(exc) from exc
        if not removed:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        return {"removed": True, "id": task_id, "delete_file": delete_file}

    # ---- 动作 ---------------------------------------------------------
    async def _act(task_id: str, action: str) -> dict[str, Any]:
        try:
            if action == "pause":
                await manager.pause(task_id)
            elif action == "resume":
                await manager.resume(task_id)
            elif action == "cancel":
                await manager.cancel(task_id)
            elif action == "retry":
                await manager.retry(task_id)
            elif action == "retry_reset":
                await manager.retry(task_id, reset=True)
            else:
                raise InvalidOperation(f"未知动作：{action}")
            return manager.public(manager.require(task_id))
        except Exception as exc:
            raise _translate(exc) from exc

    @router.post("/tasks/{task_id}/pause")
    async def pause_task(task_id: str) -> dict[str, Any]:
        return await _act(task_id, "pause")

    @router.post("/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, Any]:
        return await _act(task_id, "resume")

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return await _act(task_id, "cancel")

    @router.post("/tasks/{task_id}/retry")
    async def retry_task(
        task_id: str, reset: bool = Query(False, description="从零重新下载")
    ) -> dict[str, Any]:
        return await _act(task_id, "retry_reset" if reset else "retry")

    # ---- 批量 ---------------------------------------------------------
    @router.post("/tasks/pause-all")
    async def pause_all() -> dict[str, Any]:
        return {"paused": await manager.pause_all()}

    @router.post("/tasks/resume-all")
    async def resume_all() -> dict[str, Any]:
        return {"resumed": await manager.resume_all()}

    # ---- 统计 / 历史 / 配置 -------------------------------------------
    @router.get("/stats")
    async def stats() -> dict[str, Any]:
        return manager.stats()

    @router.get("/history")
    async def history(
        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)
    ) -> dict[str, Any]:
        records = manager.history(limit=limit, offset=offset)
        return {"records": records, "count": len(records)}

    @router.get("/config")
    async def get_config() -> dict[str, Any]:
        return manager.config.to_dict()

    @router.patch("/config")
    async def update_config(payload: UpdateConfigRequest) -> dict[str, Any]:
        try:
            if payload.download_dir:
                await manager.set_download_dir(payload.download_dir)
            if payload.max_concurrent_tasks is not None:
                await manager.set_max_concurrent(payload.max_concurrent_tasks)
            if payload.global_speed_limit is not None:
                await manager.set_global_speed_limit(payload.global_speed_limit)
        except Exception as exc:
            raise _translate(exc) from exc
        return manager.config.to_dict()

    # ---- 工具类 -------------------------------------------------------
    @router.post("/probe")
    async def probe(payload: AddTaskRequest) -> dict[str, Any]:
        try:
            info = await manager.probe(payload.url, payload.headers)
        except Exception as exc:
            raise _translate(exc) from exc
        return info.to_dict()

    @router.get("/events")
    async def recent_events(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return {"events": [e.to_dict() for e in manager.bus.recent(limit)]}

    # ---- WebSocket 事件流 ---------------------------------------------
    @router.websocket("/ws")
    async def ws_events(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = manager.bus.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "task_id": None,
                    "data": {"tasks": manager.snapshot(), "stats": manager.stats()},
                    "ts": time.time(),
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=3.0)
                    await websocket.send_json(event.to_dict())
                except asyncio.TimeoutError:
                    # 心跳：顺便把全量统计推给前端（空闲时也要有数据）
                    await websocket.send_json(
                        {
                            "type": "stats",
                            "task_id": None,
                            "data": manager.stats(),
                            "ts": time.time(),
                        }
                    )
        except Exception as exc:  # 客户端断开是常态
            log.debug("WebSocket 结束：%s", exc)
        finally:
            manager.bus.unsubscribe(queue)
            with contextlib.suppress(Exception):
                await websocket.close()

    return router


# ----------------------------------------------------------------------
# 应用工厂
# ----------------------------------------------------------------------
def create_app(manager: DownloadManager, *, mount_ui: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        log.info("DownloadX 服务已启动：%s", manager.config.state_dir)
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title=f"{APP_DISPLAY_NAME} API",
        version=__version__,
        description="本地优先、可被 Agent 调用的下载基础设施",
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.include_router(build_router(manager))

    from .agent_api import build_agent_router

    app.include_router(build_agent_router(manager))

    if mount_ui and UI_DIR.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    else:
        @app.get("/")
        async def index() -> JSONResponse:
            return JSONResponse({"app": APP_NAME, "display_name": APP_DISPLAY_NAME,
                                 "version": __version__, "ui": "未安装"})

    return app


def app_factory() -> FastAPI:
    """给 uvicorn 用：``uvicorn downloadx.api.local_api:app_factory --factory``。"""
    config = Config.load()
    return create_app(DownloadManager(config))


def dependency_manager(request: Request) -> DownloadManager:
    return request.app.state.manager
