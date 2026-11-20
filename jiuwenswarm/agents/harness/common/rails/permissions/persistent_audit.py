# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Append-only persistent audit for auto-permission decisions."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
    redact_reviewable_payload_text,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)

if TYPE_CHECKING:
    from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import ReviewerRecommendation

AUDIT_SCHEMA_VERSION = "auto_permission_audit_v1"
AUDIT_SUBDIRECTORY = "permission_audit"
AUDIT_FILENAME = "auto_permission.jsonl"
AUDIT_DIGEST_ALGORITHM = "sha256"
DATA_DIR_ENV_NAME = "JIUWENSWARM_DATA_DIR"
MAX_AUDIT_TEXT_LENGTH = 160
MAX_MODEL_RATIONALE_LENGTH = 1024
AUDIT_TEXT_REDACTED = "[redacted]"
EXTRA_ALLOWLIST = frozenset(
    {
        "authorization_outcome",
        "authorization_stage",
        "decision_source",
        "host_context_failure",
        "host_route_reason",
        "host_route_source",
        "record_kind",
        "reviewer_fallback_reason",
        "reviewer_lifecycle",
        "reviewer_outcome",
        "reviewer_reason_code",
        "reviewer_reason_summary",
        "stage_outcome",
    }
)
_SAFE_AUDIT_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.: -]{0,159}$")
_SAFE_NATURAL_LANGUAGE_AUDIT_TEXT_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9$_.: -]{0,159}$"
)
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s'\"\\]+/?)+")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[/\\][^\s'\"]+")
_SECRET_LIKE_PATTERN = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"(?:api[_-]?key|token|password|passwd|secret)\s*[:=]\s*\S+)"
)
_SHELL_CONTROL_PATTERN = re.compile(r"(&&|\|\||[;<>`$])")
_COMMAND_LIKE_PATTERN = re.compile(
    r"(?i)\b(?:bash|cat|cmd|cp|curl|find|git|grep|mv|powershell|python3?|rm|sh|"
    r"sudo|wget)\s+\S+"
)
_NATURAL_LANGUAGE_AUDIT_FIELDS = frozenset({"reviewer_reason_summary"})
_CLEAR_NATURAL_LANGUAGE_PATTERN = re.compile(
    r"(?i)\b(?:a|some|the)\s+cat food\b|\bplease\s+find me\b"
)
_CONTEXTUAL_PRICE_PATTERN = re.compile(
    r"(?i)\b(?:amount(?:s)?(?:\s+of)?|budget(?:ed)?|costs?|for|price(?:d)?"
    r"(?:\s+at)?|totals?|worth)\s+\$(?:0|[1-9]\d*)(?:\.\d{1,2})?\b"
)


@dataclass(frozen=True)
class LocalAuditDetails:
    """Host invocation identity and an optional model recommendation for local audit."""

    invocation_id: str
    recommendation: ReviewerRecommendation | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PersistentAuditWriteResult:
    """Result of one persistent audit write attempt."""

    persisted: bool
    degraded: bool
    reason: str
    path: Path


class PersistentAuditWriter:
    """Append sanitized permission audit records to a host-owned JSONL file."""

    def __init__(
        self,
        *,
        data_root: Path | str | None,
    ) -> None:
        self.data_root = Path(data_root) if data_root is not None else None

    @property
    def audit_path(self) -> Path:
        """Return the JSONL path under the host-owned audit root."""
        if self.data_root is None:
            return Path("")
        return self.data_root / AUDIT_SUBDIRECTORY / AUDIT_FILENAME

    def write(
        self,
        facts: ToolDecisionFacts,
        *,
        decision: str,
        reason: str,
        degraded: bool,
        grant_id: str = "",
        grant_reason: str = "",
        extra: Mapping[str, object] | None = None,
        local_details: LocalAuditDetails | None = None,
    ) -> PersistentAuditWriteResult:
        """Append one sanitized audit record without changing permission outcome."""
        if self.data_root is None:
            return PersistentAuditWriteResult(
                persisted=False,
                degraded=True,
                reason="audit_root_unavailable",
                path=Path(""),
            )

        audit_path = self.audit_path
        try:
            record = self._build_record(
                facts,
                decision=decision,
                reason=reason,
                degraded=degraded,
                grant_id=grant_id,
                grant_reason=grant_reason,
                extra=extra,
            )
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(audit_path, "a", encoding="utf-8", opener=_open_audit_file) as file_obj:
                if local_details is not None and _is_private_audit_file(file_obj.fileno()):
                    _add_local_details(record, local_details)
                file_obj.write(
                    json.dumps(
                        record,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                    + "\n"
                )
        except Exception:
            # Audit is observe-only. Bad values, encoders, or storage adapters
            # must never change the permission decision being observed.
            return PersistentAuditWriteResult(
                persisted=False,
                degraded=True,
                reason="audit_write_failed",
                path=audit_path,
            )
        return PersistentAuditWriteResult(
            persisted=True,
            degraded=False,
            reason="audit_persisted",
            path=audit_path,
        )

    @staticmethod
    def _build_record(
        facts: ToolDecisionFacts,
        *,
        decision: str,
        reason: str,
        degraded: bool,
        grant_id: str,
        grant_reason: str,
        extra: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        record = build_sanitized_audit_fields(
            facts,
            decision=decision,
            reason=reason,
            degraded=degraded,
            grant_id=grant_id,
            grant_reason=grant_reason,
            extra=extra,
        )
        record["schema_version"] = AUDIT_SCHEMA_VERSION
        record["timestamp"] = time.time()
        return record


def _open_audit_file(path: str, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)


def _is_private_audit_file(fd: int) -> bool:
    # On Linux, the mode also bounds the effective POSIX ACL mask. Other
    # platforms' access controls are not established by these checks.
    if sys.platform != "linux":
        return False
    info = os.fstat(fd)
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o077
    )


def _add_local_details(record: dict[str, Any], details: LocalAuditDetails) -> None:
    invocation_id = _sanitize_audit_text(details.invocation_id)
    if not invocation_id or invocation_id == AUDIT_TEXT_REDACTED:
        return
    record["invocation_id"] = invocation_id
    recommendation = details.recommendation
    if recommendation is not None and record.get("record_kind") == "permission_reviewer_assessment":
        record["model_recommendation"] = {
            "outcome": recommendation.outcome,
            "reason_code": _sanitize_audit_text(_sanitize_model_rationale(recommendation.reason_code)),
            "rationale": _sanitize_model_rationale(recommendation.rationale),
        }


def _sanitize_model_rationale(value: str) -> str:
    # Redact the complete value before truncating. Do not accept residual
    # sensitive patterns or invisible control characters as audit prose.
    text = redact_reviewable_payload_text(value, max_length=sys.maxsize)
    if not text.isprintable() or _audit_text_contains_sensitive_content(text):
        return AUDIT_TEXT_REDACTED
    if len(text) > MAX_MODEL_RATIONALE_LENGTH:
        return text[: MAX_MODEL_RATIONALE_LENGTH - 3] + "..."
    return text


def resolve_persistent_audit_root(config: Mapping[str, Any] | None) -> Path | None:
    """Resolve a host-owned audit data root from config or environment."""
    config_mapping = config if isinstance(config, Mapping) else {}
    raw_root = config_mapping.get("persistent_audit_root") or config_mapping.get(
        "audit_root"
    )
    auto_config = config_mapping.get("auto")
    if not raw_root and isinstance(auto_config, Mapping):
        raw_root = auto_config.get("persistent_audit_root") or auto_config.get(
            "audit_root"
        )
    if not raw_root:
        raw_root = os.environ.get(DATA_DIR_ENV_NAME)
    if not raw_root:
        return None
    return Path(str(raw_root)).expanduser()


def build_sanitized_audit_fields(
    facts: ToolDecisionFacts,
    *,
    decision: str,
    reason: str,
    degraded: bool,
    grant_id: str = "",
    grant_reason: str = "",
    extra: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Build the shared allowlisted, JSON-safe fields for every audit sink."""
    record: dict[str, Any] = {
        "decision": sanitize_audit_field("decision", decision),
        "degraded": bool(degraded),
        "accesses_known": facts.accesses_known,
        "path_counts": {
            "external": len(facts.external_paths),
            "read": len(facts.read_paths),
            "write": len(facts.write_paths),
        },
        "reason": sanitize_audit_field("reason", reason),
        "risk_tier": sanitize_audit_field("risk_tier", facts.capability.risk_tier),
        "side_effects": tuple(
            sanitize_audit_field("side_effect", item)
            for item in sorted(facts.capability.static_side_effects)
        ),
        "tool_category": sanitize_audit_field("tool_category", facts.tool_category),
        "tool_name": sanitize_audit_field("tool_name", facts.tool_name),
    }
    if grant_id:
        record["grant_id"] = sanitize_audit_field("grant_id", grant_id)
    if grant_reason:
        record["grant_reason"] = sanitize_audit_field("grant_reason", grant_reason)
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            normalized_key = str(key)
            if normalized_key in EXTRA_ALLOWLIST:
                record[normalized_key] = sanitize_audit_field(normalized_key, value)
    return record


def sanitize_audit_value(value: object) -> object:
    """Return a JSON-safe audit value with sensitive text redacted."""
    if isinstance(value, str):
        return _sanitize_audit_text(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            _sanitize_audit_text(key): sanitize_audit_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, tuple | list):
        return [sanitize_audit_value(item) for item in value]
    return _sanitize_audit_text(value)


def sanitize_audit_field(key: str, value: object) -> object:
    """Sanitize one allowlisted field with its narrow structured contract."""
    if key in _NATURAL_LANGUAGE_AUDIT_FIELDS and isinstance(value, str):
        return _sanitize_natural_language_audit_text(value)
    return sanitize_audit_value(value)


def _sanitize_audit_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _audit_text_contains_sensitive_content(text):
        return AUDIT_TEXT_REDACTED
    if _SAFE_AUDIT_TEXT_PATTERN.fullmatch(text):
        return text[:MAX_AUDIT_TEXT_LENGTH]
    return AUDIT_TEXT_REDACTED


def _sanitize_natural_language_audit_text(value: object) -> str:
    """Preserve safe reviewer prose without weakening strict audit fields."""
    text = str(value or "").strip()
    if not text:
        return ""
    # Mask only unambiguous prose and contextual prices before reusing the full
    # strict detector. Any unmasked command or dollar form remains protected.
    strict_view = _CLEAR_NATURAL_LANGUAGE_PATTERN.sub("natural phrase", text)
    strict_view = _CONTEXTUAL_PRICE_PATTERN.sub("price", strict_view)
    if "$" in strict_view or _audit_text_contains_sensitive_content(strict_view):
        return AUDIT_TEXT_REDACTED
    if _SAFE_NATURAL_LANGUAGE_AUDIT_TEXT_PATTERN.fullmatch(text):
        return text[:MAX_AUDIT_TEXT_LENGTH]
    return AUDIT_TEXT_REDACTED


def _audit_text_contains_sensitive_content(text: str) -> bool:
    return bool(
        _POSIX_ABSOLUTE_PATH_PATTERN.search(text)
        or _WINDOWS_ABSOLUTE_PATH_PATTERN.search(text)
        or _SECRET_LIKE_PATTERN.search(text)
        or _SHELL_CONTROL_PATTERN.search(text)
        or _COMMAND_LIKE_PATTERN.search(text)
    )
