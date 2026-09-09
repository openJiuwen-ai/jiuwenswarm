# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the OpenJiuwen free-search provenance adapter."""

from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved ``.invalid`` names and no test
# performs external network I/O.

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)
from openjiuwen.harness.tools import WebFreeSearchTool

from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
)
from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    SessionTrustedSearchUrls,
    bind_trusted_search_producer,
    clear_trusted_search_producer,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    RootDecisionContext,
    RootIntentTurn,
    RootIntentTurnKind,
    bind_root_decision_context,
    reset_root_decision_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionQueueRail,
    bind_root_permission_request,
    reset_root_permission_request,
)
from jiuwenswarm.server.runtime.agent_adapter.trusted_web_search import (
    TrustedWebFreeSearchTool,
)
from tests.unit_tests.agentserver.permissions.auto_permission_test_support import (
    AutoPermissionInterruptRail,
    AutoReviewer,
    FakeBaseRail,
    PolicyEvaluation,
    ReviewerOutcome,
    StaticPolicyEvaluator,
    StaticReviewerClient,
)


@pytest.fixture(autouse=True)
def _clear_producer_lease() -> None:
    clear_trusted_search_producer()
    yield
    clear_trusted_search_producer()


def _key() -> ToolInvocationKeyV1:
    return ToolInvocationKeyV1(
        invocation_id="invoke-1",
        root_session_id="session-1",
        request_id="request-1",
        executor_kind="agent",
        execution_session_id="session-1",
        tool_call_id="call-1",
    )


def _bind(ledger: SessionTrustedSearchUrls) -> None:
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="free_search",
        max_results=8,
    )


def _effective_intent_context(text: str, *, request_id: str) -> RootDecisionContext:
    return RootDecisionContext(
        session_id="session-1",
        request_id=request_id,
        channel_id="web",
        trusted_turns=(
            RootIntentTurn(
                request_id=request_id,
                kind=RootIntentTurnKind.FRESH,
                text=text,
            ),
        ),
    )


class _ProductionSearchCallbacks:
    def __init__(
        self,
        queue_rail: RootPermissionQueueRail,
        permission_rail: AutoPermissionInterruptRail,
    ) -> None:
        self.queue_rail = queue_rail
        self.permission_rail = permission_rail

    async def execute(
        self,
        event: AgentCallbackEvent,
        ctx: AgentCallbackContext,
    ) -> None:
        if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
            await self.queue_rail.before_tool_call(ctx)
            await self.permission_rail.before_tool_call(ctx)
        elif event is AgentCallbackEvent.AFTER_TOOL_CALL:
            await self.permission_rail.after_tool_call(ctx)
        elif event is AgentCallbackEvent.ON_TOOL_EXCEPTION:
            await self.queue_rail.on_tool_exception(ctx)


@pytest.mark.asyncio
async def test_real_fetch_callbacks_isolate_smart_and_manual_execution(monkeypatch, tmp_path):
    import requests
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.agents.harness.common.tools import web_fetch_tools as fetch
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        PUBLIC_HTTPS_FETCH_CONTEXT_ATTR,
    )

    url = "https://news.invalid/story"
    ledger = SessionTrustedSearchUrls("session-1")
    ledger.record_batch(key=_key(), urls=[url], max_results=1)
    queue = RootPermissionQueue()
    permission = AutoPermissionInterruptRail(
        base_rail=FakeBaseRail(), permission_config={"mode": "auto", "enabled": True},
        workspace_root=tmp_path, trusted_search_urls=ledger,
        policy_evaluator=StaticPolicyEvaluator(PolicyEvaluation(level="ask", reason="policy_ask")),
    )
    production = _ProductionSearchCallbacks(RootPermissionQueueRail(queue), permission)

    async def callbacks(event, ctx):
        if ctx.inputs.tool_call.id == "smart":
            await production.execute(event, ctx)

    calls = []
    def http(url, **kwargs):
        calls.append((url, kwargs))
        response = requests.Response()
        response.url, response._content, response.encoding = url, b"page body", "utf-8"
        response.status_code = 302 if kwargs.get("allow_redirects") is False else 200
        response.headers["Location"] = "http://127.0.0.1/probe"
        response._content_consumed = True
        return response

    monkeypatch.setattr(fetch, "_http_get", http)
    monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda **kwargs: fetch.mcp_fetch_webpage)
    manager = AbilityManager()
    manager.add(fetch.mcp_fetch_webpage.card)
    parent = AgentCallbackContext(agent=SimpleNamespace(agent_callback_manager=SimpleNamespace(execute=callbacks)))
    # An inherited parent marker must not affect a fresh non-Smart tool context.
    setattr(parent, PUBLIC_HTTPS_FETCH_CONTEXT_ATTR, True)
    intent = bind_root_decision_context(_effective_intent_context("Read the search result.", request_id="request-1"))
    binding = bind_root_permission_request(root_session_id="session-1", request_id="request-1", enabled=True, queue=queue)
    try:
        results = await manager.execute(parent, [
            ToolCall(id=kind, type="function", name="mcp_fetch_webpage", arguments=json.dumps({"url": url}))
            for kind in ("smart", "manual")
        ], session=SimpleNamespace(session_id="session-1"))
    finally:
        reset_root_permission_request(binding)
        reset_root_decision_context(intent)
    assert "network_scheme_not_https" in results[0][0]
    assert "page body" in results[1][0]
    assert len(calls) == 2 and all(target == url for target, _ in calls)
    assert PUBLIC_HTTPS_FETCH_CONTEXT_ATTR not in parent.extra
    assert not fetch._PUBLIC_HTTPS_FETCH.get()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inputs", "expected_max_results", "expected_timeout"),
    [
        ({"query": "release"}, 8, 20),
        ({"query": "release", "max_results": "8.0"}, 8, 20),
        ({"query": "release", "max_results": "invalid"}, 8, 20),
        ({"query": "release", "timeout_seconds": "invalid"}, 8, 20),
        ({"query": "release", "max_results": -3, "timeout_seconds": 1}, 1, 5),
        ({"query": "release", "max_results": 99, "timeout_seconds": 99}, 20, 60),
    ],
)
async def test_adapter_matches_upstream_output_and_argument_normalization(
    monkeypatch: pytest.MonkeyPatch,
    inputs: dict[str, Any],
    expected_max_results: int,
    expected_timeout: int,
) -> None:
    url = "https://release-notes.invalid/v1"
    calls: list[tuple[str, int, int]] = []

    async def fake_search(
        session: object, query: str, max_results: int, timeout_seconds: int
    ) -> tuple[str, list[dict[str, str]]]:
        del session
        calls.append((query, max_results, timeout_seconds))
        return (
            "duckduckgo",
            [
                {"title": "Release notes", "url": url, "snippet": "Summary"},
                {"title": "Additional notes", "url": "https://docs.invalid/v1"},
            ],
        )

    monkeypatch.setattr(WebFreeSearchTool, "_search_free", staticmethod(fake_search))
    upstream = WebFreeSearchTool(agent_id="agent-1")
    trusted = TrustedWebFreeSearchTool(agent_id="agent-1")
    expected = await upstream.invoke(inputs)

    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    actual = await trusted.invoke(inputs)

    assert actual == expected
    assert calls == [
        ("release", expected_max_results, expected_timeout),
        ("release", expected_max_results, expected_timeout),
    ]
    assert trusted.card.model_dump() == upstream.card.model_dump()
    assert ledger.contains(root_session_id="session-1", url=url)
    assert ledger.contains(root_session_id="session-1", url="https://docs.invalid/v1")


@pytest.mark.asyncio
async def test_root_queue_real_search_execution_records_fetch_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the real AbilityManager callbacks and trusted search adapter."""
    url = "https://news.invalid/typhoon"
    ledger = SessionTrustedSearchUrls("session-1")
    invocation_ids = iter(("invoke-search", "invoke-fetch"))
    queue = RootPermissionQueue(id_factory=lambda: next(invocation_ids))
    queue_rail = RootPermissionQueueRail(queue)
    reviewer = StaticReviewerClient(outcome=ReviewerOutcome.ALLOW_ONCE)
    permission_rail = AutoPermissionInterruptRail(
        base_rail=FakeBaseRail(),
        permission_config={"mode": "auto", "enabled": True},
        workspace_root=tmp_path,
        policy_evaluator=StaticPolicyEvaluator(
            PolicyEvaluation(level="ask", reason="policy_ask")
        ),
        auto_reviewer=AutoReviewer(client=reviewer),
        trusted_search_urls=ledger,
    )
    callbacks = _ProductionSearchCallbacks(queue_rail, permission_rail)
    manager = AbilityManager()
    tool = TrustedWebFreeSearchTool(agent_id="agent-1")

    async def fake_search(
        session: object,
        query: str,
        max_results: int,
        timeout_seconds: int,
    ) -> tuple[str, list[dict[str, str]]]:
        del session, query, max_results, timeout_seconds
        return "duckduckgo", [
            {"title": "Result", "url": url, "snippet": "Summary"}
        ]

    monkeypatch.setattr(WebFreeSearchTool, "_search_free", staticmethod(fake_search))

    async def execute_tool(**kwargs: Any) -> tuple[str, ToolMessage]:
        tool_call = kwargs["tool_call"]
        result = (
            await tool.invoke(json.loads(tool_call.arguments))
            if tool_call.name == "free_search"
            else "fetched"
        )
        return result, ToolMessage(content=result, tool_call_id=tool_call.id)

    monkeypatch.setattr(manager, "_execute_single_tool_call", execute_tool)
    parent = AgentCallbackContext(
        agent=SimpleNamespace(agent_callback_manager=callbacks)
    )
    session = SimpleNamespace(session_id="session-1")
    request_token = bind_root_permission_request(
        root_session_id="session-1",
        request_id="request-1",
        enabled=True,
        queue=queue,
    )
    try:
        result = await manager.execute(
            parent,
            ToolCall(
                id="call-1",
                type="function",
                name="free_search",
                arguments=json.dumps({"query": "typhoon", "max_results": 8}),
            ),
            session=session,
        )
    finally:
        reset_root_permission_request(request_token)

    assert url in result[0][0]
    assert ledger.contains(root_session_id="session-1", url=url)

    intent_token = bind_root_decision_context(
        _effective_intent_context(
            "Search public typhoon news and read the first result.",
            request_id="request-2",
        )
    )
    request_token = bind_root_permission_request(
        root_session_id="session-1",
        request_id="request-2",
        enabled=True,
        queue=queue,
    )
    try:
        fetch_result = await manager.execute(
            parent,
            ToolCall(
                id="call-fetch",
                type="function",
                name="fetch_webpage",
                arguments=json.dumps({"url": url}),
            ),
            session=session,
        )
    finally:
        reset_root_permission_request(request_token)
        reset_root_decision_context(intent_token)

    assert fetch_result[0][0] == "fetched"
    assert len(reviewer.requests) == 1
    metadata = parent.extra["permission_reviewer_metadata_by_tool_call_id"][
        "call-fetch"
    ]
    assert metadata["decision_source"] == "deterministic_bounded_scope"
    assert metadata["host_route_source"] == "recent_search_result"
    assert metadata["reviewer_called"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "empty"])
async def test_adapter_matches_upstream_without_recording_unsuccessful_results(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    async def fake_search(
        session: object, query: str, max_results: int, timeout_seconds: int
    ) -> tuple[str, list[dict[str, str]]]:
        del session, query, max_results, timeout_seconds
        if outcome == "failure":
            raise RuntimeError("search unavailable")
        return "duckduckgo", []

    monkeypatch.setattr(WebFreeSearchTool, "_search_free", staticmethod(fake_search))
    expected = await WebFreeSearchTool(agent_id="agent-1").invoke({"query": "release"})

    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    actual = await TrustedWebFreeSearchTool(agent_id="agent-1").invoke(
        {"query": "release"}
    )

    assert actual == expected
    assert len(ledger) == 0


@pytest.mark.asyncio
async def test_adapter_matches_upstream_empty_query_without_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_search(*args: object) -> None:
        raise AssertionError(f"search should not run: {args!r}")

    monkeypatch.setattr(
        WebFreeSearchTool, "_search_free", staticmethod(unexpected_search)
    )
    inputs = {"query": "  ", "max_results": object()}
    expected = await WebFreeSearchTool(agent_id="agent-1").invoke(inputs)

    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    actual = await TrustedWebFreeSearchTool(agent_id="agent-1").invoke(inputs)

    assert actual == expected == "[ERROR]: query cannot be empty."
    assert len(ledger) == 0


@pytest.mark.asyncio
async def test_adapter_keeps_provenance_when_rendering_fails_after_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://release-notes.invalid/v1"

    async def fake_search(
        session: object, query: str, max_results: int, timeout_seconds: int
    ) -> tuple[str, list[dict[str, str]]]:
        del session, query, max_results, timeout_seconds
        return "duckduckgo", [{"url": url}]

    monkeypatch.setattr(WebFreeSearchTool, "_search_free", staticmethod(fake_search))
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)

    with pytest.raises(KeyError, match="title"):
        await TrustedWebFreeSearchTool(agent_id="agent-1").invoke({"query": "release"})

    assert ledger.contains(root_session_id="session-1", url=url)


def test_adapter_owns_only_provenance_and_all_registrations_use_it() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    adapter_source = (
        repository_root
        / "jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py"
    ).read_text(encoding="utf-8")
    assert "run_free_search_structured" not in adapter_source

    for relative_path in (
        "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py",
        "jiuwenswarm/server/runtime/agent_adapter/interface_code.py",
    ):
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "TrustedWebFreeSearchTool" in source
        assert re.search(r"(?<!Trusted)WebFreeSearchTool\(", source) is None
