"""The routed request must reach session assembly before models and rails load."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


class AssemblyReached(Exception):
    """Stop after observing the real session creation boundary."""


@pytest.mark.asyncio
@pytest.mark.parametrize("with_request", [True, False])
async def test_session_bootstrap_uses_current_request_without_mutating_root(monkeypatch, with_request):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    parent._session_instance_config = {"channel_id": "web"}
    parent._load_skill_retrieval_session_profile = lambda _: None
    child = SimpleNamespace(
        restore_skill_retrieval_session=Mock(),
        configure_initial_execution=Mock(),
        cleanup=AsyncMock(),
    )
    request = AgentRequest(request_id="current", channel_id="web", session_id="bootstrap")

    async def create(config, **kwargs):
        assert config["channel_id"] == "web"
        if with_request:
            assert config["request"] is request
        else:
            assert "request" not in config
        assert parent._session_instance_config == {"channel_id": "web"}
        raise AssemblyReached()

    child.create_instance = create
    monkeypatch.setattr(parent, "_new_session_scoped_adapter", lambda _: child)
    monkeypatch.setattr(interface_deep, "get_config", lambda: {})
    monkeypatch.setattr(interface_deep, "load_persisted_external_harness_binding", AsyncMock(return_value=None))
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata", lambda _: {})
    with pytest.raises(AssemblyReached):
        await parent._get_or_create_session_adapter(
            "bootstrap", execution_request=request if with_request else None,
        )


@pytest.mark.asyncio
async def test_direct_dispatch_forwards_current_routing_to_session_assembly(monkeypatch):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    child = object()
    request = AgentRequest(request_id="current", channel_id="web", session_id="bootstrap")
    parent._get_or_create_session_adapter = AsyncMock(return_value=child)
    monkeypatch.setattr(parent, "_coordinates_smart_permission_lifecycle", lambda *_: False)

    assert await parent._get_session_adapter_for_request(request, reserve_activity=True) is child
    assert parent._get_or_create_session_adapter.await_args.kwargs["execution_request"] is request


@pytest.mark.asyncio
async def test_mcp_preparation_forwards_request_before_loading_session_policy(monkeypatch):
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    request = AgentRequest(request_id="current", channel_id="web", session_id="bootstrap")
    parent._get_or_create_session_adapter = AsyncMock(side_effect=AssemblyReached)

    with pytest.raises(AssemblyReached):
        await parent.reconcile_session_mcp("bootstrap", [], execution_request=request)
    assert parent._get_or_create_session_adapter.await_args.kwargs["execution_request"] is request
