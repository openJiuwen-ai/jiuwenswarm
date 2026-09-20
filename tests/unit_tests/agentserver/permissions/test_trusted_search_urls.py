# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the session-owned trusted search URL ledger."""

from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved ``.invalid`` names and no test
# performs external network I/O.

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
)
from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    MAX_TRUSTED_SEARCH_URLS_PER_SESSION,
    SessionTrustedSearchUrls,
    bind_trusted_search_producer,
    clear_trusted_search_producer,
    complete_trusted_search_producer,
)
from jiuwenswarm.agents.harness.common.tools.search_tools import (
    MAX_SEARCH_MAX_RESULTS,
)


def _key(
    *, session_id: str = "session-1", invocation_id: str = "invoke-1"
) -> ToolInvocationKeyV1:
    return ToolInvocationKeyV1(
        invocation_id=invocation_id,
        root_session_id=session_id,
        request_id="request-1",
        executor_kind="agent",
        execution_session_id=session_id,
        tool_call_id="call-1",
    )


def test_successful_host_callback_records_canonical_urls_across_requests() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=20,
    )

    accepted, dropped = complete_trusted_search_producer(
        tool_name="mcp_free_search",
        success=True,
        urls=(
            "HTTPS://Example.INVALID/news?id=1",
            "https://example.invalid/news?id=1",
            "http://example.invalid/unsafe",
        ),
    )

    assert (accepted, dropped) == (1, 0)
    assert ledger.contains(
        root_session_id="session-1", url="https://example.invalid/news?id=1"
    )
    assert not ledger.contains(
        root_session_id="session-2", url="https://example.invalid/news?id=1"
    )


def test_callback_requires_exact_host_tool_binding_and_is_one_shot() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=20,
    )

    assert complete_trusted_search_producer(
        tool_name=" mcp_free_search ",
        success=True,
        urls=("https://example.invalid/a",),
    ) == (0, 0)
    assert complete_trusted_search_producer(
        tool_name="mcp_free_search",
        success=True,
        urls=("https://example.invalid/a",),
    ) == (1, 0)
    assert complete_trusted_search_producer(
        tool_name="mcp_free_search",
        success=True,
        urls=("https://example.invalid/b",),
    ) == (0, 0)
    assert len(ledger) == 1


def test_concurrent_success_and_failure_have_one_terminal_winner() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=20,
    )
    start = Barrier(2)

    def complete(*, success: bool) -> tuple[int, int]:
        start.wait()
        return complete_trusted_search_producer(
            tool_name="mcp_free_search",
            success=success,
            urls=("https://example.invalid/winner",),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        success_context = copy_context()
        failure_context = copy_context()
        success_result = executor.submit(success_context.run, complete, success=True)
        failure_result = executor.submit(failure_context.run, complete, success=False)
        outcomes = (success_result.result(), failure_result.result())

    accepted = sum(result[0] for result in outcomes)
    assert accepted in {0, 1}
    assert len(ledger) == accepted
    assert complete_trusted_search_producer(
        tool_name="mcp_free_search",
        success=True,
        urls=("https://example.invalid/late",),
    ) == (0, 0)


def test_failed_completion_consumes_lease_without_recording() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=20,
    )

    assert complete_trusted_search_producer(
        tool_name="mcp_free_search", success=False
    ) == (0, 0)
    assert complete_trusted_search_producer(
        tool_name="mcp_free_search",
        success=True,
        urls=("https://example.invalid/late",),
    ) == (0, 0)
    assert len(ledger) == 0


def test_copied_context_cannot_complete_revoked_lease() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=20,
    )

    async def complete_from_copied_task() -> tuple[int, int]:
        await asyncio.sleep(0)
        return complete_trusted_search_producer(
            tool_name="mcp_free_search",
            success=True,
            urls=("https://example.invalid/late",),
        )

    async def scenario() -> tuple[int, int]:
        task = asyncio.create_task(complete_from_copied_task())
        clear_trusted_search_producer()
        return await task

    assert asyncio.run(scenario()) == (0, 0)
    assert len(ledger) == 0


def test_invocation_cap_is_twenty_and_session_capacity_partially_accepts() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    first = tuple(
        f"https://example.invalid/item/{index}"
        for index in range(MAX_TRUSTED_SEARCH_URLS_PER_SESSION - 5)
    )
    assert ledger.record_batch(key=_key(), urls=first, max_results=500) == (
        MAX_SEARCH_MAX_RESULTS,
        0,
    )
    for batch_index in range(1, 25):
        urls = tuple(
            f"https://example.invalid/batch/{batch_index}/{index}" for index in range(20)
        )
        ledger.record_batch(
            key=_key(invocation_id=f"invoke-{batch_index + 1}"),
            urls=urls,
            max_results=20,
        )
    assert len(ledger) == MAX_TRUSTED_SEARCH_URLS_PER_SESSION

    accepted, dropped = ledger.record_batch(
        key=_key(invocation_id="invoke-capacity"),
        urls=tuple(f"https://example.invalid/overflow/{index}" for index in range(20)),
        max_results=20,
    )
    assert (accepted, dropped) == (0, 20)


def test_ledger_respects_requested_limit_and_zero_disables_recording() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    urls = tuple(f"https://example.invalid/result/{index}" for index in range(25))

    assert ledger.record_batch(key=_key(), urls=urls, max_results=3) == (3, 0)
    assert ledger.record_batch(
        key=_key(invocation_id="invoke-zero"),
        urls=urls[3:],
        max_results=0,
    ) == (0, 0)
    assert ledger.record_batch(
        key=_key(invocation_id="invoke-negative"),
        urls=urls[3:],
        max_results=-1,
    ) == (0, 0)
    assert len(ledger) == 3


def test_remaining_capacity_accepts_prefix_in_producer_order() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    for batch_index in range(24):
        ledger.record_batch(
            key=_key(invocation_id=f"invoke-{batch_index}"),
            urls=tuple(
                f"https://example.invalid/{batch_index}/{index}" for index in range(20)
            ),
            max_results=20,
        )
    assert len(ledger) == 480
    ledger.record_batch(
        key=_key(invocation_id="invoke-481"),
        urls=tuple(f"https://example.invalid/fill/{index}" for index in range(15)),
        max_results=20,
    )
    assert len(ledger) == 495

    accepted, dropped = ledger.record_batch(
        key=_key(invocation_id="invoke-final"),
        urls=tuple(f"https://example.invalid/final/{index}" for index in range(20)),
        max_results=20,
    )

    assert (accepted, dropped) == (5, 15)
    assert ledger.contains(
        root_session_id="session-1", url="https://example.invalid/final/4"
    )
    assert not ledger.contains(
        root_session_id="session-1", url="https://example.invalid/final/5"
    )


def test_dispose_closes_session_ledger_permanently() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    ledger.record_batch(
        key=_key(), urls=("https://example.invalid/result",), max_results=20
    )
    ledger.dispose()
    clear_trusted_search_producer()

    assert len(ledger) == 0
    assert not ledger.contains(
        root_session_id="session-1", url="https://example.invalid/result"
    )
    assert ledger.sources(root_session_id="session-1") == ()
    assert ledger.record_batch(
        key=_key(invocation_id="invoke-after-dispose"),
        urls=("https://example.invalid/late",),
        max_results=20,
    ) == (0, 0)
    with pytest.raises(ValueError, match="trusted_search_session_closed"):
        ledger.bind_session("session-1")


def test_dispose_prevents_copied_lease_from_recording_late_success() -> None:
    ledger = SessionTrustedSearchUrls("session-1")
    bind_trusted_search_producer(
        ledger=ledger,
        key=_key(),
        tool_name="mcp_free_search",
        max_results=20,
    )

    async def complete_from_copied_task() -> tuple[int, int]:
        await asyncio.sleep(0)
        return complete_trusted_search_producer(
            tool_name="mcp_free_search",
            success=True,
            urls=("https://example.invalid/late",),
        )

    async def scenario() -> tuple[int, int]:
        task = asyncio.create_task(complete_from_copied_task())
        ledger.dispose()
        return await task

    assert asyncio.run(scenario()) == (0, 0)
    assert len(ledger) == 0


def test_success_before_dispose_is_removed_and_new_session_is_independent() -> None:
    old_ledger = SessionTrustedSearchUrls("session-1")
    assert old_ledger.record_batch(
        key=_key(), urls=("https://example.invalid/old",), max_results=20
    ) == (1, 0)

    old_ledger.dispose()
    new_ledger = SessionTrustedSearchUrls("session-2")
    assert new_ledger.record_batch(
        key=_key(session_id="session-2", invocation_id="invoke-new"),
        urls=("https://example.invalid/new",),
        max_results=20,
    ) == (1, 0)

    assert len(old_ledger) == 0
    assert new_ledger.contains(
        root_session_id="session-2", url="https://example.invalid/new"
    )
