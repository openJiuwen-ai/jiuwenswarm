# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Builders shared by the trajectory diagnosis tests."""

from __future__ import annotations

import json
from typing import Any

from jiuwenswarm.diagnosis.models import (
    DiagnosisInput,
    DiagnosisRecord,
    DiagnosisStatus,
    FullScope,
    ModeProfile,
    ModelInfo,
)

TRACE_A = "a" * 32
TRACE_B = "b" * 32
SESSION_ID = "session-1"


def span_id(index: int) -> str:
    """Return a valid 16-hex span id for ``index``."""
    return f"{index:016x}"


def _string(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def make_span(
    index: int,
    start: int,
    end: int,
    *,
    trace_id: str = TRACE_A,
    name: str = "span",
    operation: str | None = None,
    tool_name: str | None = None,
    tool_arguments: str | None = None,
    tool_result: str | None = None,
    output_text: str | None = None,
    tool_calls: bool = False,
    status_code: int = 0,
) -> dict[str, Any]:
    """Build one OTLP span dictionary."""
    attributes: list[dict[str, Any]] = []
    if operation:
        attributes.append(_string("gen_ai.operation.name", operation))
    if tool_name:
        attributes.append(_string("gen_ai.tool.name", tool_name))
    if tool_arguments is not None:
        attributes.append(_string("gen_ai.tool.call.arguments", tool_arguments))
    if tool_result is not None:
        attributes.append(_string("gen_ai.tool.call.result", tool_result))
    if output_text is not None or tool_calls:
        parts: list[dict[str, Any]] = []
        if output_text is not None:
            parts.append({"type": "text", "content": output_text})
        if tool_calls:
            parts.append({"type": "tool_call", "name": "x"})
        attributes.append(
            _string("gen_ai.output.messages", json.dumps([{"role": "assistant", "parts": parts}])),
        )
    return {
        "traceId": trace_id,
        "spanId": span_id(index),
        "name": name,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attributes,
        "status": {"code": status_code},
    }


def make_row(
    spans: list[dict[str, Any]],
    *,
    lifecycle: str = "final",
    sequences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap spans into a detail record as ``get_subject_records`` returns it."""
    return {
        "lifecycle": lifecycle,
        "trace_id": spans[0]["traceId"],
        "span_id": spans[0]["spanId"],
        "otlp": {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]},
        "sequences": sequences or {},
    }


class FakeReader:
    """In-memory stand-in for ``AsyncTrajectoryReader``."""

    def __init__(
        self,
        subjects: dict[str, list[dict[str, Any]]],
        *,
        page_size: int | None = None,
        chains: dict[str, list[str]] | None = None,
        blobs: dict[str, str] | None = None,
        kinds: dict[str, str] | None = None,
    ) -> None:
        self._subjects = subjects
        self._page_size = page_size
        self._chains = chains or {}
        self._blobs = blobs or {}
        self._kinds = kinds or {}
        self.record_calls = 0

    async def list_subjects(
        self,
        session_id: str,
        *,
        after_revision: int = 0,
    ) -> tuple[list[dict[str, Any]], str, int]:
        items = [
            {
                "subject_id": subject_id,
                "display_name": subject_id.title(),
                "kind": self._kinds.get(subject_id, "main_agent"),
            }
            for subject_id in self._subjects
        ]
        return items, "epoch", 0

    async def get_subject_records(
        self,
        session_id: str,
        subject_id: str,
        *,
        since_revision: int,
        limit: int,
        max_bytes: int = 0,
    ) -> dict[str, Any] | None:
        self.record_calls += 1
        rows = self._subjects.get(subject_id)
        if rows is None:
            return None
        size = self._page_size or len(rows) or 1
        window = rows[since_revision : since_revision + size]
        next_revision = since_revision + len(window)
        return {
            "records": window,
            "has_more": next_revision < len(rows),
            "next_since_revision": next_revision,
        }

    async def resolve_sequences(
        self,
        session_id: str,
        seq_hashes: Any,
        *,
        since_revision: int = 0,
    ) -> dict[str, Any] | None:
        return {
            "sequences": {key: self._chains[key] for key in seq_hashes if key in self._chains},
            "blobs": dict(self._blobs),
        }


def make_record(
    *,
    session_id: str = SESSION_ID,
    diagnosis_id: str = "dg_0123456789abcdef",
    ordinal: int = 1,
    status: DiagnosisStatus = DiagnosisStatus.COMPLETED,
    profile: ModeProfile = ModeProfile.AGENT_WORK,
) -> DiagnosisRecord:
    """Build a minimal persisted record."""
    return DiagnosisRecord(
        diagnosis_id=diagnosis_id,
        ordinal=ordinal,
        status=status,
        created_at="2026-10-15T00:00:00.000Z",
        scope_kind="full",
        session_id=session_id,
        input=DiagnosisInput(mode_profile=profile, model=ModelInfo(type="model", id="m"), scope=FullScope()),
    )
