# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Data contract of the trajectory diagnosis feature.

The structures mirror ``docs/ai/design/轨迹诊断/01-接口与数据契约.md``. Time
points are Unix-nanosecond decimal strings, and every step is referenced by a
span anchor (:class:`StepRef`) rather than by a UI ordinal.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_USER_NOTE_CHARS = 2000
MAX_SELECTION_RANGES = 50

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_NANO_PATTERN = re.compile(r"^\d{1,19}$")


class DiagnosisStatus(StrEnum):
    """Lifecycle of one diagnosis record."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = frozenset(
    {
        DiagnosisStatus.COMPLETED,
        DiagnosisStatus.FAILED,
        DiagnosisStatus.CANCELLED,
        DiagnosisStatus.INTERRUPTED,
    },
)


class StageStatus(StrEnum):
    """State of one engine stage."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ModeProfile(StrEnum):
    """Mode tier derived by the host from the session metadata."""

    AGENT_WORK = "agent.work"
    AGENT_CODE = "agent.code"
    TEAM = "team"


class RootCauseCategory(StrEnum):
    """Attribution category of a root cause."""

    SKILL_DEFECT = "skill_defect"
    TTSE_SUCCESS = "ttse_success"
    TTSE_PARTIAL = "ttse_partial"
    TTSE_FAIL = "ttse_fail"
    TOOL_OR_ENV = "tool_or_env"
    MODEL_CAPABILITY = "model_capability"
    REQUIREMENT_UNCLEAR = "requirement_unclear"
    OTHER = "other"


#: Experience categories: the verdict of TTSE success detection on the diagnosed
#: stretch. A diagnosis reports at most one of them.
TTSE_ROOT_CAUSE_CATEGORIES: frozenset[RootCauseCategory] = frozenset(
    {
        RootCauseCategory.TTSE_SUCCESS,
        RootCauseCategory.TTSE_PARTIAL,
        RootCauseCategory.TTSE_FAIL,
    },
)

#: Root-cause categories each mode profile may report; the host drops the rest.
ALLOWED_ROOT_CAUSE_CATEGORIES: dict[ModeProfile, frozenset[RootCauseCategory]] = {
    ModeProfile.AGENT_WORK: frozenset(RootCauseCategory),
    ModeProfile.AGENT_CODE: frozenset(
        {
            RootCauseCategory.TOOL_OR_ENV,
            RootCauseCategory.MODEL_CAPABILITY,
            RootCauseCategory.REQUIREMENT_UNCLEAR,
            RootCauseCategory.OTHER,
        },
    ),
    ModeProfile.TEAM: frozenset(RootCauseCategory) - TTSE_ROOT_CAUSE_CATEGORIES,
}


#: TTSE outcome and fixed task score each experience category stands for, the
#: values TTSE's own success detection assigns. Triggering hands them to TTSE
#: as the explicit score, so the induction does not detect the outcome again.
TTSE_OUTCOME_BY_CATEGORY: dict[RootCauseCategory, tuple[str, float]] = {
    RootCauseCategory.TTSE_SUCCESS: ("success", 1.0),
    RootCauseCategory.TTSE_PARTIAL: ("partial", 0.5),
    RootCauseCategory.TTSE_FAIL: ("fail", 0.0),
}


class TtseTriggerOutcome(StrEnum):
    """Result of handing one TTSE root cause to the experience pipeline.

    ``triggered`` only says that TTSE accepted the work; what it generates
    and saves afterwards is decided by TTSE itself and is not reported here.
    """

    TRIGGERED = "triggered"
    FAILED = "failed"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=False)


class StepRef(_Model):
    """Span anchor of one trajectory step.

    ``node_id`` names the exact message or tool row (``<subject_id>:<seq>``)
    inside the source span. Engines must fill it; the host rejects references
    whose ``node_id`` is missing or malformed.
    """

    trace_id: str
    span_id: str
    subject_id: str
    node_id: str | None = None

    @field_validator("trace_id")
    @classmethod
    def _check_trace_id(cls, value: str) -> str:
        if not _TRACE_ID_PATTERN.match(value):
            raise ValueError("trace_id must be 32 lowercase hex characters")
        return value

    @field_validator("span_id")
    @classmethod
    def _check_span_id(cls, value: str) -> str:
        if not _SPAN_ID_PATTERN.match(value):
            raise ValueError("span_id must be 16 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _check_node_id(self) -> StepRef:
        if self.node_id is not None and not is_valid_node_id(self.node_id, self.subject_id):
            raise ValueError("node_id must be '<subject_id>:<non-negative integer>'")
        return self

    @property
    def key(self) -> str:
        """Return the ``trace_id:span_id`` identity used for de-duplication."""
        return f"{self.trace_id}:{self.span_id}"


def is_valid_node_id(node_id: str, subject_id: str) -> bool:
    """Return whether ``node_id`` has the form ``<subject_id>:<sequence>``."""
    prefix = f"{subject_id}:"
    return node_id.startswith(prefix) and node_id.removeprefix(prefix).isdecimal()


class SelectionRange(_Model):
    """Inclusive time interval of one subject."""

    subject_id: str = Field(min_length=1)
    start_unix_nano: str
    end_unix_nano: str

    @field_validator("start_unix_nano", "end_unix_nano")
    @classmethod
    def _check_nano(cls, value: str) -> str:
        if not _NANO_PATTERN.match(value):
            raise ValueError("time point must be a decimal Unix-nanosecond string")
        return value


class FullScope(_Model):
    """Diagnose every recorded step."""

    kind: Literal["full"] = "full"


class SelectionScope(_Model):
    """Diagnose the steps that start inside the selected intervals."""

    kind: Literal["selection"] = "selection"
    ranges: list[SelectionRange] = Field(min_length=1, max_length=MAX_SELECTION_RANGES)

    @field_validator("ranges")
    @classmethod
    def _check_ranges(cls, value: list[SelectionRange]) -> list[SelectionRange]:
        seen: set[str] = set()
        for item in value:
            if int(item.start_unix_nano) > int(item.end_unix_nano):
                raise ValueError("start_unix_nano must not exceed end_unix_nano")
            if item.subject_id in seen:
                raise ValueError("each subject may have at most one range")
            seen.add(item.subject_id)
        return value


DiagnosisScope = FullScope | SelectionScope


class DiagnosisRunRequest(_Model):
    """Body of the run endpoint."""

    scope: DiagnosisScope = Field(discriminator="kind")
    user_note: str | None = Field(default=None, max_length=MAX_USER_NOTE_CHARS)
    locale: Literal["zh", "en"] = "zh"


class ModelInfo(_Model):
    """Model selection that was used. Never contains credentials or URLs."""

    type: str = ""
    id: str = ""
    display_name: str | None = None


class DiagnosisInput(_Model):
    """Resolved input stored with the record."""

    mode_profile: ModeProfile
    model: ModelInfo
    scope: DiagnosisScope = Field(discriminator="kind")
    user_note: str | None = None
    evaluate_artifact: bool = False
    locale: Literal["zh", "en"] = "zh"
    resolved_span_count: int = 0
    resolved_ranges: list[SelectionRange] = Field(default_factory=list)
    excluded_running: list[StepRef] = Field(default_factory=list)


class DiagnosisStage(_Model):
    """One engine stage; count, title and order are decided by the engine."""

    stage_id: str
    title: str = ""
    status: StageStatus = StageStatus.RUNNING
    summary: str | None = None
    output: str = ""
    skip_reason: str | None = None
    started_at: str | None = None
    elapsed_ms: int | None = None


class ArtifactIssue(_Model):
    """One defect reported by the artifact evaluation."""

    description: str
    refs: list[StepRef] = Field(default_factory=list)


class ArtifactEval(_Model):
    """Artifact evaluation outcome."""

    applied: bool = False
    skipped_reason: str | None = None
    verdict: Literal["pass", "partial", "fail"] | None = None
    score: float | None = None
    summary: str | None = None
    issues: list[ArtifactIssue] = Field(default_factory=list)


class DiagnosisEvidence(_Model):
    """Evidence stored with a record; only the artifact evaluation remains."""

    artifact_eval: ArtifactEval | None = None


class SignalRef(_Model):
    """The diagnostic signal an attributed root cause answers.

    A root cause pairs a signal (what went wrong, how badly, where it surfaced)
    with an attribution (where the cause lies and what it is). Reward signals
    carry ``tier``, ``score`` and ``category``; rule signals only carry the
    source and ``tier == "uncertain"``.

    Attributes:
        id: Signal id; root causes of one signal share it.
        source: ``keyword_tool`` / ``keyword_user`` for rule hits, ``reward``
            for a Reward dimension.
        tier: ``perfect`` / ``fair`` / ``weak`` / ``uncertain``.
        score: Dimension score (0-100) of a Reward signal.
        category: Reward dimension category.
        node_ids: Nodes where the problem surfaced (a hint, not the cause).
    """

    id: str
    source: str
    tier: str = ""
    score: float | None = None
    category: str | None = None
    node_ids: list[str] = Field(default_factory=list)


class RootCause(_Model):
    """One attributed root cause."""

    id: str
    category: RootCauseCategory
    subject_id: str
    refs: list[StepRef] = Field(default_factory=list)
    summary: str
    evidence: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    signal: SignalRef | None = None


class ArtifactFix(_Model):
    """Instruction that is filled into the composer."""

    instruction: str
    root_cause_ids: list[str] = Field(default_factory=list)


class SkillSuggestion(_Model):
    """Skill evolution suggestion that is filled into the composer."""

    id: str
    skill_name: str
    change_summary: str
    rationale: str = ""
    message: str
    root_cause_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    selectable: bool = True
    blocked_reason: str | None = None


class DiagnosisSuggestions(_Model):
    """Optimization outlets of one diagnosis."""

    artifact_fix: ArtifactFix | None = None
    skills: list[SkillSuggestion] = Field(default_factory=list)


class TtseTriggerResult(_Model):
    """Outcome of triggering TTSE with one experience root cause."""

    result: TtseTriggerOutcome
    message: str | None = None
    triggered_at: str


class DiagnosisError(_Model):
    """Failure description stored on a failed record."""

    code: str
    message: str


class DiagnosisSummary(_Model):
    """Record summary used by the list endpoint."""

    diagnosis_id: str
    ordinal: int
    status: DiagnosisStatus
    created_at: str
    finished_at: str | None = None
    scope_kind: Literal["full", "selection"]
    evaluate_artifact: bool = False
    artifact_eval_applied: bool = False
    headline: str | None = None


class DiagnosisRecord(DiagnosisSummary):
    """Complete persisted diagnosis record."""

    session_id: str
    input: DiagnosisInput
    seq: int = 0
    stages: list[DiagnosisStage] = Field(default_factory=list)
    evidence: DiagnosisEvidence = Field(default_factory=DiagnosisEvidence)
    root_causes: list[RootCause] = Field(default_factory=list)
    suggestions: DiagnosisSuggestions = Field(default_factory=DiagnosisSuggestions)
    ttse_trigger: dict[str, TtseTriggerResult] = Field(default_factory=dict)
    error: DiagnosisError | None = None
    reward: dict[str, Any] | None = None
    task_description: str | None = None

    def to_summary(self) -> DiagnosisSummary:
        """Return the list-endpoint projection of this record."""
        return DiagnosisSummary(
            diagnosis_id=self.diagnosis_id,
            ordinal=self.ordinal,
            status=self.status,
            created_at=self.created_at,
            finished_at=self.finished_at,
            scope_kind=self.scope_kind,
            evaluate_artifact=self.evaluate_artifact,
            artifact_eval_applied=self.artifact_eval_applied,
            headline=self.headline,
        )


class DiagnosisProgressEvent(_Model):
    """``trajectory.diagnosis.progress`` push payload."""

    session_id: str
    diagnosis_id: str
    seq: int
    stage_id: str
    kind: Literal["stage_started", "output_delta", "stage_finished", "root_causes"]
    title: str | None = None
    text_delta: str | None = None
    status: Literal["completed", "failed", "skipped"] | None = None
    summary: str | None = None
    skip_reason: str | None = None
    elapsed_ms: int | None = None
    # ``root_causes`` events: the causes now known for one signal, replacing earlier ones.
    signal_id: str | None = None
    root_causes: list[RootCause] | None = None


class DiagnosisStatusEvent(_Model):
    """``trajectory.diagnosis.status`` push payload."""

    session_id: str
    diagnosis_id: str
    seq: int
    status: DiagnosisStatus
    summary: DiagnosisSummary


class TtseTriggerRequest(_Model):
    """Body of the TTSE trigger endpoint."""

    root_cause_ids: list[str] = Field(min_length=1, max_length=50)


class DiagnosisHostError(Exception):
    """Business error carrying the HTTP status and contract error code."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


#: HTTP status of every error code a diagnosis request can answer. The service
#: reports codes only across the AgentServer boundary; the Gateway restores the
#: status from this table. Unknown codes fall back to 500.
ERROR_STATUS_BY_CODE: dict[str, int] = {
    "BAD_REQUEST": 400,
    "FEATURE_DISABLED": 403,
    "UNSUPPORTED_SESSION_MODE": 403,
    "SESSION_NOT_FOUND": 404,
    "NOT_FOUND": 404,
    "DIAGNOSIS_ALREADY_RUNNING": 409,
    "FULL_SCOPE_WHILE_BUSY": 409,
    "DIAGNOSIS_RUNNING": 409,
    "DIAGNOSIS_NOT_COMPLETED": 409,
    "TTSE_NOT_SUPPORTED": 409,
    "TTSE_UNAVAILABLE": 409,
    "EMPTY_SCOPE": 422,
    "INTERNAL_ERROR": 500,
    "AGENT_UNAVAILABLE": 502,
    "MODEL_UNAVAILABLE": 503,
}

__all__ = [
    "ALLOWED_ROOT_CAUSE_CATEGORIES",
    "ERROR_STATUS_BY_CODE",
    "MAX_SELECTION_RANGES",
    "MAX_USER_NOTE_CHARS",
    "TERMINAL_STATUSES",
    "TTSE_OUTCOME_BY_CATEGORY",
    "TTSE_ROOT_CAUSE_CATEGORIES",
    "ArtifactEval",
    "ArtifactFix",
    "ArtifactIssue",
    "DiagnosisError",
    "DiagnosisEvidence",
    "DiagnosisHostError",
    "DiagnosisInput",
    "DiagnosisProgressEvent",
    "DiagnosisRecord",
    "DiagnosisRunRequest",
    "DiagnosisScope",
    "DiagnosisStage",
    "DiagnosisStatus",
    "DiagnosisStatusEvent",
    "DiagnosisSuggestions",
    "DiagnosisSummary",
    "FullScope",
    "ModeProfile",
    "ModelInfo",
    "RootCause",
    "RootCauseCategory",
    "SelectionRange",
    "SelectionScope",
    "SignalRef",
    "SkillSuggestion",
    "StageStatus",
    "StepRef",
    "TtseTriggerOutcome",
    "TtseTriggerRequest",
    "TtseTriggerResult",
    "is_valid_node_id",
]
