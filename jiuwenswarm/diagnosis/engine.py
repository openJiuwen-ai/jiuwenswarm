# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diagnosis engine contract.

The engine reads the trajectory snapshot and per-subject ranges that the host
passes in; it never touches the trajectory store, the Skill library or the TTSE
bank. It reports progress through an ``emit`` callback and returns structured
results; the host (``service.py``) filters those results against the scope and
mode profile before persisting. Failures that the engine can name are raised as
:class:`EngineError`.

The host only depends on this contract. The engine it uses is returned by
``build_diagnosis_engine`` in ``server/diagnosis/host.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import (
    ArtifactEval,
    DiagnosisError,
    DiagnosisSuggestions,
    ModeProfile,
    RootCause,
)
from jiuwenswarm.diagnosis.snapshot import SubjectRange, TrajectorySnapshot

EngineEventKind = Literal["stage_started", "output_delta", "stage_finished", "root_causes"]


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """One progress notification produced by an engine.

    A ``root_causes`` event carries the root causes found so far for one signal
    (``signal_id``); it replaces what the host holds for that signal, so an
    engine can report findings while the stage is still running.
    """

    stage_id: str
    kind: EngineEventKind
    title: str | None = None
    text_delta: str | None = None
    status: Literal["completed", "failed", "skipped"] | None = None
    summary: str | None = None
    skip_reason: str | None = None
    signal_id: str | None = None
    root_causes: list[RootCause] | None = None


EmitFn = Callable[[EngineEvent], Awaitable[None]]


class EngineError(Exception):
    """Failure an engine can name; the host stores it as the diagnosis error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class EngineInputs:
    """Everything an engine may read.

    Attributes:
        session_id: Session being diagnosed.
        mode_profile: Mode tier deciding which outputs are meaningful.
        locale: Language of user-facing text.
        user_note: Optional note from the caller.
        snapshot: Finished records of every subject of the session.
        ranges: At most one closed time range per diagnosed subject.
        model: Resolved model the engine should call.
    """

    session_id: str
    mode_profile: ModeProfile
    locale: Literal["zh", "en"]
    user_note: str | None
    snapshot: TrajectorySnapshot
    ranges: list[SubjectRange]
    model: ResolvedDiagnosisModel


@dataclass(slots=True)
class EngineResult:
    """Structured engine output, before host-side filtering.

    ``error`` carries a terminal failure the engine wants the host to keep
    alongside the partial results (for example an attribution that ran out of
    budget after reporting some causes): the host stores it on the record and
    marks the failed stage, but keeps the record completed.
    """

    headline: str | None = None
    artifact_eval: ArtifactEval | None = None
    root_causes: list[RootCause] = field(default_factory=list)
    suggestions: DiagnosisSuggestions = field(default_factory=DiagnosisSuggestions)
    error: DiagnosisError | None = None
    reward: dict[str, Any] | None = None
    task_description: str | None = None


class DiagnosisEngine(Protocol):
    """Contract between the host and any diagnosis engine."""

    async def run(self, inputs: EngineInputs, emit: EmitFn) -> EngineResult:
        """Analyse the scope and return structured results.

        Args:
            inputs: Trajectory snapshot, per-subject ranges, resolved model and
                the caller's note. Node building, artifact evaluation and
                model calls stay inside the engine.
            emit: Progress callback; must be awaited in order.

        Returns:
            Structured results that the host filters and persists.

        Raises:
            EngineError: When the engine cannot produce a diagnosis for a
                reason it can name (for example ``TASK_INCOMPLETE``).
        """
        ...


__all__ = [
    "DiagnosisEngine",
    "EmitFn",
    "EngineError",
    "EngineEvent",
    "EngineInputs",
    "EngineResult",
]
