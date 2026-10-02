"""project.lifecycle 单项目分支（cron 准入闸门热路径）的直接单测。

issue #4885：该分支的读盘挪入 ``asyncio.to_thread`` 并合并重复读（state 只读
一次、经 ``projection(value=)`` 复用）。这里钉住分支的 payload 契约——
``scheduler.project_execution_allowed`` 据此处的 ``exists`` / ``hidden`` /
``execution_blocked`` 判定项目准入，防止后续重构悄悄改变闸门语义。
"""

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def _redirect_agent_root(monkeypatch, tmp_path):
    # lifecycle（…/agent/lifecycle/resources）与 project_store（…/agent/projects.json）
    # 的路径最终都解析到 utils.get_user_workspace_dir() 之下，一处补丁整体重定向
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )


def _write_projects(agent_root, records):
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "projects.json").write_text(
        json.dumps({"projects": records}, ensure_ascii=False), encoding="utf-8"
    )


def _write_project_state(agent_root, project_id, state):
    resources = agent_root / "lifecycle" / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    (resources / f"project_{project_id}.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


async def _lifecycle_payload(server, project_id):
    request = AgentRequest(
        request_id=f"req-{project_id}",
        channel_id="__cron__",
        req_method=ReqMethod.PROJECT_LIFECYCLE,
        params={"project_id": project_id},
    )
    ws = _FakeWebSocket()
    handled = await server._handle_lifecycle_request(ws, request, asyncio.Lock())
    assert handled is True
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["ok"] is True
    return frame["payload"]


@pytest.mark.asyncio
async def test_project_lifecycle_single_project_contract(tmp_path, monkeypatch):
    _redirect_agent_root(monkeypatch, tmp_path)
    agent_root = tmp_path / "agent"
    _write_projects(
        agent_root,
        [
            {
                "project_id": "proj_visible",
                "name": "visible",
                "project_dir": "d1",
                "work_mode": "work",
            },
            {
                "project_id": "proj_hidden",
                "name": "hidden",
                "project_dir": "d2",
                "work_mode": "work",
                "hidden": True,
            },
        ],
    )
    _write_project_state(
        agent_root,
        "proj_visible",
        {
            "blocked": True,
            "operation": {
                "operation_id": "op_archive_1",
                "resource_type": "project",
                "resource_id": "proj_visible",
                "kind": "archive",
                "status": "running",
                "phase": "block",
            },
        },
    )

    # __new__ 绕过 __init__；预置 _archive_service 跳过 SessionArchiveService 构建
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._archive_service = object()
    monkeypatch.setattr(
        agent_ws_server,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "payload": resp.payload,
            "ok": resp.ok,
        },
    )

    # 可见且带运行中 lifecycle operation 的项目：闸门三项判定 + projection 透传
    payload = await _lifecycle_payload(server, "proj_visible")
    assert payload["exists"] is True
    assert payload["hidden"] is False
    assert payload["execution_blocked"] is True  # state.blocked 经 projection 传导
    assert payload["operation"]["operation_id"] == "op_archive_1"
    assert payload["lifecycle_operation"]["operation_id"] == "op_archive_1"

    # 隐藏项目：scheduler 闸门据此拒绝（exists=True + hidden=True）
    payload = await _lifecycle_payload(server, "proj_hidden")
    assert payload["exists"] is True
    assert payload["hidden"] is True
    assert payload["execution_blocked"] is False

    # 不存在的项目：exists=False；无状态文件 → operation 为 None、不阻塞
    payload = await _lifecycle_payload(server, "proj_missing")
    assert payload["exists"] is False
    assert payload["hidden"] is False
    assert payload["operation"] is None
    assert payload["execution_blocked"] is False
