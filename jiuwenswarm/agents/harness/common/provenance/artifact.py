# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic artifact and evidence provenance normalization.

This module handles caller-supplied references only. It never reads artifact
content or computes a digest from a file.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, TypedDict

from jiuwenswarm.common.utils import mask_sensitive


MAX_ARTIFACT_REFS = 256
_SENSITIVE_KEY = re.compile(
    r"authorization|cookie|api[_-]?key|password|bearer|request[_-]?headers?"
    r"|environment(?:[_-]?variables?)?|env[_-]?vars?",
    re.IGNORECASE,
)
_STRUCTURED_HASH = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_LOCAL_ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_LOCAL_FILE_URI = re.compile(
    r"^file://(?:localhost(?:/|$)|/|[A-Za-z]:[\\/])", re.IGNORECASE
)
_FREE_TEXT_SECRET = (
    re.compile(r"(?i)(\bAuthorization\s*:\s*Bearer\s+)([^\s,;\]}\)]+)"),
    re.compile(r"(?i)(\bBearer\s+)([^\s,;\]}\)]+)"),
    re.compile(r"(?i)(\bapi[_-]?key\s*[:=]\s*)([^\s,;\]}\)]+)"),
    re.compile(r"(?i)(\bpassword\s*[:=]\s*)([^\s,;\]}\)]+)"),
)
_REF_FIELDS = frozenset(
    {
        "artifact_id",
        "evidence_id",
        "uri",
        "path",
        "name",
        "mime_type",
        "content_hash",
        "hash",
        "source",
        "producer",
        "task_id",
        "stage_id",
        "created_at",
        "metadata",
    }
)


class SourceProvenance(TypedDict, total=False):
    type: str
    uri: str
    identifier: str
    metadata: dict[str, Any]


class ProducerProvenance(TypedDict, total=False):
    agent_id: str
    tool_name: str
    tool_call_id: str
    session_id: str
    task_id: str
    stage_id: str


@dataclass
class ArtifactProvenance:
    """Canonical, optional provenance fields for one artifact reference."""

    artifact_id: str | None = None
    evidence_id: str | None = None
    uri: str | None = None
    name: str | None = None
    mime_type: str | None = None
    content_hash: str | None = None
    source: dict[str, Any] | None = None
    producer: dict[str, Any] | None = None
    task_id: str | None = None
    stage_id: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return normalize_artifact_ref(asdict(self)) or {}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value).lower()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _sanitize(value: Any, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "******"
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, key) for item in value]
    return _json_safe(value)


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ArtifactProvenance):
        return asdict(value)
    if is_dataclass(value):
        raw = asdict(value)
        return raw if isinstance(raw, dict) else None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict()
        except Exception:
            raw = None
        if isinstance(raw, dict):
            return raw
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _content_hash(value: Any) -> str | None:
    text = _text(value)
    if text is None or not _STRUCTURED_HASH.fullmatch(text):
        return None
    algorithm, digest = text.split(":", 1)
    return f"{algorithm.lower()}:{digest}"


def _sanitize_text(value: Any) -> str:
    text = str(value)
    for pattern in _FREE_TEXT_SECRET:
        text = pattern.sub(r"\1******", text)
    return mask_sensitive(text)


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _provenance_mapping(value: Any) -> dict[str, Any] | None:
    raw = _mapping(value)
    if not isinstance(raw, dict):
        return None
    return _sanitize(raw)


def sanitize_provenance_value(value: Any) -> Any:
    """Return a JSON-safe recursively sanitized provenance payload."""
    return _sanitize(_json_safe(value))


def normalize_artifact_ref(value: Any) -> dict[str, Any] | None:
    """Normalize one explicit artifact reference without touching its content."""
    if isinstance(value, str):
        raw: dict[str, Any] = {"uri": value}
    else:
        raw = _mapping(value) or {}
    nested = _mapping(raw.get("artifact_provenance"))
    payload: dict[str, Any] = dict(nested or {})
    for key, item in raw.items():
        if key in _REF_FIELDS and key != "artifact_provenance":
            payload[key] = item
    if not payload:
        return None

    metadata: dict[str, Any] = {}
    nested_metadata = payload.get("metadata")
    if isinstance(nested_metadata, dict):
        metadata.update(nested_metadata)
    top_metadata = raw.get("metadata")
    if isinstance(top_metadata, dict):
        metadata.update(top_metadata)
    metadata = _sanitize(_json_safe(metadata))
    if not isinstance(metadata, dict):
        metadata = {}

    content_hash = _content_hash(
        payload.get("content_hash")
        if payload.get("content_hash") is not None
        else payload.get("hash")
    )
    source = _provenance_mapping(payload.get("source"))
    producer = _provenance_mapping(payload.get("producer"))

    normalized: dict[str, Any] = {}
    explicit_id = _text(payload.get("artifact_id"))
    if explicit_id:
        artifact_id = explicit_id
    elif content_hash:
        artifact_id = f"artifact-{_canonical_hash({'content_hash': content_hash})}"
    else:
        basis = {
            key: payload.get(key)
            for key in ("uri", "path", "name", "mime_type")
            if _text(payload.get(key)) is not None
        }
        if source:
            for key in ("type", "uri", "identifier"):
                text = _text(source.get(key))
                if text is not None:
                    basis[f"source_{key}"] = text
        if not basis:
            return None
        artifact_id = f"artifact-{_canonical_hash(basis)}"
    normalized["artifact_id"] = artifact_id

    evidence_id = _text(payload.get("evidence_id")) or artifact_id
    normalized["evidence_id"] = evidence_id
    for key in ("uri", "path", "name", "mime_type", "task_id", "stage_id", "created_at"):
        text = _text(payload.get(key))
        if text is not None:
            normalized[key] = _sanitize(text, key)
    if content_hash:
        normalized["content_hash"] = content_hash
    if source:
        normalized["source"] = source
    if producer:
        normalized["producer"] = producer
    normalized["metadata"] = metadata
    return _sanitize(_json_safe(normalized))


def _is_local_absolute_reference(value: Any) -> bool:
    text = _text(value)
    if text is None:
        return False
    return bool(_LOCAL_ABSOLUTE_PATH.match(text) or _LOCAL_FILE_URI.match(text))


def prepare_artifact_provenance_for_external(
    value: Any,
) -> list[dict[str, Any]]:
    """Project explicit provenance without exposing local absolute paths."""
    projected: list[dict[str, Any]] = []
    for normalized in normalize_artifact_refs(value):
        item = dict(normalized)
        item.pop("path", None)
        if _is_local_absolute_reference(item.get("uri")):
            item.pop("uri", None)
        source = item.get("source")
        if isinstance(source, dict):
            source_copy = dict(source)
            if _is_local_absolute_reference(source_copy.get("uri")):
                source_copy.pop("uri", None)
            item["source"] = source_copy
        projected.append(item)
    return projected


def normalize_artifact_refs(value: Any) -> list[dict[str, Any]]:
    """Normalize, stably deduplicate, and bound a collection of references."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        normalized = normalize_artifact_ref(item)
        if not normalized:
            continue
        artifact_id = str(normalized["artifact_id"])
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        output.append(normalized)
        if len(output) >= MAX_ARTIFACT_REFS:
            break
    return output


def extract_explicit_artifact_provenance(value: Any) -> list[dict[str, Any]]:
    """Extract only an explicitly named artifact_provenance namespace."""
    raw = _mapping(value)
    if not isinstance(raw, dict) or "artifact_provenance" not in raw:
        return []
    return normalize_artifact_refs(raw.get("artifact_provenance"))


__all__ = [
    "ArtifactProvenance",
    "MAX_ARTIFACT_REFS",
    "ProducerProvenance",
    "SourceProvenance",
    "extract_explicit_artifact_provenance",
    "normalize_artifact_ref",
    "sanitize_provenance_value",
    "normalize_artifact_refs",
    "prepare_artifact_provenance_for_external",
]
