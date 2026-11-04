from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.session.session_token_usage import (
    SessionTokenUsageLedger,
    classify_usage_owner,
)


def test_session_token_usage_aggregates_two_categories_and_deduplicates_calls(tmp_path):
    ledger = SessionTokenUsageLedger(tmp_path, "sess_usage")
    ledger.record("call-main-1", "main", 100, 20)
    ledger.record("call-main-2", "main", 50, 10)
    ledger.record("call-other-1", "others", 80, 15)
    ledger.record("call-main-1", "others", 999, 999)
    duplicate = ledger.snapshot()

    assert duplicate == {
        "schema_version": 1,
        "revision": 3,
        "session_id": "sess_usage",
        "main": {
            "input_tokens": 150,
            "output_tokens": 30,
            "input_calls": 2,
            "output_calls": 2,
            "calls_total": 2,
        },
        "others": {
            "input_tokens": 80,
            "output_tokens": 15,
            "input_calls": 1,
            "output_calls": 1,
            "calls_total": 1,
        },
    }

    reopened = SessionTokenUsageLedger(tmp_path, "sess_usage")
    assert reopened.snapshot() == duplicate


def test_session_token_usage_keeps_missing_values_unknown(tmp_path):
    ledger = SessionTokenUsageLedger(tmp_path, "sess_usage")
    ledger.record("call-without-output", "main", 12, None)
    snapshot = ledger.snapshot()

    assert snapshot["main"] == {
        "input_tokens": 12,
        "output_tokens": None,
        "input_calls": 1,
        "output_calls": 0,
        "calls_total": 1,
    }
    assert snapshot["others"]["input_tokens"] == 0
    assert snapshot["others"]["output_tokens"] == 0


def test_snapshot_treats_an_existing_database_without_ledger_tables_as_empty(tmp_path):
    (tmp_path / "session_token_usage.sqlite3").touch()

    snapshot = SessionTokenUsageLedger(tmp_path, "sess_usage").snapshot()

    assert snapshot == {
        "schema_version": 1,
        "revision": 0,
        "session_id": "sess_usage",
        "main": {
            "input_tokens": None,
            "output_tokens": None,
            "input_calls": 0,
            "output_calls": 0,
            "calls_total": 0,
        },
        "others": {
            "input_tokens": None,
            "output_tokens": None,
            "input_calls": 0,
            "output_calls": 0,
            "calls_total": 0,
        },
    }


def test_concurrent_agents_do_not_lose_session_token_updates(tmp_path):
    ledger = SessionTokenUsageLedger(tmp_path, "sess_usage")

    def record(index):
        ledger.record(f"call-{index}", "others", index, index + 1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(1, 17)))

    snapshot = ledger.snapshot()
    assert snapshot["revision"] == 16
    assert snapshot["others"]["input_tokens"] == 136
    assert snapshot["others"]["output_tokens"] == 152
    assert snapshot["others"]["calls_total"] == 16


def test_usage_owner_groups_leader_as_main_and_team_or_nested_agents_as_others():
    assert classify_usage_owner({"role": "leader", "depth": 0}) == "main"
    assert classify_usage_owner({"role": "teammate", "depth": 0}) == "others"
    assert classify_usage_owner({"role": "leader", "depth": 2}) == "main"
    assert classify_usage_owner({"role": "leader", "delegation_id": "child-call"}) == "others"
    assert classify_usage_owner({"depth": 1, "parent_invocation_id": "parent"}) == "others"
    assert classify_usage_owner({"depth": 0, "agent_path": ["main"]}) == "main"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_subagent, category", [(False, "main"), (True, "others")])
async def test_model_calls_without_usage_remain_in_incomplete_totals(tmp_path, monkeypatch, is_subagent, category):
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import record_model_token_usage
    from jiuwenswarm.server.runtime.session import lifecycle, session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: tmp_path)
    ctx = SimpleNamespace(
        extra={},
        inputs=SimpleNamespace(
            context_usage_request_id="unreported-call",
            context_usage_attribution={"product_session_id": "sess_usage"},
            response=SimpleNamespace(usage_metadata=None),
        ),
    )
    await record_model_token_usage(ctx, is_subagent=is_subagent)
    await record_model_token_usage(ctx, is_subagent=is_subagent)
    ledger = SessionTokenUsageLedger(tmp_path / "sess_usage", "sess_usage")
    assert ledger.snapshot()[category] == {
        "input_tokens": None,
        "output_tokens": None,
        "input_calls": 0,
        "output_calls": 0,
        "calls_total": 1,
    }

    ctx.inputs.context_usage_request_id = "reported-call"
    ctx.inputs.response.usage_metadata = {"input_tokens": 100, "output_tokens": 20}
    await record_model_token_usage(ctx, is_subagent=is_subagent)
    snapshot = ledger.snapshot()
    assert snapshot["revision"] == 2
    assert snapshot[category] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_calls": 1,
        "output_calls": 1,
        "calls_total": 2,
    }


@pytest.mark.asyncio
async def test_model_call_without_response_does_not_create_usage_ledger(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import record_model_token_usage
    from jiuwenswarm.server.runtime.session import lifecycle, session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: tmp_path)
    ctx = SimpleNamespace(
        extra={},
        inputs=SimpleNamespace(
            context_usage_request_id="failed-call",
            context_usage_attribution={"product_session_id": "sess_usage"},
            response=None,
        ),
    )
    await record_model_token_usage(ctx)
    assert not (tmp_path / "sess_usage" / "session_token_usage.sqlite3").exists()


@pytest.mark.asyncio
async def test_model_usage_uses_agent_build_context_session_id(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.session_token_usage_rail import record_model_token_usage
    from jiuwenswarm.server.runtime.session import lifecycle, session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: tmp_path)
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_build_context=SimpleNamespace(session_id="sess_usage", role="leader")),
        extra={},
        inputs=SimpleNamespace(
            context_usage_request_id="build-context-call",
            context_usage_attribution={},
            conversation_id=None,
            response=SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 2}),
        ),
    )

    await record_model_token_usage(ctx)

    assert SessionTokenUsageLedger(tmp_path / "sess_usage", "sess_usage").snapshot()["revision"] == 1
