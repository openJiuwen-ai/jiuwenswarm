"""Configuration persistence, synchronization and sandbox compilation contracts."""

from copy import deepcopy

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.common import file_guard_config as service
from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("permissions:\n  enabled: true\n  file_guard:\n    enabled: true\n    paths: []\n"
                    "sandbox:\n  type: jiuwenbox\n  files: []\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(builder, "_resolve_workspace_dir", lambda: None)
    monkeypatch.setattr(builder, "_resolve_project_dir", lambda _: None)
    monkeypatch.setattr(builder, "_resolve_config_ro_path", lambda: None)
    return path


def rule(path, write="deny"):
    return {"path": str(path), "read": "allow", "write": write}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("old_files", [{"paths": []}, {"allow": ["old"], "deny": []}])
def test_empty_files_list_and_reject_nested_paths(config_file, enabled, old_files):
    assert config.get_sandbox_runtime()["files"] == []
    policy, _ = builder.build_filesystem_policy([])
    assert policy["filesystem_policy"] == {"files": [], "directories": []}
    with pytest.raises(ValueError, match="must be a list"):
        builder.build_filesystem_policy({"paths": []})
    data = config.load_yaml_round_trip(config_file)
    data["sandbox"]["files"] = old_files
    data["sandbox"]["enabled"] = enabled
    config.dump_yaml_round_trip(config_file, data)
    assert config.get_sandbox_runtime()["files"] == []
    config.update_sandbox_runtime({"enabled": False})
    assert config.load_yaml_round_trip(config_file)["sandbox"]["files"] == []


def test_update_then_explicit_sync_and_delete(config_file, tmp_path):
    entry = {**rule(tmp_path), "exec": "ask"}
    service.update_file_guard_config({"paths": [entry], "defaults": {"read": "ask"}})
    assert service.get_file_guard_config()["paths"] == [entry]
    assert config.get_sandbox_runtime()["files"] == []
    result = service.sync_file_guard_to_sandbox()
    assert result == {"files": [rule(tmp_path)], "skipped": [], "restart_required": True}
    assert config.get_sandbox_runtime()["files"] == result["files"]
    assert service.get_file_guard_config()["paths"][0]["exec"] == "ask"
    service.update_file_guard_config({"paths": [rule(tmp_path, "allow")]})
    assert "exec" not in service.get_file_guard_config()["paths"][0]
    service.update_file_guard_config({"paths": []})
    assert service.sync_file_guard_to_sandbox()["files"] == []


@pytest.mark.parametrize("bad", [
    {"path": "**/.ssh/**", "match": "glob", "read": "allow", "write": "deny"},
    {"path": "relative", "read": "allow", "write": "deny"},
])
def test_sync_skips_unsupported_and_saves_supported(config_file, tmp_path, bad):
    entries = [bad, rule(tmp_path)]
    service.update_file_guard_config({"paths": entries})
    result = service.sync_file_guard_to_sandbox()
    assert result["files"] == [rule(tmp_path)]
    assert result["skipped"][0]["path"] == bad["path"]
    assert result["skipped"][0]["reason"]
    assert config.get_sandbox_runtime()["files"] == result["files"]
    assert service.get_file_guard_config()["paths"] == entries


def test_file_guard_rpc_read_update(config_file, tmp_path):
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        dispatch_permissions_config_request,
    )
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    response = dispatch_permissions_config_request(AgentRequest(
        request_id="update", channel_id="test", req_method=ReqMethod.PERMISSIONS_FILE_GUARD_UPDATE,
        params={"patch": {"paths": [rule(tmp_path)]}}))
    assert response.ok
    response = dispatch_permissions_config_request(AgentRequest(
        request_id="get", channel_id="test", req_method=ReqMethod.PERMISSIONS_FILE_GUARD_GET, params={}))
    assert response.ok
    assert response.payload["file_guard"]["paths"] == [rule(tmp_path)]


def test_sync_all_unsupported_replaces_previous_snapshot_with_empty(config_file, tmp_path):
    service.update_file_guard_config({"paths": [rule(tmp_path)]})
    service.sync_file_guard_to_sandbox()
    service.update_file_guard_config({"paths": [{**rule(tmp_path), "write": "ask"}]})
    result = service.sync_file_guard_to_sandbox()
    assert result["files"] == []
    assert "explicit allow/deny" in result["skipped"][0]["reason"]
    assert config.get_sandbox_runtime()["files"] == []


def test_sync_skips_nonexistent_path(config_file, tmp_path):
    service.update_file_guard_config({"paths": [rule(tmp_path / "missing"), rule(tmp_path)]})
    result = service.sync_file_guard_to_sandbox()
    assert result["files"] == [rule(tmp_path)]
    assert "existing absolute path" in result["skipped"][0]["reason"]


def test_sync_inherits_defaults_but_preserves_explicit_permissions(config_file, tmp_path):
    service.update_file_guard_config({
        "defaults": {"read": "allow", "write": "allow", "exec": "ask"},
        "paths": [{"path": str(tmp_path), "write": "deny"}],
    })
    result = service.sync_file_guard_to_sandbox()
    assert result["files"] == [rule(tmp_path)]
    assert result["skipped"] == []
    assert "read" not in service.get_file_guard_config()["paths"][0]
    service.update_file_guard_config({"paths": [{"path": str(tmp_path), "read": "allow"}]})
    assert service.sync_file_guard_to_sandbox()["files"] == [rule(tmp_path, "allow")]
    service.update_file_guard_config({"paths": [{"path": str(tmp_path), "write": "ask"}]})
    assert service.sync_file_guard_to_sandbox()["files"] == []


def test_invalid_update_does_not_write(config_file, tmp_path):
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        service.update_file_guard_config({"paths": [{**rule(tmp_path, "allow"), "read": "invalid"}]})
    assert config_file.read_bytes() == before


def test_readonly_and_readwrite_compile(config_file, tmp_path):
    ro = tmp_path / "ro"
    rw = tmp_path / "rw"
    ro.mkdir()
    rw.mkdir()
    policy, uploads = builder.build_filesystem_policy([rule(ro), rule(rw, "allow")])
    assert not uploads
    assert str(ro) in policy["filesystem_policy"]["read_only"]
    assert str(rw) in policy["filesystem_policy"]["read_write"]
    assert {m["sandbox_path"] for m in policy["filesystem_policy"]["bind_mounts"]} == {str(ro), str(rw)}


def test_read_deny_never_degrades_to_readonly(config_file, tmp_path, monkeypatch):
    entry = {**rule(tmp_path), "read": "deny"}
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(NotImplementedError, match="read deny is unsupported"):
        builder.build_filesystem_policy([entry])
    monkeypatch.setattr(service.sys, "platform", "win32")
    policy, _ = builder.build_filesystem_policy([entry])
    assert policy["windows"]["filesystem"] == {
        "deny_read": [str(tmp_path)], "deny_write": [str(tmp_path)]}


def test_conflicting_nested_rules_rejected(config_file, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="conflicting"):
        builder.build_filesystem_policy([rule(tmp_path), rule(child, "allow")])


@pytest.mark.parametrize("parent_first", [True, False])
def test_sync_skips_conflicts_and_replaces_previous_files(config_file, tmp_path, parent_first):
    parent = tmp_path / "parent"
    child = parent / "child"
    independent = tmp_path / "independent"
    old = tmp_path / "old"
    child.mkdir(parents=True)
    independent.mkdir()
    old.mkdir()
    service.update_file_guard_config({"paths": [rule(old)]})
    service.sync_file_guard_to_sandbox()
    pair = [rule(parent), rule(child, "allow")]
    if not parent_first:
        pair.reverse()
    entries = [*pair, rule(independent, "allow")]
    service.update_file_guard_config({"paths": entries})
    result = service.sync_file_guard_to_sandbox()
    assert result["files"] == [rule(parent), rule(independent, "allow")]
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["path"] == str(child)
    assert "conflicting sandbox rules" in result["skipped"][0]["reason"]
    assert config.get_sandbox_runtime()["files"] == result["files"]
    assert service.get_file_guard_config()["paths"] == entries


def test_other_runtime_updates_preserve_synced_rules(config_file, tmp_path):
    service.update_file_guard_config({"paths": [rule(tmp_path)]})
    service.sync_file_guard_to_sandbox()
    files = deepcopy(config.get_sandbox_runtime()["files"])
    config.update_sandbox_runtime({"enabled": True})
    assert config.get_sandbox_runtime()["files"] == files
    with pytest.raises(ValueError, match="managed by"):
        config.update_sandbox_runtime({"files": []})


def test_read_deny_overrides_inherited_allow_and_is_synchronized(config_file, tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    service.update_file_guard_config({
        "defaults": {"read": "allow", "write": "allow", "exec": "allow"},
        "paths": [{"path": str(tmp_path), "read": "deny"}],
    })
    guard = service.get_file_guard_config()["paths"][0]
    assert guard["write"] == guard["exec"] == "deny"
    result = service.sync_file_guard_to_sandbox()
    assert result["skipped"] == []
    assert result["files"] == [{"path": str(tmp_path), "read": "deny", "write": "deny"}]


def test_linux_read_deny_cannot_be_dropped_under_allowed_parent(config_file, tmp_path, monkeypatch):
    child = tmp_path / "private"
    child.mkdir()
    service.update_file_guard_config({"paths": [rule(tmp_path, "allow"), {"path": str(child), "read": "deny"}]})
    before = config_file.read_bytes()
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(NotImplementedError, match="read deny is unsupported"):
        service.sync_file_guard_to_sandbox()
    assert config_file.read_bytes() == before


def test_old_sandbox_rpc_keeps_runtime_copy(config_file, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import sandbox_config_rpc as rpc
    from jiuwenswarm.server import sandbox_policy_render as render
    calls = []
    monkeypatch.setattr(render, "set_sandbox_files_config", lambda a, d: calls.append((a, d)) or {"allow": a, "deny": d})
    monkeypatch.setattr(rpc, "_trigger_apply", lambda kind: calls.append(kind))
    before = config_file.read_bytes()
    response = rpc.dispatch_sandbox_config_request(AgentRequest(
        request_id="test", channel_id="test", req_method=ReqMethod.SANDBOX_FILES_SET,
        params={"allow": ["C:/sample"], "deny": []}))
    assert response.ok
    assert calls == [(["C:/sample"], []), "files"]
    assert config_file.read_bytes() == before
