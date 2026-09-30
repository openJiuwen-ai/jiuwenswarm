# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ExperimentIntegrityRail lifecycle capture.

``test_tool_capture``: the rail observes tracked tool calls and model usage
without altering them, and persists one invoke record per session into
``<manifest_dir>/sessions/``. ``test_artifact_lineage`` lives in
``test_lineage_tools.py`` (run-level chain) — here the rail's job is only
to record which tools really ran and link the run ids they returned.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from jiuwenswarm.research_integrity.rail import (
    DEFAULT_TRACKED_TOOLS,
    EXTRA_STATE_KEY,
    ExperimentIntegrityRail,
    RESEARCH_STAGE_KEY,
)


def _make_rail(tmp_path: Path, **kwargs: object) -> ExperimentIntegrityRail:
    return ExperimentIntegrityRail(
        project_root=tmp_path,
        manifest_dir=tmp_path / ".jiuwen" / "research_integrity",
        **kwargs,  # type: ignore[arg-type]
    )


def _ctx(
    *,
    extra: dict | None = None,
    session_id: str = "sess-1",
    tool_name: str = "",
    tool_args: object = None,
    tool_result: object = None,
    tool_call_id: str = "call-1",
    exception: Exception | None = None,
    response: object = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        extra=extra if extra is not None else {},
        session=types.SimpleNamespace(get_session_id=lambda: session_id),
        inputs=types.SimpleNamespace(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            tool_call=types.SimpleNamespace(id=tool_call_id),
            response=response,
        ),
        exception=exception,
    )


@pytest.mark.asyncio
async def test_tool_capture_records_tracked_tool_call(tmp_path: Path) -> None:
    """before/after tool call -> one event with stage, status and run_id."""
    rail = _make_rail(tmp_path)
    extra: dict = {RESEARCH_STAGE_KEY: "experiment"}
    ctx = _ctx(
        extra=extra,
        tool_name="bash",
        tool_args={"command": "python evaluate.py --seed 42"},
        tool_result=json.dumps({"run_id": "run_abc", "exit_code": 0}),
    )

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    state = extra[EXTRA_STATE_KEY]
    (event,) = state["tool_events"]
    assert event["tool_name"] == "bash"
    assert event["stage"] == "experiment"
    assert event["status"] == "ok"
    assert event["run_id"] == "run_abc"
    assert "python evaluate.py" in event["tool_args"]
    assert state["linked_run_ids"] == ["run_abc"]
    assert state["usage_totals"]["tool_events"] == 1


@pytest.mark.asyncio
async def test_tool_capture_redacts_common_credentials(tmp_path: Path) -> None:
    """Nested fields and common command-line credentials never persist raw."""
    rail = _make_rail(tmp_path)
    extra: dict = {RESEARCH_STAGE_KEY: "experiment"}
    ctx = _ctx(
        extra=extra,
        tool_name="bash",
        tool_args={
            "api_key": "key-should-not-survive",
            "headers": {"Authorization": "Bearer bearer-should-not-survive"},
            "command": (
                "API_SECRET=env-should-not-survive python evaluate.py "
                "--token cli-should-not-survive"
            ),
        },
        tool_result={
            "run_id": "run_redacted",
            "access_token": "result-should-not-survive",
        },
    )

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    serialized = json.dumps(extra[EXTRA_STATE_KEY], ensure_ascii=False)
    for secret in (
        "key-should-not-survive",
        "bearer-should-not-survive",
        "env-should-not-survive",
        "cli-should-not-survive",
        "result-should-not-survive",
    ):
        assert secret not in serialized
    assert serialized.count("<redacted>") >= 5
    assert extra[EXTRA_STATE_KEY]["linked_run_ids"] == ["run_redacted"]


@pytest.mark.asyncio
async def test_tool_capture_ignores_untracked_tool(tmp_path: Path) -> None:
    """A tool outside tracked_tools leaves no provenance event."""
    rail = _make_rail(tmp_path)
    assert "web_search" not in DEFAULT_TRACKED_TOOLS
    extra: dict = {RESEARCH_STAGE_KEY: "experiment"}
    ctx = _ctx(extra=extra, tool_name="web_search", tool_result="ok")

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert extra[EXTRA_STATE_KEY]["tool_events"] == []


@pytest.mark.asyncio
async def test_stage_filter_restricts_capture(tmp_path: Path) -> None:
    """tracked_stages gates which stages are provenance events."""
    rail = _make_rail(
        tmp_path, tracked_stages={"experiment", "ablation"}
    )
    extra: dict = {RESEARCH_STAGE_KEY: "literature_review"}
    ctx = _ctx(extra=extra, tool_name="bash", tool_result="ok")

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)

    assert extra[EXTRA_STATE_KEY]["tool_events"] == []


@pytest.mark.asyncio
async def test_tool_exception_recorded(tmp_path: Path) -> None:
    """A raising tool call is captured with status=exception."""
    rail = _make_rail(tmp_path)
    extra: dict = {RESEARCH_STAGE_KEY: "experiment"}
    ctx = _ctx(
        extra=extra,
        tool_name="bash",
        tool_args={"command": "boom"},
        exception=RuntimeError("boom"),
    )

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.on_tool_exception(ctx)

    state = extra[EXTRA_STATE_KEY]
    (event,) = state["tool_events"]
    assert event["status"] == "exception"
    assert "boom" in event["result_summary"]


@pytest.mark.asyncio
async def test_model_usage_recorded_not_results(tmp_path: Path) -> None:
    """after_model_call records tokens/model only, never content."""
    rail = _make_rail(tmp_path)
    extra: dict = {}
    response = types.SimpleNamespace(
        model="glm-5.3",
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        },
        content="the accuracy is 0.75",
    )
    ctx = _ctx(extra=extra, response=response)

    await rail.before_invoke(ctx)
    await rail.after_model_call(ctx)

    state = extra[EXTRA_STATE_KEY]
    (usage,) = state["model_usage"]
    assert usage == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "model": "glm-5.3",
    }
    assert state["usage_totals"]["model_calls"] == 1
    assert state["usage_totals"]["total_tokens"] == 150
    # Model-generated text never reaches the provenance record.
    assert "0.75" not in json.dumps(state)


@pytest.mark.asyncio
async def test_after_invoke_persists_session_record(tmp_path: Path) -> None:
    """after_invoke writes sessions/<id>.json and clears rail state."""
    rail = _make_rail(tmp_path)
    extra: dict = {RESEARCH_STAGE_KEY: "experiment"}
    ctx = _ctx(
        extra=extra,
        tool_name="bash",
        tool_args={"command": "python evaluate.py"},
        tool_result=json.dumps({"run_id": "run_xyz"}),
    )

    await rail.before_invoke(ctx)
    await rail.before_tool_call(ctx)
    await rail.after_tool_call(ctx)
    await rail.after_invoke(ctx)

    # State cleared from the rail context...
    assert EXTRA_STATE_KEY not in extra
    # ...and persisted with internal bookkeeping stripped.
    sessions_dir = rail.manifest_dir / "sessions"
    (record_path,) = list(sessions_dir.glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["session_id"] == "sess-1"
    assert record["project_root"] == rail.project_root
    assert record["linked_run_ids"] == ["run_xyz"]
    assert record["finished_at"]
    assert "_open_tool_calls" not in record
    assert record["usage_totals"]["tool_events"] == 1


@pytest.mark.asyncio
async def test_hooks_fail_safe_on_broken_context(tmp_path: Path) -> None:
    """No hook ever raises into the agent loop, whatever the context."""
    rail = _make_rail(tmp_path)

    broken = types.SimpleNamespace(extra=None)  # extra is not a dict
    await rail.before_invoke(broken)  # type: ignore[arg-type]
    await rail.before_tool_call(broken)  # type: ignore[arg-type]
    await rail.after_tool_call(broken)  # type: ignore[arg-type]
    await rail.on_tool_exception(broken)  # type: ignore[arg-type]
    await rail.after_model_call(broken)  # type: ignore[arg-type]
    await rail.after_invoke(broken)  # type: ignore[arg-type]
