# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-scoped in-memory secret vault keyed by HMAC fingerprints.

Plaintext never leaves process memory: the vault maps a session-keyed
fingerprint to the observed value, and tokens carry only that fingerprint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

MIN_SECRET_LENGTH = 8
MAX_SECRET_LENGTH = 512
MAX_VARIANT_SOURCE_LENGTH = 256
FINGERPRINT_LENGTH = 16
MAX_EVICTED_TOMBSTONES = 1024


@dataclass(slots=True)
class SecretEntry:
    """One observed value, addressed by its keyed fingerprint."""

    fp: str
    plaintext: str
    variants: tuple[str, ...] = field(default_factory=tuple)
    created_at: float = 0.0


def _encoded_variants(value: str) -> tuple[str, ...]:
    """Pre-compute whole-value encodings so matching needs no decoding."""
    if len(value) > MAX_VARIANT_SOURCE_LENGTH:
        return ()
    raw = value.encode("utf-8", "surrogatepass")
    return (
        base64.b64encode(raw).decode("ascii"),
        raw.hex(),
    )


class SecretVault:
    """Bounded LRU store of observed secrets plus the grant ledger."""

    def __init__(
        self,
        *,
        max_entries: int = 64,
        session_key: bytes | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = session_key or os.urandom(32)
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, SecretEntry] = OrderedDict()
        # Bounded tombstones let the execution gate tell an evicted token apart
        # from a fabricated one without retaining plaintext.
        self._evicted: OrderedDict[str, None] = OrderedDict()
        self.evicted_count = 0

    def fingerprint(self, value: str) -> str:
        return hmac.new(
            self._key, value.encode("utf-8", "surrogatepass"), hashlib.sha256
        ).hexdigest()[:FINGERPRINT_LENGTH]

    def remember(self, value: str, *, defer_evict: bool = False) -> SecretEntry | None:
        """Store a candidate value; returns the entry only when it is new.

        ``defer_evict`` keeps one batch of candidates addressable until the
        caller finishes rewriting the current result; call ``evict_to_limit``
        afterwards.
        """
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not MIN_SECRET_LENGTH <= len(stripped) <= MAX_SECRET_LENGTH:
            return None
        fp = self.fingerprint(stripped)
        existing = self._entries.get(fp)
        if existing is not None:
            self._entries.move_to_end(fp)
            return None
        entry = SecretEntry(
            fp=fp,
            plaintext=stripped,
            variants=_encoded_variants(stripped),
            created_at=self._clock(),
        )
        self._entries[fp] = entry
        if not defer_evict:
            self.evict_to_limit()
        return entry

    def evict_to_limit(self) -> list[SecretEntry]:
        """Trim to the configured bound and return the evicted entries."""
        evicted: list[SecretEntry] = []
        while len(self._entries) > self._max_entries:
            _, entry = self._entries.popitem(last=False)
            self._remember_evicted(entry.fp)
            self.evicted_count += 1
            evicted.append(entry)
        return evicted

    def _remember_evicted(self, fp: str) -> None:
        self._evicted[fp] = None
        self._evicted.move_to_end(fp)
        while len(self._evicted) > MAX_EVICTED_TOMBSTONES:
            self._evicted.popitem(last=False)

    def was_evicted(self, fp: str) -> bool:
        return fp in self._evicted

    def entry(self, fp: str) -> SecretEntry | None:
        return self._entries.get(fp)

    def entries(self) -> tuple[SecretEntry, ...]:
        return tuple(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


__all__ = [
    "FINGERPRINT_LENGTH",
    "MAX_SECRET_LENGTH",
    "MIN_SECRET_LENGTH",
    "SecretEntry",
    "SecretVault",
]
