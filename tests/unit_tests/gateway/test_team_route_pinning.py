# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise Team routing inside ``RuntimeRoutedAgentClient``.

Covers: binding capture after ``route()`` for team-mode sessions, fast-path
pinning on subsequent requests (no ``route()``), owner tenant-id override for
other members, ``team.runtime_ready`` back-fill, and fallback to ``route()``
when the pinned pod fails.
"""

from __future__ import annotations

import hashlib
import types

import httpx
import pytest

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.request_identity import apply_routing_metadata
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod, ensure_team_control_req_methods
from jiuwenswarm.extensions.runtime_management_extension.invoke_ids import (
    apply_invoke_ids_to_envelope,
)
from jiuwenswarm.extensions.runtime_management_extension.runtime_routed_client import (
    RuntimeRoutedAgentClient,
)
from jiuwenswarm.extensions.runtime_management_extension.session_route_client import RouteResult
from gateway.enterprise.team.route import TeamRouteResolver


@pytest.fixture(autouse=True)
def _register_team_control_methods():
    ensure_team_control_req_methods()
from jiuwenswarm.enterprise.team.route_binding_store import InMemoryTeamRouteBindingStore


@pytest.fixture(autouse=True)
def agent_resources(monkeypatch):
    from unittest.mock import AsyncMock

    repo = types.SimpleNamespace(get=AsyncMock(return_value={"enabled": True, "match_expr": []}))
    monkeypatch.setattr(
        "gateway.config.enterprise.access.get_enterprise_record_repository",
        lambda _: repo,
    )
    return repo


def _env(*, user: str, session: str = "sess-team", mode: str = "team", method=ReqMethod.CHAT_SEND,
         params: dict | None = None, stream: bool = True):
    p = {"query": "hi", "mode": mode}
    if params:
        p.update(params)
    return e2a_from_agent_fields(
        request_id=f"req-{user}-{len(session)}",
        channel_id="web",
        session_id=session,
        req_method=method,
        params=p,
        is_stream=stream,
        user_id=user,
        metadata=apply_routing_metadata({}, {"user_id": user, "group_id": "grp", "bot_id": "bot"}),
    )


class _Route:
    def __init__(self) -> None:
        self.routes: list[dict] = []
        self.touches: list[dict] = []
        self.pod = ("http://10.0.0.1:8086/sse", "pod-1")

    async def route(self, **kwargs):
        self.routes.append(kwargs)
        return RouteResult(pod_sse_url=self.pod[0], pod_id=self.pod[1], request_id=kwargs["request_id"])

    async def touch(self, **kwargs):
        self.touches.append(kwargs)
        return True

    async def rebind(self, **kwargs):
        return "rebound"

    async def aclose(self):
        return None


class _Http:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.fail_first = False
        self.ready_payload: dict | None = None

    async def send_request(self, envelope, *, base_url=None):
        self.calls.append(("unary", base_url, envelope))
        if self.fail_first:
            self.fail_first = False
            raise httpx.ConnectError("pod gone")
        return AgentResponse(request_id=str(envelope.request_id), channel_id="web", ok=True, payload={})

    async def send_request_stream(self, envelope, *, base_url=None):
        self.calls.append(("stream", base_url, envelope))
        if self.fail_first:
            self.fail_first = False
            raise httpx.ConnectError("pod gone")
        if self.ready_payload is not None:
            yield AgentResponseChunk(
                request_id=str(envelope.request_id), channel_id="web",
                payload=dict(self.ready_payload), is_complete=False,
            )
        yield AgentResponseChunk(
            request_id=str(envelope.request_id), channel_id="web",
            payload={"content": "hi"}, is_complete=True,
        )

    async def disconnect(self):
        return None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


@pytest.fixture
def harness():
    store = InMemoryTeamRouteBindingStore()
    resolver = TeamRouteResolver(store)
    route = _Route()
    http = _Http()
    client = RuntimeRoutedAgentClient(
        route_client=route, http_client=http, touch_interval_seconds=999,
        team_route_resolver=resolver,
    )
    return types.SimpleNamespace(store=store, resolver=resolver, route=route, http=http, client=client)


async def _drain(aiter):
    return [c async for c in aiter]


@pytest.mark.asyncio
async def test_first_team_chat_routes_and_records_binding(harness):
    await harness.client.connect("")
    chunks = await _drain(harness.client.send_request_stream(_env(user="owner")))
    assert chunks and len(harness.route.routes) == 1

    binding = harness.store.get_by_session("sess-team")
    assert binding is not None
    assert binding.endpoint == "http://10.0.0.1:8086"
    assert binding.agent_service_id == "pod-1"
    assert binding.owner_triple() == ("grp", "bot", "owner")
    assert binding.team_id == ""


@pytest.mark.asyncio
async def test_runtime_ready_backfills_team_id_and_owner(harness):
    await harness.client.connect("")
    harness.http.ready_payload = {
        "event_type": "team.runtime_ready",
        "team_id": "T-123",
        "owner": {"user_id": "owner", "group_id": "grp", "bot_id": "bot"},
    }
    await _drain(harness.client.send_request_stream(_env(user="owner")))
    binding = harness.store.get_by_session("sess-team")
    assert binding.team_id == "T-123"
    assert harness.store.get_by_team_id("T-123").team_session_id == "sess-team"


@pytest.mark.asyncio
async def test_second_member_is_pinned_and_uses_owner_tenant_ids(harness):
    await harness.client.connect("")
    await _drain(harness.client.send_request_stream(_env(user="owner")))
    assert len(harness.route.routes) == 1

    member_env = _env(user="member")
    await _drain(harness.client.send_request_stream(member_env))

    # no second route(): pinned to the bound pod
    assert len(harness.route.routes) == 1
    kind, base_url, sent = harness.http.calls[-1]
    assert kind == "stream" and base_url == "http://10.0.0.1:8086"
    # tenant ids derive from the OWNER triple, caller user_id preserved
    assert sent.service_id == _md5("grpbot")
    assert sent.agent_id == _md5("grpbotowner")
    assert sent.workspace_key == _md5("grpbotowner")
    assert sent.user_id == "member"
    # touch keeps the team session alive with the original route id
    assert harness.route.touches[-1]["session_id"] == "sess-team"


@pytest.mark.asyncio
async def test_non_team_request_is_not_pinned(harness):
    await harness.client.connect("")
    await _drain(harness.client.send_request_stream(_env(user="owner")))
    await _drain(harness.client.send_request_stream(_env(user="member", mode="agent", session="sess-team")))
    # agent-mode request on the same session routes normally, no owner override
    assert len(harness.route.routes) == 2
    _kind, _url, sent = harness.http.calls[-1]
    assert sent.agent_id == _md5("grpbotmember")


@pytest.mark.asyncio
async def test_team_quota_get_is_not_pinned_to_session_binding(harness):
    await harness.client.connect("")
    await _drain(harness.client.send_request_stream(_env(user="owner")))
    binding = harness.store.get_by_session("sess-team")
    assert binding is not None
    version = binding.route_version

    quota = _env(
        user="owner", session="sess-team", mode="team",
        method=ReqMethod.TEAM_QUOTA_GET, params={}, stream=False,
    )
    await harness.client.send_request(quota)
    assert len(harness.route.routes) == 2
    assert harness.store.get_by_session("sess-team").route_version == version

    team_get = _env(
        user="owner", session="sess-team", mode="",
        method=ReqMethod.TEAM_GET, params={}, stream=False,
    )
    await harness.client.send_request(team_get)
    assert len(harness.route.routes) == 2
    kind, base_url, _sent = harness.http.calls[-1]
    assert kind == "unary" and base_url == "http://10.0.0.1:8086"


@pytest.mark.asyncio
async def test_team_control_method_by_team_id_is_pinned(harness):
    await harness.client.connect("")
    harness.http.ready_payload = {"event_type": "team.runtime_ready", "team_id": "T-9"}
    await _drain(harness.client.send_request_stream(_env(user="owner")))

    env = _env(user="member", session="other-session", mode="", method=ReqMethod.TEAM_MEMBER_JOIN,
               params={"team_id": "T-9"}, stream=False)
    await harness.client.send_request(env)
    assert len(harness.route.routes) == 1
    kind, base_url, sent = harness.http.calls[-1]
    assert kind == "unary" and base_url == "http://10.0.0.1:8086"
    assert sent.agent_id == _md5("grpbotowner")


@pytest.mark.asyncio
async def test_pinned_pod_failure_falls_back_to_route_and_rebinds(harness):
    await harness.client.connect("")
    await _drain(harness.client.send_request_stream(_env(user="owner")))
    v1 = harness.store.get_by_session("sess-team").route_version

    harness.route.pod = ("http://10.0.0.2:8086/sse", "pod-2")
    harness.http.fail_first = True
    await _drain(harness.client.send_request_stream(_env(user="member")))

    assert len(harness.route.routes) == 2
    binding = harness.store.get_by_session("sess-team")
    assert binding.endpoint == "http://10.0.0.2:8086"
    assert binding.agent_service_id == "pod-2"
    assert binding.route_version == v1 + 1
    assert binding.status == "active"
    assert binding.owner_user_id == "owner"  # owner survives re-route by a member


@pytest.mark.asyncio
async def test_resolver_disabled_keeps_legacy_behaviour():
    route, http = _Route(), _Http()
    client = RuntimeRoutedAgentClient(route_client=route, http_client=http, touch_interval_seconds=999)
    client._team_routes = None
    await client.connect("")
    await _drain(client.send_request_stream(_env(user="owner")))
    await _drain(client.send_request_stream(_env(user="member")))
    assert len(route.routes) == 2
    assert http.calls[-1][2].agent_id == _md5("grpbotmember")


def test_pinned_pod_http_404_is_not_a_pod_failure():
    """404 不换机。只有 5xx/429 才抛给编排丢掉绑定。"""
    from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import (
        _raise_for_pod_http_error,
    )

    missing = httpx.Response(
        404,
        json={"detail": "Not Found"},
        request=httpx.Request("DELETE", "http://10.0.0.9:8766/api/v1/teams/demo"),
    )
    _raise_for_pod_http_error(missing, base_url="http://10.0.0.9:8766")
    _raise_for_pod_http_error(missing, base_url=None)

    unavailable = httpx.Response(
        503,
        json={"ok": False},
        request=httpx.Request("GET", "http://10.0.0.9:8766/api/v1/health"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        _raise_for_pod_http_error(unavailable, base_url="http://10.0.0.9:8766")


@pytest.mark.asyncio
async def test_shared_session_does_not_pin_or_record_team_binding(harness):
    await harness.client.connect("")
    harness.store.upsert(
        team_session_id="shared_user_owner",
        endpoint="http://10.9.9.9:8766",
        agent_service_id="old-pod",
        route_request_id="old",
    )
    env = _env(
        user="owner",
        session="shared_user_owner",
        mode="team",
        method=ReqMethod.TEAM_DELETE,
        params={"team_name": "demo"},
        stream=False,
    )
    await harness.client.send_request(env)

    assert len(harness.route.routes) == 1
    kind, base_url, _sent = harness.http.calls[-1]
    assert kind == "unary" and base_url == "http://10.0.0.1:8086"
    stale = harness.store.get_by_session("shared_user_owner")
    assert stale.endpoint == "http://10.9.9.9:8766"
    assert stale.agent_service_id == "old-pod"


@pytest.mark.asyncio
async def test_team_delete_with_team_id_uses_leader_not_shared_session(harness):
    await harness.client.connect("")
    harness.http.ready_payload = {"event_type": "team.runtime_ready", "team_id": "T-1"}
    await _drain(harness.client.send_request_stream(_env(user="owner")))
    assert harness.store.get_by_team_id("T-1") is not None

    env = _env(
        user="owner",
        session="shared_user_owner",
        mode="team",
        method=ReqMethod.TEAM_DELETE,
        params={"team_name": "demo", "team_id": "T-1"},
        stream=False,
    )
    await harness.client.send_request(env)

    assert len(harness.route.routes) == 1
    kind, base_url, sent = harness.http.calls[-1]
    assert kind == "unary" and base_url == "http://10.0.0.1:8086"
    assert sent.agent_id == _md5("grpbotowner")
    assert harness.store.get_by_session("shared_user_owner") is None


@pytest.mark.asyncio
async def test_team_delete_without_leader_does_not_record_shared_session(harness):
    await harness.client.connect("")
    env = _env(
        user="owner",
        session="shared_user_owner",
        mode="team",
        method=ReqMethod.TEAM_DELETE,
        params={"team_name": "demo", "team_id": "missing"},
        stream=False,
    )
    await harness.client.send_request(env)

    assert len(harness.route.routes) == 1
    assert harness.store.get_by_session("shared_user_owner") is None
    assert harness.store.get_by_team_id("missing") is None


def test_apply_invoke_ids_tenant_override_resets_stale_ids():
    env = _env(user="member", stream=False)
    env.service_id = "stale"
    env.agent_id = "stale"
    env.workspace_key = "stale"
    apply_invoke_ids_to_envelope(env, tenant_triple=("grp", "bot", "owner"))
    assert env.agent_id == _md5("grpbotowner")
    assert env.user_id == "member"
