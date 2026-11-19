# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

from jiuwenswarm.common import projectless_workspace, utils
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from e2a.models import E2AEnvelope
from e2a.wire_codec import parse_agent_server_wire_unary
from e2a.message import ReqMethod
from jiuwenswarm.server.front.protocol import parse_raw_message
from jiuwenswarm.server.front.router import MethodRouter, is_control_method
from jiuwenswarm.server.http_invoke import _request_tenant_scope
from jiuwenswarm.server.lifecycle import Readiness


# --- from test_enterprise_projectless_workspace.py ---

def test_enterprise_task_roots_are_tenant_scoped_and_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.delenv("JIUWENSWARM_TASK_REGISTRY_DIR", raising=False)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    with TenantContext.scoped("group::bot::admin"):
        admin = projectless_workspace.get_projectless_task_workspace("same-session")
        assert admin.root_dir.is_relative_to(utils.get_default_project_workspace_dir())
    with TenantContext.scoped("group::bot::user1"):
        user = projectless_workspace.get_projectless_task_workspace("same-session")
        assert user.root_dir.is_relative_to(utils.get_default_project_workspace_dir())
        assert user == projectless_workspace.get_projectless_task_workspace("same-session")
    with TenantContext.scoped("group::bot::admin"):
        assert admin == projectless_workspace.get_projectless_task_workspace("same-session")
    assert admin.root_dir != user.root_dir

def test_personal_documents_and_explicit_override_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.setattr(projectless_workspace, "_get_documents_dir", lambda: tmp_path)
    assert projectless_workspace.get_projectless_tasks_dir() == tmp_path / "JiuwenSwarm"
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "explicit"))
    assert projectless_workspace.get_projectless_tasks_dir() == tmp_path / "explicit"

# --- from test_hub_catalog_control.py ---

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "hub-error", "unexpected-error"])
async def test_ws_hub_catalog_reaches_query_without_admission_or_tenant(monkeypatch, outcome):
    envelope = E2AEnvelope(
        request_id="catalog-ws", channel="gateway", method=ReqMethod.HUB_EXPERT_PACKAGES_CATALOG.value, params={},
    )
    request = parse_raw_message(json.dumps(envelope.to_dict()))
    assert is_control_method(request)
    payload = {"items": [{"kind": "agent_template", "id": "asset-a"}], "error": ""}
    if outcome == "hub-error":
        payload = {"items": [], "error": "Hub down"}
    query = AsyncMock(return_value=payload)
    if outcome == "unexpected-error":
        query.side_effect = RuntimeError("unexpected query error")
    monkeypatch.setattr("jiuwenswarm.server.control.hub_catalog_service.list_hub_equipment_catalog", query)
    tenant = Mock(side_effect=AssertionError("must not bind user tenant"))
    monkeypatch.setattr("jiuwenswarm.enterprise.tenant.tenant_identity.bind_control_tenant", tenant)
    admission = SimpleNamespace(dispatch=AsyncMock())
    send = AsyncMock(return_value=True)
    monkeypatch.setattr("jiuwenswarm.server.front.router.send_wire_payload", send)
    router = MethodRouter(Readiness(), admission)

    await router.dispatch(object(), request, asyncio.Lock())

    query.assert_awaited_once_with()
    admission.dispatch.assert_not_awaited()
    tenant.assert_not_called()
    send.assert_awaited_once()
    response = parse_agent_server_wire_unary(send.call_args.args[1])
    assert response.request_id == "catalog-ws"
    if outcome == "unexpected-error":
        assert response.ok is False
        assert response.payload["code"] == "INTERNAL_ERROR"
    else:
        assert response.ok is True
        assert response.payload == payload

@pytest.mark.asyncio
async def test_missing_http_method_keeps_existing_scope_behavior():
    lookup = Mock(return_value=None)
    ctx = SimpleNamespace(
        request=SimpleNamespace(req_method=None, user_id="", metadata={}),
        services=SimpleNamespace(execution_runtime=lookup),
    )
    async with _request_tenant_scope(ctx):
        pass
    lookup.assert_called_once_with()

