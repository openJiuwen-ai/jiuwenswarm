"""Disable and restart share lifecycle cleanup across current and historical URLs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from jiuwenswarm.server import sandbox_lifecycle as lifecycle
from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box


@pytest.fixture(autouse=True)
def isolated_provider_state(monkeypatch):
    for name in ("_shared_sandbox_ids", "_creation_locks", "_creation_failures", "_lifecycle_hooks",
                 "_pending_deletions", "_delete_tasks", "_idle_timeout_cache"):
        monkeypatch.setattr(box._JiuwenBoxProviderMixin, name, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "policy_changed"])
async def test_cleanup_covers_pending_only_old_url_and_current_cache(monkeypatch, reason):
    cls = box._JiuwenBoxProviderMixin
    cls._shared_sandbox_ids["http://localhost:9000|current"] = "current"
    cls._pending_deletions[("http://localhost:8321|old", "old")] = (True, None)
    requested = []

    def client_for(base_url, **kwargs):
        client = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        client.__enter__.return_value = client
        client.delete_sandbox.side_effect = lambda sid: requested.append((base_url, sid))
        return client

    monkeypatch.setattr(box, "_JiuwenBoxClient", client_for)
    assert await lifecycle.cleanup_jiuwenbox_sandboxes(reason=reason, strict=True) == 2
    assert set(requested) == {("http://localhost:9000", "current"), ("http://localhost:8321", "old")}
    assert not cls._shared_sandbox_ids and not cls._pending_deletions


@pytest.mark.asyncio
async def test_unreachable_external_service_remains_pending_and_blocks_strict_cleanup(monkeypatch):
    cls = box._JiuwenBoxProviderMixin
    key = "http://localhost:8321|external"
    cls._shared_sandbox_ids[key] = "external"
    client = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
    client.__enter__.return_value = client
    client.delete_sandbox.side_effect = httpx.ConnectError("external unavailable")
    monkeypatch.setattr(box, "_JiuwenBoxClient", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="external unavailable"):
        await lifecycle.cleanup_jiuwenbox_sandboxes(strict=True)
    assert cls._shared_sandbox_ids[key] == "external"
    assert (key, "external") in cls._pending_deletions
    client.delete_sandbox.side_effect = None
    assert await lifecycle.cleanup_jiuwenbox_sandboxes(strict=True) == 1


def test_disable_unregisters_session_sysops_outside_default_tag(monkeypatch):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.sandbox_config_rpc import _teardown_registered_sandbox_sysops

    local = SimpleNamespace(id="local", isolation_key_template=None)
    session = SimpleNamespace(id="session", isolation_key_template="session-key")
    project = SimpleNamespace(id="project", isolation_key_template="project-key")
    registry = SimpleNamespace(_sys_operations={op.id: op for op in (local, session, project)})
    rm = SimpleNamespace(
        _resource_registry=SimpleNamespace(sys_operation=lambda: registry),
        get_sys_operation=lambda: local,
        remove_sys_operation=Mock(side_effect=registry._sys_operations.pop),
    )
    monkeypatch.setattr(Runner, "resource_mgr", rm)
    assert _teardown_registered_sandbox_sysops() == 2
    assert registry._sys_operations == {"local": local}


@pytest.mark.parametrize("registry", [None, SimpleNamespace()])
def test_disable_warns_when_full_registry_falls_back_to_default_tag(monkeypatch, registry):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server import sandbox_config_rpc as rpc

    fallback = SimpleNamespace(id="fallback", isolation_key_template="scope")
    rm = SimpleNamespace(
        _resource_registry=registry, get_sys_operation=Mock(return_value=fallback), remove_sys_operation=Mock(),
    )
    monkeypatch.setattr(Runner, "resource_mgr", rm)
    warning = Mock()
    monkeypatch.setattr(rpc.logger, "warning", warning)
    assert rpc._teardown_registered_sandbox_sysops() == 1
    rm.get_sys_operation.assert_called_once()
    rm.remove_sys_operation.assert_called_once_with("fallback")
    warning.assert_called_once()
    assert "session/project cleanup may be incomplete" in warning.call_args.args[0]


def test_disable_warns_when_default_tag_fallback_also_fails(monkeypatch):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server import sandbox_config_rpc as rpc

    monkeypatch.setattr(Runner, "resource_mgr", SimpleNamespace(
        get_sys_operation=Mock(side_effect=RuntimeError("lookup unavailable")),
    ))
    warning = Mock()
    monkeypatch.setattr(rpc.logger, "warning", warning)
    assert rpc._teardown_registered_sandbox_sysops() == 0
    assert warning.call_count == 2
    assert "fallback get_sys_operation failed" in warning.call_args.args[0]


@pytest.mark.asyncio
async def test_cli_disable_cleans_external_and_historical_entries_and_retries_failure(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server import sandbox_config_rpc as rpc

    cls = box._JiuwenBoxProviderMixin
    failed_key, healthy_key = "http://localhost:8321|external", "http://localhost:9000|historical"
    cls._shared_sandbox_ids[failed_key] = "external"
    cls._pending_deletions[(healthy_key, "historical")] = (True, None)
    requests = []
    unavailable = True

    def client_for(base_url, **kwargs):
        client = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        client.__enter__.return_value = client

        def delete(sid):
            requests.append((base_url, sid))
            if sid == "external" and unavailable:
                raise httpx.ConnectError("external unavailable")

        client.delete_sandbox.side_effect = delete
        return client

    monkeypatch.setattr(box, "_JiuwenBoxClient", client_for)
    runtime = {"enabled": True}

    def update(patch):
        runtime.update(patch)
        return dict(runtime)

    monkeypatch.setattr(server_module, "update_sandbox_runtime", update)
    teardown = Mock(return_value=2)
    monkeypatch.setattr(rpc, "_teardown_registered_sandbox_sysops", teardown)
    runner = SimpleNamespace(get_owned_endpoint=lambda: None, stop=AsyncMock())
    server = SimpleNamespace(
        _jiuwenbox_runner=runner, _agent_manager=SimpleNamespace(recreate_agent=AsyncMock()),
    )
    disable = server_module.AgentWebSocketServer._handle_sandbox_disable
    with pytest.raises(RuntimeError, match="external unavailable"):
        await disable(server, "web")
    assert runtime["enabled"] is False
    assert requests == [("http://localhost:8321", "external"), ("http://localhost:9000", "historical")]
    assert (failed_key, "external") in cls._pending_deletions
    assert (healthy_key, "historical") not in cls._pending_deletions
    unavailable = False
    result = await disable(server, "web")
    assert not result["jiuwenbox_stopped"] and not result["runtime"]["enabled"]
    assert not cls._pending_deletions and not cls._shared_sandbox_ids
    assert runner.stop.await_count == teardown.call_count == 2


@pytest.mark.asyncio
async def test_cli_disable_retires_dead_owned_process_without_live_endpoint(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server import sandbox_config_rpc as rpc
    from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

    cls = box._JiuwenBoxProviderMixin
    cls._pending_deletions[("http://127.0.0.1:8321|dead", "dead")] = (True, None)
    runner = JiuwenBoxRunner()
    runner.process = SimpleNamespace(returncode=1)
    runner.owns_process = True
    assert runner.get_owned_endpoint() is None
    monkeypatch.setattr(box, "_JiuwenBoxClient", Mock(side_effect=AssertionError("no DELETE to dead service")))
    monkeypatch.setattr(rpc, "_teardown_registered_sandbox_sysops", Mock(return_value=0))
    monkeypatch.setattr(server_module, "update_sandbox_runtime", lambda patch: patch)
    server = SimpleNamespace(
        _jiuwenbox_runner=runner, _agent_manager=SimpleNamespace(recreate_agent=AsyncMock()),
    )
    result = await server_module.AgentWebSocketServer._handle_sandbox_disable(server, "web")
    assert not result["runtime"]["enabled"]
    assert not cls._pending_deletions


@pytest.mark.asyncio
async def test_disable_awaits_owned_exit_then_uses_shared_strict_cleanup(monkeypatch):
    from jiuwenswarm.common import config
    from jiuwenswarm.server import sandbox_config_rpc as rpc
    from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

    events = []
    runner = SimpleNamespace(stop=AsyncMock(side_effect=lambda: events.append("stop")))
    monkeypatch.setattr(JiuwenBoxRunner, "instance", lambda: runner)
    monkeypatch.setattr(config, "get_sandbox_runtime", lambda: {"enabled": False})
    monkeypatch.setattr(rpc, "_teardown_registered_sandbox_sysops", lambda: 2)

    async def cleanup(**kwargs):
        assert kwargs == {"reason": "disabled", "strict": True}
        events.append("cleanup")
        raise RuntimeError("pending external delete")

    monkeypatch.setattr(lifecycle, "cleanup_jiuwenbox_sandboxes", cleanup)
    with pytest.raises(RuntimeError, match="pending external delete"):
        await rpc._apply_sandbox_change("enabled", strict=True)
    assert events == ["stop", "cleanup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,strict", [("cli", True), ("rpc", True), ("rpc", False)])
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_disable_attempts_all_deletes_after_stop_failure(monkeypatch, entry, strict, cleanup_failure):
    from jiuwenswarm.common import config
    from jiuwenswarm.server import agent_ws_server as server_module
    from jiuwenswarm.server import sandbox_config_rpc as rpc
    from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

    cls = box._JiuwenBoxProviderMixin
    live_key, historical_key = "http://localhost:8321|live", "http://localhost:9000|historical"
    cls._shared_sandbox_ids[live_key] = "live"
    cls._pending_deletions[(historical_key, "historical")] = (True, None)
    stop_error = RuntimeError("owned process could not be killed")
    cleanup_error = httpx.ConnectError("live sandbox delete failed")
    events = []

    async def stop():
        events.append("stop")
        raise stop_error

    def client_for(base_url, **kwargs):
        client = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        client.__enter__.return_value = client

        def delete(sid):
            events.append((base_url, sid))
            if cleanup_failure and sid == "live":
                raise cleanup_error

        client.delete_sandbox.side_effect = delete
        return client

    monkeypatch.setattr(box, "_JiuwenBoxClient", client_for)
    runner = SimpleNamespace(get_owned_endpoint=lambda: ("localhost", 8321), stop=AsyncMock(side_effect=stop))
    monkeypatch.setattr(JiuwenBoxRunner, "instance", lambda: runner)
    monkeypatch.setattr(config, "get_sandbox_runtime", lambda: {"enabled": False})
    monkeypatch.setattr(rpc, "_teardown_registered_sandbox_sysops", Mock(return_value=2))
    monkeypatch.setattr(server_module, "update_sandbox_runtime", lambda patch: patch)
    server = SimpleNamespace(
        _jiuwenbox_runner=runner, _agent_manager=SimpleNamespace(recreate_agent=AsyncMock()),
    )

    async def disable():
        if entry == "cli":
            return await server_module.AgentWebSocketServer._handle_sandbox_disable(server, "web")
        return await rpc._apply_sandbox_change("enabled", strict=strict)

    if strict:
        with pytest.raises(RuntimeError) as caught:
            await disable()
        if cleanup_failure:
            assert caught.value.__cause__ is cleanup_error
        else:
            assert caught.value is stop_error
    else:
        assert await disable() is None
    assert events == ["stop", ("http://localhost:8321", "live"), ("http://localhost:9000", "historical")]
    if cleanup_failure:
        assert cls._shared_sandbox_ids == {live_key: "live"}
        assert set(cls._pending_deletions) == {(live_key, "live")}
    else:
        assert not cls._shared_sandbox_ids and not cls._pending_deletions
    assert not cls._delete_tasks
