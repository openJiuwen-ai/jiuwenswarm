# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Issue short-lived SSH key pairs and register public fingerprints."""

from __future__ import annotations

import logging
import time
from typing import Protocol

from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import (
    KeyRegistry,
    KeyRegistryEntry,
)

logger = logging.getLogger(__name__)


class SshKeyIssuer(Protocol):
    """Issue short-lived SSH key pairs for TUI / launcher handoff."""

    def issue_ephemeral_key(
        self,
        *,
        user_id: str,
        username: str,
        session_id: str,
        ttl_sec: float,
    ) -> str:
        """Generate a key pair, register the public fingerprint, return private key."""
        ...


class AgentOSSshKeyIssuer:
    """Generate OpenSSH ephemeral keys and register fingerprints in KeyRegistry."""

    def __init__(
        self,
        registry: KeyRegistry,
        *,
        key_type: str = "ssh-ed25519",
    ) -> None:
        self._registry = registry
        self._key_type = str(key_type or "ssh-ed25519").strip() or "ssh-ed25519"

    @property
    def registry(self) -> KeyRegistry:
        return self._registry

    def issue_ephemeral_key(
        self,
        *,
        user_id: str,
        username: str,
        session_id: str,
        ttl_sec: float,
    ) -> str:
        try:
            import asyncssh
        except ImportError as exc:
            raise RuntimeError(
                "SSH ephemeral key issuance requires optional dependency "
                "`asyncssh>=2.14.0,<2.24`. Install with "
                '`uv sync --extra ssh` / `pip install "jiuwenswarm[ssh]"`.'
            ) from exc

        key = asyncssh.generate_private_key(self._key_type)
        fingerprint = key.get_fingerprint()
        now = time.time()
        ttl = max(0.0, float(ttl_sec))
        self._registry.register(
            KeyRegistryEntry(
                fingerprint=fingerprint,
                user_id=str(user_id or "").strip(),
                username=str(username or user_id or "").strip() or "unknown",
                source="tui_switch",
                session_id=str(session_id or "").strip() or None,
                expires_at=(now + ttl) if ttl > 0 else None,
                created_at=now,
            )
        )
        logger.info(
            "[AgentOSAuth] issued ephemeral SSH key: user_id=%s session=%s ttl=%.0fs fp=%s",
            user_id,
            session_id,
            ttl,
            fingerprint,
        )
        exported = key.export_private_key("openssh")
        if isinstance(exported, bytes):
            return exported.decode("utf-8")
        return str(exported)
