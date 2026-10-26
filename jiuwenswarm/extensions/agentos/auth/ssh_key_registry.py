# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SSH public-key fingerprint registry for AgentOS identity mapping."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class KeyRegistryEntry:
    """Map an SSH public-key fingerprint to a business identity."""

    fingerprint: str
    user_id: str
    username: str
    source: str  # "tui_switch" | "container"
    session_id: str | None
    expires_at: float | None
    created_at: float


class KeyRegistry:
    """In-memory SSH public-key fingerprint -> identity registry."""

    def __init__(self) -> None:
        self._entries: dict[str, KeyRegistryEntry] = {}

    def register(self, entry: KeyRegistryEntry) -> None:
        """Register or overwrite one public-key mapping."""
        self._entries[entry.fingerprint] = entry

    def lookup(self, fingerprint: str) -> KeyRegistryEntry | None:
        """Look up by fingerprint; drop and return None when expired."""
        entry = self._entries.get(fingerprint)
        if entry is None:
            return None
        if self._is_expired(entry):
            self._entries.pop(fingerprint, None)
            return None
        return entry

    def revoke(self, fingerprint: str) -> bool:
        """Revoke one registration. Return True when an entry was removed."""
        return self._entries.pop(fingerprint, None) is not None

    def cleanup_expired(self) -> int:
        """Remove expired entries. Return the number removed."""
        expired_fps = [fp for fp, e in self._entries.items() if self._is_expired(e)]
        for fp in expired_fps:
            self._entries.pop(fp, None)
        return len(expired_fps)

    @staticmethod
    def _is_expired(entry: KeyRegistryEntry) -> bool:
        return entry.expires_at is not None and time.time() >= entry.expires_at
