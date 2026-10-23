"""Unified security RPC reports individual runtime apply outcomes."""

from copy import deepcopy

import pytest

from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.security_config_service import SecurityConfigService
from jiuwenswarm.server.security_config_rpc import (
    apply_trusted_cloud_policy,
    dispatch_managed_sandbox_request,
    dispatch_security_config_request,
    resume_managed_security_on_start,
)


class Manager:
    def __init__(self):
        self.configs = []

    async def reload_agents_config(self, config, env):
        self.configs.append(deepcopy(config))


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "config.yaml"
    dump_yaml_round_trip(path, {
        "model": "untouched", "permissions": {"enabled": True, "tools": {"bash": "allow"}},
        "sandbox": {"type": "jiuwenbox", "enabled": False},
    })
    return SecurityConfigService(path, builtin_reader=lambda: {"permissions": {}})


@pytest.mark.asyncio
async def test_get_reports_uninitialized_without_adopting_live_config(service):
    async def unexpected_restart(_params):
        pytest.fail("read must not apply sandbox")

    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_GET, {}, agent_manager=Manager(),
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert result == {"initialized": False}
    assert not service.settings_path.exists()
    assert not service.local_path.exists()


@pytest.mark.asyncio
async def test_initialize_and_update_apply_without_sandbox_restart(service, monkeypatch):
    manager = Manager()
    published = []
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda config: published.append(deepcopy(config)),
    )

    async def unexpected_restart(params):
        pytest.fail("tool-only update must not restart sandbox")

    first = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert first["applied"] and first["apply"]["targets"]["sandbox"] == "not_required"
    queried = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_GET, {}, agent_manager=manager,
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert queried["initialized"] is True
    assert queried["apply"]["revision"] == first["apply"]["revision"]
    second = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE,
        {"module": "tool", "value": {"bash": "deny"}, "expected_revision": first["apply"]["revision"]},
        agent_manager=manager, restart_sandboxes=unexpected_restart, service=service,
    )
    assert second["applied"]
    assert manager.configs[-1]["permissions"]["tools"]["bash"] == "deny"
    assert published[-1]["permissions"]["composition"]["revision"] == second["apply"]["revision"]
    assert service.get_candidate()["apply"]["status"] == "applied"


@pytest.mark.asyncio
async def test_profile_defaults_change_does_not_restart_sandbox(service, monkeypatch):
    from jiuwenswarm.common import config as config_module

    service._builtin_reader = lambda: {"permissions": {}}
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    live["permissions"]["file_guard"] = {
        "enabled": True, "defaults": {"read": "allow", "write": "allow", "exec": "allow"}, "paths": [],
    }
    live["permissions"]["net_guard"] = {"enabled": True, "defaults": "allow", "urls": {}}
    dump_yaml_round_trip(service.config_path, live)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def unexpected_restart(_params):
        pytest.fail("file defaults are not a sandbox ACL change")

    first = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=Manager(),
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert service.get_candidate()["permissions"]["file_guard"]["paths"] == []
    assert first["applied"], first["apply"]["targets"]
    profile = {
        "enabled": True, "permission_mode": "strict", "bash_level": "ask",
        "network_level": "ask", "file_guard_default_rw": "ask",
    }
    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_PROFILE_UPDATE,
        {"patch": profile, "expected_revision": first["apply"]["revision"]},
        agent_manager=Manager(), restart_sandboxes=unexpected_restart, service=service,
    )
    assert result["applied"], result["apply"]["targets"]
    assert result["apply"]["targets"]["sandbox"] == "not_required"


@pytest.mark.asyncio
async def test_initialization_keeps_existing_sandbox_when_builtin_paths_cannot_project(service):
    service._builtin_reader = lambda: {"permissions": {"file_guard": {"paths": [
        {"path": "**/.ssh/**", "match": "glob", "read": "deny", "write": "deny", "exec": "deny"},
    ]}}}
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"].update(enabled=True, files=[])
    live["permissions"]["file_guard"] = {"enabled": True, "paths": []}
    dump_yaml_round_trip(service.config_path, live)
    manager = Manager()

    async def unexpected_restart(_params):
        pytest.fail("unrepresentable builtins must not restart the sandbox")

    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert result["saved"] and not result["applied"]
    assert "UNSUPPORTED_COVERAGE" in result["apply"]["targets"]["sandbox"]
    assert load_yaml_round_trip(service.config_path) == live
    assert not manager.configs


@pytest.mark.asyncio
async def test_builtin_get_does_not_publish_or_restart(service):
    service.initialize_from_live()

    async def unexpected_restart(_params):
        pytest.fail("read-only builtin query must not restart")

    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_BUILTIN_GET, {"module": "tool"},
        agent_manager=Manager(), restart_sandboxes=unexpected_restart, service=service,
    )
    assert result == {"module": "tool", "enabled": True, "value": {}}
    with pytest.raises(ValueError, match="requires module only"):
        await dispatch_security_config_request(
            ReqMethod.SECURITY_BUILTIN_GET, {"module": "tool", "other": 1},
            agent_manager=Manager(), restart_sandboxes=unexpected_restart, service=service,
        )


@pytest.mark.asyncio
async def test_startup_replays_pending_sources_before_ready(service, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    assert await resume_managed_security_on_start(
        agent_manager=Manager(), restart_sandboxes=no_restart, service=service,
    ) is None
    service.initialize_from_live()
    manager = Manager()
    state = await resume_managed_security_on_start(
        agent_manager=manager, restart_sandboxes=no_restart, service=service,
    )
    assert state["applied"] and manager.configs
    already_applied = Manager()
    await resume_managed_security_on_start(
        agent_manager=already_applied, restart_sandboxes=no_restart, service=service,
    )
    assert not already_applied.configs
    service.update_local("tool", {"bash": "deny"})
    resumed = Manager()
    state = await resume_managed_security_on_start(
        agent_manager=resumed, restart_sandboxes=no_restart, service=service,
    )
    assert state["applied"]
    assert resumed.configs[-1]["permissions"]["tools"]["bash"] == "deny"


@pytest.mark.asyncio
async def test_startup_does_not_announce_ready_after_apply_failure(service, monkeypatch):
    service.initialize_from_live()

    def fail(_config):
        raise RuntimeError("host exit unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        fail,
    )

    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    with pytest.raises(RuntimeError, match="not ready"):
        await resume_managed_security_on_start(
            agent_manager=Manager(), restart_sandboxes=no_restart, service=service,
        )
    assert service.get_candidate()["apply"]["status"] == "failed"


@pytest.mark.asyncio
async def test_startup_rejects_old_core_even_for_applied_state(service, monkeypatch):
    from openjiuwen.harness.security.permission_engine import core

    service.initialize_from_live()
    revision = service.publish_candidate()["apply"]["revision"]
    service.record_apply(revision, {"rail": "applied", "host_exit": "applied", "sandbox": "not_required"})
    monkeypatch.setattr(core, "COMPOSITION_SCHEMA_VERSION", 0)
    with pytest.raises(RuntimeError, match="does not support"):
        await resume_managed_security_on_start(
            agent_manager=Manager(), restart_sandboxes=None, service=service,
        )


@pytest.mark.asyncio
async def test_host_exit_failure_stays_saved_but_not_applied(service, monkeypatch):
    def fail(_config):
        raise RuntimeError("host exit unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        fail,
    )

    async def unexpected_restart(_params):
        pytest.fail("sandbox disabled")

    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=Manager(),
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert result["saved"] and not result["applied"]
    assert "host exit unavailable" in result["apply"]["targets"]["host_exit"]
    assert result["apply"]["status"] == "failed"


@pytest.mark.asyncio
async def test_old_core_reports_saved_but_not_published(service, monkeypatch):
    from openjiuwen.harness.security.permission_engine import core

    monkeypatch.setattr(core, "COMPOSITION_SCHEMA_VERSION", 0)

    async def unexpected_restart(_params):
        pytest.fail("incompatible core must not apply")

    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=Manager(),
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert result["saved"] and not result["applied"]
    assert "does not support" in result["apply"]["targets"]["rail"]
    assert "composition" not in load_yaml_round_trip(service.config_path)["permissions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["https://example.org/path", "*.example.org"])
async def test_unrepresentable_sandbox_rule_is_not_published(service, monkeypatch, pattern):
    manager = Manager()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def unexpected_restart(_params):
        pytest.fail("unrepresentable policy must not restart the sandbox")

    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=unexpected_restart, service=service,
    )
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(service.config_path, live)
    before = service.config_path.read_bytes()
    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE,
        {"module": "network", "value": {pattern: "deny"}},
        agent_manager=manager, restart_sandboxes=unexpected_restart, service=service,
    )
    assert result["saved"] and not result["applied"]
    assert "UNSUPPORTED_COVERAGE" in result["apply"]["targets"]["sandbox"]
    assert service.config_path.read_bytes() == before
    assert len(manager.configs) == 1


@pytest.mark.asyncio
async def test_external_sandbox_network_is_pending(service, monkeypatch):
    manager = Manager()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def restart(_params):
        return {"network_status": "externally_managed"}

    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=restart, service=service,
    )
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(service.config_path, live)
    monkeypatch.setattr(
        "jiuwenswarm.common.net_guard_config.sync_net_guard_to_sandbox",
        lambda: {"skipped": [], "urls": {"example.org": "deny"}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.file_guard_config.sync_file_guard_to_sandbox",
        lambda: {"skipped": [], "files": []},
    )
    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE,
        {"module": "network", "value": {"example.org": "deny"}},
        agent_manager=manager, restart_sandboxes=restart, service=service,
    )
    assert result["saved"] and not result["applied"]
    assert result["apply"]["targets"]["sandbox"] == "external_pending"


@pytest.mark.asyncio
async def test_trusted_cloud_policy_uses_same_apply_path(service, monkeypatch):
    manager = Manager()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def unexpected_restart(_params):
        pytest.fail("sandbox disabled")

    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=unexpected_restart, service=service,
    )
    result = await apply_trusted_cloud_policy(
        1, {"tools": {"bash": "deny"}}, agent_manager=manager,
        restart_sandboxes=unexpected_restart, service=service,
    )
    assert result["applied"]
    assert service.get_candidate()["cloud"]["policy_version"] == 1
    assert manager.configs[-1]["permissions"]["tools"]["bash"] == "deny"


@pytest.mark.asyncio
async def test_trusted_cloud_policy_duplicate_skips_apply_but_failed_apply_retries(service, monkeypatch):
    manager = Manager()
    attempts = []

    def publish(_config):
        attempts.append("host_exit")
        if len(attempts) == 2:
            raise RuntimeError("temporary failure")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        publish,
    )

    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    initialized = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    assert initialized["applied"]
    failed = await apply_trusted_cloud_policy(
        1, {"tools": {"bash": "deny"}}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    assert not failed["applied"]
    assert len(manager.configs) == 2
    retried = await apply_trusted_cloud_policy(
        1, {"tools": {"bash": "deny"}}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    assert retried["applied"]
    assert retried["apply"]["revision"] == failed["apply"]["revision"]
    assert len(manager.configs) == 3
    repeated = await apply_trusted_cloud_policy(
        1, {"tools": {"bash": "deny"}}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    assert repeated == retried
    assert len(manager.configs) == 3


@pytest.mark.asyncio
async def test_disabled_cloud_source_saves_policy_without_enforcing_until_enabled(service, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    manager = Manager()
    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    disabled = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SETTINGS_UPDATE,
        {"patch": {"policy_sources": [
            {"id": "user", "enabled": True},
            {"id": "cloud", "enabled": False},
            {"id": "builtin", "enabled": True},
        ]}},
        agent_manager=manager, restart_sandboxes=no_restart, service=service,
    )
    assert disabled["applied"]
    received = await apply_trusted_cloud_policy(
        1, {"tools": {"cloud_only": "deny"}}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    assert received["applied"]
    assert service.cloud_path.is_file()
    assert "cloud_only" not in manager.configs[-1]["permissions"]["tools"]
    view = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_GET, {}, agent_manager=manager,
        restart_sandboxes=no_restart, service=service,
    )
    assert view["cloud"]["permissions"]["tools"]["cloud_only"] == "deny"
    assert "cloud_only" not in view["permissions"]["tools"]
    assert view["settings"]["security"]["policy_sources"][1]["enabled"] is False
    assert "permissions" in view["builtin"]
    enabled = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SETTINGS_UPDATE,
        {"patch": {"policy_sources": [
            {"id": "user", "enabled": True},
            {"id": "cloud", "enabled": True},
            {"id": "builtin", "enabled": True},
        ]}},
        agent_manager=manager, restart_sandboxes=no_restart, service=service,
    )
    assert enabled["applied"]
    assert manager.configs[-1]["permissions"]["tools"]["cloud_only"] == "deny"


@pytest.mark.asyncio
async def test_network_source_uses_native_sandbox_sync(service, monkeypatch):
    from jiuwenswarm.common import config as config_module

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )
    manager = Manager()
    restarts = []

    async def restart(params):
        restarts.append(params)
        return {"status": "applied"}

    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=restart, service=service,
    )
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(service.config_path, live)
    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_MODULE_UPDATE,
        {"module": "network", "patch": {"enabled": True}},
        agent_manager=manager, restart_sandboxes=restart, service=service,
    )
    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE,
        {"module": "network", "value": {"example.org": "deny"}},
        agent_manager=manager, restart_sandboxes=restart, service=service,
    )
    assert result["applied"]
    live = load_yaml_round_trip(service.config_path)
    assert live["permissions"]["net_guard"]["urls"] == {"example.org": "deny"}
    assert live["sandbox"]["urls"] == {"example.org": "deny"}
    assert restarts


@pytest.mark.asyncio
async def test_cloud_network_source_switch_updates_sandbox_projection(service, monkeypatch):
    from jiuwenswarm.common import config as config_module

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )
    restarts = []

    async def restart(params):
        restarts.append(params)
        return {"status": "applied"}

    manager = Manager()
    initialized = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=manager,
        restart_sandboxes=restart, service=service,
    )
    assert initialized["applied"]
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(service.config_path, live)
    enabled = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_MODULE_UPDATE,
        {"module": "network", "patch": {"enabled": True}},
        agent_manager=manager, restart_sandboxes=restart, service=service,
    )
    assert enabled["applied"]
    restarts_before_cloud = len(restarts)
    cloud = await apply_trusted_cloud_policy(
        1, {"net_guard": {"urls": {"blocked.example.org": "deny"}}},
        agent_manager=manager, restart_sandboxes=restart, service=service,
    )
    assert cloud["applied"]
    assert load_yaml_round_trip(service.config_path)["sandbox"]["urls"] == {
        "blocked.example.org": "deny",
    }
    disabled = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SETTINGS_UPDATE,
        {"patch": {"policy_sources": [
            {"id": "user", "enabled": True},
            {"id": "cloud", "enabled": False},
            {"id": "builtin", "enabled": True},
        ]}},
        agent_manager=manager, restart_sandboxes=restart, service=service,
    )
    assert disabled["applied"]
    assert load_yaml_round_trip(service.config_path)["sandbox"]["urls"] == {}
    assert len(restarts) == restarts_before_cloud + 2


@pytest.mark.asyncio
async def test_native_sandbox_facade_preserves_four_axes_and_reports_apply(service, monkeypatch, tmp_path):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server import sandbox_policy_render as render

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    service.initialize_from_live()
    source_revision = service.publish_candidate()["apply"]["revision"]
    service.record_apply(source_revision, {"rail": "applied", "host_exit": "applied", "sandbox": "not_required"})
    restarts = []

    async def restart(params):
        restarts.append(params)
        return {"status": "applied"}

    files = {
        "allow_read": ["C:/read-only"], "allow_write": ["C:/write-only"],
        "deny_read": ["C:/blocked-read"], "deny_write": ["C:/blocked-write"],
    }
    config = {
        "enabled": False,
        "filesystem": files,
        "network": {"disable_all": False, "allow_domains": ["native.example"],
                    "deny_domains": ["blocked.example"]},
    }
    stored = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE, {"config": config},
        agent_manager=Manager(), restart_sandboxes=restart, service=service,
    )
    assert stored["applied"] and stored["targets"]["sandbox"] == "not_required"
    assert not restarts
    queried = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_GET, {},
        agent_manager=Manager(), restart_sandboxes=restart, service=service,
    )
    assert queried["config"] == {
        **config, "startup_mode": "internal",
        "limits": {"max_file_mb": 100, "exec_timeout_seconds": 600},
    }
    assert render.get_sandbox_files_axes() == files
    assert service.get_candidate()["apply"]["revision"] == source_revision
    assert service.get_candidate()["apply"]["applied"]

    apply_kinds = []

    async def enabled_change(kind, *, strict=False):
        assert strict
        apply_kinds.append(kind)

    monkeypatch.setattr("jiuwenswarm.server.sandbox_config_rpc._apply_sandbox_change", enabled_change)
    enabled = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE,
        {"config": {**config, "enabled": True}},
        agent_manager=Manager(), restart_sandboxes=restart, service=service,
    )
    assert enabled["applied"] and len(restarts) == 1
    live = load_yaml_round_trip(service.config_path)
    assert live["sandbox"]["enabled"] is True
    assert live["sandbox"]["security_config_user_set"] is True
    limited = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE,
        {"config": {**queried["config"], "enabled": True,
                    "limits": {"max_file_mb": 12, "exec_timeout_seconds": 90}}},
        agent_manager=Manager(), restart_sandboxes=restart, service=service,
    )
    assert limited["applied"] and len(restarts) == 2
    assert apply_kinds == ["enabled", "limits"]
    assert limited["config"]["limits"] == {"max_file_mb": 12, "exec_timeout_seconds": 90}


@pytest.mark.asyncio
async def test_managed_legacy_sandbox_set_uses_native_facade(service, monkeypatch, tmp_path):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server import sandbox_policy_render as render

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    service.initialize_from_live()

    async def mode_change(_kind, *, strict=False):
        assert strict

    monkeypatch.setattr("jiuwenswarm.server.sandbox_config_rpc._apply_sandbox_change", mode_change)

    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    network = await dispatch_managed_sandbox_request(
        ReqMethod.SANDBOX_NETWORK_SET,
        {"disable_all": False, "allow_domains": ["native.example"], "deny_domains": []},
        restart_sandboxes=no_restart, service=service,
    )
    assert network["applied"]
    assert network["network"]["allow_domains"] == ["native.example"]
    files = await dispatch_managed_sandbox_request(
        ReqMethod.SANDBOX_FILES_SET,
        {"allow": ["C:/shared"], "deny": []},
        restart_sandboxes=no_restart, service=service,
    )
    assert files["applied"]
    assert render.get_sandbox_files_axes()["allow_write"] == ["C:/shared"]
    mode = await dispatch_managed_sandbox_request(
        ReqMethod.SANDBOX_STARTUP_MODE_SET, {"startup_mode": "external"},
        restart_sandboxes=no_restart, service=service,
    )
    assert mode["applied"] and mode["startup_mode"] == "external"
    assert load_yaml_round_trip(service.config_path)["sandbox"]["startup_mode"] == "external"
    limits = await dispatch_managed_sandbox_request(
        ReqMethod.SANDBOX_LIMITS_SET, {"limits": {"max_file_mb": 12}},
        restart_sandboxes=no_restart, service=service,
    )
    assert limits["applied"] and limits["limits"] == {
        "max_file_mb": 12, "exec_timeout_seconds": 600,
    }
    queried_limits = await dispatch_managed_sandbox_request(
        ReqMethod.SANDBOX_LIMITS_GET, {}, restart_sandboxes=no_restart, service=service,
    )
    assert queried_limits == {"limits": limits["limits"]}


@pytest.mark.asyncio
async def test_native_sandbox_facade_reports_restart_failure(service, monkeypatch, tmp_path):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server import sandbox_policy_render as render

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    service.initialize_from_live()

    async def enabled_change(_kind, *, strict=False):
        assert strict

    async def fail_restart(_params):
        raise RuntimeError("box unavailable")

    monkeypatch.setattr("jiuwenswarm.server.sandbox_config_rpc._apply_sandbox_change", enabled_change)
    config = {
        "enabled": True,
        "filesystem": {key: [] for key in ("allow_read", "allow_write", "deny_read", "deny_write")},
        "network": {"disable_all": False, "allow_domains": [], "deny_domains": []},
    }
    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE, {"config": config},
        agent_manager=Manager(), restart_sandboxes=fail_restart, service=service,
    )
    assert result["saved"] and not result["applied"]
    assert "box unavailable" in result["targets"]["sandbox"]
    assert (await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_GET, {},
        agent_manager=Manager(), restart_sandboxes=fail_restart, service=service,
    ))["apply"]["status"] == "failed"

    async def retry_restart(_params):
        return {"status": "applied"}

    retried = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE, {"config": config},
        agent_manager=Manager(), restart_sandboxes=retry_restart, service=service,
    )
    assert retried["applied"]
    assert retried["targets"]["sandbox"] == "applied"


@pytest.mark.asyncio
async def test_startup_retries_unapplied_native_sandbox_config(service, monkeypatch, tmp_path):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server import sandbox_policy_render as render

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    service.initialize_from_live()
    revision = service.publish_candidate()["apply"]["revision"]
    service.record_apply(revision, {"rail": "applied", "host_exit": "applied", "sandbox": "not_required"})

    async def enabled_change(_kind, *, strict=False):
        assert strict

    async def fail_restart(_params):
        raise RuntimeError("box unavailable")

    monkeypatch.setattr("jiuwenswarm.server.sandbox_config_rpc._apply_sandbox_change", enabled_change)
    config = {
        "enabled": True,
        "filesystem": {key: [] for key in ("allow_read", "allow_write", "deny_read", "deny_write")},
        "network": {"disable_all": False, "allow_domains": [], "deny_domains": []},
    }
    failed = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE, {"config": config},
        agent_manager=Manager(), restart_sandboxes=fail_restart, service=service,
    )
    assert not failed["applied"]

    async def retry_restart(_params):
        return {"status": "applied"}

    state = await resume_managed_security_on_start(
        agent_manager=Manager(), restart_sandboxes=retry_restart, service=service,
    )
    assert state["applied"]
    assert (await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_SANDBOX_GET, {},
        agent_manager=Manager(), restart_sandboxes=retry_restart, service=service,
    ))["apply"]["status"] == "applied"


def test_native_network_and_composed_urls_remain_distinct(service, monkeypatch, tmp_path):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.common.net_guard_config import render_saved_sandbox_urls
    from jiuwenswarm.server import sandbox_policy_render as render

    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    monkeypatch.setattr(config_module, "get_config_file", lambda: service.config_path)
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    service.initialize_from_live()
    render.set_native_sandbox_network_config({
        "disable_all": False, "allow_domains": ["native.example"],
        "deny_domains": ["native-blocked.example"],
    })
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["urls"] = {"cloud.example": "deny", "native.example": "deny"}
    dump_yaml_round_trip(service.config_path, live)
    render_saved_sandbox_urls(render._runtime_copy_path())
    assert render.get_sandbox_network_config() == {
        "disable_all": False, "allow_domains": [],
        "deny_domains": ["native-blocked.example", "cloud.example", "native.example"],
    }
    assert render.get_native_sandbox_network_config()["allow_domains"] == ["native.example"]
    live["sandbox"]["urls"] = {}
    dump_yaml_round_trip(service.config_path, live)
    render_saved_sandbox_urls(render._runtime_copy_path())
    assert render.get_sandbox_network_config()["allow_domains"] == ["native.example"]
    assert render.get_sandbox_network_config()["deny_domains"] == ["native-blocked.example"]


@pytest.mark.asyncio
async def test_native_file_acl_is_rejected_on_non_windows(service, monkeypatch, tmp_path):
    from jiuwenswarm.server import sandbox_policy_render as render

    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(render, "_is_windows", lambda: False)
    config = {
        "enabled": False,
        "filesystem": {
            "allow_read": ["/private"], "allow_write": [], "deny_read": [], "deny_write": [],
        },
        "network": {"disable_all": False, "allow_domains": [], "deny_domains": []},
    }

    async def no_restart(_params):
        pytest.fail("unsupported ACL must not restart")

    with pytest.raises(NotImplementedError, match="Windows only"):
        await dispatch_security_config_request(
            ReqMethod.SECURITY_CONFIG_SANDBOX_UPDATE, {"config": config},
            agent_manager=Manager(), restart_sandboxes=no_restart, service=service,
        )


@pytest.mark.asyncio
async def test_independent_sandbox_urls_are_not_overwritten(service, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )

    async def unexpected_restart(_params):
        pytest.fail("independently managed sandbox URLs must not be replaced")

    await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=Manager(),
        restart_sandboxes=unexpected_restart, service=service,
    )
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"].update(enabled=True, urls={"manual.example": "deny"})
    dump_yaml_round_trip(service.config_path, live)
    before = service.config_path.read_bytes()
    result = await dispatch_security_config_request(
        ReqMethod.SECURITY_CONFIG_LOCAL_UPDATE,
        {"module": "network", "value": {"example.org": "deny"}},
        agent_manager=Manager(), restart_sandboxes=unexpected_restart, service=service,
    )
    assert not result["applied"]
    assert "independently managed" in result["apply"]["targets"]["sandbox"]
    assert service.config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_cloud_domain_deny_reaches_real_core_host_exit(service):
    from openjiuwen.harness.security.outbound import policy as host_exit

    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    host_exit.reset_host_exit_policy()
    try:
        initialized = await dispatch_security_config_request(
            ReqMethod.SECURITY_CONFIG_INITIALIZE, {}, agent_manager=Manager(),
            restart_sandboxes=no_restart, service=service,
        )
        assert initialized["applied"]
        enabled = await dispatch_security_config_request(
            ReqMethod.SECURITY_CONFIG_MODULE_UPDATE,
            {"module": "network", "patch": {
                "enabled": True, "max_request_mb": 2, "timeout_seconds": 7,
            }},
            agent_manager=Manager(), restart_sandboxes=no_restart, service=service,
        )
        assert enabled["applied"]
        assert host_exit.get_host_exit_state().policy.max_request_mb == 2
        assert host_exit.get_host_exit_state().policy.timeout_seconds == 7
        cloud = await apply_trusted_cloud_policy(
            1, {"net_guard": {"urls": {"blocked.example.org": "deny"}}},
            agent_manager=Manager(), restart_sandboxes=no_restart, service=service,
        )
        assert cloud["applied"]
        with pytest.raises(host_exit.OutboundBlockedError):
            host_exit.check_outbound_url("https://blocked.example.org/resource")
    finally:
        host_exit.reset_host_exit_policy()
