# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify SSH public-key fingerprints via KeyRegistry."""

from __future__ import annotations

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)
from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import KeyRegistry, KeyRegistryEntry


class SshPublicKeyAuthenticator(CredentialAuthenticator):
    """Look up an SSH fingerprint (+ username) and return AuthResult."""

    def __init__(self, registry: KeyRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> KeyRegistry:
        return self._registry

    def lookup_entry(self, fingerprint: str) -> KeyRegistryEntry | None:
        return self._registry.lookup(fingerprint)

    def verify(self, *, fingerprint: str, username: str = "") -> AuthResult:
        """Sync verify for SSHServer callbacks that cannot await."""
        fingerprint = str(fingerprint or "").strip()
        username = str(username or "").strip()
        if not fingerprint:
            return AuthResult(
                success=False,
                error="缺少 SSH fingerprint",
                extensions={"error_code": "MISSING_FINGERPRINT"},
            )
        entry = self._registry.lookup(fingerprint)
        if entry is None:
            return AuthResult(
                success=False,
                error="未登记的 SSH 公钥",
                extensions={"error_code": "UNKNOWN_FINGERPRINT"},
            )
        if username and username != entry.username:
            return AuthResult(
                success=False,
                error="SSH username 与登记身份不一致",
                extensions={
                    "error_code": "USERNAME_MISMATCH",
                    "expected_username": entry.username,
                },
            )
        return AuthResult(
            success=True,
            user_id=entry.user_id,
            extensions={
                "username": entry.username,
                "auth_method": "ssh_public_key",
                "source": entry.source,
                "session_id": entry.session_id,
                "fingerprint": entry.fingerprint,
            },
        )

    async def authenticate(self, context: AuthContext) -> AuthResult:
        credentials = context.credentials or {}
        return self.verify(
            fingerprint=str(credentials.get("fingerprint") or ""),
            username=str(credentials.get("username") or ""),
        )
