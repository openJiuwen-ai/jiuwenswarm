# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Versioned, serializable contracts emitted by :mod:`evidence_rail`.

The contracts deliberately persist hashes and selectors instead of raw tool
arguments or outputs.  This keeps the audit trail useful without turning it
into a second store for credentials or sensitive payloads.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "1.0"

EvidenceStatus = Literal["verified", "unverified", "rejected"]
ReceiptStatus = Literal["started", "succeeded", "retry_requested", "failed"]
RunStatus = Literal["running", "completed", "failed", "blocked"]


class EvidenceItem(BaseModel):
    """A minimal evidence record safe to project into model context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    evidence_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: str = Field(min_length=1)
    status: EvidenceStatus = "verified"
    content_digest: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextDigest(BaseModel):
    """Receipt for the evidence projection used for one model call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    run_id: str
    sequence: int = Field(ge=1)
    created_at: str
    digest: str
    evidence_ids: list[str] = Field(default_factory=list)


class ToolReceipt(BaseModel):
    """Hash-only receipt for one tool attempt."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    receipt_id: str
    run_id: str
    tool_name: str
    args_digest: str
    status: ReceiptStatus
    started_at: str
    ended_at: str | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    output_digest: str | None = None
    reason_code: str | None = None
    retry_index: int = Field(default=0, ge=0)
    retryable: bool = False
    error_type: str | None = None
    error_digest: str | None = None


class RunManifest(BaseModel):
    """Replay-oriented summary for one invocation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    run_id: str
    status: RunStatus
    started_at: str
    ended_at: str | None = None
    query_digest: str
    conversation_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    context_digests: list[str] = Field(default_factory=list)
    tool_receipt_ids: list[str] = Field(default_factory=list)
    recovery_count: int = Field(default=0, ge=0)
    terminal_reason_code: str | None = None
    output_digest: str | None = None


__all__ = [
    "SCHEMA_VERSION",
    "ContextDigest",
    "EvidenceItem",
    "RunManifest",
    "ToolReceipt",
]
