"""SSH public-key authenticator and key issuer."""

from __future__ import annotations

import time

import pytest

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)
from jiuwenswarm.extensions.agentos.auth.ssh_authenticator import SshPublicKeyAuthenticator
from jiuwenswarm.extensions.agentos.auth.ssh_key_issuer import AgentOSSshKeyIssuer
from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import (
    KeyRegistry,
    KeyRegistryEntry,
)


def _register(registry: KeyRegistry, *, fingerprint: str, user_id: str, username: str) -> None:
    now = time.time()
    registry.register(
        KeyRegistryEntry(
            fingerprint=fingerprint,
            user_id=user_id,
            username=username,
            source="tui_switch",
            session_id="sess-1",
            expires_at=now + 60,
            created_at=now,
        )
    )


def test_ssh_authenticator_is_credential_authenticator():
    auth = SshPublicKeyAuthenticator(KeyRegistry())
    assert isinstance(auth, CredentialAuthenticator)


def test_verify_accepts_registered_fingerprint_and_username():
    registry = KeyRegistry()
    _register(registry, fingerprint="SHA256:ok", user_id="u1", username="u1")
    auth = SshPublicKeyAuthenticator(registry)
    result = auth.verify(fingerprint="SHA256:ok", username="u1")
    assert result.success is True
    assert result.user_id == "u1"
    assert result.extensions["username"] == "u1"
    assert result.extensions["auth_method"] == "ssh_public_key"


def test_verify_rejects_unknown_fingerprint():
    auth = SshPublicKeyAuthenticator(KeyRegistry())
    result = auth.verify(fingerprint="SHA256:missing", username="u1")
    assert result.success is False
    assert result.extensions["error_code"] == "UNKNOWN_FINGERPRINT"


def test_verify_rejects_username_mismatch():
    registry = KeyRegistry()
    _register(registry, fingerprint="SHA256:ok", user_id="u1", username="u1")
    auth = SshPublicKeyAuthenticator(registry)
    result = auth.verify(fingerprint="SHA256:ok", username="other")
    assert result.success is False
    assert result.extensions["error_code"] == "USERNAME_MISMATCH"


@pytest.mark.asyncio
async def test_authenticate_uses_context_credentials():
    registry = KeyRegistry()
    _register(registry, fingerprint="SHA256:ok", user_id="u1", username="u1")
    auth = SshPublicKeyAuthenticator(registry)
    result = await auth.authenticate(
        AuthContext(
            channel_type="ssh",
            credentials={"fingerprint": "SHA256:ok", "username": "u1"},
        )
    )
    assert isinstance(result, AuthResult)
    assert result.success is True
    assert result.user_id == "u1"


def test_issue_ephemeral_key_registers_fingerprint_for_authenticator():
    asyncssh = pytest.importorskip("asyncssh")
    registry = KeyRegistry()
    issuer = AgentOSSshKeyIssuer(registry)
    private_key = issuer.issue_ephemeral_key(
        user_id="u1",
        username="u1",
        session_id="sess-9",
        ttl_sec=120,
    )
    assert "BEGIN OPENSSH PRIVATE KEY" in private_key
    loaded = asyncssh.import_private_key(private_key.encode("utf-8"))
    fingerprint = loaded.get_fingerprint()
    auth = SshPublicKeyAuthenticator(registry)
    result = auth.verify(fingerprint=fingerprint, username="u1")
    assert result.success is True
    assert result.user_id == "u1"
