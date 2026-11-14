# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Credential-field registry: which config paths hold credential values.

Loaded from ``resources/credential_fields.json`` next to the package root.
Entries are glob patterns matched against the dotted field path built by
``resolve_env_vars`` (e.g. ``models.defaults.model_client_config.api_key``;
list indices are absent because the recursive walk does not carry them —
a single ``*`` segment matches both bare and indexed forms).

Matching is per-segment (split on ``.``, equal segment count, then fnmatch
per segment): a whole-string fnmatch would let ``*`` cross dots, hitting
arbitrary-depth paths outside the declared coverage.
"""

from __future__ import annotations

import fnmatch
import json
import threading
from pathlib import Path

_REGISTRY_FILE = Path(__file__).parents[2] / "resources" / "credential_fields.json"
_CACHE_LOCK = threading.Lock()
_CACHE: list[list[str]] | None = None


def _load_patterns() -> list[list[str]]:
    """Load registry patterns once per process; unreadable file means no fields.

    The hot path reads ``_CACHE`` without the lock: module-level attribute
    assignment is atomic in CPython, and a torn read is impossible because
    the cache is written once (worst case a racing reader loads again under
    the lock and gets the same value).
    """
    global _CACHE
    cache = _CACHE
    if cache is not None:
        return cache
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
            patterns = data.get("fields") if isinstance(data, dict) else None
            _CACHE = [str(p).split(".") for p in patterns] if isinstance(patterns, list) else []
        except (OSError, ValueError):
            # Missing/corrupt registry: no field is a credential reference.
            # Degrades to current behavior (no exec: resolution) instead of
            # breaking every get_config() call.
            _CACHE = []
        return _CACHE


def is_credential_field(field_path: str) -> bool:
    """Return True when the dotted field path matches a registry pattern.

    Segment-wise match: split both path and pattern on ``.``, require equal
    length, then fnmatch each segment. This is what keeps ``*`` from
    crossing dots (e.g. ``models.x.y.z.model_client_config.api_key`` is
    rejected — four segments under ``models`` vs the pattern's three).
    """
    if not field_path:
        return False
    segments = field_path.split(".")
    for pat_segments in _load_patterns():
        if len(segments) != len(pat_segments):
            continue
        if all(fnmatch.fnmatch(s, p) for s, p in zip(segments, pat_segments)):
            return True
    return False
