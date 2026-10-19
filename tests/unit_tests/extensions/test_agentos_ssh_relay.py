# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the AgentOS Router southbound SSH relay into YuanRong."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentManager
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    SshChannelEndpoint,
    load_router_config,
)
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import (
    DEFAULT_CLIENT_KEYS_DIR,
    DEFAULT_SSH_USER_TEMPLATE,
    YuanrongSshRelay,
    YuanrongSshSettings,
    _is_ssh_connect_retryable,
    list_client_key_paths,
    load_yuanrong_ssh_settings,
    resolve_client_keys_dir,
)
from jiuwenswarm.gateway.channel_manager.protocol.ssh.ssh_connect import SshRelaySession

from tests.unit_tests.extensions.test_agentos_router import (
    FakeRegistryClient,
    FakeYuanRongClient,
)


def _relay_session(session_id: str = "ssh_alice_1234") -> SshRelaySession:
    return SshRelaySession(
        session_id=session_id,
        process=None,
    )


def _ssh_envelope(
    session: SshRelaySession | None = None,
    *,
    agent_type: str | None = "jiuwenswarm",
    session_id: str | None = None,
    command: str | None = None,
) -> E2AEnvelope:
    params: dict[str, Any] = {}
    if session is not None:
        params["relay_session"] = session
    if agent_type is not None:
        params["agent_type"] = agent_type
    if command is not None:
        params["command"] = command
        if session is not None:
            session.command = command
    return E2AEnvelope(
        request_id="req-ssh-1",
        channel="ssh",
        user_id="alice",
        session_id=session_id or (session.session_id if session else "ssh_missing"),
        method=ReqMethod.SSH_RELAY.value,
        params=params,
    )


class StubSshRelay:
    """Records relay invocations and resolves the session like the real relay."""

    def __init__(
        self,
        *,
        backend_host: str = "frontend.yuanrong.test",
        backend_port: int = 2222,
    ) -> None:
        self.ran: list[tuple[str, str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.backend_host = backend_host
        self.backend_port = backend_port

    def backend_username(self, instance_id: str) -> str:
        return DEFAULT_SSH_USER_TEMPLATE.format(instance=instance_id)

    async def run(
        self,
        session: Any,
        instance_id: str,
        *,
        user_id: str = "",
    ) -> int:
        self.ran.append((session.session_id, instance_id, user_id))
        session.exit_code = 0
        session.done.set()
        return 0

    def fail_session(self, session: Any, reason: str) -> None:
        self.failed.append((session.session_id, reason))
        session.exit_code = 1
        session.done.set()

    async def wait_until_ready(self, instance_id: str, *, user_id: str = "") -> None:
        del instance_id, user_id


# ---------- settings / username ----------


def test_backend_username_uses_yr_instance_template() -> None:
    relay = YuanrongSshRelay(
        YuanrongSshSettings(),
        frontend_endpoint="http://frontend.yuanrong.test:31220",
    )
    assert (
        relay.backend_username("inst-42")
        == "yr:instance:inst-42"
    )
    assert relay.backend_host == "frontend.yuanrong.test"
    assert relay.backend_port == 2222


def test_backend_username_requires_instance_id() -> None:
    relay = YuanrongSshRelay(YuanrongSshSettings())
    with pytest.raises(ValueError):
        relay.backend_username("  ")


def test_backend_host_from_frontend_endpoint() -> None:
    relay = YuanrongSshRelay(
        YuanrongSshSettings(port=2200),
        frontend_endpoint="http://frontend.yuanrong.test:31220",
    )
    assert relay.backend_host == "frontend.yuanrong.test"
    assert relay.backend_port == 2200


def test_load_yuanrong_ssh_settings_defaults() -> None:
    settings = load_yuanrong_ssh_settings(None)
    assert settings.port == 2222
    assert settings.user_template == DEFAULT_SSH_USER_TEMPLATE
    assert settings.connect_timeout_s == 30.0
    assert settings.client_keys_dir == DEFAULT_CLIENT_KEYS_DIR

    custom = load_yuanrong_ssh_settings(
        {
            "port": 2223,
            "user_template": "yr:{instance}",
            "client_keys_dir": "/data/{user_id}/keys",
        }
    )
    assert custom.port == 2223
    assert custom.user_template == "yr:{instance}"
    assert custom.client_keys_dir == "/data/{user_id}/keys"


def test_resolve_client_keys_dir_defaults_to_root_ssh() -> None:
    assert resolve_client_keys_dir(DEFAULT_CLIENT_KEYS_DIR, "alice") == Path(
        "/root/.ssh"
    )
    assert resolve_client_keys_dir(
        "/home/{user_id}/.ssh", "alice/../bob"
    ) == Path("/home/alice_.._bob/.ssh")


def test_list_client_key_paths_reads_default_names(tmp_path: Path) -> None:
    (tmp_path / "id_ed25519").write_text("key-ed", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("key-rsa", encoding="utf-8")
    (tmp_path / "id_rsa.pub").write_text("pub", encoding="utf-8")
    (tmp_path / "known_hosts").write_text("h", encoding="utf-8")
    assert list_client_key_paths(tmp_path) == [
        str(tmp_path / "id_ed25519"),
        str(tmp_path / "id_rsa"),
    ]
    assert list_client_key_paths(tmp_path / "missing") == []


def test_resolve_client_keys_requires_private_key(tmp_path: Path) -> None:
    relay = YuanrongSshRelay(
        YuanrongSshSettings(client_keys_dir=str(tmp_path / "{user_id}")),
    )
    with pytest.raises(ValueError, match="no SSH private keys found"):
        relay._resolve_client_keys("alice")

    key_dir = tmp_path / "alice"
    key_dir.mkdir()
    (key_dir / "id_ed25519").write_text("k", encoding="utf-8")
    assert relay._resolve_client_keys("alice") == [str(key_dir / "id_ed25519")]


def test_is_ssh_connect_retryable_for_cold_start_errors() -> None:
    assert _is_ssh_connect_retryable(ConnectionRefusedError())
    assert _is_ssh_connect_retryable(TimeoutError("timeout"))
    assert _is_ssh_connect_retryable(RuntimeError("SSH connection closed"))
    assert not _is_ssh_connect_retryable(ValueError("no SSH private keys found"))


@pytest.mark.asyncio
async def test_wait_until_ready_retries_closed_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jiuwenswarm.extensions.agentos.agentos_router.ssh_relay as relay_mod

    monkeypatch.setattr(relay_mod, "_SSH_CONNECT_RETRY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(relay_mod, "_SSH_CONNECT_READY_TIMEOUT_SECONDS", 2.0)

    key_dir = tmp_path / "alice"
    key_dir.mkdir()
    (key_dir / "id_ed25519").write_text("k", encoding="utf-8")

    class _Conn:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    attempts = {"n": 0}

    class _FakeAsyncssh:
        async def connect(self, *args: Any, **kwargs: Any) -> _Conn:
            del args, kwargs
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionRefusedError("SSH connection closed")
            return _Conn()

    monkeypatch.setattr(relay_mod, "_import_asyncssh", lambda: _FakeAsyncssh())
    relay = YuanrongSshRelay(
        YuanrongSshSettings(client_keys_dir=str(tmp_path / "{user_id}")),
        frontend_endpoint="http://frontend.test:8888",
    )
    await relay.wait_until_ready("inst-1", user_id="alice")
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_thirdagent_switch_waits_for_sshd_after_create() -> None:
    class _WaitRelay(StubSshRelay):
        def __init__(self) -> None:
            super().__init__()
            self.ready_calls: list[tuple[str, str]] = []

        async def wait_until_ready(self, instance_id: str, *, user_id: str = "") -> None:
            self.ready_calls.append((instance_id, user_id))

    relay = _WaitRelay()
    yuanrong = FakeYuanRongClient()
    client = AgentOSRouterClient(
        yuanrong,
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=relay,
        ssh_channel_endpoint=SshChannelEndpoint(ip="0.0.0.0", port=2222),
    )
    try:
        response = await client.thirdagent_switch(
            user_id="alice",
            agent_type="opencode",
            session_id="sess-1",
        )
    finally:
        await client.shutdown()

    assert response["ok"] is True
    assert yuanrong.create_calls == 1
    assert relay.ready_calls == [("sbx-1", "alice")]


@pytest.mark.asyncio
async def test_thirdagent_switch_sshd_not_ready_fails() -> None:
    class _WaitRelay(StubSshRelay):
        async def wait_until_ready(self, instance_id: str, *, user_id: str = "") -> None:
            raise ConnectionRefusedError("SSH connection closed")

    client = AgentOSRouterClient(
        FakeYuanRongClient(),
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=_WaitRelay(),
        ssh_channel_endpoint=SshChannelEndpoint(ip="0.0.0.0", port=2222),
    )
    try:
        response = await client.thirdagent_switch(
            user_id="alice",
            agent_type="opencode",
            session_id="sess-1",
        )
    finally:
        await client.shutdown()

    assert response["ok"] is False
    assert response["code"] == "SSH_NOT_READY"
    assert "sshd not ready" in response["error"]


def test_import_asyncssh_raises_actionable_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import jiuwenswarm.extensions.agentos.agentos_router.ssh_relay as relay_mod

    real_import = builtins.__import__

    def _block_asyncssh(name: str, *args: Any, **kwargs: Any):
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_asyncssh)
    with pytest.raises(RuntimeError, match=r"jiuwenswarm\[ssh\]"):
        relay_mod._import_asyncssh()


def test_load_router_config_parses_ssh_block() -> None:
    config = {
        "gateway": {
            "agent_client": {
                "type": "agentos_router",
                "frontend_endpoint": "http://yuanrong.test",
                "function_version_urn": "urn:test",
            },
            "agentos": {
                "ssh": {"port": 2222},
            },
        },
        "channels": {
            "ssh": {
                "enabled": True,
                "listen_host": "192.168.1.10",
                "listen_port": 2222,
            }
        },
    }
    loaded = load_router_config(config)
    assert loaded.ssh.port == 2222
    assert loaded.ssh.user_template == DEFAULT_SSH_USER_TEMPLATE
    assert loaded.ssh_channel == SshChannelEndpoint(ip="192.168.1.10", port=2222)
    assert loaded.ssh.client_keys_dir == DEFAULT_CLIENT_KEYS_DIR
    assert loaded.workspace_root == "/home/agentos/users"


def test_load_router_config_ssh_channel_requires_enabled() -> None:
    config = {
        "gateway": {
            "agent_client": {
                "type": "agentos_router",
                "frontend_endpoint": "http://yuanrong.test",
                "function_version_urn": "urn:test",
            },
        },
        "channels": {
            "ssh": {
                "enabled": False,
                "listen_host": "0.0.0.0",
                "listen_port": 2222,
            }
        },
    }
    loaded = load_router_config(config)
    assert loaded.ssh_channel is None


# ---------- router dispatch ----------


@pytest.mark.asyncio
async def test_ssh_relay_creates_instance_and_starts_relay() -> None:
    session = _relay_session()
    stub_relay = StubSshRelay()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        FakeYuanRongClient(),
        FakeRegistryClient(),
        agent_manager,
        ssh_relay=stub_relay,
    )
    try:
        response = await client.send_request(
            _ssh_envelope(session, agent_type="opencode")
        )
        assert response.ok
        assert response.payload["status"] == "relay_started"

        await asyncio.wait_for(session.done.wait(), timeout=5)
        assert stub_relay.ran == [("ssh_alice_1234", "sbx-1", "alice")]
        assert stub_relay.failed == []
        assert session.exit_code == 0

        agents = await agent_manager.list_user_agents("alice")
        assert len(agents) == 1
        assert agents[0].info.sandbox_id == "sbx-1"
        assert agents[0].info.agent_type == "opencode"
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_reuses_existing_instance() -> None:
    yuanrong = FakeYuanRongClient()
    stub_relay = StubSshRelay()
    client = AgentOSRouterClient(
        yuanrong,
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=stub_relay,
    )
    first = _relay_session("ssh_alice_a")
    second = _relay_session("ssh_alice_b")
    try:
        await client.send_request(_ssh_envelope(first, agent_type="opencode"))
        await asyncio.wait_for(first.done.wait(), timeout=5)
        await client.send_request(_ssh_envelope(second, agent_type="opencode"))
        await asyncio.wait_for(second.done.wait(), timeout=5)

        assert yuanrong.create_calls == 1
        assert [item[1] for item in stub_relay.ran] == ["sbx-1", "sbx-1"]
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_follows_user_current_agent_type() -> None:
    """SSH 未显式指定 agent_type 时，跟随 3rdagent.switch 切换后的用户当前值。"""
    yuanrong = FakeYuanRongClient()
    stub_relay = StubSshRelay()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        ssh_relay=stub_relay,
        ssh_channel_endpoint=SshChannelEndpoint(ip="0.0.0.0", port=2222),
    )
    session = _relay_session("ssh_alice_switch")
    try:
        # 用户切换到 opencode
        result = await client.thirdagent_switch(user_id="alice", agent_type="opencode")
        assert result["ok"]
        assert result["payload"]["ssh_ip"] == "0.0.0.0"
        assert result["payload"]["ssh_port"] == 2222
        assert "ssh_user" not in result["payload"]
        assert client.get_current_agent_type("alice") == "opencode"

        # SSH 接入不带 agent_type -> 复用 opencode 实例（不新建）
        await client.send_request(_ssh_envelope(session, agent_type=None))
        await asyncio.wait_for(session.done.wait(), timeout=5)

        assert yuanrong.create_calls == 1  # switch 已创建，SSH 复用
        assert stub_relay.ran == [("ssh_alice_switch", "sbx-1", "alice")]
        agents = await agent_manager.list_user_agents("alice")
        assert [a.info.agent_type for a in agents] == ["opencode"]
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_without_switch_uses_opencode_command_prefix() -> None:
    """未 switch：``ssh ... "opencode ..."`` 按指令首词拉起 opencode。"""
    yuanrong = FakeYuanRongClient()
    stub_relay = StubSshRelay()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        ssh_relay=stub_relay,
    )
    session = _relay_session("ssh_alice_opencode_cmd")
    try:
        await client.send_request(
            _ssh_envelope(session, agent_type=None, command="opencode -p hi")
        )
        await asyncio.wait_for(session.done.wait(), timeout=5)

        assert yuanrong.create_calls == 1
        assert stub_relay.ran == [("ssh_alice_opencode_cmd", "sbx-1", "alice")]
        agents = await agent_manager.list_user_agents("alice")
        assert [a.info.agent_type for a in agents] == ["opencode"]
        assert session.exit_code == 0
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_without_switch_or_command_fails() -> None:
    """未 switch 且无指令前缀：无法选择第三方 sandbox，应失败。"""
    yuanrong = FakeYuanRongClient()
    stub_relay = StubSshRelay()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        ssh_relay=stub_relay,
    )
    session = _relay_session("ssh_alice_default")
    try:
        await client.send_request(_ssh_envelope(session, agent_type=None))
        await asyncio.wait_for(session.done.wait(), timeout=5)

        assert yuanrong.create_calls == 0
        assert stub_relay.ran == []
        assert len(stub_relay.failed) == 1
        assert "derived from its first token" in stub_relay.failed[0][1]
        assert await agent_manager.list_user_agents("alice") == []
        assert session.exit_code == 1
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_explicit_jiuwenswarm_still_fails() -> None:
    """显式指定内置 agent_type 时仍无 sandbox，应失败。"""
    yuanrong = FakeYuanRongClient()
    stub_relay = StubSshRelay()
    client = AgentOSRouterClient(
        yuanrong,
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=stub_relay,
    )
    session = _relay_session("ssh_alice_builtin")
    try:
        await client.send_request(_ssh_envelope(session, agent_type="jiuwenswarm"))
        await asyncio.wait_for(session.done.wait(), timeout=5)

        assert yuanrong.create_calls == 0
        assert stub_relay.ran == []
        assert len(stub_relay.failed) == 1
        assert "builtin agent_type has no AgentOS sandbox for SSH" in stub_relay.failed[0][1]
        assert session.exit_code == 1
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_missing_session_returns_error() -> None:
    client = AgentOSRouterClient(
        FakeYuanRongClient(),
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=StubSshRelay(),
    )
    try:
        response = await client.send_request(_ssh_envelope(session_id="ssh_missing"))
        assert not response.ok
        assert "ssh relay session not found" in response.payload["error"]
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_without_relay_configured_fails_session() -> None:
    session = _relay_session("ssh_norelay")
    client = AgentOSRouterClient(
        FakeYuanRongClient(),
        FakeRegistryClient(),
        AgentManager(),
    )
    try:
        response = await client.send_request(_ssh_envelope(session))
        assert not response.ok
        assert session.done.is_set()
        assert session.exit_code == 1
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_agent_creation_failure_releases_session() -> None:
    class FailingYuanRong(FakeYuanRongClient):
        async def create_sandbox(self, **kwargs: Any):
            raise RuntimeError("create failed")

    session = _relay_session("ssh_fail")
    stub_relay = StubSshRelay()
    client = AgentOSRouterClient(
        FailingYuanRong(),
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=stub_relay,
    )
    try:
        response = await client.send_request(
            _ssh_envelope(session, agent_type="opencode")
        )
        assert response.ok  # relay task started; failure is reported via session
        await asyncio.wait_for(session.done.wait(), timeout=5)
        assert session.exit_code == 1
        assert stub_relay.ran == []
        assert len(stub_relay.failed) == 1
        assert "create failed" in stub_relay.failed[0][1]
    finally:
        await client.shutdown()


# ---------- bidirectional disconnect ----------


class _FakeStream:
    """Minimal async stream used by disconnect tests."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        raise_on_read: BaseException | None = None,
        block_read: bool = False,
    ) -> None:
        self._chunks = list(chunks or [])
        self._raise_on_read = raise_on_read
        self._block_read = block_read
        self.writes: list[bytes] = []
        self.eof_written = False
        self._read_event = asyncio.Event()

    async def read(self, _n: int) -> bytes:
        if self._raise_on_read is not None:
            raise self._raise_on_read
        if self._block_read:
            await self._read_event.wait()
            return b""
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        self.eof_written = True

    def unblock(self) -> None:
        self._read_event.set()


class _FakeProcess:
    def __init__(self, stdin: _FakeStream, stdout: _FakeStream, stderr: _FakeStream) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.exit_codes: list[int] = []

    def get_terminal_type(self) -> str:
        return "xterm"

    def get_terminal_size(self) -> tuple[int, int]:
        return (80, 24)

    def exit(self, code: int) -> None:
        self.exit_codes.append(code)


class _FakeBackend:
    def __init__(
        self,
        stdout: _FakeStream,
        stderr: _FakeStream,
        stdin: _FakeStream | None = None,
        *,
        exit_status: int | None = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = stdin or _FakeStream()
        self.exit_status = exit_status
        self.closed = False
        self.wait_closed_called = False

    def close(self) -> None:
        self.closed = True
        self.stdout.unblock()
        self.stderr.unblock()
        self.stdin.unblock()

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


class _FakeConn:
    def __init__(self, backend: _FakeBackend) -> None:
        self._backend = backend
        self.create_calls = 0

    async def create_process(self, *args: Any, **kwargs: Any) -> _FakeBackend:
        del args, kwargs
        self.create_calls += 1
        return self._backend


def _install_fake_asyncssh(modules: dict[str, Any]) -> type[BaseException]:
    from types import ModuleType

    fake_asyncssh = ModuleType("asyncssh")

    class ConnectionLost(Exception):
        pass

    class TerminalSizeChanged(Exception):
        pass

    class BreakReceived(Exception):
        pass

    fake_asyncssh.ConnectionLost = ConnectionLost  # type: ignore[attr-defined]
    fake_asyncssh.TerminalSizeChanged = TerminalSizeChanged  # type: ignore[attr-defined]
    fake_asyncssh.BreakReceived = BreakReceived  # type: ignore[attr-defined]
    modules["asyncssh"] = fake_asyncssh
    return ConnectionLost


@pytest.mark.asyncio
async def test_southbound_eof_closes_northbound_and_cancels_pumps() -> None:
    """Southbound stdout EOF must exit the northbound process and stop other pumps."""
    import sys
    from unittest.mock import patch

    modules: dict[str, Any] = dict(sys.modules)
    _install_fake_asyncssh(modules)

    north_stdin = _FakeStream(block_read=True)
    north_stdout = _FakeStream()
    north_stderr = _FakeStream()
    process = _FakeProcess(north_stdin, north_stdout, north_stderr)

    backend_stdout = _FakeStream(chunks=[b"bye"])  # one chunk then EOF
    backend_stderr = _FakeStream(block_read=True)
    backend = _FakeBackend(backend_stdout, backend_stderr, exit_status=7)
    conn = _FakeConn(backend)

    session = _relay_session("ssh_s2n")
    session.process = process

    relay = YuanrongSshRelay(
        YuanrongSshSettings(),
        frontend_endpoint="http://127.0.0.1:31220",
    )
    with patch.dict(sys.modules, modules):
        code = await asyncio.wait_for(
            relay._relay_over_connection(session, conn),
            timeout=5,
        )

    assert code == 7
    assert backend.closed is True
    assert process.exit_codes == [7]
    assert north_stdout.writes == [b"bye"]


@pytest.mark.asyncio
async def test_northbound_disconnect_closes_southbound() -> None:
    """Northbound ConnectionLost must close the southbound backend."""
    import sys
    from unittest.mock import patch

    modules: dict[str, Any] = dict(sys.modules)
    ConnectionLost = _install_fake_asyncssh(modules)

    north_stdin = _FakeStream(raise_on_read=ConnectionLost())
    process = _FakeProcess(north_stdin, _FakeStream(), _FakeStream())

    backend_stdout = _FakeStream(block_read=True)
    backend_stderr = _FakeStream(block_read=True)
    backend = _FakeBackend(backend_stdout, backend_stderr, exit_status=None)
    conn = _FakeConn(backend)

    session = _relay_session("ssh_n2s")
    session.process = process

    relay = YuanrongSshRelay(
        YuanrongSshSettings(),
        frontend_endpoint="http://127.0.0.1:31220",
    )
    with patch.dict(sys.modules, modules):
        code = await asyncio.wait_for(
            relay._relay_over_connection(session, conn),
            timeout=5,
        )

    assert code == 0
    assert backend.closed is True
    assert process.exit_codes == [0]


@pytest.mark.asyncio
async def test_relay_run_cancelled_releases_session_done() -> None:
    """Cancelling the relay task must still set session.done for the northbound waiter."""
    session = _relay_session("ssh_cancel")
    session.process = _FakeProcess(_FakeStream(), _FakeStream(), _FakeStream())
    relay = YuanrongSshRelay(
        YuanrongSshSettings(),
        frontend_endpoint="http://127.0.0.1:31220",
    )

    async def _hang(
        _session: Any, _instance_id: str, *, user_id: str = ""
    ) -> int:
        del user_id
        await asyncio.Event().wait()
        return 0

    relay._relay = _hang  # type: ignore[method-assign]
    task = asyncio.create_task(relay.run(session, "inst-1"))
    await asyncio.sleep(0)
    assert not session.done.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.done.is_set()
    assert session.exit_code == 130


@pytest.mark.asyncio
async def test_router_disconnect_cancels_background_ssh_relay() -> None:
    """Router disconnect must cancel in-flight SSH relay background tasks."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingRelay(StubSshRelay):
        async def run(
            self,
            session: Any,
            instance_id: str,
            *,
            user_id: str = "",
        ) -> int:
            del instance_id, user_id
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                session.exit_code = 130
                session.done.set()
                raise
            return 0

    session = _relay_session("ssh_drain")
    client = AgentOSRouterClient(
        FakeYuanRongClient(),
        FakeRegistryClient(),
        AgentManager(),
        ssh_relay=HangingRelay(),
    )
    try:
        await client.send_request(_ssh_envelope(session, agent_type="opencode"))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert session.relay_task is not None
        assert not session.relay_task.done()
        await client.disconnect()
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert session.done.is_set()
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_wait_relay_done_timeout_cancels_southbound_task() -> None:
    """Northbound relay timeout must cancel the southbound relay_task."""
    from jiuwenswarm.gateway.channel_manager.protocol.ssh.ssh_connect import (
        SshChannel,
        SshChannelConfig,
    )

    cancelled = asyncio.Event()

    async def _hanging_relay() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            session.exit_code = 130
            session.done.set()
            raise

    session = _relay_session("ssh_timeout")
    session.relay_task = asyncio.create_task(_hanging_relay())

    channel = SshChannel(
        SshChannelConfig(enabled=False, relay_timeout_sec=0.05),
        router=None,  # type: ignore[arg-type]
    )
    await channel._register_session(
        session_id="ssh_timeout",
        process=object(),
        username="alice",
        client_addr="127.0.0.1:1",
    )
    channel._sessions["ssh_timeout"].relay = session
    try:
        code = await channel._wait_relay_done("ssh_timeout")
        assert code == 124
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert session.done.is_set()
    finally:
        await channel._unregister_session("ssh_timeout")
        if session.relay_task and not session.relay_task.done():
            session.relay_task.cancel()
            try:
                await session.relay_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_unregister_session_cancels_southbound_task() -> None:
    """Northbound session teardown must cancel a still-running southbound
    relay, even when the pumps never notice the dead client (stdin blocked)."""
    from jiuwenswarm.gateway.channel_manager.protocol.ssh.ssh_connect import (
        SshChannel,
        SshChannelConfig,
    )

    cancelled = asyncio.Event()

    async def _hanging_relay() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            session.exit_code = 130
            session.done.set()
            raise

    session = _relay_session("ssh_unreg")
    session.relay_task = asyncio.create_task(_hanging_relay())
    # Let the relay task start; cancelling a never-started task would skip
    # its CancelledError handler.
    await asyncio.sleep(0)

    channel = SshChannel(
        SshChannelConfig(enabled=False),
        router=None,  # type: ignore[arg-type]
    )
    await channel._register_session(
        session_id="ssh_unreg",
        process=object(),
        username="alice",
        client_addr="127.0.0.1:1",
    )
    channel._sessions["ssh_unreg"].relay = session
    try:
        await channel._unregister_session("ssh_unreg")
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert session.done.is_set()
        assert "ssh_unreg" not in channel._sessions
    finally:
        if session.relay_task and not session.relay_task.done():
            session.relay_task.cancel()
            try:
                await session.relay_task
            except asyncio.CancelledError:
                pass
