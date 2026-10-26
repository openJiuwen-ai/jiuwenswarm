# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.protocol.ssh.config import proxy_config_from_dict
from jiuwenswarm.extensions.agentos.auth.ssh_key_registry import (
    KeyRegistry,
    KeyRegistryEntry,
)
from jiuwenswarm.gateway.channel_manager.protocol.ssh.server import ProxySSHServer, SSHProxy
from jiuwenswarm.gateway.channel_manager.protocol.ssh.ssh_connect import (
    SshAuthConfig,
    SshChannel,
    SshChannelConfig,
)


def test_ssh_channel_config_from_dict():
    conf = {
        "enabled": True,
        "listen_host": "127.0.0.1",
        "listen_port": 3333,
        "relay_timeout_sec": 120,
    }
    cfg = SshChannelConfig.from_dict(conf)
    assert cfg.enabled is True
    assert cfg.listen_port == 3333
    assert cfg.relay_timeout_sec == 120.0
    assert cfg.auth.enabled is False
    assert cfg.auth.ephemeral_key_ttl_sec == 300.0

    proxy = cfg.to_proxy_config()
    assert proxy.listen_host == "127.0.0.1"
    assert proxy.listen_port == 3333


def test_ssh_channel_config_parses_auth_block():
    cfg = SshChannelConfig.from_dict(
        {
            "enabled": True,
            "auth": {
                "enabled": True,
                "ephemeral_key_ttl_sec": 120,
                "ephemeral_key_type": "ssh-ed25519",
                "cleanup_interval_sec": 30,
            },
        }
    )
    assert cfg.auth.enabled is True
    assert cfg.auth.ephemeral_key_ttl_sec == 120.0
    assert cfg.auth.ephemeral_key_type == "ssh-ed25519"
    assert cfg.auth.cleanup_interval_sec == 30.0


def test_proxy_config_from_dict_sets_default_host_key_path():
    proxy = proxy_config_from_dict({"listen_port": 2222})
    assert proxy.host_key_path.endswith("ssh_host_key")


def test_key_registry_lookup_and_expiry():
    registry = KeyRegistry()
    now = time.time()
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:alive",
            user_id="u1",
            username="u1",
            source="tui_switch",
            session_id="sess-1",
            expires_at=now + 60,
            created_at=now,
        )
    )
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:expired",
            user_id="u2",
            username="u2",
            source="tui_switch",
            session_id="sess-2",
            expires_at=now - 1,
            created_at=now - 10,
        )
    )

    alive = registry.lookup("SHA256:alive")
    assert alive is not None
    assert alive.user_id == "u1"
    assert registry.lookup("SHA256:expired") is None
    assert registry.lookup("SHA256:missing") is None


def test_key_registry_cleanup_expired():
    registry = KeyRegistry()
    now = time.time()
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:keep",
            user_id="u1",
            username="u1",
            source="tui_switch",
            session_id=None,
            expires_at=None,
            created_at=now,
        )
    )
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:drop",
            user_id="u2",
            username="u2",
            source="tui_switch",
            session_id="s",
            expires_at=now - 5,
            created_at=now - 10,
        )
    )
    assert registry.cleanup_expired() == 1
    assert registry.lookup("SHA256:keep") is not None
    assert registry.lookup("SHA256:drop") is None


def test_proxy_auth_disabled_accepts_any_key_and_password():
    server = ProxySSHServer(
        proxy_config_from_dict({}),
        key_registry=KeyRegistry(),
        auth_enabled=False,
    )
    key = SimpleNamespace(get_fingerprint=lambda: "SHA256:anything")
    assert server.password_auth_supported() is True
    assert server.validate_public_key("alice", key) is True
    assert server.validate_password("alice", "secret") is True
    assert server.authenticated_entry is None


def test_proxy_auth_enabled_rejects_unregistered_key():
    registry = KeyRegistry()
    server = ProxySSHServer(
        proxy_config_from_dict({}),
        key_registry=registry,
        auth_enabled=True,
    )
    key = SimpleNamespace(get_fingerprint=lambda: "SHA256:unknown")
    assert server.password_auth_supported() is False
    assert server.validate_public_key("alice", key) is False
    assert server.validate_password("alice", "secret") is False
    assert server.authenticated_entry is None


def test_proxy_auth_enabled_accepts_registered_key():
    registry = KeyRegistry()
    now = time.time()
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:ok",
            user_id="user-42",
            username="user-42",
            source="tui_switch",
            session_id="sess-1",
            expires_at=now + 60,
            created_at=now,
        )
    )
    server = ProxySSHServer(
        proxy_config_from_dict({}),
        key_registry=registry,
        auth_enabled=True,
    )
    key = SimpleNamespace(get_fingerprint=lambda: "SHA256:ok")
    assert server.validate_public_key("user-42", key) is True
    assert server.authenticated_entry is not None
    assert server.authenticated_entry.user_id == "user-42"


def test_proxy_auth_enabled_rejects_registered_key_for_different_username():
    registry = KeyRegistry()
    now = time.time()
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:ok",
            user_id="user-a",
            username="user-a",
            source="tui_switch",
            session_id="sess-1",
            expires_at=now + 60,
            created_at=now,
        )
    )
    server = ProxySSHServer(
        proxy_config_from_dict({}),
        key_registry=registry,
        auth_enabled=True,
    )
    key = SimpleNamespace(get_fingerprint=lambda: "SHA256:ok")

    assert server.validate_public_key("user-b", key) is False
    assert server.authenticated_entry is None


@pytest.mark.asyncio
async def test_handle_ssh_client_uses_authenticated_identity(monkeypatch):
    asyncssh = pytest.importorskip("asyncssh")

    registry = KeyRegistry()
    now = time.time()
    registry.register(
        KeyRegistryEntry(
            fingerprint="SHA256:ok",
            user_id="auth-user",
            username="auth-user",
            source="tui_switch",
            session_id="sess-1",
            expires_at=now + 60,
            created_at=now,
        )
    )
    proxy_server = ProxySSHServer(
        proxy_config_from_dict({}),
        key_registry=registry,
        auth_enabled=True,
    )
    key = SimpleNamespace(get_fingerprint=lambda: "SHA256:ok")
    assert proxy_server.validate_public_key("auth-user", key) is True

    captured: dict = {}

    async def register_session(**kwargs):
        captured["register"] = kwargs

    async def submit_relay(session_id, metadata):
        captured["session_id"] = session_id
        captured["metadata"] = metadata

    async def wait_relay_done(session_id):
        del session_id
        return 0

    async def unregister_session(session_id):
        captured["unregistered"] = session_id

    hooks = SimpleNamespace(
        register_session=register_session,
        submit_relay=submit_relay,
        wait_relay_done=wait_relay_done,
        unregister_session=unregister_session,
    )
    proxy = SSHProxy(
        proxy_config_from_dict({}),
        agent_hooks=hooks,
        key_registry=registry,
        auth_enabled=True,
    )

    conn = SimpleNamespace(get_extra_info=lambda name: proxy_server if name == "proxy_auth" else None)
    channel = SimpleNamespace(get_connection=lambda: conn)
    process = SimpleNamespace(
        channel=channel,
        get_extra_info=lambda name: "ssh-name" if name == "username" else None,
        subsystem=None,
        command=None,
        stdout=SimpleNamespace(write=lambda data: None),
        exit=lambda code: None,
    )

    # Avoid depending on asyncssh.Error path; keep exception type available.
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.protocol.ssh.server.asyncssh",
        asyncssh,
        raising=False,
    )
    await proxy._handle_ssh_client(process)

    assert captured["metadata"]["user_id"] == "auth-user"
    assert captured["metadata"]["username"] == "auth-user"
    assert captured["metadata"]["auth_source"] == "tui_switch"
    assert captured["register"]["username"] == "auth-user"


def test_issue_ephemeral_key_registers_fingerprint():
    asyncssh = pytest.importorskip("asyncssh")

    channel = SshChannel(
        SshChannelConfig(
            enabled=True,
            auth=SshAuthConfig(enabled=True, ephemeral_key_ttl_sec=120),
        ),
        router=SimpleNamespace(),
        key_registry=KeyRegistry(),
    )
    private_key = channel.issue_ephemeral_key(
        user_id="u1",
        username="u1",
        session_id="sess-9",
        ttl_sec=120,
    )
    assert "BEGIN OPENSSH PRIVATE KEY" in private_key
    assert "END OPENSSH PRIVATE KEY" in private_key

    loaded = asyncssh.import_private_key(private_key.encode("utf-8"))
    fingerprint = loaded.get_fingerprint()
    entry = channel.key_registry.lookup(fingerprint)
    assert entry is not None
    assert entry.user_id == "u1"
    assert entry.source == "tui_switch"
    assert entry.session_id == "sess-9"


@pytest.mark.asyncio
async def test_submit_relay_uses_metadata_user_id():
    channel = SshChannel(
        SshChannelConfig(enabled=True),
        router=SimpleNamespace(),
        key_registry=KeyRegistry(),
    )
    process = SimpleNamespace(stdout=SimpleNamespace(write=lambda data: None))
    await channel._register_session(
        session_id="ssh_abc",
        process=process,
        username="auth-user",
        client_addr="127.0.0.1:1",
    )
    seen: list = []

    def on_message(msg):
        seen.append(msg)

    channel.on_message(on_message)
    await channel._submit_relay(
        "ssh_abc",
        {
            "username": "auth-user",
            "user_id": "auth-user",
            "auth_source": "tui_switch",
        },
    )
    assert len(seen) == 1
    assert seen[0].user_id == "auth-user"
