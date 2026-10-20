"""本地专家目录列表不依赖在线包仓库。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import jiuwenswarm.server.agent_ws_server as server_module
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.expert import expert_store
from jiuwenswarm.server.runtime.expert.expert_service import (
    ExpertOpResult,
    ExpertService,
)


def _make_local_agent(root: Path, name: str) -> None:
    package = root / name
    (package / "persona").mkdir(parents=True)
    (package / "persona" / "AGENT.md").write_text("# 专家", encoding="utf-8")
    (package / "manifest.json").write_text(json.dumps({
        "packageType": "agent_template",
        "agentCard": {"id": name, "name": "本地专家", "description": "离线可用"},
        "persona": {"dir": "persona"},
    }, ensure_ascii=False), encoding="utf-8")


def _service() -> ExpertService:
    return ExpertService(agent_manager=object(), adapter_resolver=lambda agent: agent)


@pytest.mark.asyncio
async def test_ws_forwards_local_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    server = server_module.AgentWebSocketServer()
    list_experts = AsyncMock(return_value=ExpertOpResult(ok=True, payload={"experts": []}))
    monkeypatch.setattr(server._expert_service, "list_experts", list_experts)
    monkeypatch.setattr(
        server_module,
        "encode_agent_response_for_wire",
        lambda response, response_id: {"ok": response.ok, "response_id": response_id},
    )
    request = AgentRequest(
        request_id="req-local",
        channel_id="desktop",
        req_method=ReqMethod.EXPERTS_LIST,
        params={"source": "local"},
    )

    await server._handle_experts_list(SimpleNamespace(send=AsyncMock()), request, asyncio.Lock())
    list_experts.assert_awaited_once_with(source="local")


@pytest.mark.asyncio
async def test_local_scope_does_not_contact_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "experts"
    _make_local_agent(root, "local-agent")
    monkeypatch.delenv(expert_store.LOCAL_DIRS_ENV, raising=False)
    monkeypatch.setattr(expert_store, "get_agent_experts_dir", lambda: root)

    def _unexpected_repository():
        raise AssertionError("本地列表不应访问仓库来源")

    monkeypatch.setattr(expert_store, "get_expert_source", _unexpected_repository)
    result = await _service().list_experts(source="local")

    assert result.ok is True
    assert [
        (item["id"], item["type"], item["source"]) for item in result.payload["experts"]
    ] == [("local-agent", "agent", "local")]


@pytest.mark.asyncio
async def test_local_scope_respects_explicit_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(expert_store.LOCAL_DIRS_ENV, "0")
    result = await _service().list_experts(source="local")
    assert result.ok is False
    assert result.payload["code"] == "LOCAL_SOURCE_DISABLED"


@pytest.mark.asyncio
async def test_local_team_keeps_type_and_tags_when_invalid(tmp_path: Path) -> None:
    package = tmp_path / "local-team"
    package.mkdir()
    (package / "manifest.json").write_text(json.dumps({
        "package_type": "agent_group",
        "name": "local-team",
        "metadata": {"displayName": "本地专家团", "tags": ["本地"]},
    }, ensure_ascii=False), encoding="utf-8")
    (summary,) = await expert_store.LocalDirExpertPackageSource(
        experts_dir=tmp_path
    ).list()
    assert summary.type == "team"
    assert summary.name == "本地专家团"
    assert summary.tags == ["本地"]
    assert summary.available is False
    assert summary.unavailable_reason
