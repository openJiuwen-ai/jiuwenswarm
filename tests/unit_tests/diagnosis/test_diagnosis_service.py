# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the diagnosis host service."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.diagnosis.engine import (
    EngineError,
    EngineEvent,
    EngineInputs,
    EngineResult,
)
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import (
    DiagnosisHostError,
    DiagnosisRunRequest,
    DiagnosisStatus,
    EvolutionJudge,
    FullScope,
    ModeProfile,
    ModelInfo,
    RewardFailureCause,
    SelectionRange,
    SelectionScope,
    StepRef,
)
from jiuwenswarm.diagnosis.snapshot import ScopeIndex, load_trajectory_snapshot, resolve_ranges
from jiuwenswarm.diagnosis.service import (
    PROGRESS_EVENT,
    STATUS_EVENT,
    DiagnosisService,
    derive_mode_profile,
    filter_engine_result,
)
from jiuwenswarm.diagnosis import spans as _spans
from jiuwenswarm.diagnosis.store import DiagnosisStore
from tests.unit_tests.diagnosis.stub_engine import StubDiagnosisEngine
from tests.unit_tests.diagnosis.diagnosis_fixtures import (
    SESSION_ID,
    TRACE_A,
    FakeReader,
    make_row,
    make_span,
    span_id,
)

_ENGINE_DONE_TIMEOUT = 5.0

# The span normalizer lazily imports the harness rails on first use; warm it up
# so the first diagnosis run is not charged for the cold import.
_spans._tool_result_has_error("warm-up")


class _Push:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, session_id: str, event: str, payload: dict[str, Any]) -> None:
        self.events.append((session_id, event, payload))

    def of(self, name: str) -> list[dict[str, Any]]:
        return [payload for _sid, event, payload in self.events if event == name]


class _GatedEngine:
    """Engine that stays in its first stage until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def run(self, inputs: EngineInputs, emit: Any) -> EngineResult:
        await emit(EngineEvent("evidence", "stage_started", title="Evidence"))
        self.started.set()
        await self.release.wait()
        await emit(EngineEvent("evidence", "stage_finished", status="completed"))
        return EngineResult()


def _ref(index: int, *, subject: str = "main", node: bool = True) -> StepRef:
    return StepRef(
        trace_id=TRACE_A,
        span_id=span_id(index),
        subject_id=subject,
        node_id=f"{subject}:{index - 1}" if node else None,
    )


def _cause(
    cause_id: str,
    *,
    dimension: str = "stub",
    index: int = 2,
    suggestion: tuple[str, ...] = ("fix it",),
) -> RewardFailureCause:
    return RewardFailureCause(
        id=cause_id,
        dimension=dimension,
        summary=cause_id,
        evidence=[_ref(index)],
        suggestion=list(suggestion),
    )


def _skill(judge_id: str, *, target: str | None = "general", index: int = 2) -> EvolutionJudge:
    return EvolutionJudge(
        id=judge_id,
        category="skill",
        summary=judge_id,
        evidence=[_ref(index)],
        suggestion=["advice"],
        score=0.4,
        target=target,
    )


def _experience(judge_id: str, *, outcome: str = "fail", index: int = 2) -> EvolutionJudge:
    return EvolutionJudge(
        id=judge_id,
        category="experience",
        summary=judge_id,
        evidence=[_ref(index)],
        suggestion=["tip"],
        score={"success": 1.0, "partial": 0.5, "fail": 0.0}[outcome],
        outcome=outcome,
    )


def _reward_payload(*requirements: str) -> dict[str, Any]:
    return {
        "schema_version": "attribution.reward.v1",
        "overall": {"comment": "ok", "total_score": 80},
        "categories": [],
        "dimensions": [
            {"category": "c", "requirement": requirement, "comment": "fix it", "score": 80}
            for requirement in requirements
        ],
    }


class _StreamingTablesEngine:
    """Engine that reports the two tables while its stage runs, then waits to be released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.reported = asyncio.Event()

    async def run(self, inputs: EngineInputs, emit: Any) -> EngineResult:
        await emit(EngineEvent("attribution", "stage_started", title="Attribution"))
        await emit(
            EngineEvent(
                "attribution",
                "reward_failure_causes",
                reward_failure_causes=[_cause("rfc-1"), _cause("rfc-out", index=99)],
            ),
        )
        await emit(EngineEvent("attribution", "evolution_judge", evolution_judge=[_skill("evo-1")]))
        # A resubmission replaces the whole table.
        await emit(
            EngineEvent(
                "attribution",
                "reward_failure_causes",
                reward_failure_causes=[_cause("rfc-1b", index=1)],
            ),
        )
        self.reported.set()
        await self.release.wait()
        await emit(EngineEvent("attribution", "stage_finished", status="completed"))
        return EngineResult(
            trajectory_summary="final",
            reward_failure_causes=[_cause("rfc-final")],
            evolution_judge=[_skill("evo-final"), _experience("evo-exp")],
            reward=_reward_payload("stub"),
        )


class _FailingEngine:
    async def run(self, inputs: EngineInputs, emit: Any) -> EngineResult:
        await emit(EngineEvent("evidence", "stage_started", title="Evidence"))
        raise RuntimeError("engine exploded")


class _NamedFailureEngine:
    async def run(self, inputs: EngineInputs, emit: Any) -> EngineResult:
        await emit(EngineEvent("evidence", "stage_started", title="Evidence"))
        raise EngineError("TASK_AMBIGUOUS", "the task cannot be identified")


def _reader() -> FakeReader:
    return FakeReader(
        {
            "main": [
                make_row([make_span(1, 100, 200, operation="chat", output_text="plan", tool_calls=True)]),
                make_row([make_span(2, 210, 300, tool_name="shell", tool_result="boom", status_code=2)]),
                make_row([make_span(3, 310, 400, operation="chat", output_text="done")]),
            ],
        },
    )


def _model_resolver(session_id: str, *, team_name: str | None = None) -> ResolvedDiagnosisModel:
    return ResolvedDiagnosisModel(info=ModelInfo(type="model", id="m-1", display_name="M"), resolved=None)


def _service(
    tmp_path: Path,
    *,
    push: _Push | None = None,
    mode: str = "agent.work.normal",
    team_name: str = "",
    busy: bool = False,
    engine: Any = None,
    enabled: bool = True,
    ttse_trigger: Any = None,
    reader: FakeReader | None = None,
    model_resolver: Any = _model_resolver,
) -> tuple[DiagnosisService, _Push]:
    sink = push or _Push()

    def metadata(session_id: str) -> dict[str, str]:
        if session_id == SESSION_ID:
            return {"session_id": session_id, "mode": mode, "team_name": team_name}
        return {}

    service = DiagnosisService(
        store=DiagnosisStore(tmp_path),
        reader_provider=lambda: reader or _reader(),
        push=sink,
        is_busy=lambda _sid: busy,
        metadata_loader=metadata,
        engine=engine or StubDiagnosisEngine(step_delay_s=0),
        model_resolver=model_resolver,
        ttse_trigger=ttse_trigger,
        is_enabled=lambda: enabled,
    )
    return service, sink


async def _wait_finished(service: DiagnosisService, diagnosis_id: str) -> Any:
    async def poll() -> Any:
        while True:
            record = service.get_record(SESSION_ID, diagnosis_id)
            # The slot is released only after the terminal snapshot is saved and pushed.
            if record.status is not DiagnosisStatus.RUNNING and service._running.get(SESSION_ID) is None:
                return record
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), _ENGINE_DONE_TIMEOUT)


def _full_request(**overrides: Any) -> DiagnosisRunRequest:
    return DiagnosisRunRequest(scope=FullScope(), **overrides)


# ── mode profile ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("metadata", "profile", "team"),
    [
        ({"mode": "agent.work.normal", "team_name": ""}, ModeProfile.AGENT_WORK, None),
        ({"mode": "agent.work.plan"}, ModeProfile.AGENT_WORK, None),
        ({"mode": "agent.code.normal"}, ModeProfile.AGENT_CODE, None),
        ({"mode": "team.work.normal", "team_name": "alpha"}, ModeProfile.TEAM, "alpha"),
    ],
)
def test_derive_mode_profile_maps_known_sessions(metadata: dict[str, str], profile: ModeProfile, team: str | None) -> None:
    context = derive_mode_profile(metadata)

    assert context.mode_profile is profile
    assert context.team_name == team


@pytest.mark.parametrize(
    "metadata",
    [{}, {"mode": ""}, {"mode": "team.work.normal", "team_name": ""}, {"mode": "agent.work.normal", "team_name": "x"}],
)
def test_derive_mode_profile_rejects_unsupported_sessions(metadata: dict[str, str]) -> None:
    with pytest.raises(DiagnosisHostError) as caught:
        derive_mode_profile(metadata)

    assert caught.value.code == "UNSUPPORTED_SESSION_MODE"
    assert caught.value.status == 403


# ── result filtering ────────────────────────────────────────────────────────


async def _index() -> ScopeIndex:
    snapshot, _excluded = await load_trajectory_snapshot(_reader(), SESSION_ID)
    return ScopeIndex.build(snapshot, resolve_ranges(snapshot, FullScope()))


def _raw_result() -> EngineResult:
    return EngineResult(
        trajectory_summary="summary",
        reward=_reward_payload("r1"),
        reward_failure_causes=[
            _cause("rfc-1", dimension="r1"),
            _cause("rfc-out", dimension="r1", index=99),
            _cause("rfc-unknown", dimension="nope", index=1),
            _cause("rfc-empty", dimension="r1", index=1, suggestion=(" ",)),
        ],
        evolution_judge=[
            _skill("evo-skill"),
            _skill("evo-skill-out", index=99),
            _skill("evo-no-target", target=None, index=1),
            _experience("evo-exp"),
            _experience("evo-exp-2", index=3),
        ],
    )


@pytest.mark.asyncio
async def test_filter_drops_out_of_scope_rows_and_empty_suggestions() -> None:
    filtered = filter_engine_result(_raw_result(), index=await _index(), profile=ModeProfile.AGENT_WORK)

    assert [cause.id for cause in filtered.reward_failure_causes] == ["rfc-1"]
    assert [judge.id for judge in filtered.evolution_judge] == ["evo-skill", "evo-exp"]
    assert filtered.trajectory_summary == "summary"


@pytest.mark.asyncio
async def test_filter_agent_code_keeps_causes_but_no_evolution() -> None:
    filtered = filter_engine_result(_raw_result(), index=await _index(), profile=ModeProfile.AGENT_CODE)

    assert [cause.id for cause in filtered.reward_failure_causes] == ["rfc-1"]
    assert filtered.evolution_judge == []


@pytest.mark.asyncio
async def test_filter_team_keeps_skill_but_not_experience() -> None:
    filtered = filter_engine_result(_raw_result(), index=await _index(), profile=ModeProfile.TEAM)

    assert [cause.id for cause in filtered.reward_failure_causes] == ["rfc-1"]
    assert [judge.id for judge in filtered.evolution_judge] == ["evo-skill"]


@pytest.mark.asyncio
async def test_filter_drops_refs_without_a_valid_node_id() -> None:
    cause = RewardFailureCause(
        id="rfc-1",
        dimension="r1",
        summary="a",
        evidence=[_ref(2, node=False), _ref(3)],
        suggestion=["fix"],
    )
    only_unanchored = RewardFailureCause(
        id="rfc-2",
        dimension="r1",
        summary="b",
        evidence=[_ref(1, node=False)],
        suggestion=["fix"],
    )

    filtered = filter_engine_result(
        EngineResult(
            trajectory_summary="s",
            reward=_reward_payload("r1"),
            reward_failure_causes=[cause, only_unanchored],
        ),
        index=await _index(),
        profile=ModeProfile.AGENT_WORK,
    )

    assert [item.id for item in filtered.reward_failure_causes] == ["rfc-1"]
    assert [ref.node_id for ref in filtered.reward_failure_causes[0].evidence] == ["main:2"]


@pytest.mark.asyncio
async def test_without_a_reward_every_failure_cause_is_dropped() -> None:
    filtered = filter_engine_result(
        EngineResult(trajectory_summary="s", reward_failure_causes=[_cause("rfc-1", dimension="r1")]),
        index=await _index(),
        profile=ModeProfile.AGENT_WORK,
    )

    assert filtered.reward_failure_causes == []


# ── run lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_runs_to_completion_with_ordered_events(tmp_path: Path) -> None:
    service, push = _service(tmp_path)

    started = await service.start(SESSION_ID, _full_request(user_note="  look at step 2  "))
    assert started.status is DiagnosisStatus.RUNNING
    assert started.ordinal == 1
    assert started.input.user_note == "look at step 2"
    assert started.input.resolved_span_count == 3

    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.status is DiagnosisStatus.COMPLETED
    assert [stage.stage_id for stage in finished.stages] == ["evidence", "attribution", "suggestions"]
    assert finished.trajectory_summary
    assert [cause.id for cause in finished.reward_failure_causes] == ["rfc-1"]
    assert [judge.id for judge in finished.evolution_judge] == ["evo-1", "evo-2"]
    assert [judge.category for judge in finished.evolution_judge] == ["skill", "experience"]
    assert finished.reward_failure_causes[0].evidence[0].node_id == "main:1"
    assert finished.input.resolved_ranges == [
        SelectionRange(subject_id="main", start_unix_nano="100", end_unix_nano="400"),
    ]

    sequences = [payload["seq"] for _sid, event, payload in push.events if event in {PROGRESS_EVENT, STATUS_EVENT}]
    assert sequences == sorted(set(sequences))
    assert push.of(STATUS_EVENT)[-1]["status"] == "completed"
    assert push.of(STATUS_EVENT)[-1]["seq"] == finished.seq
    stored = DiagnosisStore(tmp_path).load(SESSION_ID, started.diagnosis_id)
    assert stored is not None and stored.status is DiagnosisStatus.COMPLETED


@pytest.mark.asyncio
async def test_tables_are_visible_and_pushed_while_the_run_goes_on(tmp_path: Path) -> None:
    engine = _StreamingTablesEngine()
    service, push = _service(tmp_path, engine=engine)

    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.reported.wait(), _ENGINE_DONE_TIMEOUT)

    live = service.get_record(SESSION_ID, started.diagnosis_id)
    assert live.status is DiagnosisStatus.RUNNING
    # Out-of-scope rows are filtered like in the final result; a resubmission replaces the table.
    assert [cause.id for cause in live.reward_failure_causes] == ["rfc-1b"]
    assert [judge.id for judge in live.evolution_judge] == ["evo-1"]

    updates = [payload for payload in push.of(PROGRESS_EVENT) if payload["kind"] == "reward_failure_causes"]
    assert [[cause["id"] for cause in item["reward_failure_causes"]] for item in updates] == [
        ["rfc-1"],
        ["rfc-1b"],
    ]
    judge_updates = [payload for payload in push.of(PROGRESS_EVENT) if payload["kind"] == "evolution_judge"]
    assert [item["id"] for item in judge_updates[0]["evolution_judge"]] == ["evo-1"]
    sequences = [payload["seq"] for payload in push.of(PROGRESS_EVENT)]
    assert sequences == list(range(1, len(sequences) + 1))

    engine.release.set()
    finished = await _wait_finished(service, started.diagnosis_id)
    # The final result has the last word.
    assert [cause.id for cause in finished.reward_failure_causes] == ["rfc-final"]
    assert [judge.id for judge in finished.evolution_judge] == ["evo-final", "evo-exp"]
    assert finished.trajectory_summary == "final"


@pytest.mark.asyncio
async def test_output_deltas_are_kept_as_separate_lines(tmp_path: Path) -> None:
    service, push = _service(tmp_path)

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    deltas = [
        payload["text_delta"]
        for _sid, event, payload in push.events
        if event == PROGRESS_EVENT and payload.get("kind") == "output_delta"
    ]
    assert deltas and all(delta.endswith("\n") for delta in deltas)
    stored_output = "".join(stage.output for stage in finished.stages)
    assert stored_output == "".join(deltas)


@pytest.mark.asyncio
async def test_agent_code_session_keeps_causes_but_no_evolution(tmp_path: Path) -> None:
    service, _push = _service(tmp_path, mode="agent.code.normal")

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.input.mode_profile is ModeProfile.AGENT_CODE
    assert finished.trajectory_summary
    assert [cause.id for cause in finished.reward_failure_causes] == ["rfc-1"]
    assert finished.evolution_judge == []


@pytest.mark.asyncio
async def test_team_session_keeps_skill_but_not_experience(tmp_path: Path) -> None:
    service, _push = _service(tmp_path, mode="team.work.normal", team_name="alpha")

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.input.mode_profile is ModeProfile.TEAM
    assert [judge.category for judge in finished.evolution_judge] == ["skill"]


@pytest.mark.asyncio
async def test_selection_scope_resolves_only_the_selected_steps(tmp_path: Path) -> None:
    service, _push = _service(tmp_path)
    scope = SelectionScope(ranges=[SelectionRange(subject_id="main", start_unix_nano="205", end_unix_nano="305")])

    started = await service.start(SESSION_ID, DiagnosisRunRequest(scope=scope))
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.input.resolved_span_count == 1
    assert finished.scope_kind == "selection"


@pytest.mark.asyncio
async def test_running_spans_are_excluded_and_reported(tmp_path: Path) -> None:
    reader = FakeReader(
        {
            "main": [
                make_row([make_span(1, 1, 2)]),
                make_row([make_span(2, 3, 4)], lifecycle="running"),
            ],
        },
    )
    service, _push = _service(tmp_path, reader=reader)

    started = await service.start(SESSION_ID, _full_request())

    assert started.input.resolved_span_count == 1
    assert [ref.span_id for ref in started.input.excluded_running] == [span_id(2)]
    await _wait_finished(service, started.diagnosis_id)


@pytest.mark.asyncio
async def test_second_start_while_running_is_rejected(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.start(SESSION_ID, _full_request())

    assert caught.value.code == "DIAGNOSIS_ALREADY_RUNNING"
    assert caught.value.status == 409
    engine.release.set()
    await _wait_finished(service, started.diagnosis_id)


@pytest.mark.asyncio
async def test_concurrent_starts_admit_exactly_one(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)

    results = await asyncio.gather(
        service.start(SESSION_ID, _full_request()),
        service.start(SESSION_ID, _full_request()),
        return_exceptions=True,
    )

    failures = [item for item in results if isinstance(item, DiagnosisHostError)]
    successes = [item for item in results if not isinstance(item, BaseException)]
    assert len(successes) == 1
    assert [item.code for item in failures] == ["DIAGNOSIS_ALREADY_RUNNING"]
    engine.release.set()
    await _wait_finished(service, successes[0].diagnosis_id)


@pytest.mark.asyncio
async def test_full_scope_is_rejected_while_the_session_is_busy_but_selection_is_allowed(tmp_path: Path) -> None:
    service, _push = _service(tmp_path, busy=True)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.start(SESSION_ID, _full_request())
    assert caught.value.code == "FULL_SCOPE_WHILE_BUSY"

    scope = SelectionScope(ranges=[SelectionRange(subject_id="main", start_unix_nano="0", end_unix_nano="500")])
    started = await service.start(SESSION_ID, DiagnosisRunRequest(scope=scope))
    await _wait_finished(service, started.diagnosis_id)


@pytest.mark.asyncio
async def test_cancel_marks_the_record_cancelled_and_frees_the_slot(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    cancelled = await service.cancel(SESSION_ID, started.diagnosis_id)

    assert cancelled.status is DiagnosisStatus.CANCELLED
    assert push.of(STATUS_EVENT)[-1]["status"] == "cancelled"
    assert all(stage.status.value != "running" for stage in cancelled.stages)
    engine.release.set()
    again = await service.start(SESSION_ID, _full_request())
    engine.release.set()
    await _wait_finished(service, again.diagnosis_id)


@pytest.mark.asyncio
async def test_engine_failure_is_recorded(tmp_path: Path) -> None:
    service, push = _service(tmp_path, engine=_FailingEngine())

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.status is DiagnosisStatus.FAILED
    assert finished.error is not None and "engine exploded" in finished.error.message
    assert finished.error.code == "INTERNAL_ERROR"
    assert push.of(STATUS_EVENT)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_engine_error_keeps_its_own_code_and_fails_the_open_stage(tmp_path: Path) -> None:
    service, push = _service(tmp_path, engine=_NamedFailureEngine())

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.status is DiagnosisStatus.FAILED
    assert finished.error is not None
    assert (finished.error.code, finished.error.message) == ("TASK_AMBIGUOUS", "the task cannot be identified")
    assert [stage.status.value for stage in finished.stages] == ["failed"]
    assert push.of(STATUS_EVENT)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_delete_requires_a_finished_record(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    with pytest.raises(DiagnosisHostError) as running:
        service.delete(SESSION_ID, started.diagnosis_id)
    assert running.value.code == "DIAGNOSIS_RUNNING"

    engine.release.set()
    await _wait_finished(service, started.diagnosis_id)
    service.delete(SESSION_ID, started.diagnosis_id)
    with pytest.raises(DiagnosisHostError) as missing:
        service.get_record(SESSION_ID, started.diagnosis_id)
    assert missing.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_list_includes_the_live_running_record(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    summaries = service.list_summaries(SESSION_ID)

    assert [item.diagnosis_id for item in summaries] == [started.diagnosis_id]
    assert summaries[0].status is DiagnosisStatus.RUNNING
    engine.release.set()
    await _wait_finished(service, started.diagnosis_id)


@pytest.mark.asyncio
async def test_shutdown_interrupts_running_diagnoses(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    await service.shutdown()

    stored = DiagnosisStore(tmp_path).load(SESSION_ID, started.diagnosis_id)
    assert stored is not None and stored.status is DiagnosisStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_a_removed_session_is_not_resurrected_by_a_late_save(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)
    # Deleting or archiving the session moves its directory away mid-run.
    shutil.rmtree(tmp_path / SESSION_ID)
    service._metadata_loader = lambda _sid: {}

    engine.release.set()
    for _ in range(300):
        if service._running.get(SESSION_ID) is None:
            break
        await asyncio.sleep(0.01)

    assert service._running.get(SESSION_ID) is None
    assert not (tmp_path / SESSION_ID).exists()
    assert DiagnosisStore(tmp_path).load(SESSION_ID, started.diagnosis_id) is None


@pytest.mark.asyncio
async def test_a_failing_session_lookup_does_not_lose_a_finished_record(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine)
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    def broken(_sid: str) -> dict[str, str]:
        raise OSError("disk")

    service._metadata_loader = broken
    engine.release.set()
    for _ in range(300):
        if service._running.get(SESSION_ID) is None:
            break
        await asyncio.sleep(0.01)

    stored = DiagnosisStore(tmp_path).load(SESSION_ID, started.diagnosis_id)
    assert stored is not None and stored.status is DiagnosisStatus.COMPLETED


# ── access errors ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_access_errors_use_contract_codes(tmp_path: Path) -> None:
    disabled, _ = _service(tmp_path, enabled=False)
    with pytest.raises(DiagnosisHostError) as caught:
        disabled.list_summaries(SESSION_ID)
    assert (caught.value.code, caught.value.status) == ("FEATURE_DISABLED", 403)

    service, _ = _service(tmp_path)
    with pytest.raises(DiagnosisHostError) as missing:
        service.list_summaries("session-2")
    assert (missing.value.code, missing.value.status) == ("SESSION_NOT_FOUND", 404)
    with pytest.raises(DiagnosisHostError) as invalid:
        service.list_summaries("../escape")
    assert (invalid.value.code, invalid.value.status) == ("BAD_REQUEST", 400)

    unsupported, _ = _service(tmp_path, mode="")
    with pytest.raises(DiagnosisHostError) as mode:
        unsupported.list_summaries(SESSION_ID)
    assert mode.value.code == "UNSUPPORTED_SESSION_MODE"


@pytest.mark.asyncio
async def test_model_unavailable_prevents_the_run(tmp_path: Path) -> None:
    def unavailable(session_id: str, *, team_name: str | None = None) -> ResolvedDiagnosisModel:
        raise DiagnosisHostError("MODEL_UNAVAILABLE", "no model", 503)

    service, _push = _service(tmp_path, model_resolver=unavailable)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.start(SESSION_ID, _full_request())

    assert (caught.value.code, caught.value.status) == ("MODEL_UNAVAILABLE", 503)
    assert DiagnosisStore(tmp_path).list_summaries(SESSION_ID) == []


@pytest.mark.asyncio
async def test_empty_scope_does_not_create_a_record(tmp_path: Path) -> None:
    service, _push = _service(tmp_path, reader=FakeReader({"main": [make_row([make_span(1, 1, 2)], lifecycle="running")]}))

    with pytest.raises(DiagnosisHostError) as caught:
        await service.start(SESSION_ID, _full_request())

    assert caught.value.code == "EMPTY_SCOPE"
    assert DiagnosisStore(tmp_path).list_summaries(SESSION_ID) == []
    # The slot is released again.
    assert SESSION_ID not in service._running and SESSION_ID not in service._starting


# ── TTSE ────────────────────────────────────────────────────────────────────


class _Forward:
    def __init__(self, response: tuple[bool, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        self._response = response

    async def __call__(self, session_id: str, signals: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
        self.calls.append((session_id, signals))
        if self._response is not None:
            return self._response
        return True, {"results": {item["id"]: {"result": "triggered"} for item in signals}}


def _ttse_reader() -> FakeReader:
    """Trajectory whose first model call carries the user task, so TTSE has a conversation."""
    first = make_span(1, 100, 200, operation="chat", output_text="plan", tool_calls=True)
    first["attributes"].append(
        {
            "key": "gen_ai.input.messages",
            "value": {
                "stringValue": json.dumps([{"role": "user", "parts": [{"type": "text", "content": "fix the build"}]}]),
            },
        },
    )
    return FakeReader(
        {
            "main": [
                make_row([first]),
                make_row([make_span(2, 210, 300, tool_name="shell", tool_result="boom", status_code=2)]),
                make_row([make_span(3, 310, 400, operation="chat", output_text="done")]),
            ],
        },
    )


async def _completed(tmp_path: Path, forward: _Forward, **kwargs: Any) -> tuple[DiagnosisService, str]:
    service, _push = _service(tmp_path, ttse_trigger=forward, reader=_ttse_reader(), **kwargs)
    started = await service.start(SESSION_ID, _full_request())
    await _wait_finished(service, started.diagnosis_id)
    return service, started.diagnosis_id


@pytest.mark.asyncio
async def test_stub_experience_item_is_anchored_and_carries_outcome_and_score(tmp_path: Path) -> None:
    service, diagnosis_id = await _completed(tmp_path, _Forward())

    judge = next(item for item in service.get_record(SESSION_ID, diagnosis_id).evolution_judge if item.id == "evo-2")

    assert judge.category == "experience"
    assert judge.outcome == "partial"
    assert judge.score == 0.5
    assert judge.target is None
    assert judge.evidence and all(ref.node_id for ref in judge.evidence)


@pytest.mark.asyncio
async def test_trigger_ttse_hands_the_conversation_to_ttse_once(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)

    first = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")
    second = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert first["evo-2"].result.value == "triggered"
    assert second["evo-2"].triggered_at == first["evo-2"].triggered_at
    assert len(forward.calls) == 1
    payload = forward.calls[0][1][0]
    assert (payload["id"], payload["outcome"], payload["score"]) == ("evo-2", "partial", 0.5)
    assert payload["task_query"] == "fix the build"
    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert payload["messages"][1]["content"] == "plan"
    assert payload["messages"][2]["name"] == "shell"
    stored = DiagnosisStore(tmp_path).load(SESSION_ID, diagnosis_id)
    assert stored is not None and "evo-2" in stored.ttse_trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "score"), [("success", 1.0), ("partial", 0.5), ("fail", 0.0)])
async def test_trigger_ttse_uses_the_item_outcome_and_score(
    tmp_path: Path,
    outcome: str,
    score: float,
) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)
    record = service.get_record(SESSION_ID, diagnosis_id)
    judge = next(item for item in record.evolution_judge if item.id == "evo-2")
    judge.outcome = outcome  # type: ignore[assignment]
    judge.score = score
    DiagnosisStore(tmp_path).save(record)

    await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    payload = forward.calls[0][1][0]
    assert (payload["outcome"], payload["score"]) == (outcome, score)


@pytest.mark.asyncio
async def test_trigger_ttse_resolves_the_scope_for_records_without_stored_ranges(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)
    record = service.get_record(SESSION_ID, diagnosis_id)
    record.input.resolved_ranges = []
    DiagnosisStore(tmp_path).save(record)

    results = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert results["evo-2"].result.value == "triggered"
    assert forward.calls[0][1][0]["task_query"] == "fix the build"


@pytest.mark.asyncio
async def test_trigger_ttse_retries_a_failed_item(tmp_path: Path) -> None:
    failing = _Forward((True, {"results": {"evo-2": {"result": "failed", "message": "disabled"}}}))
    service, diagnosis_id = await _completed(tmp_path, failing)

    first = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")
    again = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert first["evo-2"].result.value == "failed"
    assert first["evo-2"].message == "disabled"
    assert again["evo-2"].result.value == "failed"
    assert len(failing.calls) == 2


@pytest.mark.asyncio
async def test_trigger_ttse_marks_missing_results_as_failed(tmp_path: Path) -> None:
    forward = _Forward((True, {"results": {}}))
    service, diagnosis_id = await _completed(tmp_path, forward)

    results = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert results["evo-2"].result.value == "failed"


@pytest.mark.asyncio
async def test_trigger_ttse_fails_an_item_whose_trajectory_has_no_conversation(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)
    record = service.get_record(SESSION_ID, diagnosis_id)
    record.input.resolved_ranges = [SelectionRange(subject_id="main", start_unix_nano="5000", end_unix_nano="6000")]
    DiagnosisStore(tmp_path).save(record)

    results = await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert results["evo-2"].result.value == "failed"
    assert forward.calls == []


@pytest.mark.asyncio
async def test_trigger_ttse_surfaces_an_unavailable_agent(tmp_path: Path) -> None:
    forward = _Forward((False, {"error": "no agent"}))
    service, diagnosis_id = await _completed(tmp_path, forward)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert (caught.value.code, caught.value.status) == ("AGENT_UNAVAILABLE", 502)
    stored = DiagnosisStore(tmp_path).load(SESSION_ID, diagnosis_id)
    assert stored is not None and stored.ttse_trigger == {}


@pytest.mark.asyncio
async def test_trigger_ttse_keeps_the_worker_ttse_error_code(tmp_path: Path) -> None:
    forward = _Forward((False, {"error": "TTSE is not enabled", "code": "TTSE_UNAVAILABLE"}))
    service, diagnosis_id = await _completed(tmp_path, forward)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-2")

    assert (caught.value.code, caught.value.status) == ("TTSE_UNAVAILABLE", 409)


@pytest.mark.asyncio
async def test_trigger_ttse_rejects_unknown_ids_and_other_modes(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)
    with pytest.raises(DiagnosisHostError) as unknown:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, "nope")
    assert unknown.value.code == "BAD_REQUEST"
    # A skill item cannot be triggered either.
    with pytest.raises(DiagnosisHostError) as not_experience:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, "evo-1")
    assert not_experience.value.code == "BAD_REQUEST"
    assert forward.calls == []

    code_service, code_id = await _completed(tmp_path / "code", _Forward(), mode="agent.code.normal")
    with pytest.raises(DiagnosisHostError) as unsupported:
        await code_service.trigger_ttse(SESSION_ID, code_id, "evo-2")
    assert (unsupported.value.code, unsupported.value.status) == ("TTSE_NOT_SUPPORTED", 409)


@pytest.mark.asyncio
async def test_trigger_ttse_requires_a_completed_diagnosis(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine, ttse_trigger=_Forward())
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.trigger_ttse(SESSION_ID, started.diagnosis_id, "evo-2")

    assert caught.value.code == "DIAGNOSIS_NOT_COMPLETED"
    engine.release.set()
