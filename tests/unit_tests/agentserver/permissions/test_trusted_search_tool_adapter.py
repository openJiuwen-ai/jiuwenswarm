# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Host-owned MCP search integration adapters."""

from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved ``.invalid`` names and no test
# performs external network I/O.

import ast
import inspect
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
)
from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    SessionTrustedSearchUrls,
    bind_trusted_search_producer,
    clear_trusted_search_producer,
)
from jiuwenswarm.agents.harness.common.tools import trusted_search_tool_adapter
from jiuwenswarm.agents.harness.common.tools.search_tools import (
    DEFAULT_SEARCH_MAX_RESULTS,
    MAX_SEARCH_MAX_RESULTS,
    MIN_SEARCH_MAX_RESULTS,
    normalize_search_max_results,
)
from jiuwenswarm.agents.harness.common.tools.mcp_toolkits import (
    mcp_free_search,
    mcp_paid_search,
)


def _key(*, tool_call_id: str = "call-1") -> ToolInvocationKeyV1:
    return ToolInvocationKeyV1(
        invocation_id=f"invoke-{tool_call_id}",
        root_session_id="session-1",
        request_id="request-1",
        executor_kind="agent",
        execution_session_id="session-1",
        tool_call_id=tool_call_id,
    )


def test_neutral_search_result_count_contract() -> None:
    assert DEFAULT_SEARCH_MAX_RESULTS == 8
    assert MIN_SEARCH_MAX_RESULTS == 1
    assert MAX_SEARCH_MAX_RESULTS == 20
    assert [normalize_search_max_results(value) for value in (-1, 1, 8, 20, 21)] == [
        1,
        1,
        8,
        20,
        20,
    ]


def test_neutral_search_module_has_no_permission_or_tool_registration() -> None:
    root = Path(__file__).parents[4]
    source = (
        root / "jiuwenswarm/agents/harness/common/tools/search_tools.py"
    ).read_text(encoding="utf-8")

    assert "rails.permissions" not in source
    assert "@tool" not in source
    assert "def mcp_free_search" not in source
    assert "def mcp_paid_search" not in source


def test_mcp_search_result_limit_has_no_permission_side_duplicate() -> None:
    root = Path(__file__).parents[4]
    production_paths = (
        "jiuwenswarm/agents/harness/common/tools/search_tools.py",
        "jiuwenswarm/agents/harness/common/tools/trusted_search_tool_adapter.py",
        "jiuwenswarm/agents/harness/common/rails/permissions/trusted_search_urls.py",
    )
    production = "\n".join(
        (root / path).read_text(encoding="utf-8") for path in production_paths
    )

    assert "MAX_TRUSTED_SEARCH_URLS_PER_INVOCATION" not in production
    assert "max(1, min(max_results, 20))" not in production
    assert 'get("max_results", 8)' not in production
    assert "max(1, min(int(max_results)," not in production


def test_mcp_toolkit_exports_the_unique_adapter_tool_objects() -> None:
    assert mcp_free_search is trusted_search_tool_adapter.mcp_free_search
    assert mcp_paid_search is trusted_search_tool_adapter.mcp_paid_search
    assert mcp_free_search.card.name == "mcp_free_search"
    assert mcp_paid_search.card.name == "mcp_paid_search"
    assert mcp_free_search.card.input_params["properties"] == {
        "query": {"type": "string", "description": "query"},
        "max_results": {
            "type": "integer",
            "description": "max results",
            "default": 8,
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "timeout seconds",
            "default": 20,
        },
    }
    assert mcp_paid_search.card.input_params["properties"] == {
        "query": {"type": "string", "description": "query"},
        "provider": {
            "type": "string",
            "description": "provider",
            "default": "auto",
        },
        "max_results": {
            "type": "integer",
            "description": "max results",
            "default": 8,
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "timeout seconds",
            "default": 45,
        },
    }


@pytest.mark.asyncio
async def test_free_adapter_runs_normally_without_provenance_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_trusted_search_producer()
    monkeypatch.setattr(
        trusted_search_tool_adapter,
        "run_free_search_structured",
        lambda query, max_results, timeout_seconds: (
            "duckduckgo",
            [{"title": "Result", "url": "https://example.invalid/a", "snippet": ""}],
        ),
    )

    result = await mcp_free_search.invoke({"query": "news"})

    assert "https://example.invalid/a" in result


@pytest.mark.asyncio
async def test_free_adapter_commits_structured_urls_before_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=8,
    )
    url = "https://example.invalid/a"
    monkeypatch.setattr(
        trusted_search_tool_adapter,
        "run_free_search_structured",
        lambda query, max_results, timeout_seconds: (
            "duckduckgo",
            [{"title": "Result", "url": url, "snippet": ""}],
        ),
    )

    def fail_render(**kwargs: object) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(
        trusted_search_tool_adapter, "render_free_search_result", fail_render
    )

    with pytest.raises(RuntimeError, match="render failed"):
        await mcp_free_search.invoke({"query": "news"})

    assert ledger.contains(root_session_id="session-1", url=url)
    clear_trusted_search_producer()


@pytest.mark.asyncio
async def test_paid_adapter_commits_only_structured_provider_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(tool_call_id="paid"),
        tool_name="mcp_paid_search",
        max_results=3,
    )
    urls = ["https://example.invalid/a", "https://example.invalid/b"]

    async def run_paid(**kwargs: object) -> tuple[str, str, list[str]]:
        return "bocha", "Answer", urls

    monkeypatch.setattr(
        trusted_search_tool_adapter, "run_paid_search_structured", run_paid
    )

    result = await mcp_paid_search.invoke({"query": "news"})

    assert result == (
        "Paid search provider: bocha\nAnswer:\nAnswer\nURLs:\n"
        "1. https://example.invalid/a\n2. https://example.invalid/b"
    )
    assert all(ledger.contains(root_session_id="session-1", url=url) for url in urls)
    clear_trusted_search_producer()


@pytest.mark.asyncio
async def test_provider_failure_consumes_lease_without_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=8,
    )

    def fail(*args: object) -> object:
        raise RuntimeError("provider failed")

    monkeypatch.setattr(trusted_search_tool_adapter, "run_free_search_structured", fail)

    assert await mcp_free_search.invoke({"query": "news"}) == (
        "[ERROR]: free search failed: provider failed"
    )
    assert len(ledger) == 0
    clear_trusted_search_producer()


def test_adapter_owns_the_only_decorated_mcp_search_functions() -> None:
    source = inspect.getsource(trusted_search_tool_adapter)
    module = ast.parse(source)
    decorated = {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "tool"
            for decorator in node.decorator_list
        )
    }

    assert decorated == {"mcp_free_search", "mcp_paid_search"}
