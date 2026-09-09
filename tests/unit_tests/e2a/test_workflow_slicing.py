# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for swarmflow-workflow-slicing: four-layer paging + agent field parts.

Covers the NEW command.workflows protocol (list/get_workflow/get_phase/get_agent)
and the _{field}_parts reassembly for oversized agent fields. These tests are
written Red-first against the spec (swarmflow-workflow-snapshot-paging).
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server import wire_truncate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_workflow(run_id: str = "wf_1", phase_count: int = 3, agents_per_phase: int = 5) -> dict[str, Any]:
    """Build a synthetic workflow dict mimicking WorkflowRunState.to_workflow_run_dict."""
    phases = []
    for pi in range(phase_count):
        agents = []
        for ai in range(agents_per_phase):
            agents.append({
                "id": f"agent_{pi}_{ai}",
                "name": f"worker_{ai}",
                "status": "completed",
                "kind": "agent",
                "model": "gpt-test",
                "prompt": f"prompt-{pi}-{ai}",
                "outcome": f"outcome-{pi}-{ai}",
                "token_count": 100,
            })
        phases.append({
            "id": f"phase_{pi}",
            "name": f"phase-{pi}",
            "status": "completed",
            "agent_count": agents_per_phase,
            "completed_agent_count": agents_per_phase,
            "phase_type": "child" if pi > 0 else None,
            "parent_phase": "root" if pi > 0 else None,
            "agents": agents,
        })
    return {
        "id": run_id,
        "name": "test-flow",
        "summary": "a test workflow",
        "status": "completed",
        "agent_count": phase_count * agents_per_phase,
        "completed_agent_count": phase_count * agents_per_phase,
        "started_at": "2026-08-19T10:00:00+08:00",
        "phases": phases,
        "logs": ["log-1", "log-2"],
        "budget": {"total": 1000, "spent": 500, "remaining": 500, "scope": "leader", "exhausted": False},
    }


# ---------------------------------------------------------------------------
# list paging
# ---------------------------------------------------------------------------

class TestListPaging:
    def test_list_default_paging(self) -> None:
        workflows = [_make_workflow(f"wf_{i}", phase_count=1, agents_per_phase=1) for i in range(80)]
        payload = wire_truncate._build_workflow_list_payload(
            workflows, session_id="sess", offset=0, limit=50, total=len(workflows)
        )
        assert payload["action"] == "list"
        assert len(payload["workflows"]) == 50
        assert payload["total"] == 80
        assert payload["has_more"] is True

    def test_list_second_page(self) -> None:
        workflows = [_make_workflow(f"wf_{i}") for i in range(80)]
        payload = wire_truncate._build_workflow_list_payload(
            workflows, session_id="sess", offset=50, limit=50, total=80
        )
        assert len(payload["workflows"]) == 30
        assert payload["has_more"] is False
        assert payload["workflows"][0]["id"] == "wf_50"

    def test_list_summary_has_no_phases_and_detail_pending(self) -> None:
        workflows = [_make_workflow()]
        payload = wire_truncate._build_workflow_list_payload(
            workflows, session_id="sess", offset=0, limit=50, total=1
        )
        item = payload["workflows"][0]
        assert "phases" not in item
        assert item["detail_pending"] is True
        assert item["id"] == "wf_1"
        assert "budget" in item  # budget kept on summary

    def test_list_clamps_limit_to_max(self) -> None:
        workflows = [_make_workflow(f"wf_{i}") for i in range(300)]
        payload = wire_truncate._build_workflow_list_payload(
            workflows, session_id="sess", offset=0, limit=10_000, total=300
        )
        # limit clamped to _WORKFLOW_LIST_MAX_LIMIT (200)
        assert len(payload["workflows"]) == 200


# ---------------------------------------------------------------------------
# get_workflow paging (phase summaries)
# ---------------------------------------------------------------------------

class TestGetWorkflowPaging:
    def test_get_workflow_default_phase_page(self) -> None:
        wf = _make_workflow("wf_1", phase_count=35, agents_per_phase=2)
        payload = wire_truncate._build_workflow_detail_paginated(
            wf, session_id="sess", phase_offset=0, phase_limit=20
        )
        assert payload["action"] == "get_workflow"
        assert payload["phase_total"] == 35
        assert payload["has_more"] is True
        assert len(payload["workflow"]["phases"]) == 20
        phase = payload["workflow"]["phases"][0]
        assert "agents" not in phase
        assert phase["detail_pending"] is True
        assert phase["id"] == "phase_0"

    def test_get_workflow_second_phase_page(self) -> None:
        wf = _make_workflow("wf_1", phase_count=35, agents_per_phase=2)
        payload = wire_truncate._build_workflow_detail_paginated(
            wf, session_id="sess", phase_offset=20, phase_limit=20
        )
        assert len(payload["workflow"]["phases"]) == 15
        assert payload["has_more"] is False

    def test_get_workflow_meta_has_no_phases_in_top_meta(self) -> None:
        """workflow meta carries run-level fields; phases are the paged slice."""
        wf = _make_workflow("wf_1", phase_count=5, agents_per_phase=2)
        payload = wire_truncate._build_workflow_detail_paginated(
            wf, session_id="sess", phase_offset=0, phase_limit=20
        )
        w = payload["workflow"]
        assert w["id"] == "wf_1"
        assert w["status"] == "completed"
        assert w["name"] == "test-flow"
        # meta may carry budget/logs but phases is the paged slice (len <= phase_limit)
        assert len(w["phases"]) == 5


# ---------------------------------------------------------------------------
# get_phase paging (agent summaries — no heavy text fields)
# ---------------------------------------------------------------------------

class TestGetPhasePaging:
    def test_get_phase_default_agent_page(self) -> None:
        wf = _make_workflow("wf_1", phase_count=2, agents_per_phase=80)
        payload = wire_truncate._build_phase_detail_paginated(
            wf, session_id="sess", phase_id="phase_0", agent_offset=0, agent_limit=50
        )
        assert payload["action"] == "get_phase"
        assert payload["agent_total"] == 80
        assert payload["has_more"] is True
        agents = payload["phase"]["agents"]
        assert len(agents) == 50
        # Summaries carry id/name/status and detail_pending, but NOT heavy text.
        assert agents[0]["id"] == "agent_0_0"
        assert agents[0]["detail_pending"] is True
        assert "prompt" not in agents[0]
        assert "outcome" not in agents[0]
        assert "human_prompt" not in agents[0]
        # A short outcome_preview is carried for the row stub.
        assert agents[0]["outcome_preview"] == "outcome-0-0"

    def test_get_phase_outcome_preview_truncates_long_text(self) -> None:
        long_outcome = "x" * 500  # exceeds 200-char preview cap
        agent = {
            "id": "a_long", "name": "n", "status": "completed", "kind": "agent",
            "outcome": long_outcome,
        }
        summary = wire_truncate._workflow_agent_summary(agent)
        assert "outcome" not in summary
        assert summary["outcome_preview"] == "x" * 200
        assert summary["detail_pending"] is True

    def test_get_phase_error_preview_when_agent_failed(self) -> None:
        agent = {
            "id": "a_err", "name": "n", "status": "failed", "kind": "agent",
            "error": "boom",
        }
        summary = wire_truncate._workflow_agent_summary(agent)
        assert "error" not in summary
        assert summary["error_preview"] == "boom"

    def test_get_phase_second_agent_page(self) -> None:
        wf = _make_workflow("wf_1", phase_count=2, agents_per_phase=80)
        payload = wire_truncate._build_phase_detail_paginated(
            wf, session_id="sess", phase_id="phase_0", agent_offset=50, agent_limit=50
        )
        assert len(payload["phase"]["agents"]) == 30
        assert payload["has_more"] is False
        assert payload["phase"]["agents"][0]["id"] == "agent_0_50"

    def test_get_phase_unknown_phase_returns_error(self) -> None:
        wf = _make_workflow("wf_1", phase_count=2, agents_per_phase=2)
        payload = wire_truncate._build_phase_detail_paginated(
            wf, session_id="sess", phase_id="missing", agent_offset=0, agent_limit=50
        )
        assert payload.get("ok") is False or "error" in payload
        assert "phase not found" in str(payload.get("error", "")).lower() or "not found" in str(payload).lower()


# ---------------------------------------------------------------------------
# get_agent (single agent full body)
# ---------------------------------------------------------------------------

class TestGetAgent:
    def test_get_agent_returns_full_body(self) -> None:
        wf = _make_workflow("wf_1", phase_count=2, agents_per_phase=3)
        payload = wire_truncate._build_agent_detail(
            wf, session_id="sess", phase_id="phase_0", agent_id="agent_0_1"
        )
        assert payload["action"] == "get_agent"
        agent = payload["agent"]
        assert agent["id"] == "agent_0_1"
        assert agent["prompt"] == "prompt-0-1"
        assert agent["outcome"] == "outcome-0-1"

    def test_get_agent_carries_human_prompt(self) -> None:
        wf = _make_workflow("wf_1", phase_count=1, agents_per_phase=1)
        wf["phases"][0]["agents"][0]["human_prompt"] = "please approve the plan"
        wf["phases"][0]["agents"][0]["status"] = "waiting_for_human"
        wf["phases"][0]["agents"][0]["kind"] = "human"
        payload = wire_truncate._build_agent_detail(
            wf, session_id="sess", phase_id="phase_0", agent_id="agent_0_0"
        )
        assert payload["agent"]["human_prompt"] == "please approve the plan"

    def test_get_agent_unknown_returns_error(self) -> None:
        wf = _make_workflow("wf_1", phase_count=1, agents_per_phase=1)
        payload = wire_truncate._build_agent_detail(
            wf, session_id="sess", phase_id="phase_0", agent_id="missing"
        )
        assert payload.get("ok") is False or "error" in payload


# ---------------------------------------------------------------------------
# agent field part splitting
# ---------------------------------------------------------------------------

class TestAgentFieldParts:
    def test_oversize_field_split_into_parts(self) -> None:
        big = "Q" * 100_000  # 100KB
        agent = {
            "id": "agent_big",
            "name": "asker",
            "status": "waiting_for_human",
            "kind": "human",
            "human_prompt": big,
            "prompt": "small",
        }
        out = wire_truncate._split_oversized_agent_fields(agent)
        assert "human_prompt" not in out
        assert "human_prompt_parts" in out
        parts = out["human_prompt_parts"]
        assert isinstance(parts, list)
        assert all("part_idx" in p and "total_parts" in p and "content" in p for p in parts)
        # reassemble
        sorted_parts = sorted(parts, key=lambda p: p["part_idx"])
        rejoined = "".join(p["content"] for p in sorted_parts)
        assert rejoined == big
        # small field untouched
        assert out["prompt"] == "small"
        assert "prompt_parts" not in out

    def test_small_fields_untouched(self) -> None:
        agent = {"id": "a", "name": "n", "prompt": "hi", "outcome": "ok"}
        out = wire_truncate._split_oversized_agent_fields(agent)
        assert out == agent

    def test_parts_boundary_is_char_safe(self) -> None:
        # multi-byte chars (中文) should not be split mid-character
        big = "你" * 50_000  # 150KB UTF-8, 50000 chars
        agent = {"id": "a", "human_prompt": big}
        out = wire_truncate._split_oversized_agent_fields(agent)
        parts = out["human_prompt_parts"]
        rejoined = "".join(p["content"] for p in sorted(parts, key=lambda p: p["part_idx"]))
        assert rejoined == big
        # each content slice is valid (decodable) — implicit by str join success


# ---------------------------------------------------------------------------
# deleted symbols must not exist
# ---------------------------------------------------------------------------

class TestDeletedSymbols:
    def test_human_prompt_helpers_removed(self) -> None:
        for name in (
            "_extract_waiting_human_prompts",
            "_restore_waiting_human_prompts",
            "_minimal_workflow_detail_preserving_waiting_human",
            "_build_workflow_human_prompt_payload",
            "_build_workflow_detail_payload",
            "_build_workflow_snapshot_payload",
            "_collapse_oversized_workflow_snapshot_item",
            "_minimal_workflow_snapshot_item_for_wire",
            "_sanitize_workflow_snapshot_item_for_wire",
            "_fit_workflow_detail_to_budget",
        ):
            assert not hasattr(wire_truncate, name), f"{name} should be deleted"

    def test_human_prompt_max_bytes_constant_removed(self) -> None:
        assert not hasattr(wire_truncate, "_WORKFLOW_WAITING_HUMAN_PROMPT_MAX_BYTES")
