# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the OpenJiuwen free-search provenance adapter."""

from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved ``.invalid`` names and no test
# performs external network I/O.

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

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
def _clear_producer_lease(monkeypatch) -> None:
    monkeypatch.delenv("FREE_SEARCH_BING_MARKET", raising=False)
    monkeypatch.delenv("FREE_SEARCH_BING_FALLBACK_MARKET", raising=False)
    clear_trusted_search_producer()

    from openjiuwen.harness.tools.web import free_search

    labels = {
        "Remote desktop support": "rejected",
        "Unrelated device manual": "rejected",
        "What is artificial intelligence (AI)?": "rejected",
        "AI agents take the next step": "uncertain",
    }

    async def scripted_invoke(**kwargs):
        # These are fixed replies for adapter plumbing, not a semantic evaluator.
        payload = json.loads(kwargs["messages"][1]["content"])
        return SimpleNamespace(content=json.dumps({"results": [
            {"id": row["id"], "relevance": labels.get(row["title"], "relevant"), "reason": "scripted judgment"}
            for row in payload["candidates"]
        ]}))

    resolve = free_search.resolve_search_model
    scripted = SimpleNamespace(invoke=scripted_invoke)
    monkeypatch.setattr(free_search, "resolve_search_model", lambda context, fallback=None: resolve(context, fallback or scripted))
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
        self.contexts: list[AgentCallbackContext] = []

    async def execute(
        self,
        event: AgentCallbackEvent,
        ctx: AgentCallbackContext,
    ) -> None:
        self.contexts.append(ctx)
        if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
            await self.queue_rail.before_tool_call(ctx)
            await self.permission_rail.before_tool_call(ctx)
        elif event is AgentCallbackEvent.AFTER_TOOL_CALL:
            await self.queue_rail.after_tool_call(ctx)
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
    parent = AgentCallbackContext(agent=SimpleNamespace(
        agent_callback_manager=SimpleNamespace(execute=callbacks), ability_manager=manager,
    ))
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

    async def fake_search(request: Any) -> tuple[str, list[dict[str, str]]]:
        calls.append((request.query, request.max_results, request.timeout_seconds))
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
@pytest.mark.parametrize("assessment_status", ["ok", "unavailable"])
async def test_adapter_preserves_quality_annotations_and_search_context(monkeypatch, assessment_status):
    from openjiuwen.harness.tools.web import free_search

    url = "https://release-notes.invalid/v1"
    rows = [{
        "title": "Provisional release notes",
        "url": url,
        "snippet": "Unverified summary",
        "date": "2026-11-06",
        "relevance": "uncertain",
        "relevance_reason": "entity needs verification",
        "assessment_status": assessment_status,
    }]
    callback = object()
    model = object()
    budget = SimpleNamespace(original_task="Check the release date")
    requests = []

    def get_budget(actual_callback):
        assert actual_callback is callback
        return budget

    def get_model(actual_callback, configured_model):
        assert actual_callback is callback
        return model

    async def search(request):
        requests.append(request)
        return "duckduckgo", rows

    monkeypatch.setattr(free_search, "get_web_budget", get_budget)
    monkeypatch.setattr(free_search, "resolve_search_model", get_model)
    monkeypatch.setattr(WebFreeSearchTool, "_search_free", staticmethod(search))
    inputs = {"query": "release", "provider": "duckduckgo"}
    expected = await WebFreeSearchTool(agent_id="agent-1").invoke(
        inputs, _tool_callback_context=callback,
    )
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)

    actual = await TrustedWebFreeSearchTool(agent_id="agent-1").invoke(
        inputs, _tool_callback_context=callback,
    )

    assert actual == expected
    assert "Content status: uncertain" in actual
    assert "Search-reported date (unverified): 2026-11-06" in actual
    assert ("Assessment status: unavailable" in actual) == (assessment_status == "unavailable")
    assert ledger.contains(root_session_id="session-1", url=url)
    assert len(requests) == 2
    for request in requests:
        assert request.provider == "duckduckgo"
        assert request.budget is budget
        assert request.model is model
        assert request.original_task == budget.original_task


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

    async def fake_search(request: Any) -> tuple[str, list[dict[str, str]]]:
        del request
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
        agent=SimpleNamespace(agent_callback_manager=callbacks, ability_manager=manager)
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
    fetch_context = next(
        ctx
        for ctx in reversed(callbacks.contexts)
        if getattr(getattr(ctx.inputs, "tool_call", None), "id", None)
        == "call-fetch"
    )
    metadata = fetch_context.extra["permission_reviewer_metadata_by_tool_call_id"][
        "call-fetch"
    ]
    assert "permission_reviewer_metadata_by_tool_call_id" not in parent.extra
    assert metadata["decision_source"] == "deterministic_bounded_scope"
    assert metadata["host_route_source"] == "recent_search_result"
    assert metadata["reviewer_called"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "empty"])
async def test_adapter_matches_upstream_without_recording_unsuccessful_results(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    async def fake_search(request: Any) -> tuple[str, list[dict[str, str]]]:
        del request
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

    async def fake_search(request: Any) -> tuple[str, list[dict[str, str]]]:
        del request
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


@pytest.mark.asyncio
@pytest.mark.parametrize("relevant", [True, False])
async def test_real_sdk_quality_gate_controls_rendered_result_and_provenance(monkeypatch, relevant):
    from openjiuwen.harness.tools.web import _http

    url = "https://source.invalid/weather"
    title = "Hangzhou weather forecast" if relevant else "Remote desktop support"

    async def request(session, method, target, **kwargs):
        page = f'<li class="b_algo"><h2><a href="{url}">{title}</a></h2></li>'
        return 200, {"Content-Type": "text/html"}, page.encode(), target, False

    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    monkeypatch.setattr(_http, "request", request)
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await TrustedWebFreeSearchTool(enabled_engines=("bing",)).invoke({
        "query": "Hangzhou weather", "provider": "bing",
    })
    assert ledger.contains(root_session_id="session-1", url=url) is relevant
    assert (url in result) is relevant
    assert ("Content status: ok" if relevant else "Content status: low_relevance") in result


@pytest.mark.asyncio
async def test_adapter_passes_callback_budget_and_does_not_record_over_budget_result(monkeypatch):
    from openjiuwen.harness.rails.web_research_rail import WebResearchBudgetRail
    from openjiuwen.harness.tools.web import _http
    from openjiuwen.harness.tools.web._budget import WEB_RESEARCH_BUDGET_KEY

    requests = []

    async def request(session, method, target, **kwargs):
        requests.append(target)
        page = '<li class="b_algo"><h2><a href="https://source.invalid/weather">Hangzhou weather</a></h2></li>'
        return 200, {"Content-Type": "text/html"}, page.encode(), target, False

    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    monkeypatch.setenv("WEB_RESEARCH_SEARCHES_PER_ENGINE", "2")
    monkeypatch.setattr(_http, "request", request)
    ctx = AgentCallbackContext(agent=None)
    rail = WebResearchBudgetRail()
    await rail.before_model_call(ctx)
    tool = TrustedWebFreeSearchTool(enabled_engines=("bing",))
    for attempt in range(3):
        ledger = SessionTrustedSearchUrls("session-1")
        _bind(ledger)
        result = await tool.invoke({"query": "Hangzhou weather"}, _tool_callback_context=ctx)
        assert len(ledger) == (1 if attempt < 2 else 0)
    assert "Content status: budget_exhausted" in result
    assert len(requests) == 2 and ctx.extra[WEB_RESEARCH_BUDGET_KEY].searches == {"bing": 2}


@pytest.mark.asyncio
async def test_provisional_search_candidate_keeps_sdk_warning_and_source_provenance(monkeypatch):
    from openjiuwen.harness.tools.web import _http

    url = "https://source.invalid/agents"

    async def request(session, method, target, **kwargs):
        page = f'<li class="b_algo"><h2><a href="{url}">AI agents take the next step</a></h2><p>Capabilities</p></li>'
        return 200, {"Content-Type": "text/html"}, page.encode(), target, False

    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    monkeypatch.setattr(_http, "request", request)
    inputs = {"query": "AI Agent news", "provider": "bing"}
    expected = await WebFreeSearchTool(enabled_engines=("bing",)).invoke(inputs)
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await TrustedWebFreeSearchTool(enabled_engines=("bing",)).invoke(inputs)
    assert result == expected
    assert "Content status: uncertain" in result
    assert "Relevance: uncertain (scripted judgment)" in result
    assert ledger.contains(root_session_id="session-1", url=url)


@pytest.mark.asyncio
async def test_actual_dispatch_reuses_active_model_and_records_only_selected_urls(monkeypatch):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.rail.base import InvokeInputs
    from openjiuwen.harness.rails.web_research_rail import WebResearchBudgetRail
    from openjiuwen.harness.tools.web import _http

    selected_url = "https://source.invalid/selected"
    rejected_url = "https://source.invalid/rejected"

    async def request(session, method, target, **kwargs):
        page = (f'<li class="b_algo"><h2><a href="{selected_url}">A</a></h2></li>'
                f'<li class="b_algo"><h2><a href="{rejected_url}">B</a></h2></li>')
        return 200, {}, page.encode(), target, False

    monkeypatch.setattr(_http, "request", request)
    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    model = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({"results": [
        {"id": 0, "relevance": "relevant", "reason": "Supports the requested subquestion"},
        {"id": 1, "relevance": "rejected", "reason": "Different subject"},
    ]}))))
    tool = TrustedWebFreeSearchTool(enabled_engines=("bing",))
    manager = AbilityManager()
    manager.add([tool.card])
    monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda tool_id, **kwargs: tool)
    ctx = AgentCallbackContext(agent=SimpleNamespace(
        get_llm=lambda: model, agent_callback_manager=SimpleNamespace(execute=AsyncMock()), ability_manager=manager,
    ), inputs=InvokeInputs(query="Original user task"))
    await WebResearchBudgetRail().before_invoke(ctx)
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await manager.execute(
        ctx, ToolCall(id="call-1", type="function", name="free_search", arguments='{"query":"Subquestion"}'),
        session=SimpleNamespace(session_id="session-1"),
    )
    assert selected_url in result[0][0] and rejected_url not in result[0][0]
    assert ledger.contains(root_session_id="session-1", url=selected_url)
    assert not ledger.contains(root_session_id="session-1", url=rejected_url)
    model.invoke.assert_awaited_once()
    sent = json.loads(model.invoke.call_args.kwargs["messages"][1]["content"])
    assert sent["original_task"] == "Original user task" and sent["search_query"] == "Subquestion"


@pytest.mark.asyncio
async def test_truncated_assessment_preserves_later_discovered_sources_through_dispatch(monkeypatch):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.rail.base import InvokeInputs
    from openjiuwen.harness.rails.web_research_rail import WebResearchBudgetRail
    from openjiuwen.harness.tools.web import _http
    from openjiuwen.harness.tools.web._budget import WEB_RESEARCH_BUDGET_KEY

    urls = [f"https://source.invalid/document-{index}" for index in range(5)]
    page = "".join(f'<li class="b_algo"><h2><a href="{url}">Document {index}</a></h2></li>'
                   for index, url in enumerate(urls))
    request = AsyncMock(return_value=(200, {"Content-Type": "text/html"}, page.encode(), "https://bing.com/search", False))
    monkeypatch.setattr(_http, "request", request)
    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    model = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content="", finish_reason="length")))
    tool = TrustedWebFreeSearchTool(enabled_engines=("bing",))
    manager = AbilityManager()
    manager.add([tool.card])
    monkeypatch.setattr(Runner.resource_mgr, "get_tool", lambda tool_id, **kwargs: tool)
    ctx = AgentCallbackContext(agent=SimpleNamespace(
        get_llm=lambda: model, agent_callback_manager=SimpleNamespace(execute=AsyncMock()), ability_manager=manager,
    ), inputs=InvokeInputs(query="Find the original publication"))
    await WebResearchBudgetRail().before_invoke(ctx)
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await manager.execute(
        ctx, ToolCall(id="call-unassessed", type="function", name="free_search",
                      arguments='{"query":"Original publication","max_results":4}'),
        session=SimpleNamespace(session_id="session-1"),
    )
    text = result[0][0]
    assert "Assessment status: unavailable" in text and "assessment_output_truncated" in text
    assert "Content status: uncertain" in text and "Content status: ok" not in text
    assert "Recommended fetch targets" not in text
    for url in urls[:4]:
        assert url in text and ledger.contains(root_session_id="session-1", url=url)
    assert urls[4] not in text and not ledger.contains(root_session_id="session-1", url=urls[4])
    assert ctx.extra[WEB_RESEARCH_BUDGET_KEY].searches == {"bing": 1}
    assert model.invoke.call_args.kwargs["reasoning"].mode == "disabled"
    request.assert_awaited_once()
    model.invoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_relevant", [True, False])
async def test_generic_ai_is_not_trusted_and_fallback_preserves_source_budget(monkeypatch, fallback_relevant):
    from openjiuwen.harness.tools.web import _http
    from openjiuwen.harness.tools.web._budget import WEB_RESEARCH_BUDGET_KEY, WebResearchBudget

    calls = []
    generic_url = "https://source.invalid/generic-ai"
    agent_url = "https://source.invalid/agent-research"

    async def request(session, method, target, **kwargs):
        market = parse_qs(urlsplit(target).query)["mkt"][0]
        calls.append(market)
        relevant = market == "zh-CN" and fallback_relevant
        title = "Agentic AI research trends report" if relevant else "What is artificial intelligence (AI)?"
        url = agent_url if relevant else generic_url
        page = f'<li class="b_algo"><h2><a href="{url}">{title}</a></h2><p>AI research report</p></li>'
        return 200, {"Content-Type": "text/html"}, page.encode(), target, False

    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    monkeypatch.setattr(_http, "request", request)
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    budget = WebResearchBudget()
    ctx = SimpleNamespace(extra={WEB_RESEARCH_BUDGET_KEY: budget})
    result = await TrustedWebFreeSearchTool(enabled_engines=("bing",)).invoke(
        {"query": "AI Agent research trends", "provider": "bing"}, _tool_callback_context=ctx,
    )
    assert calls == ["en-US", "zh-CN"] and budget.searches == {"bing": 1}
    assert not ledger.contains(root_session_id="session-1", url=generic_url)
    assert generic_url not in result
    assert ledger.contains(root_session_id="session-1", url=agent_url) is fallback_relevant
    assert ("Content status: ok" if fallback_relevant else "Content status: low_relevance") in result


@pytest.mark.asyncio
async def test_traditional_finance_source_reaches_swarm_output_and_provenance(monkeypatch):
    from openjiuwen.harness.tools.web import _http

    url = "https://finance.invalid/quote/IXIC"
    calls = []

    async def request(session, method, target, **kwargs):
        calls.append(target)
        title = "\u7d0d\u65af\u9054\u514b\u6307\u6578 (^IXIC) \u5716\u8868\u3001\u6578\u64da\u548c\u65b0\u805e"
        page = f'<li class="b_algo"><h2><a href="{url}">{title}</a></h2><p>Market quote</p></li>'
        return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode(), target, False

    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    monkeypatch.setattr(_http, "request", request)
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await TrustedWebFreeSearchTool(enabled_engines=("bing",)).invoke({
        "query": "\u7eb3\u65af\u8fbe\u514b\u6307\u6570 \u6700\u65b0\u70b9\u4f4d", "provider": "bing",
    })
    assert "Content status: ok" in result and len(calls) == 1
    assert url in result and ledger.contains(root_session_id="session-1", url=url)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_relevant", [True, False])
async def test_market_fallback_preserves_sdk_budget_and_trusted_provenance(monkeypatch, fallback_relevant):
    from openjiuwen.harness.rails.web_research_rail import WebResearchBudgetRail
    from openjiuwen.harness.tools.web import _http
    from openjiuwen.harness.tools.web._budget import WEB_RESEARCH_BUDGET_KEY

    calls = []
    queries = []
    accepted_url = "https://source.invalid/weather"
    rejected_url = "https://source.invalid/devices"

    async def request(session, method, target, **kwargs):
        params = parse_qs(urlsplit(target).query)
        market = params["mkt"][0]
        calls.append(market)
        queries.append(params["q"][0])
        relevant = market == "en-US" and fallback_relevant
        title = "Hangzhou weather forecast" if relevant else "Unrelated device manual"
        url = accepted_url if relevant else rejected_url
        page = f'<li class="b_algo"><h2><a href="{url}">{title}</a></h2></li>'
        return 200, {"Content-Type": "text/html"}, page.encode(), target, False

    monkeypatch.setenv("FREE_SEARCH_DEBUG", "false")
    monkeypatch.setenv("FREE_SEARCH_BING_MARKET", "zh-CN")
    monkeypatch.setenv("FREE_SEARCH_BING_FALLBACK_MARKET", "en-US")
    monkeypatch.setenv("WEB_RESEARCH_SEARCHES_PER_ENGINE", "2")
    monkeypatch.setattr(_http, "request", request)
    ctx = AgentCallbackContext(agent=None)
    await WebResearchBudgetRail().before_model_call(ctx)
    tool = TrustedWebFreeSearchTool(enabled_engines=("bing",))
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await tool.invoke({"query": "Hangzhou weather"}, _tool_callback_context=ctx)
    assert calls == ["zh-CN", "en-US"]
    assert ctx.extra[WEB_RESEARCH_BUDGET_KEY].searches == {"bing": 1}
    assert ledger.contains(root_session_id="session-1", url=accepted_url) is fallback_relevant
    assert not ledger.contains(root_session_id="session-1", url=rejected_url)
    assert rejected_url not in result
    assert ("Content status: ok" if fallback_relevant else "Content status: low_relevance") in result

    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await tool.invoke({"query": "Hangzhou weather forecast"}, _tool_callback_context=ctx)
    assert ("Content status: ok" if fallback_relevant else "Content status: low_relevance") in result
    assert calls == ["zh-CN", "en-US", "zh-CN", "en-US"]
    assert queries == [
        "Hangzhou weather", "Hangzhou weather", "Hangzhou weather forecast", "Hangzhou weather forecast",
    ]
    assert ctx.extra[WEB_RESEARCH_BUDGET_KEY].searches == {"bing": 2}
    assert ledger.contains(root_session_id="session-1", url=accepted_url) is fallback_relevant
    assert not ledger.contains(root_session_id="session-1", url=rejected_url)

    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await tool.invoke({"query": "Hangzhou weather tomorrow"}, _tool_callback_context=ctx)
    assert "Content status: budget_exhausted" in result and len(calls) == 4 and len(ledger) == 0


@pytest.mark.asyncio
async def test_old_sdk_contract_fails_closed_without_losing_producer_cleanup(monkeypatch):
    monkeypatch.delattr(WebFreeSearchTool, "_on_search_results")
    ledger = SessionTrustedSearchUrls("session-1")
    _bind(ledger)
    result = await TrustedWebFreeSearchTool().invoke({"query": "weather"})
    assert "matching agent-core version" in result and len(ledger) == 0


@pytest.mark.parametrize("mode", ["agent", "code"])
def test_work_and_code_assembly_include_one_sdk_budget_rail(monkeypatch, mode):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
    from openjiuwen.harness.rails.web_research_rail import WebResearchBudgetRail

    adapter = JiuWenSwarmDeepAdapter() if mode == "agent" else JiuwenSwarmCodeAdapter()
    original_instantiate = adapter._instantiate_rails

    def instantiate_selected(infos, config):
        selected = [info for info in infos if info.attr_name == "_web_research_budget_rail"]
        assert len(selected) == 1
        return original_instantiate(selected, config)

    monkeypatch.setattr(adapter, "_instantiate_rails", instantiate_selected)
    rails = adapter._build_agent_rails({}, {"permissions": {"enabled": False}}, mode=mode)
    budget_rails = [rail for rail in rails if isinstance(rail, WebResearchBudgetRail)]
    assert len(budget_rails) == 1
    assert adapter._web_research_budget_rail is budget_rails[0]


@pytest.mark.parametrize("mode", ["agent", "code"])
@pytest.mark.parametrize("failure", ["missing_module", "constructor"])
def test_optional_search_budget_failure_does_not_abort_agent_assembly(monkeypatch, caplog, mode, failure):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
    from openjiuwen.harness.rails import web_research_rail

    adapter = JiuWenSwarmDeepAdapter() if mode == "agent" else JiuwenSwarmCodeAdapter()
    original_instantiate = adapter._instantiate_rails

    def instantiate_selected(infos, config):
        selected = [info for info in infos if info.attr_name == "_web_research_budget_rail"]
        assert len(selected) == 1
        return original_instantiate(selected, config)

    monkeypatch.setattr(adapter, "_instantiate_rails", instantiate_selected)
    if failure == "missing_module":
        monkeypatch.setitem(sys.modules, "openjiuwen.harness.rails.web_research_rail", None)
    else:
        monkeypatch.setattr(web_research_rail, "WebResearchBudgetRail", Mock(side_effect=RuntimeError("invalid budget")))

    rails = adapter._build_agent_rails({}, {"permissions": {"enabled": False}}, mode=mode)
    assert all(type(rail).__name__ != "WebResearchBudgetRail" for rail in rails)
    assert any(type(rail).__name__ == "PromptSuggestionForkRail" for rail in rails)
    assert "WebResearchBudgetRail create failed" in caplog.text
