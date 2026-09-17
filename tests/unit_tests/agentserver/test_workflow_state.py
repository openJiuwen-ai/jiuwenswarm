"""WorkflowRunState unit tests — verify state transitions and delta computation."""

from __future__ import annotations

import pytest
from jiuwenswarm.agents.harness.team.handlers.workflow_state import (
    WorkflowRunState,
    WorkflowPhaseState,
    WorkflowAgentState,
    WorkflowAgentActivity,
    WorkflowProgress,
    PhasePlan,
)


_DEFAULT_RUN_ID = "wf_testrun00001"


def _make_progress(kind: str, **kwargs) -> WorkflowProgress:
    if "run_id" not in kwargs:
        kwargs["run_id"] = _DEFAULT_RUN_ID
    return WorkflowProgress(kind=kind, **kwargs)


class TestWorkflowRunStateLifecycle:
    """Scenario 1 & 5: workflow started -> phases -> agents -> completed."""

    @staticmethod
    def test_workflow_started_creates_run():
        progress = _make_progress("workflow_started", workflow_name="werewolf-game", text="start")
        state = WorkflowRunState()
        delta = state.apply(progress)
        assert state.id.startswith("wf_")
        assert state.name == "werewolf-game"
        assert state.status == "running"
        assert state.started_at is not None
        assert delta is not None
        assert delta["id"] == state.id
        assert delta["status"] == "running"

    @staticmethod
    def test_workflow_started_pre_populates_planned_phases():
        """Phases from META (already normalized to PhasePlan) are pre-created as planned."""
        phases_meta = [
            PhasePlan(title="发牌", description="分配身份"),
            PhasePlan(title="游戏进行"),
            PhasePlan(title="结算"),
        ]
        progress = _make_progress("workflow_started", workflow_name="werewolf-game", phases=phases_meta)
        state = WorkflowRunState()
        delta = state.apply(progress)
        assert len(state.phases) == 3
        assert state.phases[0].name == "发牌"
        assert state.phases[0].status == "planned"
        assert state.phases[0].description == "分配身份"
        assert state.phases[1].name == "游戏进行"
        assert state.phases[1].status == "planned"
        assert state.phases[1].description is None
        assert state.phases[2].name == "结算"
        assert state.phases[2].status == "planned"
        assert len(delta["phases"]) == 3
        assert all(p["status"] == "planned" for p in delta["phases"])

    @staticmethod
    def test_planned_phase_activated_on_agent_started():
        """A planned phase becomes running when an agent starts within it."""
        phases_meta = [PhasePlan(title="发牌"), PhasePlan(title="游戏进行")]
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test", phases=phases_meta))
        assert state.phases[0].status == "planned"
        assert len(state.phases) == 2

        delta = state.apply(_make_progress("agent_started", phase="发牌", label="dealer"))
        assert state.phases[0].status == "running"
        assert state.phases[1].status == "planned"
        assert delta["phases"][0]["name"] == "发牌"
        assert delta["phases"][0]["status"] == "running"

    @staticmethod
    def test_agent_started_creates_running_phase():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        progress = _make_progress("agent_started", phase="Night 1", label="agent-a")
        delta = state.apply(progress)
        assert len(state.phases) == 1
        assert state.phases[0].name == "Night 1"
        assert state.phases[0].status == "running"
        assert delta is not None
        assert delta["phases"][0]["id"] == state.phases[0].id

    @staticmethod
    def test_phase_started_event_ignored():
        """phase_started is no longer handled — ignored, no state change."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        delta = state.apply(_make_progress("phase_started", phase="Day Vote"))
        assert delta is None
        assert len(state.phases) == 0

    @staticmethod
    def test_agent_started_adds_agent_to_current_phase():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Night 1"))
        progress = _make_progress("agent_started", phase="Night 1", label="werewolf-kill", prompt="你是狼人")
        delta = state.apply(progress)
        assert state.phases[0].agent_count == 1
        assert len(state.phases[0].agents) == 1
        assert state.phases[0].agents[0].name == "werewolf-kill"
        assert state.phases[0].agents[0].prompt == "你是狼人"
        assert state.phases[0].agents[0].status == "running"
        assert state.agent_count == 1

    @staticmethod
    def test_agent_completed_updates_agent():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Night 1"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="werewolf-kill"))
        progress = _make_progress("agent_completed", phase="Night 1", label="werewolf-kill", outcome="击杀 Carol")
        delta = state.apply(progress)
        assert state.phases[0].agents[0].status == "completed"
        assert state.phases[0].agents[0].outcome == "击杀 Carol"
        assert state.completed_agent_count == 1
        assert state.phases[0].completed_agent_count == 1

    @staticmethod
    def test_agent_failed_marks_failed():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Night 1"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="witch-action"))
        progress = _make_progress("agent_failed", phase="Night 1", label="witch-action")
        delta = state.apply(progress)
        assert state.phases[0].agents[0].status == "failed"
        assert state.phases[0].agents[0].error is not None
        # failed is a terminal status — completed_agent_count counts all terminal agents
        assert state.phases[0].completed_agent_count == 1
        assert state.completed_agent_count == 1

    @staticmethod
    def test_phase_sealed_on_switch_to_next_phase():
        """A running phase is sealed to completed when an agent starts in the next phase."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="agent-a"))
        assert state.phases[0].status == "running"
        state.apply(_make_progress("agent_started", phase="Day 1", label="agent-b"))
        assert state.phases[0].status == "completed"
        assert state.phases[1].status == "running"

    @staticmethod
    def test_workflow_completed_marks_terminal():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Night 1"))
        state.apply(_make_progress("phase_completed", phase="Night 1"))
        progress = _make_progress("workflow_completed", text="done")
        delta = state.apply(progress)
        assert state.status == "completed"
        assert state.completed_at is not None
        assert state.is_terminal is True
        assert delta["status"] == "completed"

    @staticmethod
    def test_workflow_failed_marks_terminal_with_error():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Night 1"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="agent-1"))
        state.apply(_make_progress("agent_failed", phase="Night 1", label="agent-1"))
        progress = _make_progress("workflow_failed", text="error")
        delta = state.apply(progress)
        assert state.status == "failed"
        assert state.error is not None
        assert state.is_terminal is True

    @staticmethod
    def test_workflow_completed_finalizes_running_phases_and_agents():
        """All running phases and agents are marked completed on workflow_completed."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        # Phase 2 entered but no agent_completed events
        state.apply(_make_progress("phase", phase="Phase 2"))
        state.apply(_make_progress("agent_started", phase="Phase 2", label="agent-b"))
        state.apply(_make_progress("workflow_completed", text="done"))
        assert state.status == "completed"
        assert state.phases[0].status == "completed"
        assert state.phases[0].agents[0].status == "completed"
        assert state.phases[1].status == "completed"
        assert state.phases[1].agents[0].status == "completed"
        # teardown stamped both agents terminal — completed_agent_count counts them
        assert state.phases[0].completed_agent_count == 1
        assert state.phases[1].completed_agent_count == 1
        assert state.completed_agent_count == 2
        assert state.agent_count == 2

    @staticmethod
    def test_workflow_failed_finalizes_running_phases_and_agents():
        """All running phases and agents are marked failed on workflow_failed."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        state.apply(_make_progress("workflow_failed", text="error"))
        assert state.status == "failed"
        assert state.phases[0].status == "failed"
        assert state.phases[0].agents[0].status == "failed"
        # failed is terminal — teardown-stamped agent counts toward completed_agent_count
        assert state.phases[0].completed_agent_count == 1
        assert state.completed_agent_count == 1
        assert state.agent_count == 1

    @staticmethod
    def test_log_event_produces_delta_with_logs():
        """Log events produce a delta with ``logs`` at the same level as ``phases``."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        progress = _make_progress("log", text="some narration")
        delta = state.apply(progress)
        assert delta is not None
        assert "logs" in delta
        assert delta["logs"] == ["some narration"]
        assert "phases" not in delta  # log delta does not include phases
        assert len(state.logs) == 1

    @staticmethod
    def test_log_with_phase_and_label_stored_in_logs_only():
        """Log with phase + label is stored in self.logs only, not in agent activity."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        progress = _make_progress("log", phase="Phase 1", label="agent-a", text="thinking...")
        delta = state.apply(progress)
        assert delta is not None
        assert "logs" in delta
        assert delta["logs"] == ["thinking..."]
        agent = state.phases[0].agents[0]
        assert len(agent.activity) == 0  # log is not written to agent activity
        assert len(state.logs) == 1

    @staticmethod
    def test_log_with_phase_only_stored_in_logs():
        """Log with phase but no label is stored in self.logs only, not in agent activity."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        progress = _make_progress("log", phase="Phase 1", text="phase-level log")
        delta = state.apply(progress)
        assert delta is not None
        assert "logs" in delta
        assert delta["logs"] == ["phase-level log"]
        agent = state.phases[0].agents[0]
        assert len(agent.activity) == 0  # log is not written to agent activity
        assert len(state.logs) == 1

    @staticmethod
    def test_log_without_phase_stored_in_top_level_only():
        """Log without phase or label only stored in self.logs, delta includes logs."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        progress = _make_progress("log", text="orphan log")
        delta = state.apply(progress)
        assert delta is not None
        assert "logs" in delta
        assert delta["logs"] == ["orphan log"]
        assert len(state.logs) == 1
        assert state.logs[0] == "orphan log"

    @staticmethod
    def test_multiple_phases_and_agents():
        """Scenario 2: multi-phase workflow with multiple agents."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="game"))
        state.apply(_make_progress("phase", phase="Night 1"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="werewolf", prompt="狼人行动"))
        state.apply(_make_progress("agent_completed", phase="Night 1", label="werewolf", outcome="击杀"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="witch", prompt="女巫行动"))
        state.apply(_make_progress("agent_completed", phase="Night 1", label="witch", outcome="救人"))
        state.apply(_make_progress("phase_completed", phase="Night 1"))
        state.apply(_make_progress("phase", phase="Day 1 Vote"))
        state.apply(_make_progress("agent_started", phase="Day 1 Vote", label="alice-vote", prompt="投票"))
        assert len(state.phases) == 2
        assert state.phases[0].status == "completed"
        assert state.phases[0].agent_count == 2
        assert state.phases[1].status == "running"
        assert state.phases[1].agents[0].name == "alice-vote"
        assert state.agent_count == 3


class TestWorkflowRunStateDelta:
    """Verify delta only contains changed phase/agent objects."""

    @staticmethod
    def test_delta_contains_finalized_and_new_phase():
        """Entering a new phase via agent_started: delta includes finalized previous + new phase."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        progress = _make_progress("agent_started", phase="Phase 2", label="agent-b")
        delta = state.apply(progress)
        # Delta includes finalized Phase 1 + new Phase 2
        assert len(delta["phases"]) == 2
        assert delta["phases"][0]["name"] == "Phase 1"
        assert delta["phases"][0]["status"] == "completed"
        assert delta["phases"][1]["name"] == "Phase 2"
        assert delta["phases"][1]["status"] == "running"

    @staticmethod
    def test_delta_on_agent_completed_contains_updated_agent():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        progress = _make_progress("agent_completed", phase="Phase 1", label="agent-a", outcome="done")
        delta = state.apply(progress)
        assert len(delta["phases"]) == 1
        agent_in_delta = delta["phases"][0]["agents"][0]
        assert agent_in_delta["status"] == "completed"
        assert agent_in_delta["outcome"] == "done"


class TestWorkflowRunStateSerialization:
    """Scenario 6: checkpoint persist/restore round-trip."""

    @staticmethod
    def test_model_dump_and_restore():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a", prompt="prompt"))
        data = state.model_dump()
        restored = WorkflowRunState.model_validate(data)
        assert restored.id == state.id
        assert restored.name == state.name
        assert restored.status == state.status
        assert len(restored.phases) == 1
        assert len(restored.phases[0].agents) == 1

    @staticmethod
    def test_to_workflow_run_dict_returns_full_snapshot():
        """command.workflows returns complete WorkflowRun."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="game"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        snapshot = state.to_workflow_run_dict()
        assert snapshot["id"] == state.id
        assert snapshot["status"] == "running"
        assert len(snapshot["phases"]) == 1
        assert len(snapshot["phases"][0]["agents"]) == 1


class TestWorkflowRunStateTimestamps:
    """Verify timestamp and duration fields."""

    @staticmethod
    def test_started_at_set_on_workflow_started():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        assert state.started_at is not None

    @staticmethod
    def test_completed_at_and_duration_on_workflow_completed():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.started_at = "2026-06-04T10:00:00+08:00"
        progress = _make_progress("workflow_completed", text="done")
        state.apply(progress)
        assert state.completed_at is not None
        assert state.duration_ms is not None

    @staticmethod
    def test_agent_started_at_on_agent_started():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        assert state.phases[0].agents[0].started_at is not None

    @staticmethod
    def test_agent_completed_at_on_agent_completed():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Phase 1"))
        state.apply(_make_progress("agent_started", phase="Phase 1", label="agent-a"))
        state.phases[0].agents[0].started_at = "2026-06-04T10:00:08+08:00"
        state.apply(_make_progress("agent_completed", phase="Phase 1", label="agent-a", outcome="done"))
        assert state.phases[0].agents[0].completed_at is not None
        assert state.phases[0].agents[0].duration_ms is not None


class TestIDGeneration:
    """Verify ID generation: uuid for workflow, slug+seq for phase/agent."""

    @staticmethod
    def test_workflow_id_starts_with_wf():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        assert state.id.startswith("wf_")

    @staticmethod
    def test_phase_id_is_slug_with_sequence():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="a"))
        assert state.phases[0].id == "night-1-1"
        state.apply(_make_progress("agent_started", phase="Day Vote", label="b"))
        assert state.phases[1].id == "day-vote-2"

    @staticmethod
    def test_agent_id_is_slug_with_sequence():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        state.apply(_make_progress("phase", phase="Night 1"))
        state.apply(_make_progress("agent_started", phase="Night 1", label="werewolf-kill"))
        assert state.phases[0].agents[0].id == "werewolf-kill-1"
        state.apply(_make_progress("agent_started", phase="Night 1", label="werewolf-kill"))
        assert state.phases[0].agents[1].id == "werewolf-kill-2"

    @staticmethod
    def test_unknown_kind_returns_none():
        """Unknown kind values are ignored — delta is None."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="test"))
        progress = _make_progress("unknown_kind")
        delta = state.apply(progress)
        assert delta is None


class TestAgentIdResolution:
    """agent_id / correlation_id exact matching for same-label nodes."""

    @staticmethod
    def test_for_loop_same_label_completed_lands_on_correct_instance():
        """A 2nd loop iteration's completion matches its own agent_id, not the 1st."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="loop"))
        state.apply(_make_progress("agent_started", phase="p", label="x", agent_id="path/call:1"))
        state.apply(_make_progress("agent_started", phase="p", label="x", agent_id="path/call:2"))
        # Complete the 2nd node — must land on instance #2, leaving #1 running.
        state.apply(_make_progress("agent_completed", phase="p", label="x", agent_id="path/call:2", outcome="done2"))
        a1, a2 = state.phases[0].agents
        assert a1.status == "running"
        assert a2.status == "completed"
        assert a2.outcome == "done2"
        assert state.phases[0].completed_agent_count == 1

    @staticmethod
    def test_agent_session_multi_turn_each_unique_agent_id():
        """agent_session multi-turn: each turn gets a distinct agent_id + kind=agent + correlation_id."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="sess"))
        for i in (1, 2, 3):
            state.apply(_make_progress(
                "agent_started", phase="review", label="chat",
                agent_id=f"main/call:{i}", node_type="agent_session",
                correlation_id=f"review:chat:{i - 1}",
            ))
        assert len(state.phases[0].agents) == 3
        ids = [a.id for a in state.phases[0].agents]
        assert len(set(ids)) == 3
        assert all(a.kind == "agent" for a in state.phases[0].agents)
        assert [a.correlation_id for a in state.phases[0].agents] == [
            "review:chat:0", "review:chat:1", "review:chat:2",
        ]

    @staticmethod
    def test_human_session_multi_turn_correlation_id_locates_node():
        """human_session multi-turn: HUMAN_PROMPT/REPLIED locate the node by correlation_id (no phase)."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="h"))
        state.apply(_make_progress(
            "agent_started", phase="review", label="host",
            agent_id="main/call:5", node_type="human_session", correlation_id="review:host:0",
        ))
        state.apply(_make_progress(
            "agent_started", phase="review", label="host",
            agent_id="main/call:7", node_type="human_session", correlation_id="review:host:1",
        ))
        # HUMAN_PROMPT for turn 1 (no phase field) -> matches turn-1 node by correlation_id.
        state.apply(_make_progress(
            "human_prompt", label="host", correlation_id="review:host:1", prompt="ok?",
        ))
        a0, a1 = state.phases[0].agents
        assert a0.status == "running"
        assert a1.status == "waiting_for_human"
        assert a1.human_prompt == "ok?"
        assert a1.kind == "human"
        # HUMAN_REPLIED clears waiting, stores answer.
        state.apply(_make_progress(
            "human_replied", label="host", correlation_id="review:host:1", answer="yes",
        ))
        assert a1.status == "running"
        assert a1.human_reply == "yes"

    @staticmethod
    def test_human_prompt_and_replied_do_not_append_activity():
        """Human nodes never produce WorkflowAgentActivity (question/answer live on the node)."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="h"))
        state.apply(_make_progress(
            "agent_started", phase="review", label="host",
            agent_id="main/call:1", node_type="human_session", correlation_id="review:host:0",
        ))
        state.apply(_make_progress("human_prompt", label="host", correlation_id="review:host:0", prompt="q"))
        state.apply(_make_progress("human_replied", label="host", correlation_id="review:host:0", answer="a"))
        agent = state.phases[0].agents[0]
        assert agent.activity == []

    @staticmethod
    def test_legacy_event_without_agent_id_falls_back_to_last_non_terminal():
        """Old agent-core events (no agent_id) fall back to label + last non-terminal."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="legacy"))
        # No agent_id on any event (legacy).
        state.apply(_make_progress("agent_started", phase="p", label="x"))
        state.apply(_make_progress("agent_started", phase="p", label="x"))
        # The 1st is still running; completion lands on the last non-terminal (instance #2).
        state.apply(_make_progress("agent_completed", phase="p", label="x", outcome="done2"))
        a1, a2 = state.phases[0].agents
        assert a2.status == "completed"
        assert a2.outcome == "done2"

    @staticmethod
    def test_waiting_for_human_finalized_to_stopped_on_teardown():
        """finalize_if_running closes a waiting_for_human node to stopped (no perpetual spin)."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="h"))
        state.apply(_make_progress(
            "agent_started", phase="review", label="host",
            agent_id="main/call:1", node_type="human_session", correlation_id="review:host:0",
        ))
        state.apply(_make_progress("human_prompt", label="host", correlation_id="review:host:0", prompt="q"))
        agent = state.phases[0].agents[0]
        assert agent.status == "waiting_for_human"
        # Teardown without a reply.
        changed = state.finalize_if_running("stopped")
        assert changed is True
        assert agent.status == "stopped"
        assert state.status == "stopped"
        # stopped is terminal — the node now counts toward completed_agent_count
        assert state.phases[0].completed_agent_count == 1
        assert state.completed_agent_count == 1


class TestPhaseReuseAndJump:
    """Same-name phase card is reused; status may jump; counters accumulate."""

    @staticmethod
    def test_same_name_phase_reused_not_duplicated():
        """Re-entering a same-name phase reuses the one card, not a new one."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="iter"))
        state.apply(_make_progress("agent_started", phase="数据采集", label="a", agent_id="c:1"))
        assert len(state.phases) == 1
        # Round 2: same phase name arrives again -> reuse, not a new card.
        state.apply(_make_progress("agent_started", phase="数据采集", label="a", agent_id="c:2"))
        assert len(state.phases) == 1
        assert state.phases[0].status == "running"
        # Two agents accumulated on the same card.
        assert len(state.phases[0].agents) == 2

    @staticmethod
    def test_phase_status_jumps_completed_back_to_running():
        """A phase sealed to completed flips back to running when re-entered.

        A phase is sealed only when a *different-name* phase is entered. So the
        jump test interleaves a different-name phase to seal P, then re-enters P.
        """
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="iter"))
        state.apply(_make_progress("agent_started", phase="P", label="a", agent_id="c:1"))
        agent_a = None
        for ph in state.phases:
            for ag in ph.agents:
                if ag.name == "a":
                    agent_a = ag
        assert agent_a is not None and agent_a.status == "running"
        # Enter a different-name phase -> seals P to completed (finalizes running agents).
        state.apply(_make_progress("agent_started", phase="Q", label="b", agent_id="c:2"))
        p, q = state.phases
        assert p.status == "completed"
        assert q.status == "running"
        # agent1 (label=a) was running in P; sealing P finalized it to completed.
        assert agent_a.status == "completed"
        assert agent_a.completed_at is not None
        # Re-enter P (same name) -> jumps back to running, same card.
        state.apply(_make_progress("agent_started", phase="P", label="c", agent_id="c:3"))
        assert len(state.phases) == 2  # no new card
        assert p.status == "running"   # jumped back from completed
        # P now holds agent1 (completed) + agent3 (running).
        assert len(p.agents) == 2
        assert p.agents[0].status == "completed"  # agent1
        assert p.agents[1].status == "running"    # agent3
        # agent1 (label=a) still completed — the jump does not revive finalized agents.
        assert agent_a.status == "completed"
        assert agent_a.name == "a"

    @staticmethod
    def test_agent_count_accumulates_across_iterations():
        """agent_count / completed_agent_count keep accumulating on the reused card."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="iter"))
        # Round 1: 2 agents, both complete.
        state.apply(_make_progress("agent_started", phase="P", label="a", agent_id="c:1"))
        state.apply(_make_progress("agent_completed", phase="P", label="a", agent_id="c:1", outcome="o1"))
        state.apply(_make_progress("agent_started", phase="P", label="b", agent_id="c:2"))
        state.apply(_make_progress("agent_completed", phase="P", label="b", agent_id="c:2", outcome="o2"))
        assert state.phases[0].agent_count == 2
        assert state.phases[0].completed_agent_count == 2
        # Round 2: 1 more agent on the same card.
        state.apply(_make_progress("agent_started", phase="P", label="a", agent_id="c:3"))
        assert state.phases[0].agent_count == 3
        assert state.phases[0].completed_agent_count == 2  # round-2 agent still running
        state.apply(_make_progress("agent_completed", phase="P", label="a", agent_id="c:3", outcome="o3"))
        assert state.phases[0].agent_count == 3
        assert state.phases[0].completed_agent_count == 3  # accumulated

    @staticmethod
    def test_different_name_phase_still_seals_previous():
        """Switching to a different-name phase still seals the running previous one."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="iter"))
        state.apply(_make_progress("agent_started", phase="采集", label="a", agent_id="c:1"))
        state.apply(_make_progress("agent_started", phase="计算", label="b", agent_id="c:2"))
        assert state.phases[0].status == "completed"  # 采集 sealed
        assert state.phases[1].status == "running"    # 计算 active
        assert len(state.phases) == 2

    @staticmethod
    def test_completed_phase_can_jump_back_to_running_then_re_sealed():
        """A re-entered completed phase jumps to running; a later switch re-seals it."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="iter"))
        state.apply(_make_progress("agent_started", phase="P", label="a", agent_id="c:1"))
        state.apply(_make_progress("agent_started", phase="Q", label="b", agent_id="c:2"))  # seal P
        state.apply(_make_progress("agent_started", phase="P", label="c", agent_id="c:3"))  # jump P back
        p, q = state.phases
        assert p.status == "running"
        # Switch to a different name again -> P sealed again (still one card).
        state.apply(_make_progress("agent_started", phase="R", label="d", agent_id="c:4"))
        assert p.status == "completed"
        assert len(state.phases) == 3


class TestAgentOutcomeBackfill:
    """Outcome persistence when phase seal or agent_id mismatch occurs."""

    @staticmethod
    def test_agent_completed_backfills_outcome_after_phase_seal():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="seal"))
        state.apply(_make_progress("agent_started", phase="P1", label="worker", agent_id="engine:1"))
        state.apply(_make_progress("agent_started", phase="P2", label="next", agent_id="engine:2"))
        sealed = state.phases[0].agents[0]
        assert sealed.status == "completed"
        assert sealed.outcome is None
        # Seal stamps the worker terminal (completed) — derived counters reflect it,
        # so completed_agent_count already counts this node even before the outcome
        # backfill arrives.
        assert state.phases[0].completed_agent_count == 1
        state.apply(_make_progress(
            "agent_completed", phase="P1", label="worker", agent_id="engine:1", outcome="done",
        ))
        assert sealed.outcome == "done"
        assert state.phases[0].completed_agent_count == 1

    @staticmethod
    def test_agent_completed_backfills_when_agent_id_mismatch():
        """Slug id from agent_started without engine id + late agent_completed by label."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="mismatch"))
        state.apply(_make_progress("agent_started", phase="P", label="worker"))
        assert state.phases[0].agents[0].id == "worker-1"
        state.apply(_make_progress("agent_started", phase="Q", label="other", agent_id="engine:2"))
        sealed = state.phases[0].agents[0]
        assert sealed.status == "completed"
        assert sealed.outcome is None
        state.apply(_make_progress(
            "agent_completed", phase="P", label="worker", agent_id='[["call", 0]]', outcome="payload",
        ))
        assert sealed.outcome == "payload"
        assert state.phases[0].completed_agent_count == 1

    @staticmethod
    def test_duplicate_agent_completed_is_idempotent():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="dup"))
        state.apply(_make_progress("agent_started", phase="P", label="a", agent_id="c:1"))
        first = state.apply(_make_progress(
            "agent_completed", phase="P", label="a", agent_id="c:1", outcome="once",
        ))
        assert first is not None
        assert state.phases[0].completed_agent_count == 1
        second = state.apply(_make_progress(
            "agent_completed", phase="P", label="a", agent_id="c:1", outcome="twice",
        ))
        assert second is None
        assert state.phases[0].agents[0].outcome == "once"
        assert state.phases[0].completed_agent_count == 1


class TestAgentNodeType:
    """Explicit node_type from AGENT_STARTED is persisted on WorkflowAgentState."""

    @staticmethod
    def test_agent_started_persists_node_type():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="types"))
        state.apply(_make_progress(
            "agent_started",
            phase="P1",
            label="coder",
            node_type="agent_session",
            correlation_id="P1:coder:0",
        ))
        agent = state.phases[0].agents[0]
        assert agent.node_type == "agent_session"
        assert agent.to_dict()["node_type"] == "agent_session"

    @staticmethod
    def test_agent_started_without_node_type_remains_optional():
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="legacy"))
        state.apply(_make_progress("agent_started", phase="P1", label="legacy-agent"))
        agent = state.phases[0].agents[0]
        assert agent.node_type is None
        assert "node_type" not in agent.to_dict()

    @staticmethod
    def test_restored_agent_state_keeps_node_type():
        raw = WorkflowAgentState(
            id="sess-1",
            name="reviewer",
            node_type="human_session",
            kind="human",
            correlation_id="P1:reviewer:0",
        )
        assert raw.model_dump(exclude_none=True)["node_type"] == "human_session"

    @staticmethod
    def test_kind_derived_from_node_type():
        """kind is derived from node_type (no is_human): human/human_session -> human, else agent."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="kinds"))
        cases = [
            ("agent", "agent"),
            ("agent_session", "agent"),
            ("human", "human"),
            ("human_session", "human"),
        ]
        for i, (nt, expected_kind) in enumerate(cases):
            state.apply(_make_progress(
                "agent_started", phase="P1", label=f"n{i}",
                agent_id=f"main/call:{i}", node_type=nt,
                correlation_id=f"P1:n{i}:0",
            ))
            agent = state.phases[0].agents[i]
            assert agent.node_type == nt, f"node_type {nt} not persisted"
            assert agent.kind == expected_kind, f"node_type={nt} should derive kind={expected_kind}, got {agent.kind}"

    @staticmethod
    def test_kind_defaults_to_agent_when_node_type_missing():
        """Legacy AGENT_STARTED with node_type=None derives kind=agent (pre-change default)."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="legacy"))
        state.apply(_make_progress("agent_started", phase="P1", label="legacy-agent"))
        agent = state.phases[0].agents[0]
        assert agent.node_type is None
        assert agent.kind == "agent"

    @staticmethod
    def test_label_less_human_node_falls_back_to_human_not_agent():
        """A ``human()`` / ``human_session()`` node started with no label surfaces
        as "unnamed human" (not the misleading bare "agent") so the TUI
        pending-reply list is readable. Plain ``agent()`` falls back to
        "unnamed agent". The ``unnamed `` prefix marks placeholder values."""
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="nolabel"))
        state.apply(_make_progress(
            "agent_started", phase="P1", agent_id="main/call:0",
            node_type="human", correlation_id="P1::0",
        ))
        state.apply(_make_progress(
            "agent_started", phase="P1", agent_id="main/call:1",
            node_type="human_session", correlation_id="P1::1",
        ))
        state.apply(_make_progress(
            "agent_started", phase="P1", agent_id="main/call:2",
        ))
        agents = state.phases[0].agents
        assert agents[0].name == "unnamed human"
        assert agents[0].kind == "human"
        assert agents[1].name == "unnamed human"
        assert agents[1].kind == "human"
        assert agents[2].name == "unnamed agent"
        assert agents[2].kind == "agent"

    @staticmethod
    def test_phase_less_node_falls_back_to_unnamed_phase():
        """A node whose event carries no ``phase`` lands on a placeholder phase
        card named "unnamed phase" (replacing the cryptic "?"). All phase-less
        nodes share that one card — same-name phases are reused."""
        from jiuwenswarm.agents.harness.team.handlers.workflow_state import _UNNAMED_PHASE
        state = WorkflowRunState()
        state.apply(_make_progress("workflow_started", workflow_name="nophase"))
        state.apply(_make_progress(
            "agent_started", label="coder", agent_id="main/call:0", node_type="agent",
        ))
        state.apply(_make_progress(
            "agent_started", label="reviewer", agent_id="main/call:1", node_type="human",
            correlation_id="nophase:reviewer:0",
        ))
        assert len(state.phases) == 1
        assert state.phases[0].name == _UNNAMED_PHASE == "unnamed phase"
        assert state.phases[0].status == "running"
        assert [a.name for a in state.phases[0].agents] == ["coder", "reviewer"]
        assert state.phases[0].agent_count == 2


# ---------------------------------------------------------------------------
# Task 11: Monitor 状态模型加字段
# ---------------------------------------------------------------------------

def test_workflow_progress_accepts_new_fields():
    p = WorkflowProgress(kind="agent_completed", phase="review", tokens=12700,
                         budget={"total": 5, "spent": 5, "remaining": 0, "scope": "leader", "exhausted": True},
                         phase_type="child", nested_phase="intro")
    assert p.tokens == 12700
    assert p.budget["exhausted"] is True
    assert p.phase_type == "child"


def test_workflow_phase_state_carries_child_meta():
    ph = WorkflowPhaseState(id="p1", name="▸ intro #0", phase_type="child", parent_phase="Phase1")
    d = ph.to_dict()
    assert d["phase_type"] == "child"
    assert d["parent_phase"] == "Phase1"


def test_workflow_run_state_has_budget():
    r = WorkflowRunState(id="wf_1", name="onboarding")
    r.budget = {"total": 500000, "spent": 412340, "remaining": 87660, "scope": "leader", "exhausted": False}
    d = r.to_workflow_run_dict()
    assert d["budget"]["remaining"] == 87660


# ---------------------------------------------------------------------------
# Task 12: _on_phase 分叉 + child 建卡
# ---------------------------------------------------------------------------

def test_child_phase_declared_builds_card(monkeypatch):
    r = WorkflowRunState(id="wf_1", name="onboarding")
    p = WorkflowProgress(kind="phase", phase="intro", phase_type="child", nested_phase="▸ intro #0")
    r._on_phase(p)
    assert len(r.phases) == 1
    assert r.phases[0].name == "▸ intro #0"
    assert r.phases[0].phase_type == "child"
    assert r.phases[0].agents == []


def test_author_phase_not_child_does_not_build_card():
    r = WorkflowRunState(id="wf_1", name="onboarding")
    p = WorkflowProgress(kind="phase", phase="review")  # no phase_type
    ret = r._on_phase(p)
    assert ret is None
    assert len(r.phases) == 0


def test_concurrent_same_name_child_cards_not_reused():
    r = WorkflowRunState(id="wf_1", name="onboarding")
    for nm in ["▸ intro #0", "▸ intro #1", "▸ intro #2"]:
        r._on_phase(WorkflowProgress(kind="phase", phase="intro", phase_type="child", nested_phase=nm))
    assert len(r.phases) == 3
    assert [p.name for p in r.phases] == ["▸ intro #0", "▸ intro #1", "▸ intro #2"]


def test_agent_started_hits_existing_child_card():
    r = WorkflowRunState(id="wf_1", name="onboarding")
    r._on_phase(WorkflowProgress(kind="phase", phase="intro", phase_type="child", nested_phase="▸ intro #0"))
    r._on_agent_started(WorkflowProgress(kind="agent_started", phase="greeter", label="greeter", agent_id="k1", node_type="agent", nested_phase="▸ intro #0"))
    assert len(r.phases) == 1
    assert r.phases[0].agents[0].name == "greeter"


# ---------------------------------------------------------------------------
# Task 13: _finalize_agent/_on_workflow_failed 写 token/budget + delta emit
# ---------------------------------------------------------------------------

def test_finalize_agent_writes_token_count_and_budget():
    r = WorkflowRunState(id="wf_1", name="review")
    ph = WorkflowPhaseState(id="p1", name="review")
    ph.agents.append(WorkflowAgentState(id="k1", name="analyst", status="running"))
    r.phases.append(ph)
    r._on_agent_started(WorkflowProgress(kind="agent_started", phase="review", label="analyst", agent_id="k1", node_type="agent"))
    r._on_agent_completed(WorkflowProgress(kind="agent_completed", phase="review", label="analyst", agent_id="k1", outcome="ok", tokens=12700,
                                           budget={"total": 500000, "spent": 412340, "remaining": 87660, "scope": "leader", "exhausted": False}))
    # _resolve_agent matches the first agent by agent_id (the pre-appended one)
    assert ph.agents[0].token_count == 12700
    assert r.token_count == 12700
    assert r.budget["spent"] == 412340


def test_workflow_failed_freezes_budget():
    r = WorkflowRunState(id="wf_1", name="credit-review")
    r._on_workflow_failed(WorkflowProgress(kind="workflow_failed", text="boom",
                                           budget={"total": 500000, "spent": 500000, "remaining": 0, "scope": "leader", "exhausted": True}))
    assert r.budget["exhausted"] is True
    assert r.status == "failed"