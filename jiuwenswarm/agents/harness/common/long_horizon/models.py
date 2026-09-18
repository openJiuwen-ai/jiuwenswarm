# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JSON-safe models for long-horizon tasks."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LongHorizonKind = Literal["report", "meeting", "generic"]
LongHorizonTaskStatus = Literal["draft", "active", "muted_year", "done", "cancelled"]
LongHorizonStageStatus = Literal[
    "pending", "due", "in_progress", "snoozed", "done", "skipped"
]
Recurrence = Literal["yearly", "once"]

_TASK_STATUSES = frozenset({"draft", "active", "muted_year", "done", "cancelled"})
_STAGE_STATUSES = frozenset(
    {"pending", "due", "in_progress", "snoozed", "done", "skipped"}
)
_KINDS = frozenset({"report", "meeting", "generic"})
_RECURRENCES = frozenset({"yearly", "once"})


def touch_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _require_mapping(data: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TypeError(f"{field_name}_must_be_object")
    return data


def _require_key(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"{key}_required")
    return data[key]


def _require_text(data: dict[str, Any], key: str) -> str:
    text = str(_require_key(data, key) or "").strip()
    if not text:
        raise ValueError(f"{key}_required")
    return text


def _require_id(data: dict[str, Any], key: str, prefix: str) -> str:
    value = _require_text(data, key)
    if not value.startswith(prefix) or len(value) == len(prefix):
        raise ValueError(f"invalid_{key}")
    return value


def _require_enum(
    data: dict[str, Any], key: str, allowed: frozenset[str]
) -> str:
    value = _require_text(data, key)
    if value not in allowed:
        raise ValueError(f"invalid_{key}")
    return value


def _require_aware_datetime(data: dict[str, Any], key: str) -> str:
    value = _require_text(data, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid_{key}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid_{key}")
    return value


@dataclass
class LongHorizonStage:
    id: str
    offset_days: int
    title: str
    hint: str = ""
    plan: str = ""
    status: LongHorizonStageStatus = "pending"
    due_at: str = ""
    cron_job_id: str = ""
    snooze_until: str = ""
    conclusion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LongHorizonStage:
        raw = _require_mapping(data, "stage")
        for key in (
            "offset_days",
            "plan",
            "hint",
            "due_at",
            "cron_job_id",
            "snooze_until",
            "conclusion",
        ):
            _require_key(raw, key)
        return cls(
            id=_require_id(raw, "id", "lhs_"),
            offset_days=int(raw["offset_days"]),
            title=_require_text(raw, "title"),
            hint=str(raw["hint"] or ""),
            plan=str(raw["plan"] or ""),
            status=_require_enum(  # type: ignore[arg-type]
                raw, "status", _STAGE_STATUSES
            ),
            due_at=_require_aware_datetime(raw, "due_at"),
            cron_job_id=str(raw["cron_job_id"] or ""),
            snooze_until=str(raw["snooze_until"] or ""),
            conclusion=str(raw["conclusion"] or ""),
        )


@dataclass
class Anchor:
    type: Literal["date", "month"] = "date"
    month: int = 8
    day: int | None = 1
    timezone: str = "Asia/Shanghai"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Anchor:
        raw = _require_mapping(data, "anchor")
        anchor_type = _require_enum(raw, "type", frozenset({"date", "month"}))
        try:
            month = int(_require_key(raw, "month"))
            day_raw = _require_key(raw, "day")
            day = int(day_raw) if day_raw is not None else None
            if anchor_type == "date" and day is None:
                raise ValueError("invalid_anchor_date")
            if day is None:
                date(2000, month, 1)
            else:
                date(2000, month, day)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_anchor_date") from exc
        timezone = _require_text(raw, "timezone")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid_timezone") from exc
        return cls(
            type=anchor_type,  # type: ignore[arg-type]
            month=month,
            day=day,
            timezone=timezone,
        )


@dataclass
class LongHorizonTask:
    id: str
    title: str
    kind: LongHorizonKind = "generic"
    anchor: Anchor = field(default_factory=Anchor)
    recurrence: Recurrence = "once"
    status: LongHorizonTaskStatus = "draft"
    exec_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    brief: str = ""
    stages: list[LongHorizonStage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "anchor": self.anchor.to_dict(),
            "recurrence": self.recurrence,
            "status": self.status,
            "exec_session_id": self.exec_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "brief": self.brief,
            "stages": [stage.to_dict() for stage in self.stages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LongHorizonTask:
        raw = _require_mapping(data, "task")
        for key in (
            "exec_session_id",
            "created_at",
            "updated_at",
            "brief",
            "stages",
        ):
            _require_key(raw, key)
        stages = raw["stages"]
        if not isinstance(stages, list):
            raise TypeError("stages_must_be_list")
        if not stages:
            raise ValueError("stages_required")
        if any(not isinstance(stage, dict) for stage in stages):
            raise TypeError("stage_must_be_object")
        return cls(
            id=_require_id(raw, "id", "lhc_"),
            title=_require_text(raw, "title"),
            kind=_require_enum(raw, "kind", _KINDS),  # type: ignore[arg-type]
            anchor=Anchor.from_dict(_require_key(raw, "anchor")),
            recurrence=_require_enum(  # type: ignore[arg-type]
                raw, "recurrence", _RECURRENCES
            ),
            status=_require_enum(raw, "status", _TASK_STATUSES),  # type: ignore[arg-type]
            exec_session_id=str(raw["exec_session_id"] or ""),
            created_at=_require_text(raw, "created_at"),
            updated_at=_require_text(raw, "updated_at"),
            brief=str(raw["brief"] or ""),
            stages=[LongHorizonStage.from_dict(stage) for stage in stages],
        )

    @classmethod
    def new(
        cls,
        *,
        title: str,
        kind: LongHorizonKind = "generic",
        month: int,
        day: int,
        recurrence: Recurrence = "once",
        timezone: str = "Asia/Shanghai",
    ) -> LongHorizonTask:
        now = touch_iso()
        return cls(
            id=_new_id("lhc_"),
            title=title.strip(),
            kind=kind,
            anchor=Anchor(month=month, day=day, timezone=timezone),
            recurrence=recurrence,
            created_at=now,
            updated_at=now,
        )


def new_stage_id() -> str:
    return _new_id("lhs_")
