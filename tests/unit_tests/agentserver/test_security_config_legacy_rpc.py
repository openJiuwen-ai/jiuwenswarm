"""Legacy permissions methods use the managed user source after adoption."""

import pytest

from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.security_config_service import SecurityConfigService
from jiuwenswarm.server.security_config_legacy_rpc import dispatch_managed_permissions_request


class Manager:
    def __init__(self):
        self.configs = []

    async def reload_agents_config(self, config, _env):
        self.configs.append(config)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    path = tmp_path / "config.yaml"
    dump_yaml_round_trip(path, {
        "permissions": {
            "enabled": True,
            "tools": {"bash": "allow"},
            "rules": [{"id": "local", "tools": ["bash"], "pattern": "echo *", "action": "allow"}],
            "file_guard": {"enabled": True, "paths": []},
            "net_guard": {"enabled": True, "urls": {}},
        },
        "sandbox": {"enabled": False, "type": "jiuwenbox"},
    })
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc.publish_host_exit_policy_from_config",
        lambda _config: None,
    )
    service = SecurityConfigService(path, builtin_reader=lambda: {"permissions": {}})
    service.initialize_from_live()
    return service


async def _request(managed, method, params=None, manager=None):
    async def no_restart(_params):
        pytest.fail("sandbox is disabled")

    return await dispatch_managed_permissions_request(
        method, params or {}, agent_manager=manager or Manager(),
        restart_sandboxes=no_restart, service=managed,
    )


@pytest.mark.asyncio
async def test_legacy_tools_edit_user_without_overwriting_cloud(managed):
    managed.replace_cloud_policy(1, {"tools": {"bash": "deny"}})
    manager = Manager()
    result = await _request(managed, ReqMethod.PERMISSIONS_TOOLS_UPDATE,
                            {"tool": "bash", "level": "allow"}, manager)
    assert result["applied"]
    assert result["tools"] == {"bash": "allow"}
    assert managed.get_candidate()["local"]["permissions"]["tools"]["bash"] == "allow"
    assert manager.configs[-1]["permissions"]["tools"]["bash"] == "deny"
    assert load_yaml_round_trip(managed.config_path)["permissions"]["tools"]["bash"] == "deny"
    assert (await _request(managed, ReqMethod.PERMISSIONS_TOOLS_GET))["tools"] == {"bash": "allow"}


@pytest.mark.asyncio
async def test_legacy_rule_create_update_delete_and_not_found(managed):
    created = await _request(managed, ReqMethod.PERMISSIONS_RULES_CREATE,
                             {"rule": {"tools": ["bash"], "pattern": "git *", "action": "ask"}})
    rid = created["rule"]["id"]
    assert created["applied"] and rid.startswith("ui_rule_")
    updated = await _request(managed, ReqMethod.PERMISSIONS_RULES_UPDATE,
                             {"id": rid, "patch": {"action": "deny"}})
    assert updated["rule"]["action"] == "deny"
    rules = (await _request(managed, ReqMethod.PERMISSIONS_RULES_GET))["rules"]
    assert len(rules) == 2 and all("layer" not in rule for rule in rules)
    deleted = await _request(managed, ReqMethod.PERMISSIONS_RULES_DELETE, {"id": rid})
    assert deleted["applied"]
    with pytest.raises(LookupError):
        await _request(managed, ReqMethod.PERMISSIONS_RULES_DELETE, {"id": rid})


@pytest.mark.asyncio
async def test_legacy_file_network_and_shell_split_settings_from_rules(managed, tmp_path):
    path = str(tmp_path)
    file_result = await _request(managed, ReqMethod.PERMISSIONS_FILE_GUARD_UPDATE, {
        "patch": {"defaults": {"read": "ask"}, "paths": [{"path": path, "read": "deny"}]},
    })
    assert file_result["applied"]
    net_result = await _request(managed, ReqMethod.PERMISSIONS_NET_GUARD_SET, {
        "net_guard": {"defaults": "ask", "urls": {"example.org": "deny"}},
    })
    assert net_result["applied"]
    shell_result = await _request(managed, ReqMethod.PERMISSIONS_SHELL_GUARD_UPDATE, {
        "patch": {"builtin_rules_enabled": False},
    })
    assert shell_result["applied"]
    candidate = managed.get_candidate()
    assert candidate["settings"]["permissions"]["file_guard"]["defaults"] == {"read": "ask"}
    assert candidate["local"]["permissions"]["file_guard"]["paths"][0]["path"] == path
    assert candidate["settings"]["permissions"]["net_guard"]["defaults"] == "ask"
    assert candidate["local"]["permissions"]["net_guard"]["urls"] == {"example.org": "deny"}
    assert candidate["settings"]["permissions"]["shell_guard"]["builtin_rules_enabled"] is False
    assert (await _request(managed, ReqMethod.PERMISSIONS_NET_GUARD_GET))["net_guard"]["urls"] == {"example.org": "deny"}


@pytest.mark.asyncio
async def test_legacy_tool_edit_retries_unfinished_network_projection(managed, monkeypatch):
    live = load_yaml_round_trip(managed.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(managed.config_path, live)
    pending = managed.update_local("network", {"pending.example": "deny"})
    managed.publish_candidate(expected_revision=pending["revision"])
    managed.record_apply(
        pending["revision"], {"rail": "applied", "host_exit": "applied", "sandbox": "failed: unavailable"},
        sandbox_retry={"network": True},
    )
    synced = []
    monkeypatch.setattr(
        "jiuwenswarm.common.net_guard_config.sync_net_guard_to_sandbox",
        lambda: synced.append("network") or {"skipped": [], "urls": {"pending.example": "deny"}},
    )

    async def restart(_params):
        return {"status": "applied"}

    result = await dispatch_managed_permissions_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE, {"tool": "bash", "level": "ask"},
        agent_manager=Manager(), restart_sandboxes=restart, service=managed,
    )
    assert result["applied"]
    assert synced == ["network"]
    assert result["apply"]["sandbox_retry"] == {}
