# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer-side diagnosis host.

The host owns everything that must hold regardless of which engine runs:
one running diagnosis per session, scope fixing, mode-profile constraints,
event sequencing, persistence and TTSE triggering. The engine only produces
content and is filtered before anything is stored.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from jiuwenswarm.common.mode_matrix import (
    canonicalize_mode_text,
    is_code_profile_mode,
    is_single_agent_mode,
    is_team_mode,
)
from jiuwenswarm.common.config import get_config, get_skill_evolution_enabled, get_ttse_enabled
from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.diagnosis.engine import (
    DiagnosisEngine,
    EngineError,
    EngineEvent,
    EngineInputs,
    EngineResult,
)
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel, resolve_diagnosis_model
from jiuwenswarm.diagnosis.models import (
    ERROR_STATUS_BY_CODE,
    DiagnosisError,
    DiagnosisHostError,
    DiagnosisInput,
    DiagnosisProgressEvent,
    DiagnosisRecord,
    DiagnosisRunRequest,
    DiagnosisStage,
    DiagnosisStatus,
    DiagnosisStatusEvent,
    DiagnosisSummary,
    EvolutionJudge,
    ModeProfile,
    RewardFailureCause,
    SelectionRange,
    StageStatus,
    StepRef,
    TtseTriggerOutcome,
    TtseTriggerResult,
)
from jiuwenswarm.diagnosis.snapshot import (
    ScopeIndex,
    SubjectRange,
    SubjectReader,
    TrajectorySnapshot,
    count_records_in_ranges,
    load_trajectory_snapshot,
    resolve_ranges,
)
from jiuwenswarm.diagnosis.store import (
    DiagnosisStore,
    is_valid_diagnosis_id,
    new_diagnosis_id,
    utc_now_iso,
)
from jiuwenswarm.diagnosis.ttse_evidence import build_ttse_conversation

logger = logging.getLogger(__name__)

PROGRESS_EVENT = "trajectory.diagnosis.progress"
STATUS_EVENT = "trajectory.diagnosis.status"
_CANCEL_WAIT_SECONDS = 5.0

PushFn = Callable[[str, str, dict[str, Any]], Awaitable[None]]
BusyFn = Callable[[str], bool]
MetadataLoader = Callable[[str], Mapping[str, Any]]
ReaderProvider = Callable[[], SubjectReader]
ModelResolver = Callable[..., ResolvedDiagnosisModel]
#: Hands experience root causes to the TTSE pipeline. Each payload carries ``id``
#: (the root cause id), ``outcome``, ``score``, ``task_query`` and ``messages``; the answer is
#: ``(True, {"results": {id: {"result", "message"?}}})`` or ``(False, {"error"})``.
#: TTSE failure codes the Worker may report that are shown to the user as they are.
_TTSE_PASSTHROUGH_CODES = frozenset({"TTSE_NOT_SUPPORTED", "TTSE_UNAVAILABLE"})
TtseTriggerFn = Callable[[str, list[dict[str, Any]]], Awaitable[tuple[bool, dict[str, Any]]]]
EnabledFn = Callable[[], bool]

_ALLOWED_EVOLUTION_CATEGORIES: dict[ModeProfile, frozenset[str]] = {
    ModeProfile.AGENT_WORK: frozenset({"skill", "experience"}),
    ModeProfile.AGENT_CODE: frozenset(),
    ModeProfile.TEAM: frozenset({"skill"}),
}


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Facts about the diagnosed session derived from its metadata."""

    mode_profile: ModeProfile
    team_name: str | None


@dataclass(slots=True)
class _RunState:
    """Mutable state of one running diagnosis."""

    record: DiagnosisRecord
    snapshot: TrajectorySnapshot
    ranges: list[SubjectRange]
    index: ScopeIndex
    model: ResolvedDiagnosisModel
    context: SessionContext
    task: asyncio.Task[None] | None = None
    stage_started: dict[str, float] = field(default_factory=dict)
    finishing: bool = False


def derive_mode_profile(metadata: Mapping[str, Any]) -> SessionContext:
    """Derive the diagnosis mode profile from session metadata.

    Args:
        metadata: Session metadata containing ``mode`` and ``team_name``.

    Returns:
        The session context.

    Raises:
        DiagnosisHostError: ``UNSUPPORTED_SESSION_MODE`` for other sessions.
    """
    raw_mode = metadata.get("mode")
    raw_value = getattr(raw_mode, "value", raw_mode)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise DiagnosisHostError(
            "UNSUPPORTED_SESSION_MODE",
            "diagnosis supports known Agent and Team sessions only",
            403,
        )
    canonical = canonicalize_mode_text(raw_mode)
    team_name = str(metadata.get("team_name") or "").strip()
    if is_team_mode(canonical) and team_name:
        return SessionContext(ModeProfile.TEAM, team_name)
    if is_single_agent_mode(canonical) and not team_name:
        profile = ModeProfile.AGENT_CODE if is_code_profile_mode(canonical) else ModeProfile.AGENT_WORK
        return SessionContext(profile, None)
    raise DiagnosisHostError(
        "UNSUPPORTED_SESSION_MODE",
        "diagnosis supports Agent and Team sessions only",
        403,
    )


def _scoped_refs(refs: list[StepRef], index: ScopeIndex) -> list[StepRef]:
    """Keep references inside the diagnosed scope, de-duplicated by identity."""
    seen: set[tuple[str, str, str | None]] = set()
    kept: list[StepRef] = []
    for ref in refs:
        identity = (ref.subject_id, ref.key, ref.node_id)
        if index.admits(ref) and identity not in seen:
            seen.add(identity)
            kept.append(ref)
    return kept


def _crosses_subjects(refs: list[StepRef]) -> bool:
    return len({ref.subject_id for ref in refs}) > 1


def _dominant_subject_id(refs: list[StepRef]) -> str:
    """Subject cited most often. Ties keep the subject that appears first."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for ref in refs:
        subject_id = ref.subject_id
        if subject_id == "":
            continue
        if subject_id not in counts:
            counts[subject_id] = 0
            order.append(subject_id)
        counts[subject_id] += 1
    chosen = ""
    best = 0
    for subject_id in order:
        count = counts[subject_id]
        if count > best:
            best = count
            chosen = subject_id
    return chosen


def filter_reward_failure_causes(
    causes: list[RewardFailureCause],
    *,
    index: ScopeIndex,
    reward: dict[str, Any] | None,
) -> list[RewardFailureCause]:
    """Apply interface section 5.3 to one list of failure causes."""
    requirements: set[str] | None = None
    if isinstance(reward, Mapping):
        requirements = set()
        for dimension in reward.get("dimensions") or ():
            if isinstance(dimension, Mapping) and dimension.get("requirement"):
                requirements.add(str(dimension["requirement"]))
    kept: list[RewardFailureCause] = []
    for cause in causes:
        # While a run is still in progress the host may not hold the reward yet;
        # the final filter (with the reward present) checks the dimension again.
        if requirements is not None and cause.dimension not in requirements:
            continue
        refs = _scoped_refs(cause.evidence, index)
        if not refs or _crosses_subjects(refs):
            continue
        suggestion = [item for item in cause.suggestion if item.strip()]
        if not suggestion:
            continue
        kept.append(cause.model_copy(update={"evidence": refs, "suggestion": suggestion}))
    return kept


def filter_evolution_judge(
    judges: list[EvolutionJudge],
    *,
    index: ScopeIndex,
    profile: ModeProfile,
) -> list[EvolutionJudge]:
    """Apply interface section 5.4 to one list of evolution items.

    Skill evidence may cite several subjects. Experience stays on one subject,
    because TTSE replays a single subject's window. An item with no in-scope
    reference is dropped. Reward failure causes keep the single-subject rule
    separately.
    """
    allowed = _ALLOWED_EVOLUTION_CATEGORIES[profile]
    kept: list[EvolutionJudge] = []
    experience_seen = False
    for judge in judges:
        if judge.category not in allowed:
            continue
        refs = _scoped_refs(judge.evidence, index)
        if not refs or (judge.category != "skill" and _crosses_subjects(refs)):
            continue
        suggestion = [item for item in judge.suggestion if item.strip()]
        if not suggestion and judge.category != "experience":
            continue
        if judge.category == "skill":
            if not judge.target or not 0.0 <= judge.score <= 1.0:
                continue
        else:
            if judge.outcome is None:
                continue
            if experience_seen:
                continue
            experience_seen = True
        kept.append(judge.model_copy(update={"evidence": refs, "suggestion": suggestion}))
    return kept


def filter_engine_result(
    result: EngineResult,
    *,
    index: ScopeIndex,
    profile: ModeProfile,
) -> EngineResult:
    """Enforce the host invariants on raw engine output (interface section 5).

    Step references outside the diagnosed scope (or without a valid node id)
    are dropped; causes left without an in-scope, single-subject reference are
    discarded; categories and outlets the mode profile does not support are
    removed.  ``reward`` is kept as it is; without one every failure cause is
    meaningless and the list is emptied.
    """
    causes: list[RewardFailureCause] = []
    if result.reward is not None:
        causes = filter_reward_failure_causes(result.reward_failure_causes, index=index, reward=result.reward)
    judges = filter_evolution_judge(result.evolution_judge, index=index, profile=profile)
    return EngineResult(
        trajectory_summary=result.trajectory_summary,
        reward_failure_causes=causes,
        evolution_judge=judges,
        artifact_improvement=result.artifact_improvement,
        error=result.error,
        reward=result.reward,
    )


class DiagnosisService:
    """Runs, persists and publishes diagnoses inside the AgentServer."""

    def __init__(
        self,
        *,
        store: DiagnosisStore,
        reader_provider: ReaderProvider,
        push: PushFn,
        is_busy: BusyFn,
        metadata_loader: MetadataLoader,
        engine: DiagnosisEngine,
        model_resolver: ModelResolver = resolve_diagnosis_model,
        ttse_trigger: TtseTriggerFn | None = None,
        is_enabled: EnabledFn | None = None,
    ) -> None:
        self._store = store
        self._reader_provider = reader_provider
        self._push = push
        self._is_busy = is_busy
        self._metadata_loader = metadata_loader
        self._engine = engine
        self._model_resolver = model_resolver
        self._ttse_trigger = ttse_trigger
        self._is_enabled = is_enabled or _default_enabled
        self._running: dict[str, _RunState] = {}
        self._starting: set[str] = set()
        self._ttse_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False
        try:
            interrupted = self._store.mark_running_interrupted()
            if interrupted:
                logger.info("[diagnosis] marked %d stale running record(s) as interrupted", interrupted)
        except Exception:  # noqa: BLE001
            logger.warning("[diagnosis] startup interruption sweep failed", exc_info=True)

    # ── access ────────────────────────────────────────────────────────

    def check_access(self, session_id: str) -> SessionContext:
        """Validate the feature flag and the session.

        Raises:
            DiagnosisHostError: With the contract error code on any failure.
        """
        if not self._is_enabled():
            raise DiagnosisHostError("FEATURE_DISABLED", "trajectory diagnosis is disabled", 403)
        normalized = str(session_id or "").strip()
        if normalized != session_id or not is_valid_session_id(normalized):
            raise DiagnosisHostError("BAD_REQUEST", "invalid session_id", 400)
        try:
            metadata = self._metadata_loader(normalized)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[diagnosis] session metadata lookup failed: session_id=%s", normalized)
            raise DiagnosisHostError("INTERNAL_ERROR", "session lookup failed", 500) from exc
        if not metadata:
            raise DiagnosisHostError("SESSION_NOT_FOUND", "session not found", 404)
        return derive_mode_profile(metadata)

    # ── queries ───────────────────────────────────────────────────────

    def list_summaries(self, session_id: str) -> list[DiagnosisSummary]:
        """Return summaries newest first, with live state for the running one."""
        self.check_access(session_id)
        summaries = self._store.list_summaries(session_id)
        running = self._running.get(session_id)
        if running is None:
            return summaries
        live = running.record.to_summary()
        merged = [live if item.diagnosis_id == live.diagnosis_id else item for item in summaries]
        if all(item.diagnosis_id != live.diagnosis_id for item in merged):
            merged.insert(0, live)
        return merged

    def get_record(self, session_id: str, diagnosis_id: str) -> DiagnosisRecord:
        """Return one record, using the live snapshot when it is running."""
        self.check_access(session_id)
        return self._load(session_id, diagnosis_id)

    def _load(self, session_id: str, diagnosis_id: str) -> DiagnosisRecord:
        running = self._running.get(session_id)
        if (
            running is not None
            and running.record.diagnosis_id == diagnosis_id
            and not running.finishing
        ):
            return running.record.model_copy(deep=True)
        if not is_valid_diagnosis_id(diagnosis_id):
            raise DiagnosisHostError("NOT_FOUND", "diagnosis not found", 404)
        record = self._store.load(session_id, diagnosis_id)
        if record is None:
            raise DiagnosisHostError("NOT_FOUND", "diagnosis not found", 404)
        return record

    # ── run ───────────────────────────────────────────────────────────

    async def start(self, session_id: str, request: DiagnosisRunRequest) -> DiagnosisRecord:
        """Fix the scope, create the record and start the engine task.

        Returns:
            The newly created record with ``status = running``.

        Raises:
            DiagnosisHostError: With the contract error code on any failure.
        """
        context = self.check_access(session_id)
        if session_id in self._running or session_id in self._starting:
            raise DiagnosisHostError(
                "DIAGNOSIS_ALREADY_RUNNING",
                "a diagnosis is already running for this session",
                409,
            )
        if request.scope.kind == "full" and self._is_busy(session_id):
            raise DiagnosisHostError(
                "FULL_SCOPE_WHILE_BUSY",
                "full-trajectory diagnosis requires an idle session",
                409,
            )
        model = self._model_resolver(session_id, team_name=context.team_name)
        # Reserve the slot before the first await so concurrent requests cannot
        # both pass the check above.
        self._starting.add(session_id)
        try:
            state = await self._prepare(session_id, request, context, model)
            self._running[session_id] = state
        finally:
            self._starting.discard(session_id)
        state.task = asyncio.create_task(self._run(session_id, state), name=f"diagnosis-{session_id}")
        return state.record.model_copy(deep=True)

    async def _prepare(
        self,
        session_id: str,
        request: DiagnosisRunRequest,
        context: SessionContext,
        model: ResolvedDiagnosisModel,
    ) -> _RunState:
        try:
            snapshot, excluded_running = await load_trajectory_snapshot(self._reader_provider(), session_id)
        except DiagnosisHostError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[diagnosis] trajectory read failed: session_id=%s", session_id)
            raise DiagnosisHostError("INTERNAL_ERROR", "trajectory read failed", 500) from exc
        # Range resolution and indexing are CPU work over the whole trajectory;
        # keep them off the event loop that also serves the Agent runtime.
        ranges, index, span_count = await asyncio.to_thread(_fix_ranges, snapshot, request)
        user_note = (request.user_note or "").strip() or None
        ordinal = await asyncio.to_thread(self._store.allocate_ordinal, session_id)
        record = DiagnosisRecord(
            diagnosis_id=new_diagnosis_id(),
            ordinal=ordinal,
            status=DiagnosisStatus.RUNNING,
            created_at=utc_now_iso(),
            scope_kind=request.scope.kind,
            evaluate_artifact=True,
            artifact_eval_applied=False,
            session_id=session_id,
            input=DiagnosisInput(
                mode_profile=context.mode_profile,
                model=model.info,
                scope=request.scope,
                user_note=user_note,
                evaluate_artifact=True,
                locale=request.locale,
                resolved_span_count=span_count,
                resolved_ranges=[
                    SelectionRange(
                        subject_id=item.subject_id,
                        start_unix_nano=str(item.start_nano),
                        end_unix_nano=str(item.end_nano),
                    )
                    for item in ranges
                ],
                excluded_running=excluded_running,
            ),
        )
        await asyncio.to_thread(self._store.save, record)
        return _RunState(
            record=record,
            snapshot=snapshot,
            ranges=ranges,
            index=index,
            model=model,
            context=context,
        )

    async def _run(self, session_id: str, state: _RunState) -> None:
        record = state.record
        final_status = DiagnosisStatus.COMPLETED
        try:
            # Host switches are read per run so a settings change takes effect on
            # the next diagnosis; get_config() re-reads config.yaml on mtime change.
            config = get_config()
            inputs = EngineInputs(
                session_id=session_id,
                mode_profile=state.context.mode_profile,
                locale=record.input.locale,
                user_note=record.input.user_note,
                snapshot=state.snapshot,
                ranges=state.ranges,
                model=state.model,
                # Mode gating is decided here and handed to the engine, so a mode
                # without the outlet never mounts its tools or prompt blocks.
                skill_evolution=(
                    get_skill_evolution_enabled(config)
                    and state.context.mode_profile in {ModeProfile.AGENT_WORK, ModeProfile.TEAM}
                ),
                ttse_advice=(
                    get_ttse_enabled(config) and state.context.mode_profile is ModeProfile.AGENT_WORK
                ),
            )

            async def emit(event: EngineEvent) -> None:
                await self._on_engine_event(session_id, state, event)

            raw = await self._engine.run(inputs, emit)
            filtered = filter_engine_result(raw, index=state.index, profile=state.context.mode_profile)
            self._apply_result(record, filtered)
        except asyncio.CancelledError:
            final_status = DiagnosisStatus.INTERRUPTED if self._shutting_down else DiagnosisStatus.CANCELLED
        except EngineError as exc:
            logger.warning("[diagnosis] engine reported failure: session_id=%s code=%s", session_id, exc.code)
            final_status = DiagnosisStatus.FAILED
            record.error = DiagnosisError(code=exc.code, message=exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[diagnosis] engine failed: session_id=%s", session_id)
            final_status = DiagnosisStatus.FAILED
            record.error = DiagnosisError(code="INTERNAL_ERROR", message=str(exc) or type(exc).__name__)
        await self._finish(session_id, state, final_status)

    async def _finish(self, session_id: str, state: _RunState, status: DiagnosisStatus) -> None:
        record = state.record
        for stage in record.stages:
            if stage.status is StageStatus.RUNNING:
                stage.status = StageStatus.FAILED
        record.status = status
        record.finished_at = utc_now_iso()
        record.seq += 1
        # Readers load the saved snapshot from here on, so a concurrent TTSE
        # write cannot be overwritten by this final save.
        state.finishing = True
        try:
            await self._persist(session_id, record)
        except Exception:  # noqa: BLE001
            logger.exception("[diagnosis] final persist failed: session_id=%s", session_id)
        try:
            await self._send_status(session_id, record)
        finally:
            if self._running.get(session_id) is state:
                self._running.pop(session_id, None)

    async def _on_engine_event(self, session_id: str, state: _RunState, event: EngineEvent) -> None:
        record = state.record
        if event.kind == "reward":
            await self._on_reward(session_id, state, event)
            return
        if event.kind == "reward_failure_causes":
            await self._on_reward_failure_causes(session_id, state, event)
            return
        if event.kind == "evolution_judge":
            await self._on_evolution_judge(session_id, state, event)
            return
        stage = next((item for item in record.stages if item.stage_id == event.stage_id), None)
        elapsed_ms: int | None = None
        if event.kind == "stage_started":
            if stage is None:
                stage = DiagnosisStage(stage_id=event.stage_id)
                record.stages.append(stage)
            stage.title = event.title or stage.title
            stage.status = StageStatus.RUNNING
            stage.started_at = utc_now_iso()
            state.stage_started[event.stage_id] = time.monotonic()
        elif stage is None:
            return
        elif event.kind == "output_delta":
            # Engines emit one progress line per delta; keep lines apart so the
            # accumulated output stays readable in the UI.
            if event.text_delta and not event.text_delta.endswith("\n"):
                event = replace(event, text_delta=event.text_delta + "\n")
            stage.output += event.text_delta or ""
        else:
            stage.status = StageStatus(event.status or "completed")
            stage.summary = event.summary
            stage.skip_reason = event.skip_reason
            started = state.stage_started.pop(event.stage_id, None)
            if started is not None:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                stage.elapsed_ms = elapsed_ms
        record.seq += 1
        payload = DiagnosisProgressEvent(
            session_id=session_id,
            diagnosis_id=record.diagnosis_id,
            seq=record.seq,
            stage_id=event.stage_id,
            kind=event.kind,
            title=event.title,
            text_delta=event.text_delta,
            status=event.status,
            summary=event.summary,
            skip_reason=event.skip_reason,
            elapsed_ms=elapsed_ms,
        )
        await self._safe_push(session_id, PROGRESS_EVENT, payload.model_dump(mode="json", exclude_none=True))
        if event.kind == "stage_finished":
            try:
                await self._persist(session_id, record)
            except Exception:  # noqa: BLE001
                logger.warning("[diagnosis] stage persist failed: session_id=%s", session_id, exc_info=True)

    async def _on_reward(self, session_id: str, state: _RunState, event: EngineEvent) -> None:
        """Publish the artifact evaluation as soon as diagnosis has scored it."""
        payload = event.reward
        if not isinstance(payload, Mapping):
            return
        record = state.record
        record.reward = dict(payload)
        record.seq += 1
        progress = DiagnosisProgressEvent(
            session_id=session_id,
            diagnosis_id=record.diagnosis_id,
            seq=record.seq,
            stage_id=event.stage_id,
            kind="reward",
            reward=record.reward,
        )
        await self._safe_push(session_id, PROGRESS_EVENT, progress.model_dump(mode="json", exclude_none=True))
        try:
            await self._persist(session_id, record)
        except Exception:  # noqa: BLE001
            logger.warning("[diagnosis] reward persist failed: session_id=%s", session_id, exc_info=True)

    async def _on_reward_failure_causes(
        self, session_id: str, state: _RunState, event: EngineEvent
    ) -> None:
        """Replace the failure-cause table with the one an engine just reported."""
        record = state.record
        admitted = filter_reward_failure_causes(
            event.reward_failure_causes or [],
            index=state.index,
            reward=record.reward,
        )
        record.reward_failure_causes = admitted
        record.seq += 1
        payload = DiagnosisProgressEvent(
            session_id=session_id,
            diagnosis_id=record.diagnosis_id,
            seq=record.seq,
            stage_id=event.stage_id,
            kind=event.kind,
            reward_failure_causes=admitted,
        )
        await self._safe_push(session_id, PROGRESS_EVENT, payload.model_dump(mode="json", exclude_none=True))

    async def _on_evolution_judge(
        self, session_id: str, state: _RunState, event: EngineEvent
    ) -> None:
        """Replace the evolution table with the one an engine just reported."""
        record = state.record
        admitted = filter_evolution_judge(
            event.evolution_judge or [],
            index=state.index,
            profile=state.context.mode_profile,
        )
        record.evolution_judge = admitted
        record.seq += 1
        payload = DiagnosisProgressEvent(
            session_id=session_id,
            diagnosis_id=record.diagnosis_id,
            seq=record.seq,
            stage_id=event.stage_id,
            kind=event.kind,
            evolution_judge=admitted,
        )
        await self._safe_push(session_id, PROGRESS_EVENT, payload.model_dump(mode="json", exclude_none=True))

    async def _persist(self, session_id: str, record: DiagnosisRecord) -> None:
        """Save a record of a running diagnosis unless its session was removed meanwhile.

        Deleting or archiving a session moves its directory away; saving after
        that would recreate a ghost session directory holding only this record.
        """
        if not await asyncio.to_thread(self._session_exists, session_id):
            logger.info("[diagnosis] session is gone, record not persisted: session_id=%s", session_id)
            return
        # The dump runs on a worker thread while the engine task may still be
        # mutating the record; snapshot it first so the saved file is consistent.
        await asyncio.to_thread(self._store.save, record.model_copy(deep=True))

    def _session_exists(self, session_id: str) -> bool:
        try:
            return bool(self._metadata_loader(session_id))
        except Exception:  # noqa: BLE001
            # An unreadable lookup must not lose a finished diagnosis.
            return True

    @staticmethod
    def _apply_result(record: DiagnosisRecord, result: EngineResult) -> None:
        record.trajectory_summary = result.trajectory_summary
        record.reward_failure_causes = result.reward_failure_causes
        record.evolution_judge = result.evolution_judge
        record.artifact_improvement = result.artifact_improvement
        # An engine may report a terminal attribution failure while keeping
        # its partial results; the record stays completed but carries the error.
        record.error = result.error
        record.reward = result.reward

    async def _send_status(self, session_id: str, record: DiagnosisRecord) -> None:
        payload = DiagnosisStatusEvent(
            session_id=session_id,
            diagnosis_id=record.diagnosis_id,
            seq=record.seq,
            status=record.status,
            summary=record.to_summary(),
        )
        await self._safe_push(session_id, STATUS_EVENT, payload.model_dump(mode="json", exclude_none=True))

    async def _safe_push(self, session_id: str, event: str, payload: dict[str, Any]) -> None:
        try:
            await self._push(session_id, event, payload)
        except Exception:  # noqa: BLE001
            logger.warning("[diagnosis] push failed: session_id=%s event=%s", session_id, event, exc_info=True)

    # ── cancel / delete ───────────────────────────────────────────────

    async def cancel(self, session_id: str, diagnosis_id: str) -> DiagnosisRecord:
        """Cancel a running diagnosis; terminal records are returned unchanged."""
        self.check_access(session_id)
        state = self._running.get(session_id)
        if state is not None and state.record.diagnosis_id == diagnosis_id:
            task = state.task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=_CANCEL_WAIT_SECONDS)
            return self._load(session_id, diagnosis_id)
        record = self._load(session_id, diagnosis_id)
        if record.status is DiagnosisStatus.RUNNING:
            # A running record without a task belongs to a previous process.
            record.status = DiagnosisStatus.INTERRUPTED
            record.finished_at = utc_now_iso()
            await asyncio.to_thread(self._store.save, record)
        return record

    def delete(self, session_id: str, diagnosis_id: str) -> None:
        """Delete a finished record."""
        self.check_access(session_id)
        state = self._running.get(session_id)
        if state is not None and state.record.diagnosis_id == diagnosis_id:
            raise DiagnosisHostError("DIAGNOSIS_RUNNING", "cancel the running diagnosis first", 409)
        if not is_valid_diagnosis_id(diagnosis_id) or not self._store.delete(session_id, diagnosis_id):
            raise DiagnosisHostError("NOT_FOUND", "diagnosis not found", 404)

    # ── TTSE ──────────────────────────────────────────────────────────

    async def trigger_ttse(
        self,
        session_id: str,
        diagnosis_id: str,
        evolution_judge_id: str,
    ) -> dict[str, TtseTriggerResult]:
        """Hand the experience evolution item to TTSE and record the outcome.

        The diagnosis only reports how the stretch went; TTSE generates and
        saves the experience itself. An item that was triggered successfully is
        not triggered again, a failed one may be retried.

        Args:
            session_id: Diagnosed session.
            diagnosis_id: Completed diagnosis of the session.
            evolution_judge_id: Id of the experience item (``category=experience``).

        Returns:
            The trigger result of the item.

        Raises:
            DiagnosisHostError: The session does not support TTSE, the
                diagnosis has not completed, the id is not an experience
                evolution item, or TTSE is unavailable.
        """
        self.check_access(session_id)
        lock = self._ttse_locks.setdefault(f"{session_id}/{diagnosis_id}", asyncio.Lock())
        async with lock:
            record = self._load(session_id, diagnosis_id)
            if record.input.mode_profile is not ModeProfile.AGENT_WORK:
                raise DiagnosisHostError("TTSE_NOT_SUPPORTED", "this session mode does not support TTSE", 409)
            if record.status is not DiagnosisStatus.COMPLETED:
                raise DiagnosisHostError("DIAGNOSIS_NOT_COMPLETED", "the diagnosis has not completed", 409)
            judge = next(
                (
                    item
                    for item in record.evolution_judge
                    if item.id == evolution_judge_id and item.category == "experience"
                ),
                None,
            )
            if judge is None:
                raise DiagnosisHostError(
                    "BAD_REQUEST",
                    f"unknown experience evolution judge id: {evolution_judge_id}",
                    400,
                )
            if _needs_ttse_trigger(record.ttse_trigger.get(evolution_judge_id)):
                results = await self._trigger(session_id, record.input, [judge])
                record.ttse_trigger.update(results)
                await asyncio.to_thread(self._store.save, record)
            return {evolution_judge_id: record.ttse_trigger[evolution_judge_id]}

    async def _trigger(
        self,
        session_id: str,
        diagnosed: DiagnosisInput,
        judges: list[EvolutionJudge],
    ) -> dict[str, TtseTriggerResult]:
        if self._ttse_trigger is None:
            raise DiagnosisHostError("AGENT_UNAVAILABLE", "TTSE trigger is not configured", 502)
        try:
            snapshot, _excluded = await load_trajectory_snapshot(self._reader_provider(), session_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[diagnosis] trajectory read failed: session_id=%s", session_id)
            raise DiagnosisHostError("INTERNAL_ERROR", "trajectory read failed", 500) from exc
        now = utc_now_iso()
        results: dict[str, TtseTriggerResult] = {}
        payloads: list[dict[str, Any]] = []
        for judge in judges:
            subject_id = _dominant_subject_id(judge.evidence)
            window = await asyncio.to_thread(_diagnosed_window, snapshot, diagnosed, subject_id)
            task_query, messages = (
                await asyncio.to_thread(build_ttse_conversation, snapshot, subject_id, window[0], window[1])
                if window is not None
                else ("", [])
            )
            if not messages or judge.outcome is None:
                results[judge.id] = TtseTriggerResult(
                    result=TtseTriggerOutcome.FAILED,
                    message="no trajectory evidence left for this evolution item",
                    triggered_at=now,
                )
                continue
            payloads.append(
                {
                    "id": judge.id,
                    "outcome": judge.outcome,
                    "score": judge.score,
                    "task_query": task_query,
                    "messages": messages,
                },
            )
        if not payloads:
            return results
        ok, response = await self._ttse_trigger(session_id, payloads)
        if not ok:
            code = str(response.get("code") or "")
            if code not in _TTSE_PASSTHROUGH_CODES:
                code = "AGENT_UNAVAILABLE"
            raise DiagnosisHostError(
                code,
                str(response.get("error") or "TTSE is unavailable"),
                ERROR_STATUS_BY_CODE[code],
            )
        raw_results = response.get("results")
        raw_results = raw_results if isinstance(raw_results, Mapping) else {}
        for payload in payloads:
            entry = raw_results.get(payload["id"])
            outcome = TtseTriggerOutcome.FAILED
            message: str | None = "no result returned"
            if isinstance(entry, Mapping):
                try:
                    outcome = TtseTriggerOutcome(str(entry.get("result")))
                    message = str(entry["message"]) if entry.get("message") else None
                except ValueError:
                    message = "unrecognized result"
            results[payload["id"]] = TtseTriggerResult(result=outcome, message=message, triggered_at=now)
        return results

    # ── lifecycle ─────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Cancel every running diagnosis; their records become ``interrupted``."""
        self._shutting_down = True
        tasks = [
            state.task
            for state in list(self._running.values())
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_CANCEL_WAIT_SECONDS)


def _needs_ttse_trigger(previous: TtseTriggerResult | None) -> bool:
    """Return whether a root cause still needs a TTSE trigger (never triggered, or failed)."""
    return previous is None or previous.result is TtseTriggerOutcome.FAILED


def _fix_ranges(
    snapshot: TrajectorySnapshot,
    request: DiagnosisRunRequest,
) -> tuple[list[SubjectRange], ScopeIndex, int]:
    ranges = resolve_ranges(snapshot, request.scope)
    return ranges, ScopeIndex.build(snapshot, ranges), count_records_in_ranges(snapshot, ranges)


def _diagnosed_window(
    snapshot: TrajectorySnapshot,
    diagnosed: DiagnosisInput,
    subject_id: str,
) -> tuple[int, int] | None:
    """Return the stretch of one subject that the diagnosis was fixed to.

    Records written before the ranges were stored fall back to resolving the
    scope against the current snapshot.

    Returns:
        The inclusive nanosecond window, or ``None`` when the subject has none.
    """
    if diagnosed.resolved_ranges:
        stored = [
            (int(item.start_unix_nano), int(item.end_unix_nano))
            for item in diagnosed.resolved_ranges
            if item.subject_id == subject_id
        ]
        return stored[0] if stored else None
    try:
        ranges = resolve_ranges(snapshot, diagnosed.scope)
    except DiagnosisHostError:
        return None
    resolved = [(item.start_nano, item.end_nano) for item in ranges if item.subject_id == subject_id]
    return resolved[0] if resolved else None


def _default_enabled() -> bool:
    from jiuwenswarm.observability.config import is_trajectory_diagnosis_enabled

    return is_trajectory_diagnosis_enabled()


__all__ = [
    "PROGRESS_EVENT",
    "STATUS_EVENT",
    "DiagnosisService",
    "SessionContext",
    "derive_mode_profile",
    "filter_engine_result",
    "filter_evolution_judge",
    "filter_reward_failure_causes",
]
