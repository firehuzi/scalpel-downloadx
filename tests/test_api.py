"""REST API 与 Agent API 的端到端测试（FastAPI TestClient）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from downloadx import DownloadManager
from downloadx.api.local_api import create_app
from tests.support.server import make_payload


@pytest.fixture()
def client(config):
    manager = DownloadManager(config)
    app = create_app(manager)
    with TestClient(app) as test_client:
        yield test_client


def wait_task(client: TestClient, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/tasks/{task_id}").json()
        if payload["status"] in ("completed", "failed", "paused", "canceled"):
            return payload
        time.sleep(0.05)
    raise TimeoutError(f"任务 {task_id} 未在 {timeout}s 内结束：{payload['status']}")


# ----------------------------------------------------------------------
# 基础接口
# ----------------------------------------------------------------------
def test_health_and_config(client):
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["app"] == "DownloadX"

    config = client.get("/api/config").json()
    assert "download_dir" in config and "max_concurrent_tasks" in config


def test_add_download_and_complete(client, server):
    size = 256 * 1024
    name = "api.bin"
    resp = client.post(
        "/api/tasks",
        json={"url": server.url(f"/file/{name}?size={size}"), "connections": 4},
    )
    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] in ("queued", "downloading")

    final = wait_task(client, task["id"])
    assert final["status"] == "completed"
    assert final["percent"] == 100.0
    assert Path(final["save_path"]).read_bytes() == make_payload(name, size)

    listing = client.get("/api/tasks").json()
    assert listing["count"] == 1
    assert listing["tasks"][0]["id"] == task["id"]


def test_pause_resume_via_api(client, server):
    size = 512 * 1024
    name = "apipause.bin"
    task = client.post(
        "/api/tasks",
        json={"url": server.url(f"/file/{name}?size={size}&delay=0.02&chunk=8192")},
    ).json()

    for _ in range(200):
        current = client.get(f"/api/tasks/{task['id']}").json()
        if current["downloaded_size"] > 8 * 1024:
            break
        time.sleep(0.02)

    paused = client.post(f"/api/tasks/{task['id']}/pause").json()
    assert paused["status"] == "paused"
    assert "part_path" not in paused  # 不泄露内部路径细节

    resumed = client.post(f"/api/tasks/{task['id']}/resume").json()
    assert resumed["status"] in ("queued", "downloading")
    final = wait_task(client, task["id"])
    assert final["status"] == "completed"
    assert Path(final["save_path"]).read_bytes() == make_payload(name, size)


def test_update_priority_and_delete(client, server):
    task = client.post(
        "/api/tasks",
        json={"url": server.url("/file/prio.bin?size=4096"), "start": False},
    ).json()
    updated = client.patch(f"/api/tasks/{task['id']}", json={"priority": 1}).json()
    assert updated["priority"] == 1

    removed = client.delete(f"/api/tasks/{task['id']}").json()
    assert removed["removed"] is True
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404


def test_error_mapping(client, server):
    assert client.get("/api/tasks/deadbeef").status_code == 404
    assert client.post("/api/tasks/deadbeef/pause").status_code == 404
    assert client.post("/api/tasks", json={"url": "ftp://x/y"}).status_code == 400
    assert client.post("/api/tasks", json={"url": server.url("/missing/z")}).status_code == 201
    bad_status = client.get("/api/tasks", params={"status": "nonsense"})
    assert bad_status.status_code == 400


def test_completed_task_cannot_resume(client, server):
    task = client.post(
        "/api/tasks", json={"url": server.url("/file/done.bin?size=2048")}
    ).json()
    final = wait_task(client, task["id"])
    assert final["status"] == "completed"
    resp = client.post(f"/api/tasks/{task['id']}/resume")
    assert resp.status_code == 409


def test_probe_endpoint(client, server):
    info = client.post("/api/probe", json={"url": server.url("/file/p.bin?size=12345")}).json()
    assert info["size"] == 12345
    assert info["resumable"] is True
    assert info["filename"] == "p.bin"


def test_stats_and_history(client, server):
    task = client.post(
        "/api/tasks", json={"url": server.url("/file/h.bin?size=4096")}
    ).json()
    wait_task(client, task["id"])
    stats = client.get("/api/stats").json()
    assert stats["completed_tasks"] == 1
    assert stats["total_tasks"] == 1

    history = client.get("/api/history").json()
    assert history["count"] >= 1
    assert history["records"][0]["id"] == task["id"]
    assert history["records"][0]["status"] == "completed"


def test_global_config_endpoints(client):
    updated = client.patch(
        "/api/config", json={"max_concurrent_tasks": 5, "global_speed_limit": 1048576}
    ).json()
    assert updated["max_concurrent_tasks"] == 5
    assert updated["global_speed_limit"] == 1048576


def test_websocket_snapshot(client, server):
    with client.websocket_connect("/api/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert "tasks" in first["data"]


# ----------------------------------------------------------------------
# Agent API
# ----------------------------------------------------------------------
def test_agent_manifest(client):
    manifest = client.get("/agent/manifest").json()
    names = {tool["name"] for tool in manifest["tools"]}
    assert {"add_download", "list_downloads", "pause_download", "retry_download"} <= names
    assert len(manifest["openai_tools"]) == len(manifest["tools"])
    for tool in manifest["openai_tools"]:
        assert tool["type"] == "function"
        assert "parameters" in tool["function"]

    openai_tools = client.get("/agent/openai-tools").json()
    assert len(openai_tools) == len(manifest["tools"])


def test_agent_tool_invocation(client, server):
    size = 64 * 1024
    name = "agent.bin"
    result = client.post(
        "/agent/tool",
        json={
            "name": "add_download",
            "arguments": {"url": server.url(f"/file/{name}?size={size}"), "connections": 2},
        },
    ).json()
    assert result["ok"] is True
    task_id = result["result"]["id"]
    final = wait_task(client, task_id)
    assert final["status"] == "completed"

    listed = client.post("/agent/tool", json={"name": "list_downloads", "arguments": {}}).json()
    assert listed["result"]["count"] == 1

    stats = client.post("/agent/tool", json={"name": "get_stats", "arguments": {}}).json()
    assert stats["result"]["completed_tasks"] == 1


def test_agent_tool_errors(client):
    unknown = client.post("/agent/tool", json={"name": "nope", "arguments": {}})
    assert unknown.status_code == 400
    missing = client.post("/agent/tool", json={"name": "add_download", "arguments": {}})
    assert missing.status_code == 400
    not_found = client.post(
        "/agent/tool", json={"name": "pause_download", "arguments": {"task_id": "zz"}}
    )
    assert not_found.status_code == 404


def test_nl_rules_planner_adds_download(client, server):
    size = 32 * 1024
    name = "nl.bin"
    resp = client.post(
        "/agent/nl",
        json={
            "text": f"帮我下载 {server.url(f'/file/{name}?size={size}')}，优先级 2，用 2 个连接",
            "planner": "rules",
        },
    ).json()
    assert resp["planner"] == "rules"
    assert resp["tool_calls"][0]["name"] == "add_download"
    assert resp["tool_calls"][0]["arguments"]["priority"] == 2
    assert resp["tool_calls"][0]["arguments"]["connections"] == 2
    assert "已加入下载" in resp["reply"]

    task_id = resp["results"][0]["result"]["id"]
    final = wait_task(client, task_id)
    assert final["status"] == "completed"


def test_nl_rules_planner_list_and_stats(client, server):
    client.post("/api/tasks", json={"url": server.url("/file/x1.bin?size=4096"), "start": False})
    listed = client.post("/agent/nl", json={"text": "现在有哪些任务", "planner": "rules"}).json()
    assert listed["tool_calls"][0]["name"] == "list_downloads"
    assert listed["results"][0]["result"]["count"] == 1

    stats = client.post("/agent/nl", json={"text": "看一下统计", "planner": "rules"}).json()
    assert stats["tool_calls"][0]["name"] == "get_stats"


def test_nl_rules_planner_pause_by_filename(client, server):
    size = 512 * 1024
    name = "nlspeed.bin"
    task = client.post(
        "/api/tasks",
        json={"url": server.url(f"/file/{name}?size={size}&delay=0.02&chunk=8192")},
    ).json()
    for _ in range(200):
        current = client.get(f"/api/tasks/{task['id']}").json()
        if current["downloaded_size"] > 8 * 1024:
            break
        time.sleep(0.02)

    resp = client.post("/agent/nl", json={"text": f"暂停 {name}", "planner": "rules"}).json()
    assert resp["tool_calls"][0]["name"] == "pause_download"
    assert resp["tool_calls"][0]["arguments"]["task_id"] == task["id"]
    assert resp["tool_calls"][0]["ok"] is True
    assert "paused" in resp["reply"]


def test_nl_plan_only_does_not_execute(client, server):
    resp = client.post(
        "/agent/nl",
        json={"text": f"下载 {server.url('/file/plan.bin?size=4096')}", "planner": "rules", "execute": False},
    ).json()
    assert resp["executed"] is False
    assert client.get("/api/tasks").json()["count"] == 0


def test_nl_unknown_instruction(client):
    resp = client.post("/agent/nl", json={"text": "今天天气怎么样", "planner": "rules"}).json()
    assert resp["tool_calls"] == []
    assert "没听懂" in resp["reply"]


def test_ui_is_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "DownloadX" in resp.text


# ----------------------------------------------------------------------
# 访问令牌（Phase 1）
# ----------------------------------------------------------------------
TOKEN = "test-token-0123456789"


@pytest.fixture()
def secured_client(config):
    config.api_token = TOKEN
    manager = DownloadManager(config)
    with TestClient(create_app(manager)) as test_client:
        yield test_client


def test_open_by_default(client):
    """没设令牌时不鉴权（只靠回环地址保护）。"""
    assert client.get("/api/tasks").status_code == 200


def test_token_is_required_when_configured(secured_client):
    assert secured_client.get("/api/tasks").status_code == 401
    assert secured_client.get(
        "/api/tasks", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    ok = secured_client.get("/api/tasks", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    # 备用头（给不方便设 Authorization 的客户端）
    assert secured_client.get(
        "/api/tasks", headers={"X-DownloadX-Token": TOKEN}
    ).status_code == 200
    # 健康检查永远放行，便于探活
    assert secured_client.get("/api/health").status_code == 200


def test_preflight_allowed_and_cors_headers(secured_client):
    resp = secured_client.options(
        "/api/tasks",
        headers={
            "Origin": "chrome-extension://abcdefghijklmnop",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()


def test_agent_api_is_guarded_too(secured_client):
    assert secured_client.post("/agent/tool", json={"name": "get_stats", "arguments": {}}).status_code == 401
    ok = secured_client.post(
        "/agent/tool",
        json={"name": "get_stats", "arguments": {}},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert ok.status_code == 200


def test_websocket_requires_token(secured_client):
    with pytest.raises(WebSocketDisconnect):
        with secured_client.websocket_connect("/api/ws"):
            pass
    with secured_client.websocket_connect(f"/api/ws?token={TOKEN}") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_extension_manifest_is_valid_mv3():
    import json

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["background"]["service_worker"] == "background.js"
    assert any("127.0.0.1" in host for host in manifest["host_permissions"])
    for name in ("background.js", "popup.html", "popup.js"):
        assert (root / "extension" / name).is_file(), name


def test_batch_add_creates_independent_tasks(client, server):
    size = 8192
    urls = [server.url(f"/file/batch{i}.bin?size={size}") for i in range(3)]
    urls.append("ftp://not-supported/x")  # 混一条坏链接，不该影响其余

    resp = client.post("/api/tasks/batch", json={"urls": urls, "connections": 2})
    assert resp.status_code == 201
    data = resp.json()
    assert data["count"] == 3
    assert len(data["failed"]) == 1 and "ftp" in data["failed"][0]["url"]
    ids = [task["id"] for task in data["created"]]
    assert len(set(ids)) == 3, "每条链接必须是独立任务"
    for task_id in ids:
        assert wait_task(client, task_id)["status"] == "completed"


def test_batch_requires_urls(client):
    assert client.post("/api/tasks/batch", json={"urls": []}).status_code == 422
