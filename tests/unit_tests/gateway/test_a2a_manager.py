import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwenswarm.gateway.a2a_manager import (
    A2AIngressConfig,
    A2AIngressConfigRepository,
    A2AIngressError,
    A2AIngressState,
    A2AOutboundSettingsRepository,
    A2AManager,
    load_a2a_ingress_config,
    load_a2a_ingress_config_safely,
)
from jiuwenswarm.gateway.a2a_manager import config as a2a_config_module
from jiuwenswarm.gateway.channel_manager.protocol.a2a.a2a_connect import (
    A2ADependencyMissingError,
)
from jiuwenswarm.gateway.a2a_manager.outbound import (
    A2AOutboundError,
    A2AOutboundErrorCode,
)


class _ChannelManagerProbe:
    def __init__(self) -> None:
        self.registered = []
        self.unregistered = []

    def register_channel(self, channel) -> None:
        self.registered.append(channel)

    def unregister_channel(self, channel_id: str) -> None:
        self.unregistered.append(channel_id)


class _ChannelProbe:
    channel_id = "a2a"

    def __init__(self, config, router, *, start_error: Exception | None = None) -> None:
        self.config = config
        self.router = router
        self.start_error = start_error
        self.start_calls = 0
        self.stop_calls = 0
        self.request_observer = None
        self.runtime_error_observer = None

    def set_request_observer(self, callback) -> None:
        self.request_observer = callback

    def set_runtime_error_observer(self, callback) -> None:
        self.runtime_error_observer = callback

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.stop_calls += 1


class _CancellationProbe(_ChannelProbe):
    def __init__(self, config, router) -> None:
        super().__init__(config, router)
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> None:
        self.start_calls += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
            raise


class _ConfigRepositoryProbe:
    def __init__(self) -> None:
        self.saved = []

    def save(self, config) -> None:
        self.saved.append(config)


def test_load_a2a_ingress_config_keeps_existing_environment_contract():
    config = load_a2a_ingress_config(
        {
            "A2A_SERVER_ENABLED": "yes",
            "A2A_SERVER_HOST": "0.0.0.0",
            "A2A_SERVER_PORT": "19123",
            "A2A_SERVER_PATH": "/rpc",
            "A2A_SERVER_EXPOSE_REASONING": "false",
        }
    )

    assert config.enabled is True
    assert config.host == "0.0.0.0"
    assert config.port == 19123
    assert config.rpc_path == "/rpc"
    assert config.expose_reasoning is False


@pytest.mark.parametrize("port", ["bad", "0", "65536"])
def test_load_a2a_ingress_config_rejects_invalid_port(port):
    with pytest.raises(A2AIngressError, match="A2A_SERVER_PORT") as exc_info:
        load_a2a_ingress_config({"A2A_SERVER_PORT": port})
    assert exc_info.value.code == "A2A_CONFIG_INVALID"


def test_invalid_a2a_config_has_a_disabled_boot_fallback():
    config, error = load_a2a_ingress_config_safely({"A2A_SERVER_PATH": "missing-slash"})

    assert config.enabled is False
    assert error is not None
    assert error.code == "A2A_CONFIG_INVALID"


@pytest.mark.parametrize("allow_loopback,allow_http", [(False, False), (True, False), (False, True), (True, True)])
def test_outbound_network_settings_round_trip_through_dotenv(tmp_path, monkeypatch, allow_loopback, allow_http):
    env_path = tmp_path / ".env"
    env_path.write_text(
        'KEEP_ME="yes"\nA2A_OUTBOUND_ALLOW_LOOPBACK="false"\n', "utf-8"
    )
    monkeypatch.delenv("A2A_OUTBOUND_ALLOW_LOOPBACK", raising=False)
    monkeypatch.delenv("A2A_OUTBOUND_ALLOW_HTTP", raising=False)
    repository = A2AOutboundSettingsRepository(env_path)

    assert repository.load({}) == {"allow_loopback": False, "allow_http": False}
    assert repository.load({"A2A_OUTBOUND_ALLOW_LOOPBACK_HTTP": "true"}) == {
        "allow_loopback": False, "allow_http": False,
    }
    repository.save(allow_loopback=allow_loopback, allow_http=allow_http)

    assert repository.load() == {"allow_loopback": allow_loopback, "allow_http": allow_http}
    content = env_path.read_text("utf-8")
    assert 'KEEP_ME="yes"' in content
    assert content.count("A2A_OUTBOUND_ALLOW_LOOPBACK") == 1
    assert f'A2A_OUTBOUND_ALLOW_LOOPBACK="{str(allow_loopback).lower()}"' in content
    assert f'A2A_OUTBOUND_ALLOW_HTTP="{str(allow_http).lower()}"' in content


def test_ingress_and_outbound_repositories_share_one_dotenv_writer(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text('KEEP_ME="yes"\n', "utf-8")
    ingress = A2AIngressConfigRepository(env_path)
    outbound = A2AOutboundSettingsRepository(env_path)

    # Holding the shared lock proves both public repositories enter the same
    # read-modify-write critical section instead of racing independent writers.
    executor = ThreadPoolExecutor(max_workers=2)
    a2a_config_module._DOTENV_WRITE_LOCK.acquire()
    try:
        ingress_future = executor.submit(
            ingress.save,
            A2AIngressConfig(port=19234, app_name="Concurrent ingress"),
        )
        outbound_future = executor.submit(
            outbound.save,
            allow_loopback=True, allow_http=True,
        )
        time.sleep(0.05)
        assert ingress_future.done() is False
        assert outbound_future.done() is False
    finally:
        a2a_config_module._DOTENV_WRITE_LOCK.release()
    try:
        ingress_future.result(timeout=2)
        outbound_future.result(timeout=2)
    finally:
        executor.shutdown(wait=True)

    content = env_path.read_text("utf-8")
    assert 'KEEP_ME="yes"' in content
    assert 'A2A_SERVER_PORT="19234"' in content
    assert 'A2A_OUTBOUND_ALLOW_LOOPBACK="true"' in content


@pytest.mark.asyncio
async def test_manager_exposes_boot_config_error_without_starting_a_channel():
    config, error = load_a2a_ingress_config_safely({"A2A_SERVER_PATH": "missing-slash"})
    channel_manager = _ChannelManagerProbe()
    manager = A2AManager(channel_manager, object(), config, initial_error=error)

    snapshot = await manager.start_from_config()

    assert snapshot.state.value == "error"
    assert snapshot.last_error is not None
    assert channel_manager.registered == []


@pytest.mark.asyncio
async def test_manager_registers_starts_and_stops_channel_once():
    channel_manager = _ChannelManagerProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(enabled=True),
        channel_factory=factory,
    )
    await manager.start_from_config()
    await manager.start_from_config()
    await asyncio.sleep(0)

    assert len(channel_manager.registered) == 1
    assert channels[0].start_calls == 1

    await manager.stop()
    await manager.stop()

    assert channels[0].stop_calls == 1
    assert channel_manager.unregistered == ["a2a"]
    assert manager.channel is None


@pytest.mark.asyncio
async def test_manager_retains_disabled_config_for_the_protocol_adapter():
    channel_manager = _ChannelManagerProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager, object(), A2AIngressConfig(), channel_factory=factory
    )
    await manager.start_from_config()
    await asyncio.sleep(0)
    await manager.stop()

    assert channels == []
    assert channel_manager.registered == []
    assert manager.snapshot().state.value == "disabled"


@pytest.mark.asyncio
async def test_manager_handles_start_failure_and_still_stops():
    channel_manager = _ChannelManagerProbe()

    def factory(config, router):
        return _ChannelProbe(
            config, router, start_error=RuntimeError("a2a sdk missing")
        )

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(enabled=True),
        channel_factory=factory,
    )
    await manager.start_from_config()
    await asyncio.sleep(0)
    await manager.stop()

    assert channel_manager.unregistered == ["a2a"]
    assert manager.channel is None


@pytest.mark.asyncio
async def test_manager_retains_bounded_request_history_across_channel_reload():
    channel_manager = _ChannelManagerProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(enabled=True),
        channel_factory=factory,
    )
    await manager.enable()
    channels[0].request_observer(
        {
            "request_id": "req-1",
            "context_id": "ctx-1",
            "message_id": "msg-1",
            "status": "processing",
            "started_at": 10.0,
        }
    )
    channels[0].request_observer(
        {
            "request_id": "req-1",
            "context_id": "ctx-1",
            "message_id": "msg-1",
            "status": "completed",
            "started_at": 10.0,
            "finished_at": 10.125,
            "error": None,
        }
    )
    await manager.reload()

    history = manager.history()
    assert history["total"] == 1
    assert history["items"][0] == {
        "request_id": "req-1",
        "context_id": "ctx-1",
        "message_id": "msg-1",
        "operation": "message",
        "status": "completed",
        "started_at": 10.0,
        "finished_at": 10.125,
        "duration_ms": 125,
        "error": None,
    }
    assert channels[1].request_observer is not None


def test_manager_request_history_terminal_status_cannot_be_overwritten():
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
    )
    manager._record_request_event(
        {
            "request_id": "req-completed",
            "status": "processing",
            "started_at": 10.0,
        }
    )
    manager._record_request_event(
        {
            "request_id": "req-completed",
            "status": "completed",
            "finished_at": 10.1,
        }
    )
    manager._record_request_event(
        {
            "request_id": "req-completed",
            "status": "canceled",
            "finished_at": 10.2,
        }
    )
    manager._record_request_event(
        {
            "request_id": "req-completed",
            "status": "failed",
            "finished_at": 10.3,
            "error": "late failure",
        }
    )

    item = manager.history()["items"][0]
    assert item["status"] == "completed"
    assert item["finished_at"] == 10.1
    assert item["duration_ms"] == 100
    assert item["error"] is None


def test_manager_request_history_keeps_cancellation_terminal():
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
    )
    manager._record_request_event(
        {
            "request_id": "req-canceled",
            "status": "processing",
            "started_at": 20.0,
        }
    )
    manager._record_request_event(
        {
            "request_id": "req-canceled",
            "status": "canceled",
            "finished_at": 20.2,
        }
    )
    manager._record_request_event(
        {
            "request_id": "req-canceled",
            "status": "completed",
            "finished_at": 20.3,
        }
    )

    item = manager.history()["items"][0]
    assert item["status"] == "canceled"
    assert item["finished_at"] == 20.2
    assert item["duration_ms"] == 200


@pytest.mark.asyncio
async def test_stop_propagates_its_own_cancellation_after_channel_cleanup():
    channel_manager = _ChannelManagerProbe()
    channel = _CancellationProbe(
        A2AIngressConfig(enabled=True).to_channel_config(), object()
    )
    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(enabled=True),
        channel_factory=lambda config, router: channel,
    )
    await manager.start_from_config()

    stop_task = asyncio.create_task(manager.stop())
    await channel.cancelled.wait()
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert channel.stop_calls == 1
    assert channel_manager.unregistered == ["a2a"]


@pytest.mark.asyncio
async def test_manager_persists_enable_reload_and_disable_in_order():
    channel_manager = _ChannelManagerProbe()
    repository = _ConfigRepositoryProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(),
        repository=repository,
        channel_factory=factory,
    )

    enabled = await manager.enable()
    reloaded = await manager.update({"port": 19123}, apply=True)
    disabled = await manager.disable()

    assert enabled.state.value == "running"
    assert reloaded.state.value == "running"
    assert reloaded.desired_port == 19123
    assert reloaded.effective_port == 19123
    assert disabled.state.value == "disabled"
    assert [config.enabled for config in repository.saved] == [True, True, False]
    assert len(channels) == 2
    assert channels[0].stop_calls == 1
    assert channels[1].stop_calls == 1


def test_repository_persists_only_a2a_environment_fields(tmp_path, monkeypatch):
    from jiuwenswarm.gateway.a2a_manager import config as a2a_config

    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_NAME=kept\nA2A_SERVER_PORT=19000\n", encoding="utf-8")
    environ = {}
    monkeypatch.setattr(a2a_config.os, "environ", environ)

    A2AIngressConfigRepository(env_path).save(
        A2AIngressConfig(enabled=True, port=19123)
    )

    text = env_path.read_text(encoding="utf-8")
    assert "MODEL_NAME=kept" in text
    assert 'A2A_SERVER_PORT="19123"' in text
    assert environ["A2A_SERVER_ENABLED"] == "true"


def test_repository_escapes_values_and_preserves_comments_and_export_lines(
    tmp_path, monkeypatch
):
    from jiuwenswarm.gateway.a2a_manager import config as a2a_config

    env_path = tmp_path / ".env"
    env_path.write_text(
        "# A2A_SERVER_PORT=comment\nexport A2A_SERVER_PORT=19000\nMODEL_NAME=kept\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(a2a_config.os, "environ", {})

    A2AIngressConfigRepository(env_path).save(
        A2AIngressConfig(app_name='quoted " value\nMODEL_NAME=injected')
    )

    text = env_path.read_text(encoding="utf-8")
    assert "# A2A_SERVER_PORT=comment" in text
    assert 'export A2A_SERVER_PORT="19100"' in text
    assert "MODEL_NAME=kept" in text
    assert 'A2A_SERVER_APP_NAME="quoted \\" value\\nMODEL_NAME=injected"' in text
    assert list(tmp_path.glob(".env.*.tmp")) == []


@pytest.mark.asyncio
async def test_invalid_patch_does_not_persist_or_change_running_config():
    repository = _ConfigRepositoryProbe()
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=repository,
        channel_factory=lambda config, router: _ChannelProbe(config, router),
    )

    with pytest.raises(A2AIngressError, match="must start") as exc_info:
        await manager.update({"rpc_path": "invalid"})

    assert exc_info.value.code == "A2A_CONFIG_INVALID"
    assert repository.saved == []
    assert manager.snapshot().desired_rpc_path == "/a2a"


@pytest.mark.asyncio
async def test_update_apply_false_separates_desired_and_effective_addresses():
    channel_manager = _ChannelManagerProbe()
    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(config, router),
    )

    await manager.enable()
    snapshot = await manager.update({"port": 19123}, apply=False)

    assert snapshot.desired_port == 19123
    assert snapshot.effective_port == 19100
    assert snapshot.desired_rpc_url.endswith(":19123/a2a")
    assert snapshot.effective_rpc_url.endswith(":19100/a2a")
    assert snapshot.desired_extended_card_url.endswith(
        ":19123/agent/authenticatedExtendedCard"
    )
    assert snapshot.effective_extended_card_url.endswith(
        ":19100/agent/authenticatedExtendedCard"
    )
    assert snapshot.effective_extended_card_path == "/agent/authenticatedExtendedCard"
    assert snapshot.desired_protocol_version == "1.0.0"
    assert snapshot.desired_app_name == "JiuwenSwarm Gateway A2A Server"
    assert snapshot.desired_expose_reasoning is True


@pytest.mark.asyncio
async def test_enable_applies_a_saved_but_unapplied_running_config():
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=factory,
    )
    await manager.enable()
    await manager.update({"port": 19123}, apply=False)
    snapshot = await manager.enable()

    assert snapshot.effective_port == 19123
    assert len(channels) == 2
    assert channels[0].stop_calls == 1


@pytest.mark.asyncio
async def test_update_apply_false_warns_when_desired_bind_is_public():
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(config, router),
    )

    snapshot = await manager.update({"host": "0.0.0.0"})

    assert snapshot.exposure_warning is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "warns"),
    [
        ("127.0.0.1", False),
        ("::1", False),
        ("localhost", False),
        ("0.0.0.0", True),
        ("::", True),
        ("192.168.1.20", True),
        ("gateway.example.com", True),
    ],
)
async def test_exposure_warning_covers_every_non_loopback_bind(host, warns):
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(config, router),
    )

    snapshot = await manager.update({"host": host})

    assert (snapshot.exposure_warning is not None) is warns


@pytest.mark.asyncio
async def test_manager_transitions_to_error_after_started_channel_crashes():
    channel_manager = _ChannelManagerProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=factory,
    )
    running = await manager.enable()
    assert running.state.value == "running"

    await channels[0].runtime_error_observer(RuntimeError("late server crash"))

    snapshot = manager.snapshot()
    assert snapshot.state.value == "error"
    assert "A2A ingress server stopped unexpectedly" in snapshot.last_error
    assert manager.channel is None
    assert channel_manager.unregistered == ["a2a"]


@pytest.mark.asyncio
async def test_update_apply_true_with_disabled_config_stops_running_service():
    channel_manager = _ChannelManagerProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=factory,
    )
    await manager.enable()
    snapshot = await manager.update({"enabled": False}, apply=True)

    assert snapshot.enabled is False
    assert snapshot.state.value == "disabled"
    assert snapshot.effective_rpc_url is None
    assert channels[0].stop_calls == 1
    assert channel_manager.unregistered == ["a2a"]


@pytest.mark.asyncio
async def test_reload_disposes_running_channel_when_saved_config_is_disabled():
    channel_manager = _ChannelManagerProbe()
    channels = []

    def factory(config, router):
        channel = _ChannelProbe(config, router)
        channels.append(channel)
        return channel

    manager = A2AManager(
        channel_manager,
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=factory,
    )
    await manager.enable()
    await manager.update({"enabled": False}, apply=False)
    snapshot = await manager.reload()

    assert snapshot.enabled is False
    assert snapshot.state.value == "disabled"
    assert snapshot.effective_rpc_url is None
    assert snapshot.started_at is None
    assert channels[0].stop_calls == 1
    assert channel_manager.unregistered == ["a2a"]


@pytest.mark.asyncio
async def test_on_start_done_keeps_callback_task_referenced():
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(config, router),
    )
    start_task = asyncio.create_task(asyncio.sleep(0))
    await start_task
    manager._start_task = start_task
    manager._state = A2AIngressState.STARTING
    manager._starting_config = manager._config

    manager._on_start_done(start_task)

    assert manager._pending_callbacks
    callback = next(iter(manager._pending_callbacks))
    await callback
    assert manager.snapshot().state.value == "running"
    assert manager._pending_callbacks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_error", "code"),
    [
        (
            A2ADependencyMissingError("optional dependency a2a-sdk is missing"),
            "A2A_DEPENDENCY_MISSING",
        ),
        (OSError("address already in use"), "A2A_BIND_FAILED"),
        (ValueError("bad channel config"), "A2A_CONFIG_INVALID"),
        (RuntimeError("unexpected startup failure"), "A2A_START_FAILED"),
    ],
)
async def test_enable_surfaces_stable_startup_error_codes(start_error, code):
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=lambda config, router: _ChannelProbe(
            config, router, start_error=start_error
        ),
    )

    with pytest.raises(A2AIngressError) as exc_info:
        await manager.enable()

    assert exc_info.value.code == code
    assert manager.snapshot().state.value == "error"


@pytest.mark.parametrize(
    "patch",
    [
        {"auth_type": "unknown"},
        {"auth_type": "bearer"},
        {"credential": "short"},
        {"credential": "bad\r\ncredential-value"},
        {"credential": 123},
        {"credential_hash": "a" * 64},
        {"api_key_header": "Authorization"},
        {"api_key_header": "bad header"},
        {"card_auth_required": True},
        {"clear_credential": "true"},
        {"clear_credential": True, "credential": "test-secret-with-enough-length"},
    ],
)
def test_ingress_rejects_invalid_security_without_echoing_credentials(patch):
    with pytest.raises(A2AIngressError) as error:
        A2AIngressConfig().with_patch(patch)
    if isinstance(patch.get("credential"), str):
        assert patch["credential"] not in str(error.value)


@pytest.mark.parametrize("encrypted", [False, True])
def test_ingress_credentials_use_model_key_storage_and_reload(
    tmp_path, monkeypatch, encrypted
):
    from cryptography.fernet import Fernet
    from types import SimpleNamespace
    from dotenv import dotenv_values
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    cipher = Fernet(Fernet.generate_key())
    provider = (
        SimpleNamespace(
            encrypt=lambda value: cipher.encrypt(value.encode()).decode(),
            decrypt=lambda value: cipher.decrypt(value.encode()).decode(),
        )
        if encrypted
        else None
    )
    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        lambda: SimpleNamespace(get_crypto_provider=lambda: provider),
    )
    monkeypatch.setattr(a2a_config_module.os, "environ", {})
    secret = "test-secret-with-enough-length"
    config = A2AIngressConfig().with_patch(
        {"auth_type": "bearer", "credential": secret}
    )
    repo = A2AIngressConfigRepository(tmp_path / ".env")
    repo.save(config)
    values = dotenv_values(tmp_path / ".env")
    assert (values["A2A_SERVER_API_KEY"] != secret) == encrypted
    assert config.credential_hash not in (tmp_path / ".env").read_text(encoding="utf-8")
    assert load_a2a_ingress_config(values) == config
    assert config.credential == secret
    assert not hasattr(config.to_channel_config(), "credential")
    assert secret not in repr(config)
    assert config.credential_hash not in repr(config)
    assert load_a2a_ingress_config(a2a_config_module.os.environ) == config
    assert (
        config.with_patch({"credential": "", "app_name": "Renamed"}).credential_hash
        == config.credential_hash
    )
    with pytest.raises(A2AIngressError):
        config.with_patch({"clear_credential": True})
    cleared = config.with_patch({"auth_type": "none", "clear_credential": True})
    repo.save(cleared)
    assert not load_a2a_ingress_config(a2a_config_module.os.environ).credential_hash
    assert not load_a2a_ingress_config(dotenv_values(tmp_path / ".env")).credential


@pytest.mark.asyncio
async def test_ingress_rotation_only_takes_effect_after_apply():
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        A2AIngressConfig(),
        repository=_ConfigRepositoryProbe(),
        channel_factory=_ChannelProbe,
    )
    first = "test-first-secret-with-enough-length"
    second = "test-second-secret-with-enough-length"
    await manager.update({"auth_type": "bearer", "credential": first})
    await manager.enable()
    old_channel = manager._channel
    snapshot = await manager.update({"credential": second})
    assert snapshot.security_pending_apply
    assert snapshot.credential_configured
    assert first not in str(snapshot.to_dict())
    assert second not in str(snapshot.to_dict())
    assert (await manager.edit_config())["credential"] == second
    assert second not in repr(snapshot)
    assert manager._config.credential_hash not in str(snapshot.to_dict())
    assert old_channel.config.credential_hash != manager._config.credential_hash
    snapshot = await manager.update({}, apply=True)
    assert not snapshot.security_pending_apply
    assert snapshot.effective_auth_type == "bearer"
    assert old_channel.stop_calls == 1
    assert manager._channel.config.credential_hash == manager._config.credential_hash
    await manager.disable()


def test_ingress_rotation_removes_duplicate_env_values(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    monkeypatch.setattr(a2a_config_module.os, "environ", {})
    env_path = tmp_path / ".env"
    env_path.write_text(
        "A2A_SERVER_AUTH_TYPE=none\nA2A_SERVER_AUTH_TYPE=none\nMODEL_NAME=keep",
        encoding="utf-8",
    )
    config = A2AIngressConfig().with_patch(
        {"auth_type": "bearer", "credential": "test-rotation-secret-long"}
    )
    A2AIngressConfigRepository(env_path).save(config)
    values = dotenv_values(env_path)
    assert values["MODEL_NAME"] == "keep"
    assert load_a2a_ingress_config(values) == config
    assert env_path.read_text(encoding="utf-8").count("A2A_SERVER_AUTH_TYPE=") == 1


@pytest.mark.parametrize(
    "env",
    [
        {"A2A_SERVER_AUTH_TYPE": "bearer"},
        {"A2A_SERVER_AUTH_TYPE": "invalid"},
        {"A2A_SERVER_CARD_AUTH_REQUIRED": "invalid"},
        {"A2A_SERVER_API_KEY": "short"},
    ],
)
def test_invalid_security_configuration_never_starts_anonymous_ingress(env):
    config, error = load_a2a_ingress_config_safely(
        {"A2A_SERVER_ENABLED": "true", **env}
    )
    assert error is not None
    assert not config.enabled


@pytest.mark.asyncio
async def test_failed_credential_persistence_keeps_saved_and_effective_values():
    class FailingRepository:
        def save(self, config):
            raise A2AIngressError("A2A_CONFIG_INVALID", "Storage unavailable")

    original = A2AIngressConfig().with_patch(
        {"auth_type": "bearer", "credential": "test-original-credential"}
    )
    manager = A2AManager(
        _ChannelManagerProbe(),
        object(),
        original,
        repository=FailingRepository(),
        channel_factory=_ChannelProbe,
    )
    with pytest.raises(A2AIngressError):
        await manager.update({"credential": "test-replacement-credential"}, apply=True)
    assert manager._config == original
    assert manager.snapshot().config_revision == 0


@pytest.mark.asyncio
async def test_enterprise_outbound_tools_enforce_resource_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    class _Registry:
        async def resolve_effective_a2a_agent_ids(self, resource_id):
            assert resource_id == "resource-1"
            return frozenset({"agent-1"})

        async def resolve_authorized_a2a_agent_ids(self, resource_id):
            assert resource_id == "resource-1"
            return frozenset({"agent-1", "agent-manager", "agent-user"})

        async def list_agents(self):
            return {
                "items": [{"agent_id": "agent-1"}, {"agent_id": "agent-2"}],
                "total": 2,
            }

        async def get_agent(self, agent_id):
            return {
                "agent_id": agent_id,
                "manager_enabled": agent_id != "agent-manager",
                "user_enabled": agent_id != "agent-user",
            }

        async def set_user_enabled(self, agent_id, enabled):
            return {"agent_id": agent_id, "user_enabled": enabled}

    class _Dispatcher:
        async def find_agents(self, **kwargs):
            return kwargs

        async def dispatch(self, **kwargs):
            return kwargs

        async def query_dispatch(self, dispatch_id, **kwargs):
            return {"dispatch_id": dispatch_id, **kwargs}

    manager = object.__new__(A2AManager)
    manager._outbound = _Registry()
    manager._outbound_dispatcher = _Dispatcher()

    found = await manager.outbound_find_agents(source_resource_id="resource-1")
    assert found["allowed_agent_ids"] == frozenset({"agent-1"})
    listed = await manager.outbound_list(source_resource_id="resource-1")
    assert listed == {"items": [{"agent_id": "agent-1"}], "total": 1}
    dispatched = await manager.outbound_dispatch_task(
        agent_id="agent-1",
        task="work",
        mode="sync",
        source_session_id="session-1",
        source_user_id="user-1",
        source_resource_id="resource-1",
    )
    assert dispatched["source_resource_id"] == "resource-1"
    queried = await manager.outbound_get_dispatch(
        dispatch_id="dispatch-1",
        source_session_id="session-1",
        source_user_id="user-1",
        source_resource_id="resource-1",
    )
    assert queried["source_resource_id"] == "resource-1"

    with pytest.raises(A2AOutboundError) as exc_info:
        await manager.outbound_get_dispatch(
            dispatch_id="dispatch-1",
            source_session_id="session-1",
            source_user_id="user-1",
            source_resource_id="",
        )
    assert exc_info.value.code is A2AOutboundErrorCode.AGENT_NOT_AUTHORIZED

    with pytest.raises(A2AOutboundError) as exc_info:
        await manager.outbound_dispatch_get(
            "dispatch-1",
            source_session_id="session-1",
            source_user_id="user-1",
            source_resource_id=None,
        )
    assert exc_info.value.code is A2AOutboundErrorCode.AGENT_NOT_AUTHORIZED

    with pytest.raises(A2AOutboundError) as exc_info:
        await manager.outbound_dispatch_task(
            agent_id="agent-2",
            task="work",
            mode="sync",
            source_session_id="session-1",
            source_user_id="user-1",
            source_resource_id="resource-1",
        )
    assert exc_info.value.code is A2AOutboundErrorCode.AGENT_NOT_AUTHORIZED

    for agent_id, expected_code in (
        ("agent-manager", A2AOutboundErrorCode.AGENT_MANAGER_DISABLED),
        ("agent-user", A2AOutboundErrorCode.AGENT_USER_DISABLED),
    ):
        with pytest.raises(A2AOutboundError) as exc_info:
            await manager.outbound_dispatch_task(
                agent_id=agent_id,
                task="work",
                mode="sync",
                source_session_id="session-1",
                source_user_id="user-1",
                source_resource_id="resource-1",
            )
        assert exc_info.value.code is expected_code

    with pytest.raises(A2AOutboundError) as exc_info:
        await manager.outbound_set_user_enabled(
            "agent-2", enabled=True, source_resource_id="resource-1"
        )
    assert exc_info.value.code is A2AOutboundErrorCode.AGENT_NOT_AUTHORIZED
