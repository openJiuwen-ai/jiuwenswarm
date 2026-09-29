"""ShellGuard switch persistence and RPC validation."""

from copy import deepcopy

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common import shell_guard_config as service
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
    dispatch_permissions_config_request,
    get_permissions_read_only_req_methods,
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"permissions": {
        "enabled": True,
        "shell_guard": {"unknown_structure": True},
        "file_guard": {"enabled": True},
        "net_guard": {"enabled": True},
        "rules": [{"tools": ["shell"], "pattern": "demo *", "action": "deny"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    return path


def test_default_and_roundtrip_preserve_other_policies(config_file):
    assert service.get_shell_guard_config() == {"builtin_rules_enabled": True}
    before = deepcopy(config.get_config()["permissions"])
    for enabled in (False, True):
        assert service.update_shell_guard_config({"builtin_rules_enabled": enabled}) == {"builtin_rules_enabled": enabled}
        assert service.get_shell_guard_config()["builtin_rules_enabled"] is enabled
        persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))["permissions"]
        assert persisted["shell_guard"].pop("builtin_rules_enabled") is enabled
        assert persisted == before


@pytest.mark.parametrize("patch", [None, [], {"enabled": False}, {"builtin_rules_enabled": "false"}, {"builtin_rules_enabled": 0}])
def test_invalid_patch_does_not_write(config_file, patch):
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        service.update_shell_guard_config(patch)
    assert config_file.read_bytes() == before


def test_rpc_read_update_and_validation(config_file):
    def request(method, params):
        return dispatch_permissions_config_request(AgentRequest(
            request_id="shell-test", channel_id="web", req_method=method, params=params,
        ))

    result = request(ReqMethod.PERMISSIONS_SHELL_GUARD_GET, {})
    assert result.ok and result.payload["shell_guard"]["builtin_rules_enabled"] is True
    result = request(ReqMethod.PERMISSIONS_SHELL_GUARD_UPDATE, {"patch": {"builtin_rules_enabled": False}})
    assert result.ok and result.payload["shell_guard"]["builtin_rules_enabled"] is False
    result = request(ReqMethod.PERMISSIONS_SHELL_GUARD_UPDATE, {"patch": {"builtin_rules_enabled": "false"}})
    assert not result.ok and result.payload["code"] == "BAD_REQUEST"
    assert ReqMethod.PERMISSIONS_SHELL_GUARD_GET in get_permissions_read_only_req_methods()
    assert ReqMethod.PERMISSIONS_SHELL_GUARD_UPDATE not in get_permissions_read_only_req_methods()


def test_upgrade_preserves_shell_switch():
    from jiuwenswarm.common.config_split import extract_user_keep_from_legacy

    user = {"permissions": {"shell_guard": {"builtin_rules_enabled": False}}}
    assert extract_user_keep_from_legacy(user, {})["permissions"]["shell_guard"]["builtin_rules_enabled"] is False


def rpc(method, params):
    return dispatch_permissions_config_request(AgentRequest(
        request_id="shell-rules-test", channel_id="web", req_method=method, params=params,
    ))


@pytest.mark.parametrize("action", ["deny", "allow", "ask"])
@pytest.mark.parametrize("pattern", ["demo ?", r"re:^demo\s+x$"])
def test_rule_crud_readback_and_decision(config_file, action, pattern):
    from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import evaluate_tiered_policy

    # Preserve the pre-existing rule and other permission sections.
    before = deepcopy(config.get_config()["permissions"])
    created = rpc(ReqMethod.PERMISSIONS_RULES_CREATE, {"rule": {
        "tools": ["shell"], "pattern": pattern, "action": action, "description": "test",
    }})
    assert created.ok
    rule = created.payload["rule"]
    payload = rpc(ReqMethod.PERMISSIONS_SHELL_GUARD_GET, {}).payload
    assert rule in payload["rules"]
    assert {r["action"] for r in payload["builtin_rules"]} == {"deny", "ask"}
    assert evaluate_tiered_policy({"rules": [rule]}, "bash", {"command": "demo x"})[0].value == action

    updated = rpc(ReqMethod.PERMISSIONS_RULES_UPDATE, {
        "id": rule["id"], "patch": {"pattern": "updated *", "action": "ask"},
    })
    assert updated.ok
    saved = next(r for r in service.get_shell_guard_rules()["rules"] if r.get("id") == rule["id"])
    assert saved["pattern"] == "updated *" and saved["action"] == "ask"
    assert saved["tools"] == ["shell"]
    assert rpc(ReqMethod.PERMISSIONS_RULES_DELETE, {"id": rule["id"]}).ok
    assert config.get_config()["permissions"] == before


@pytest.mark.parametrize("pattern", ["re:", "re:[", "re:(?bad)", "re:re:^shutdown", "re: RE:^shutdown"])
def test_invalid_regex_rejected_before_create_or_update(config_file, pattern):
    created = rpc(ReqMethod.PERMISSIONS_RULES_CREATE, {"rule": {
        "tools": ["powershell"], "pattern": "demo *", "action": "deny",
    }})
    assert created.ok
    before = config_file.read_bytes()
    for method, params in [
        (ReqMethod.PERMISSIONS_RULES_CREATE, {"rule": {"tools": ["shell"], "pattern": pattern, "action": "deny"}}),
        (ReqMethod.PERMISSIONS_RULES_UPDATE, {"id": created.payload["rule"]["id"], "patch": {"pattern": pattern}}),
    ]:
        result = rpc(method, params)
        assert not result.ok and result.payload["code"] == "BAD_REQUEST"
        assert config_file.read_bytes() == before


def test_shell_rule_listing_excludes_other_guards_and_honors_custom_tools(config_file):
    def mutate(data):
        data["permissions"]["categories"] = {"shell": ["custom_exec"]}
        data["permissions"]["rules"] = [
            {"id": "custom", "tools": ["custom_exec"], "pattern": "demo *", "action": "ask"},
            {"id": "file", "tools": ["read_file"], "pattern": "*", "action": "deny"},
            {"id": "builtin", "tools": ["shell"], "pattern": "*", "action": "deny", "layer": "builtin"},
        ]
        return data
    config.update_config(mutate)
    assert [rule["id"] for rule in service.get_shell_guard_rules()["rules"]] == ["custom"]
