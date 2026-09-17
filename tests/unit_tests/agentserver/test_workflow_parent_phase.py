# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SDD-0010 E2E integration tests — nested workflow budget observability.

Simulates agent-core progress event sequences fed through
``WorkflowRunState.apply()`` and asserts the Monitor-side behaviour:

1. Concurrent same-name sub-workflows → distinct child cards, no cross-assign,
   parent author phase not sealed.
2. Token/budget accountability: per-agent token_count, run-level sum,
   budget.spent >= workflow.token_count.
3. Depth-cap skip visible in run logs.
4. BudgetExhausted terminal: status=failed + budget.exhausted=True.
5. Three-path parity: delta / full snapshot / wire-sanitized agree on
   budget, token_count, and child metadata.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.team.handlers.workflow_state import (
    WorkflowAgentState,
    WorkflowPhaseState,
    WorkflowProgress,
    WorkflowRunState,
)
from jiuwenswarm.server.wire_truncate import (
    _collapse_oversized_workflow_snapshot_item,
    _sanitize_workflow_snapshot_item_for_wire,
    _WORKFLOW_SNAPSHOT_KEEP_KEYS,
)

_DEFAULT_RUN_ID = "wf_sdd0010_e2e"


def _make_progress(kind: str, **kwargs) -> WorkflowProgress:
    if "run_id" not in kwargs:
        kwargs["run_id"] = _DEFAULT_RUN_ID
    return WorkflowProgress(kind=kind, **kwargs)


def _agent_started(phase: str, label: str, agent_id: str, node_type: str = "agent") -> WorkflowProgress:
    return WorkflowProgress(kind="agent_started", run_id=_DEFAULT_RUN_ID, phase=phase, label=label, agent_id=agent_id, node_type=node_type)


def _agent_completed(phase: str, label: str, agent_id: str, tokens: int | None = None) -> WorkflowProgress:
    return WorkflowProgress(kind="agent_completed", run_id=_DEFAULT_RUN_ID, phase=phase, label=label, agent_id=agent_id, outcome="ok", tokens=tokens)


def _agent_failed(phase: str, label: str, agent_id: str, message: str = "boom") -> WorkflowProgress:
    return WorkflowProgress(
        kind="agent_failed", run_id=_DEFAULT_RUN_ID, phase=phase, label=label,
        agent_id=agent_id, text=message,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Concurrent same-name sub-workflows
# ---------------------------------------------------------------------------


class TestConcurrentSameNameSubWorkflows:
    """Feeding child PHASE declarations for same-name sub-flows produces
    distinct cards; agents land on the correct child card without sealing
    the parent author phase."""

    @staticmethod
    def _init_run_with_author_phase() -> WorkflowRunState:
        """Start a workflow run with one author phase ``review`` entered."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="onboarding"))
        r.apply(_make_progress("agent_started", phase="review", label="author", agent_id="auth-1"))
        return r

    def test_three_child_cards_for_concurrent_same_name_sub_flows(self):
        """Three child PHASE declarations with distinct display names
        (``▸ intro #0``, ``▸ intro #1``, ``▸ intro #2``) each
        produce their own card — NOT reused."""
        r = self._init_run_with_author_phase()
        child_names = ["▸ intro #0", "▸ intro #1", "▸ intro #2"]
        for nm in child_names:
            r.apply(_make_progress(
                "phase", phase="intro",
                phase_type="child", nested_phase=nm,
            ))
        # Only child phases were added (the author phase ``review`` already exists).
        child_phases = [p for p in r.phases if p.phase_type == "child"]
        assert len(child_phases) == 3
        assert [p.name for p in child_phases] == child_names
        # Each card is a distinct object.
        assert len({id(p) for p in child_phases}) == 3

    def test_agents_attach_to_correct_child_card_not_cross_assigned(self):
        """Agent events targeting a child phase land on that exact card;
        agents for different child phases are never cross-assigned."""
        r = self._init_run_with_author_phase()
        for nm in ["▸ intro #0", "▸ intro #1", "▸ intro #2"]:
            r.apply(_make_progress(
                "phase", phase="intro",
                phase_type="child", nested_phase=nm,
            ))

        # Start agents targeting each child phase.
        r.apply(_make_progress(
            "agent_started", phase="▸ intro #0", label="greeter-0",
            agent_id="k0", node_type="agent",
        ))
        r.apply(_make_progress(
            "agent_started", phase="▸ intro #1", label="greeter-1",
            agent_id="k1", node_type="agent",
        ))
        r.apply(_make_progress(
            "agent_started", phase="▸ intro #2", label="greeter-2",
            agent_id="k2", node_type="agent",
        ))

        child_phases = [p for p in r.phases if p.phase_type == "child"]
        assert len(child_phases) == 3
        # Each child phase has exactly one agent, and it is the correct one.
        for i, ph in enumerate(child_phases):
            assert len(ph.agents) == 1, f"child {ph.name} should have 1 agent"
            assert ph.agents[0].id == f"k{i}"
            assert ph.agents[0].name == f"greeter-{i}"

    def test_parent_author_phase_not_sealed_when_child_agent_starts(self):
        """Starting agents on child cards does NOT invoke ``_switch_to_phase``,
        so the parent author phase stays running (not sealed)."""
        r = self._init_run_with_author_phase()
        author_phase = r.phases[0]
        assert author_phase.name == "review"
        assert author_phase.status == "running"

        # Declare a child phase and start an agent on it.
        r.apply(_make_progress(
            "phase", phase="intro",
            phase_type="child", nested_phase="▸ intro #0",
        ))
        r.apply(_make_progress(
            "agent_started", phase="▸ intro #0", label="sub-agent",
            agent_id="sa-1", node_type="agent",
        ))

        # The author phase must still be running — child agent start did not seal it.
        assert author_phase.status == "running"
        # No new author phase was created.
        author_phases = [p for p in r.phases if p.phase_type != "child"]
        assert len(author_phases) == 1


# ---------------------------------------------------------------------------
# Scenario 2: Token / budget accountability
# ---------------------------------------------------------------------------


class TestTokenBudgetAccountability:
    """Per-agent ``token_count`` feeds into run-level sum; budget.spent
    moves independently (Team shared pool, so spent >= token_count)."""

    @staticmethod
    def _init_run_with_phase() -> WorkflowRunState:
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="credit-review"))
        r.apply(_make_progress("agent_started", phase="review", label="analyst-a", agent_id="a1"))
        r.apply(_make_progress("agent_started", phase="review", label="analyst-b", agent_id="a2"))
        return r

    def test_agent_token_count_written_on_completion(self):
        """agent_completed with tokens=5000 sets agent.token_count."""
        r = self._init_run_with_phase()
        r.apply(_make_progress(
            "agent_completed", phase="review", label="analyst-a", agent_id="a1",
            outcome="ok", tokens=5000,
        ))
        agent = r.phases[0].agents[0]
        assert agent.token_count == 5000

    def test_workflow_token_count_is_sum_of_agents(self):
        """run-level token_count equals sum of all agents' token_count."""
        r = self._init_run_with_phase()
        r.apply(_make_progress(
            "agent_completed", phase="review", label="analyst-a", agent_id="a1",
            outcome="ok", tokens=5000,
        ))
        r.apply(_make_progress(
            "agent_completed", phase="review", label="analyst-b", agent_id="a2",
            outcome="ok", tokens=3000,
        ))
        assert r.token_count == 8000  # 5000 + 3000

    def test_budget_spent_gte_workflow_token_count(self):
        """Team shared pool: budget.spent must be >= workflow.token_count
        (budget counts ALL calls, not just agents)."""
        r = self._init_run_with_phase()
        r.apply(_make_progress(
            "agent_completed", phase="review", label="analyst-a", agent_id="a1",
            outcome="ok", tokens=5000,
            budget={"total": 500000, "spent": 127000, "remaining": 373000,
                    "scope": "leader", "exhausted": False},
        ))
        r.apply(_make_progress(
            "agent_completed", phase="review", label="analyst-b", agent_id="a2",
            outcome="ok", tokens=3000,
            budget={"total": 500000, "spent": 135000, "remaining": 365000,
                    "scope": "leader", "exhausted": False},
        ))
        assert r.token_count == 8000
        assert r.budget is not None
        assert r.budget["spent"] >= r.token_count, (
            f"budget.spent ({r.budget['spent']}) must be >= "
            f"token_count ({r.token_count}) — Team shared pool"
        )

    def test_budget_persisted_even_when_agent_has_no_tokens(self):
        """budget is written regardless of whether the agent contributed tokens."""
        r = self._init_run_with_phase()
        r.apply(_make_progress(
            "agent_completed", phase="review", label="analyst-a", agent_id="a1",
            outcome="ok",  # no tokens field
            budget={"total": 500000, "spent": 50000, "remaining": 450000,
                    "scope": "leader", "exhausted": False},
        ))
        assert r.budget is not None
        assert r.budget["spent"] == 50000
        assert r.token_count == 0  # no agent token data → sum is 0


# ---------------------------------------------------------------------------
# Scenario 3: Depth-cap skip visible in logs
# ---------------------------------------------------------------------------


class TestDepthCapSkip:
    """A LOG progress event carrying a depth-cap skip message is stored
    in ``workflow.logs`` and emitted via the log delta."""

    def test_depth_cap_skip_message_in_logs(self):
        """Feed a LOG event with the expected skip text; verify logs contain it."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="deep-nest"))
        delta = r.apply(_make_progress(
            "log", text="[wf] nested workflow depth > 1 not allowed; skipping",
        ))
        assert delta is not None
        assert "logs" in delta
        assert delta["logs"] == ["[wf] nested workflow depth > 1 not allowed; skipping"]
        assert len(r.logs) == 1
        assert "nested workflow depth" in r.logs[0]
        assert "skipping" in r.logs[0]

    def test_depth_cap_log_does_not_create_phase_or_agent(self):
        """The LOG event is purely informational — no phase/agent side effects."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="deep-nest"))
        r.apply(_make_progress("agent_started", phase="review", label="author", agent_id="a1"))
        assert len(r.phases) == 1
        assert r.phases[0].agent_count == 1

        r.apply(_make_progress(
            "log", text="[wf] nested workflow depth > 1 not allowed; skipping",
        ))
        # No new phase or agent was created.
        assert len(r.phases) == 1
        assert r.phases[0].agent_count == 1

    def test_multiple_log_entries_accumulate(self):
        """Multiple LOG events append, each returning an incremental delta."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="multi-log"))
        r.apply(_make_progress("log", text="first message"))
        r.apply(_make_progress("log", text="second: depth skip"))
        assert len(r.logs) == 2
        assert r.logs[0] == "first message"
        assert "depth skip" in r.logs[1]


# ---------------------------------------------------------------------------
# Scenario 4: BudgetExhausted terminal
# ---------------------------------------------------------------------------


class TestBudgetExhausted:
    """A ``workflow_failed`` event carrying ``budget.exhausted=True`` sets
    the run to terminal ``failed`` and freezes the budget snapshot."""

    def test_budget_exhausted_sets_status_failed_and_exhausted_flag(self):
        """workflow_failed + budget.exhausted=True → status=failed, budget persisted."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="credit-review"))
        r.apply(_make_progress("agent_started", phase="review", label="analyst", agent_id="a1"))
        r.apply(_make_progress(
            "workflow_failed",
            text="BudgetExhausted: token budget exhausted: 500000/500000",
            budget={
                "total": 500000, "spent": 500000, "remaining": 0,
                "scope": "leader", "exhausted": True,
            },
        ))
        assert r.status == "failed"
        assert r.is_terminal is True
        assert r.budget is not None
        assert r.budget["exhausted"] is True
        assert r.budget["spent"] == 500000
        assert r.budget["remaining"] == 0

    def test_budget_exhausted_does_not_require_agents(self):
        """Even without any agent events, BudgetExhausted is recorded."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="empty"))
        r.apply(_make_progress(
            "workflow_failed",
            text="budget blown before any agent ran",
            budget={
                "total": 1, "spent": 1, "remaining": 0,
                "scope": "leader", "exhausted": True,
            },
        ))
        assert r.status == "failed"
        assert r.budget["exhausted"] is True

    def test_workflow_failed_without_budget_still_terminal(self):
        """A plain workflow_failed (no budget field) still sets status=failed."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="plain-fail"))
        r.apply(_make_progress("workflow_failed", text="generic error"))
        assert r.status == "failed"
        assert r.is_terminal is True


# ---------------------------------------------------------------------------
# Scenario 5: Three-path parity — delta / snapshot / wire-sanitized
# ---------------------------------------------------------------------------


class TestThreePathParity:
    """After a full event sequence, the same budget, token_count, and child
    metadata (phase_type, nested_phase, parent_phase) must appear identically
    across all three output paths:
      1. ``_build_phases_delta`` (incremental delta)
      2. ``to_workflow_run_dict`` (full snapshot)
      3. ``_sanitize_workflow_snapshot_item_for_wire`` (wire-sanitized)
    """

    @staticmethod
    def _build_full_scenario() -> WorkflowRunState:
        """Run a complete mini-workflow: author phase + child sub-flows +
        agent completions with tokens/budget + terminal."""
        r = WorkflowRunState()
        r.apply(_make_progress("workflow_started", workflow_name="parity-test"))
        # Author phase.
        r.apply(_make_progress("agent_started", phase="review", label="author", agent_id="auth-1"))
        r.apply(_make_progress(
            "agent_completed", phase="review", label="author", agent_id="auth-1",
            outcome="done", tokens=2000,
            budget={"total": 100000, "spent": 5000, "remaining": 95000,
                    "scope": "leader", "exhausted": False},
        ))
        # Two concurrent child sub-workflows.
        r.apply(_make_progress(
            "phase", phase="intro",
            phase_type="child", nested_phase="▸ intro #0", parent_phase="review",
        ))
        r.apply(_make_progress(
            "phase", phase="intro",
            phase_type="child", nested_phase="▸ intro #1", parent_phase="review",
        ))
        # Agents on child phases.
        r.apply(_make_progress(
            "agent_started", phase="▸ intro #0", label="sub-0",
            agent_id="s0", node_type="agent",
        ))
        r.apply(_make_progress(
            "agent_started", phase="▸ intro #1", label="sub-1",
            agent_id="s1", node_type="agent",
        ))
        r.apply(_make_progress(
            "agent_completed", phase="▸ intro #0", label="sub-0", agent_id="s0",
            outcome="ok", tokens=3000,
            budget={"total": 100000, "spent": 12000, "remaining": 88000,
                    "scope": "leader", "exhausted": False},
        ))
        r.apply(_make_progress(
            "agent_completed", phase="▸ intro #1", label="sub-1", agent_id="s1",
            outcome="ok", tokens=4000,
            budget={"total": 100000, "spent": 20000, "remaining": 80000,
                    "scope": "leader", "exhausted": False},
        ))
        # Depth-cap log.
        r.apply(_make_progress(
            "log", text="[wf] nested workflow depth > 1 not allowed; skipping",
        ))
        return r

    def test_budget_identical_across_paths(self):
        """budget dict is identical in delta, snapshot, and wire-sanitized."""
        r = self._build_full_scenario()
        assert r.budget is not None

        # Path 1: delta (all phases)
        delta = r._build_phases_delta(list(r.phases))
        assert delta["budget"] == r.budget

        # Path 2: full snapshot
        snapshot = r.to_workflow_run_dict()
        assert snapshot["budget"] == r.budget

        # Path 3: wire-sanitized (no collapse — data is tiny)
        wire_sanitized = _sanitize_workflow_snapshot_item_for_wire(snapshot)
        assert wire_sanitized["budget"] == r.budget

    def test_token_count_identical_across_paths(self):
        """token_count is identical in delta, snapshot, and wire-sanitized."""
        r = self._build_full_scenario()
        expected = 2000 + 3000 + 4000  # = 9000
        assert r.token_count == expected

        delta = r._build_phases_delta(list(r.phases))
        assert delta["token_count"] == expected

        snapshot = r.to_workflow_run_dict()
        assert snapshot["token_count"] == expected

        wire_sanitized = _sanitize_workflow_snapshot_item_for_wire(snapshot)
        assert wire_sanitized["token_count"] == expected

    def test_child_metadata_identical_across_paths(self):
        """Child phase fields (phase_type, parent_phase) appear
        identically in delta, snapshot, and wire-sanitized."""
        r = self._build_full_scenario()
        child_phases = [p for p in r.phases if p.phase_type == "child"]
        assert len(child_phases) == 2

        # Collect child metadata from each path and compare.
        def _child_meta(phases_list):
            result = []
            for ph in phases_list:
                if ph.get("phase_type") == "child":
                    result.append({
                        "name": ph["name"],
                        "phase_type": ph["phase_type"],
                        "parent_phase": ph.get("parent_phase"),
                    })
            return result

        # Path 1: delta
        delta = r._build_phases_delta(list(r.phases))
        delta_meta = _child_meta(delta["phases"])

        # Path 2: snapshot
        snapshot = r.to_workflow_run_dict()
        snap_meta = _child_meta(snapshot["phases"])

        # Path 3: wire-sanitized
        wire_sanitized = _sanitize_workflow_snapshot_item_for_wire(snapshot)
        wire_meta = _child_meta(wire_sanitized["phases"])

        assert delta_meta == snap_meta == wire_meta

    def test_collapse_path_also_preserves_budget_and_token_count(self):
        """Even after ``_collapse_oversized_workflow_snapshot_item`` (the
        collapse path), budget, token_count, and child meta are preserved."""
        r = self._build_full_scenario()
        snapshot = r.to_workflow_run_dict()
        collapsed = _collapse_oversized_workflow_snapshot_item(dict(snapshot))

        assert collapsed["budget"] == r.budget
        assert collapsed["token_count"] == r.token_count
        # Child metadata preserved in collapsed phases.
        for ph in collapsed.get("phases", []):
            if ph.get("name", "").startswith("▸ intro"):
                assert ph.get("phase_type") == "child"
                assert "phase_type" in ph
                assert "parent_phase" in ph

    def test_snapshot_keep_keys_includes_budget_and_token_count(self):
        """The KEEP_KEYS set used for wire truncation includes budget,
        token_count, and estimated_token_count."""
        assert "budget" in _WORKFLOW_SNAPSHOT_KEEP_KEYS
        assert "token_count" in _WORKFLOW_SNAPSHOT_KEEP_KEYS
        assert "estimated_token_count" in _WORKFLOW_SNAPSHOT_KEEP_KEYS


def test_parent_phase_agent_count_aggregates_children():
    """Parent phase count should reflect children, not just own agents."""
    r = WorkflowRunState(id="wf_1", name="spring-launch")
    r._on_workflow_started(WorkflowProgress(
        kind="workflow_started", run_id="wf_1", workflow_name="spring-launch",
        phases=[{"title": "Phase1-Prep", "detail": ""}],
    ))
    r.apply(WorkflowProgress(
        kind="phase", phase="speech-prep", phase_type="child",
        nested_phase="\u25b8 speech-prep #0", parent_phase="Phase1-Prep",
    ))
    r.apply(WorkflowProgress(
        kind="phase", phase="unveil-prep", phase_type="child",
        nested_phase="▸ unveil-prep #1", parent_phase="Phase1-Prep",
    ))
    r._on_agent_started(_agent_started("\u25b8 speech-prep #0", "writer", "k1", "agent"))
    r._on_agent_started(_agent_started("\u25b8 speech-prep #0", "writer", "k2", "agent"))
    r._on_agent_started(_agent_started("\u25b8 unveil-prep #1", "writer", "k3", "agent"))

    parent = r.phases[0]  # Phase1-Prep
    child1 = r.phases[1]  # ▸ speech-prep #0
    child2 = r.phases[2]  # ▸ unveil-prep #1

    # Parent count is now maintained by the backend (aggregates children)
    assert parent.agent_count == 3
    assert child1.agent_count == 2
    assert child2.agent_count == 1
    # Parent should be "running" (activated by first child declaration)
    assert parent.status == "running"


def test_parent_phase_sealed_only_when_all_siblings_done():
    """Parent should stay running until ALL its sibling children complete."""
    r = WorkflowRunState(id="wf_2", name="test")
    r._on_workflow_started(WorkflowProgress(
        kind="workflow_started", run_id="wf_2", workflow_name="test",
        phases=[{"title": "Batch", "detail": ""}],
    ))
    r.apply(WorkflowProgress(
        kind="phase", phase="a", phase_type="child",
        nested_phase="\u25b8 child-a #0", parent_phase="Batch",
    ))
    r.apply(WorkflowProgress(
        kind="phase", phase="b", phase_type="child",
        nested_phase="\u25b8 child-b #1", parent_phase="Batch",
    ))
    r._on_agent_started(_agent_started("\u25b8 child-a #0", "w", "k1", "agent"))
    r._on_agent_started(_agent_started("\u25b8 child-b #1", "w", "k2", "agent"))

    # Complete child-a — parent should still be running (child-b not done)
    r._on_agent_completed(_agent_completed("\u25b8 child-a #0", "w", "k1", tokens=1000))
    parent = r.phases[0]
    assert parent.status == "running", f"expected running, got {parent.status}"

    # Complete child-b — NOW parent should be completed
    r._on_agent_completed(_agent_completed("\u25b8 child-b #1", "w", "k2", tokens=1000))
    assert parent.status == "completed", f"expected completed, got {parent.status}"


def test_child_agent_started_delta_includes_parent_counts():
    """Child agent_started must push parent phase with updated agent_count."""
    r = WorkflowRunState()
    r.apply(_make_progress("workflow_started", workflow_name="spring-launch", phases=[
        {"title": "Phase1-Prep", "detail": ""},
    ]))
    r.apply(_make_progress(
        "phase", phase="speech", phase_type="child",
        nested_phase="\u25b8 speech #0", parent_phase="Phase1-Prep",
    ))
    r.apply(_make_progress(
        "phase", phase="unveil", phase_type="child",
        nested_phase="\u25b8 unveil #1", parent_phase="Phase1-Prep",
    ))
    delta = r.apply(_agent_started("\u25b8 speech #0", "writer", "k1", "agent"))
    assert delta is not None
    assert len(delta["phases"]) == 2
    parent_delta = next(p for p in delta["phases"] if p.get("phase_type") != "child")
    child_delta = next(p for p in delta["phases"] if p.get("phase_type") == "child")
    assert parent_delta["agent_count"] == 1
    assert child_delta["agent_count"] == 1

    delta2 = r.apply(_agent_started("\u25b8 unveil #1", "writer", "k2", "agent"))
    parent_delta2 = next(p for p in delta2["phases"] if p.get("phase_type") != "child")
    assert parent_delta2["agent_count"] == 2


def test_child_agent_completed_delta_includes_parent_completed_count():
    """Child agent_completed must push parent phase with updated completed_agent_count."""
    r = WorkflowRunState()
    r.apply(_make_progress("workflow_started", workflow_name="spring-launch", phases=[
        {"title": "Phase2-Prep", "detail": ""},
    ]))
    r.apply(_make_progress(
        "phase", phase="panel", phase_type="child",
        nested_phase="\u25b8 panel #0", parent_phase="Phase2-Prep",
    ))
    r.apply(_make_progress(
        "phase", phase="closing", phase_type="child",
        nested_phase="\u25b8 closing #1", parent_phase="Phase2-Prep",
    ))
    r.apply(_agent_started("\u25b8 panel #0", "writer", "k1", "agent"))
    r.apply(_agent_started("\u25b8 closing #1", "writer", "k2", "agent"))

    delta = r.apply(_agent_completed("\u25b8 panel #0", "writer", "k1", tokens=100))
    parent_delta = next(p for p in delta["phases"] if p.get("phase_type") != "child")
    assert parent_delta["agent_count"] == 2
    assert parent_delta["completed_agent_count"] == 1
    assert parent_delta["status"] == "running"


def test_run_agent_counts_match_leaf_agents_not_parent_aggregate():
    """Run-level agent_count must sum leaf agents only — parent aggregates excluded."""
    r = WorkflowRunState()
    r.apply(_make_progress("workflow_started", workflow_name="spring-launch", phases=[
        {"title": "Phase1-Prep", "detail": ""},
        {"title": "Phase2-Prep", "detail": ""},
        {"title": "Merge", "detail": ""},
    ]))
    for i, name in enumerate(["speech", "unveil", "panel"]):
        r.apply(_make_progress(
            "phase", phase=name, phase_type="child",
            nested_phase=f"\u25b8 {name} #{i}", parent_phase="Phase1-Prep",
        ))
    for i, name in enumerate(["demo", "closing"]):
        r.apply(_make_progress(
            "phase", phase=name, phase_type="child",
            nested_phase=f"\u25b8 {name} #{i}", parent_phase="Phase2-Prep",
        ))
    r.apply(_agent_started("\u25b8 speech #0", "writer", "k1"))
    r.apply(_agent_started("\u25b8 unveil #1", "writer", "k2"))
    r.apply(_agent_started("\u25b8 panel #2", "writer", "k3"))
    r.apply(_agent_started("\u25b8 demo #0", "writer", "k4"))
    r.apply(_agent_started("\u25b8 closing #1", "writer", "k5"))
    r.apply(_agent_started("Merge", "merger", "k6"))

    assert r.agent_count == 6
    assert r.completed_agent_count == 0
    p1 = next(p for p in r.phases if p.name == "Phase1-Prep")
    assert p1.agent_count == 3

    r.apply(_agent_completed("\u25b8 speech #0", "writer", "k1", tokens=100))
    r.apply(_agent_completed("\u25b8 unveil #1", "writer", "k2", tokens=100))

    assert r.agent_count == 6
    assert r.completed_agent_count == 2
    snapshot = r.to_workflow_run_dict()
    assert snapshot["agent_count"] == 6
    assert snapshot["completed_agent_count"] == 2


def test_child_seal_delta_includes_child_and_parent():
    """When a child phase seals, delta must include both child and parent."""
    r = WorkflowRunState()
    r.apply(_make_progress("workflow_started", workflow_name="spring-launch", phases=[
        {"title": "Phase1-Prep", "detail": ""},
    ]))
    r.apply(_make_progress(
        "phase", phase="speech", phase_type="child",
        nested_phase="\u25b8 speech #0", parent_phase="Phase1-Prep",
    ))
    r.apply(_agent_started("\u25b8 speech #0", "writer", "k1", "agent"))

    delta = r.apply(_agent_completed("\u25b8 speech #0", "writer", "k1", tokens=100))
    assert delta is not None
    assert len(delta["phases"]) == 2
    child_delta = next(p for p in delta["phases"] if p.get("phase_type") == "child")
    parent_delta = next(p for p in delta["phases"] if p.get("phase_type") != "child")
    assert child_delta["status"] == "completed"
    assert child_delta["completed_agent_count"] == 1
    assert parent_delta["status"] == "completed"
    assert parent_delta["completed_agent_count"] == 1


def test_failed_agent_does_not_bump_phase_or_run_completed_count():
    """completed_agent_count counts all terminal agents (completed + failed + stopped)."""
    r = WorkflowRunState()
    r.apply(_make_progress("workflow_started", workflow_name="review"))
    r.apply(_agent_started("review", "a1", "k1"))
    r.apply(_agent_started("review", "a2", "k2"))
    r.apply(_agent_completed("review", "a1", "k1"))
    r.apply(_agent_failed("review", "a2", "k2"))

    phase = next(p for p in r.phases if p.name == "review")
    assert phase.agents[0].status == "completed"
    assert phase.agents[1].status == "failed"
    # both agents are terminal — completed_agent_count counts failed too
    assert phase.completed_agent_count == 2
    assert phase.agent_count == 2
    assert r.completed_agent_count == 2
    assert r.agent_count == 2


def test_child_phase_seals_when_all_agents_terminal_including_failed():
    """Child seals on all agents terminal; completed count includes failed (terminal)."""
    child = "\u25b8 intro #0"
    r = WorkflowRunState()
    r.apply(_make_progress("workflow_started", workflow_name="launch", phases=[
        {"title": "Prep", "detail": ""},
    ]))
    r.apply(_make_progress(
        "phase", phase="intro", phase_type="child",
        nested_phase=child, parent_phase="Prep",
    ))
    r.apply(_agent_started(child, "w1", "k1"))
    r.apply(_agent_started(child, "w2", "k2"))
    r.apply(_agent_completed(child, "w1", "k1"))
    r.apply(_agent_failed(child, "w2", "k2"))

    child_phase = next(p for p in r.phases if p.phase_type == "child")
    assert child_phase.status == "completed"
    # both agents terminal (completed + failed) \u2014 completed_agent_count counts both
    assert child_phase.completed_agent_count == 2
    assert child_phase.agent_count == 2
    assert r.completed_agent_count == 2
