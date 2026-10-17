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
    ArtifactFix,
    DiagnosisHostError,
    DiagnosisRunRequest,
    DiagnosisStatus,
    DiagnosisSuggestions,
    FullScope,
    ModeProfile,
    ModelInfo,
    RootCause,
    RootCauseCategory,
    SelectionRange,
    SelectionScope,
    SkillSuggestion,
    StepRef,
    TtseSignal,
    TtseSignalOutcome,
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
    skill_lookup: Any = None,
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
        skill_lookup=skill_lookup,
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


def _ref(index: int, *, node: bool = True) -> StepRef:
    return StepRef(
        trace_id=TRACE_A,
        span_id=span_id(index),
        subject_id="main",
        node_id=f"main:{index - 1}" if node else None,
    )


def _signal(
    signal_id: str,
    cause_id: str,
    *,
    subject: str = "main",
    start: str = "100",
    end: str = "400",
) -> TtseSignal:
    return TtseSignal(
        id=signal_id,
        outcome=TtseSignalOutcome.FAIL,
        reason="r",
        subject_id=subject,
        start_unix_nano=start,
        end_unix_nano=end,
        root_cause_ids=[cause_id],
    )


def _raw_result() -> EngineResult:
    in_scope = RootCause(id="rc-1", category=RootCauseCategory.TOOL_OR_ENV, subject_id="main", refs=[_ref(2)], summary="a")
    outside = RootCause(id="rc-2", category=RootCauseCategory.OTHER, subject_id="main", refs=[_ref(99)], summary="b")
    ttse_gap = RootCause(id="rc-3", category=RootCauseCategory.TTSE_GAP, subject_id="main", refs=[_ref(1)], summary="c")
    skill_gap = RootCause(id="rc-4", category=RootCauseCategory.SKILL_DEFECT, subject_id="main", refs=[_ref(3), _ref(99)], summary="d")
    return EngineResult(
        root_causes=[in_scope, outside, ttse_gap, skill_gap],
        suggestions=DiagnosisSuggestions(
            artifact_fix=ArtifactFix(instruction="fix it", root_cause_ids=["rc-1", "rc-2"]),
            skills=[
                SkillSuggestion(id="sk-1", skill_name="s", change_summary="c", message="m", root_cause_ids=["rc-4"]),
                SkillSuggestion(id="sk-2", skill_name="s", change_summary="c", message="m", root_cause_ids=["rc-2"]),
            ],
            ttse=[
                _signal("tt-1", "rc-1", start="50", end="9999"),
                _signal("tt-2", "rc-2"),
                _signal("tt-3", "rc-1", start="5000", end="6000"),
                _signal("tt-4", "rc-1", subject="ghost"),
            ],
        ),
    )


@pytest.mark.asyncio
async def test_filter_drops_out_of_scope_refs_and_unreferenced_root_causes() -> None:
    filtered = filter_engine_result(_raw_result(), index=await _index(), profile=ModeProfile.AGENT_WORK)

    assert [cause.id for cause in filtered.root_causes] == ["rc-1", "rc-3", "rc-4"]
    skill_cause = next(cause for cause in filtered.root_causes if cause.id == "rc-4")
    assert [ref.span_id for ref in skill_cause.refs] == [span_id(3)]
    assert filtered.suggestions.artifact_fix is not None
    assert filtered.suggestions.artifact_fix.root_cause_ids == ["rc-1"]
    assert [item.id for item in filtered.suggestions.skills] == ["sk-1"]
    # tt-2 loses its root cause, tt-3 lies outside the range, tt-4 names a subject without a range.
    assert [item.id for item in filtered.suggestions.ttse] == ["tt-1"]
    # The stretch is clamped to the diagnosed range.
    assert (filtered.suggestions.ttse[0].start_unix_nano, filtered.suggestions.ttse[0].end_unix_nano) == ("100", "400")


@pytest.mark.asyncio
async def test_filter_agent_code_keeps_artifact_fix_only() -> None:
    filtered = filter_engine_result(_raw_result(), index=await _index(), profile=ModeProfile.AGENT_CODE)

    assert {cause.category for cause in filtered.root_causes} == {RootCauseCategory.TOOL_OR_ENV}
    assert filtered.suggestions.artifact_fix is not None
    assert filtered.suggestions.skills == []
    assert filtered.suggestions.ttse == []


@pytest.mark.asyncio
async def test_filter_team_keeps_skills_but_not_ttse() -> None:
    filtered = filter_engine_result(_raw_result(), index=await _index(), profile=ModeProfile.TEAM)

    assert RootCauseCategory.TTSE_GAP not in {cause.category for cause in filtered.root_causes}
    assert [item.id for item in filtered.suggestions.skills] == ["sk-1"]
    assert filtered.suggestions.ttse == []


@pytest.mark.asyncio
async def test_filter_drops_refs_without_a_valid_node_id() -> None:
    cause = RootCause(
        id="rc-1",
        category=RootCauseCategory.OTHER,
        subject_id="main",
        refs=[_ref(2, node=False), _ref(3)],
        summary="a",
    )
    only_unanchored = RootCause(
        id="rc-2",
        category=RootCauseCategory.OTHER,
        subject_id="main",
        refs=[_ref(1, node=False)],
        summary="b",
    )

    filtered = filter_engine_result(
        EngineResult(root_causes=[cause, only_unanchored]),
        index=await _index(),
        profile=ModeProfile.AGENT_WORK,
    )

    assert [item.id for item in filtered.root_causes] == ["rc-1"]
    assert [ref.node_id for ref in filtered.root_causes[0].refs] == ["main:2"]


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
    assert finished.root_causes and finished.root_causes[0].category is RootCauseCategory.TOOL_OR_ENV
    assert finished.suggestions.artifact_fix is not None
    assert finished.suggestions.skills and finished.suggestions.ttse
    assert finished.root_causes[0].refs[0].node_id == "main:1"
    assert finished.evidence.artifact_eval is not None

    sequences = [payload["seq"] for _sid, event, payload in push.events if event in {PROGRESS_EVENT, STATUS_EVENT}]
    assert sequences == sorted(set(sequences))
    assert push.of(STATUS_EVENT)[-1]["status"] == "completed"
    assert push.of(STATUS_EVENT)[-1]["seq"] == finished.seq
    stored = DiagnosisStore(tmp_path).load(SESSION_ID, started.diagnosis_id)
    assert stored is not None and stored.status is DiagnosisStatus.COMPLETED


@pytest.mark.asyncio
async def test_agent_code_session_gets_no_skill_or_ttse_suggestions(tmp_path: Path) -> None:
    service, _push = _service(tmp_path, mode="agent.code.normal")

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.input.mode_profile is ModeProfile.AGENT_CODE
    assert finished.suggestions.artifact_fix is not None
    assert finished.suggestions.skills == []
    assert finished.suggestions.ttse == []


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


@pytest.mark.asyncio
async def test_team_skill_suggestions_are_blocked_when_the_leader_lacks_the_skill(tmp_path: Path) -> None:
    async def lookup(session_id: str) -> set[str] | None:
        return {"other"}

    service, _push = _service(tmp_path, mode="team.work.normal", team_name="alpha", skill_lookup=lookup)

    started = await service.start(SESSION_ID, _full_request())
    finished = await _wait_finished(service, started.diagnosis_id)

    assert finished.input.mode_profile is ModeProfile.TEAM
    assert finished.suggestions.skills
    assert all(skill.selectable is False for skill in finished.suggestions.skills)
    assert finished.suggestions.skills[0].blocked_reason == "skill_not_installed_for_leader"
    assert finished.suggestions.ttse == []


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
async def test_stub_ttse_signal_carries_no_experience_text(tmp_path: Path) -> None:
    service, diagnosis_id = await _completed(tmp_path, _Forward())

    signal = service.get_record(SESSION_ID, diagnosis_id).suggestions.ttse[0]

    assert signal.id == "tt-1"
    assert signal.outcome.value == "fail"
    assert signal.subject_id == "main"
    assert (signal.start_unix_nano, signal.end_unix_nano) == ("100", "400")
    assert set(signal.model_dump()) == {
        "id",
        "outcome",
        "score",
        "reason",
        "subject_id",
        "start_unix_nano",
        "end_unix_nano",
        "root_cause_ids",
        "confidence",
    }


@pytest.mark.asyncio
async def test_trigger_ttse_hands_the_conversation_to_ttse_once(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)

    first = await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])
    second = await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])

    assert first["tt-1"].result.value == "triggered"
    assert second["tt-1"].triggered_at == first["tt-1"].triggered_at
    assert len(forward.calls) == 1
    payload = forward.calls[0][1][0]
    assert (payload["id"], payload["outcome"], payload["score"]) == ("tt-1", "fail", 0.0)
    assert payload["task_query"] == "fix the build"
    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert payload["messages"][1]["content"] == "plan"
    assert payload["messages"][2]["name"] == "shell"
    stored = DiagnosisStore(tmp_path).load(SESSION_ID, diagnosis_id)
    assert stored is not None and "tt-1" in stored.ttse_trigger


@pytest.mark.asyncio
async def test_trigger_ttse_retries_a_failed_signal(tmp_path: Path) -> None:
    failing = _Forward((True, {"results": {"tt-1": {"result": "failed", "message": "disabled"}}}))
    service, diagnosis_id = await _completed(tmp_path, failing)

    first = await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])
    again = await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])

    assert first["tt-1"].result.value == "failed"
    assert first["tt-1"].message == "disabled"
    assert again["tt-1"].result.value == "failed"
    assert len(failing.calls) == 2


@pytest.mark.asyncio
async def test_trigger_ttse_marks_missing_results_as_failed(tmp_path: Path) -> None:
    forward = _Forward((True, {"results": {}}))
    service, diagnosis_id = await _completed(tmp_path, forward)

    results = await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])

    assert results["tt-1"].result.value == "failed"


@pytest.mark.asyncio
async def test_trigger_ttse_fails_a_signal_whose_trajectory_has_no_conversation(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)
    record = service.get_record(SESSION_ID, diagnosis_id)
    record.suggestions.ttse[0].start_unix_nano = "5000"
    record.suggestions.ttse[0].end_unix_nano = "6000"
    DiagnosisStore(tmp_path).save(record)

    results = await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])

    assert results["tt-1"].result.value == "failed"
    assert forward.calls == []


@pytest.mark.asyncio
async def test_trigger_ttse_surfaces_an_unavailable_agent(tmp_path: Path) -> None:
    forward = _Forward((False, {"error": "no agent"}))
    service, diagnosis_id = await _completed(tmp_path, forward)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])

    assert (caught.value.code, caught.value.status) == ("AGENT_UNAVAILABLE", 502)
    stored = DiagnosisStore(tmp_path).load(SESSION_ID, diagnosis_id)
    assert stored is not None and stored.ttse_trigger == {}


@pytest.mark.asyncio
async def test_trigger_ttse_keeps_the_worker_ttse_error_code(tmp_path: Path) -> None:
    forward = _Forward((False, {"error": "TTSE is not enabled", "code": "TTSE_UNAVAILABLE"}))
    service, diagnosis_id = await _completed(tmp_path, forward)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, ["tt-1"])

    assert (caught.value.code, caught.value.status) == ("TTSE_UNAVAILABLE", 409)


@pytest.mark.asyncio
async def test_trigger_ttse_rejects_unknown_signals_and_other_modes(tmp_path: Path) -> None:
    forward = _Forward()
    service, diagnosis_id = await _completed(tmp_path, forward)
    with pytest.raises(DiagnosisHostError) as unknown:
        await service.trigger_ttse(SESSION_ID, diagnosis_id, ["nope"])
    assert unknown.value.code == "BAD_REQUEST"

    code_service, code_id = await _completed(tmp_path / "code", _Forward(), mode="agent.code.normal")
    with pytest.raises(DiagnosisHostError) as unsupported:
        await code_service.trigger_ttse(SESSION_ID, code_id, ["tt-1"])
    assert (unsupported.value.code, unsupported.value.status) == ("TTSE_NOT_SUPPORTED", 409)


@pytest.mark.asyncio
async def test_trigger_ttse_requires_a_completed_diagnosis(tmp_path: Path) -> None:
    engine = _GatedEngine()
    service, _push = _service(tmp_path, engine=engine, ttse_trigger=_Forward())
    started = await service.start(SESSION_ID, _full_request())
    await asyncio.wait_for(engine.started.wait(), _ENGINE_DONE_TIMEOUT)

    with pytest.raises(DiagnosisHostError) as caught:
        await service.trigger_ttse(SESSION_ID, started.diagnosis_id, ["tt-1"])

    assert caught.value.code == "DIAGNOSIS_NOT_COMPLETED"
    engine.release.set()
    await _wait_finished(service, started.diagnosis_id)
