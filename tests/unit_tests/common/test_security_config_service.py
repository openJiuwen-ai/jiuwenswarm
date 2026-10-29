"""Source storage and guarded publication for security configuration."""

from copy import deepcopy

import pytest

from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip
from jiuwenswarm.common.permission_profile import resolve_permission_profile_rules
from jiuwenswarm.common.security_config_compose import validate_source
from jiuwenswarm.common.security_config_service import SecurityConfigService


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "config.yaml"
    dump_yaml_round_trip(path, {
        "model": "untouched",
        "permissions": {
            "enabled": True,
            "tools": {"bash": "allow"},
            "rules": [{"id": "user-1", "tools": ["bash"], "pattern": "local *", "action": "allow"}],
            "file_guard": {"enabled": True, "defaults": {"read": "ask"}, "paths": [{"path": "/private", "read": "deny"}]},
            "net_guard": {"enabled": True, "defaults": "allow", "urls": {"local.example": "allow"}},
            "approval_overrides": [{"id": "approved-1"}],
        },
        "sandbox": {"enabled": False, "type": "jiuwenbox"},
    })
    builtin = {
        "permissions": {
            "tools": {"bash": "ask", "builtin_tool": "deny"},
            "rules": [{"id": "builtin-1", "tools": ["bash"], "pattern": "danger *", "action": "deny"}],
            "file_guard": {"paths": []},
            "net_guard": {"urls": {"builtin.example": "deny"}},
        },
    }
    return SecurityConfigService(path, builtin_reader=lambda: deepcopy(builtin))


def test_initialize_keeps_live_and_splits_sources(service):
    before = service.config_path.read_bytes()
    state = service.initialize_from_live()
    snapshot = service.get_candidate()
    assert service.config_path.read_bytes() == before
    assert service.initial_snapshot_path.is_file()
    assert snapshot["settings"]["permissions"]["file_guard"] == {"enabled": True, "defaults": {"read": "ask"}}
    assert snapshot["local"]["permissions"]["file_guard"]["paths"][0]["read"] == "deny"
    assert snapshot["permissions"]["composition"]["source_order"] == ["user", "cloud", "builtin"]
    assert snapshot["permissions"]["tools"] == {"bash": "allow", "builtin_tool": "deny"}
    assert snapshot["permissions"]["approval_overrides"] == [{"id": "approved-1"}]
    assert state["saved"] and not state["applied"]
    with pytest.raises(RuntimeError, match="already initialized"):
        service.initialize_from_live()


def test_permission_profile_updates_settings_and_local_without_touching_cloud(service):
    first = service.initialize_from_live()
    cloud = service.replace_cloud_policy(1, {"tools": {"bash": "deny"}})
    profile = {
        "enabled": False, "permission_mode": "normal", "bash_level": "allow",
        "network_level": "allow", "file_guard_default_rw": "allow",
    }
    updated = service.update_permission_profile(profile, expected_revision=cloud["revision"])
    candidate = service.get_candidate()
    assert updated["revision"] != first["revision"]
    assert candidate["settings"]["permissions"]["enabled"] is False
    assert candidate["settings"]["permissions"]["file_guard"]["defaults"]["write"] == "allow"
    assert candidate["local"]["permissions"]["tools"]["mcp_free_search"] == "allow"
    assert candidate["cloud"]["permissions"]["tools"] == {"bash": "deny"}
    assert candidate["permissions"]["tools"]["bash"] == "deny"
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        service.update_permission_profile(profile, expected_revision=cloud["revision"])
    with pytest.raises(ValueError, match="invalid permission profile"):
        service.update_permission_profile({**profile, "network_level": ["allow"]})


def test_permission_profile_preserves_existing_file_guard_switch_and_exec_default(service):
    service.initialize_from_live()
    settings = load_yaml_round_trip(service.settings_path)
    settings["permissions"]["file_guard"].update(
        enabled=False, defaults={"read": "ask", "write": "ask", "exec": "deny"},
    )
    dump_yaml_round_trip(service.settings_path, settings)
    revision = service.get_candidate()["apply"]["revision"]
    service.update_permission_profile({
        "enabled": True, "permission_mode": "normal", "bash_level": "allow",
        "network_level": "allow", "file_guard_default_rw": "allow",
    }, expected_revision=revision)
    guard = service.get_candidate()["settings"]["permissions"]["file_guard"]
    assert guard["enabled"] is False
    assert guard["defaults"] == {"read": "allow", "write": "allow", "exec": "deny"}


def test_profile_change_preserves_disabled_user_rules(service):
    service.initialize_from_live()
    sources = [
        {"id": "user", "enabled": False},
        {"id": "cloud", "enabled": True},
        {"id": "builtin", "enabled": True},
    ]
    disabled = service.update_settings({"policy_sources": sources})
    profile = {
        "enabled": True, "permission_mode": "strict", "bash_level": "ask",
        "network_level": "ask", "file_guard_default_rw": "ask",
    }
    service.update_permission_profile(profile, expected_revision=disabled["revision"])
    stored = load_yaml_round_trip(service.local_path)["permissions"]
    assert stored["rules"][0]["id"] == "user-1"
    assert stored["file_guard"]["paths"][0]["path"] == "/private"
    assert stored["tools"]["bash"] == "ask"
    sources[0]["enabled"] = True
    service.update_settings({"policy_sources": sources})
    assert service.get_candidate()["local"]["permissions"]["rules"][0]["id"] == "user-1"


def test_partial_initialization_never_reopens_legacy_writer(service):
    service.initial_snapshot_path.write_text("permissions: {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="partially initialized"):
        service.is_initialized()
    with pytest.raises(RuntimeError, match="already initialized"):
        service.initialize_from_live()


def test_ambiguous_existing_sandbox_network_is_not_silently_imported(service):
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["urls"] = {"old.example": "deny"}
    dump_yaml_round_trip(service.config_path, live)
    dump_yaml_round_trip(service.config_path.parent / "windows-policy.runtime.yaml", {
        "windows": {"network": {"disable_all": False, "egress": {
            "allowed_domains": [], "blocked_domains": ["old.example"],
        }}},
    })
    with pytest.raises(ValueError, match="origin is ambiguous"):
        service.initialize_from_live()
    assert not service.settings_path.exists()


def test_builtin_rules_are_read_only_even_when_source_is_disabled(service):
    service.initialize_from_live()
    assert service.get_builtin("tool") == {
        "module": "tool", "enabled": True,
        "value": {"bash": "ask", "builtin_tool": "deny"},
    }
    service.update_settings({"policy_sources": [{"id": "builtin", "enabled": False}]})
    assert service.get_builtin("network")["value"] == {"builtin.example": "deny"}
    assert service.get_builtin("network")["enabled"] is False
    with pytest.raises(ValueError, match="unknown security module"):
        service.get_builtin("sandbox")


def test_builtin_package_change_invalidates_applied_revision(service):
    service.initialize_from_live()
    first = service.publish_candidate()["apply"]["revision"]
    service.record_apply(first, {"rail": "applied", "host_exit": "applied", "sandbox": "not_required"})
    service._builtin_reader = lambda: {"permissions": {"tools": {"new_builtin": "deny"}}}
    snapshot = service.get_candidate()
    assert snapshot["apply"]["revision"] != first
    assert snapshot["apply"]["status"] == "applying"
    assert snapshot["permissions"]["tools"]["new_builtin"] == "deny"


def test_cloud_versions_priority_and_source_switches(service):
    service.initialize_from_live()
    cloud_rules = {
        "tools": {"bash": "ask", "cloud_tool": "allow"},
        "net_guard": {"urls": {"local.example": "ask"}},
    }
    cloud_state = service.replace_cloud_policy(2, cloud_rules)
    assert service.replace_cloud_policy(2, cloud_rules)["revision"] == cloud_state["revision"]
    with pytest.raises(ValueError, match="conflicting"):
        service.replace_cloud_policy(2, {"tools": {"bash": "deny"}})
    with pytest.raises(ValueError, match="stale"):
        service.replace_cloud_policy(1, cloud_rules)
    current = service.get_candidate()
    assert current["permissions"]["tools"]["bash"] == "allow"
    assert current["permissions"]["net_guard"]["urls"]["local.example"] == "allow"
    reordered = service.update_settings({"policy_sources": [
        {"id": "cloud", "enabled": True},
        {"id": "user", "enabled": True},
        {"id": "builtin", "enabled": True},
    ]}, expected_revision=cloud_state["revision"])
    assert service.get_candidate()["permissions"]["tools"]["bash"] == "ask"
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        service.update_settings({"fail_closed": {"enabled": True}}, expected_revision=cloud_state["revision"])
    service.replace_cloud_policy(3, {"tools": {"bash": "deny"}})
    assert service.get_candidate()["permissions"]["tools"]["bash"] == "deny"
    service.update_settings({"policy_sources": [
        {"id": "user", "enabled": False},
        {"id": "cloud", "enabled": True},
        {"id": "builtin", "enabled": False},
    ]})
    candidate = service.get_candidate()["permissions"]
    assert candidate["approval_overrides"] == []
    assert candidate["tools"] == {"bash": "deny"}
    assert candidate["composition"]["source_order"] == ["cloud"]
    assert reordered["revision"] != cloud_state["revision"]


def test_disabled_source_views_show_saved_rules_without_enforcing_them(service):
    service.initialize_from_live()
    service.replace_cloud_policy(1, {"tools": {"cloud_only": "deny"}})
    service.update_settings({"policy_sources": [
        {"id": "user", "enabled": False},
        {"id": "cloud", "enabled": False},
    ]})

    snapshot = service.get_candidate(include_disabled_sources=True)
    assert snapshot["local"]["permissions"]["tools"]["bash"] == "allow"
    assert snapshot["cloud"]["permissions"]["tools"]["cloud_only"] == "deny"
    assert snapshot["builtin"]["permissions"]["tools"]["builtin_tool"] == "deny"
    assert snapshot["permissions"]["tools"] == {}
    assert snapshot["permissions"]["composition"]["source_order"] == []
    assert snapshot["source_errors"] == {}

    service.update_local("tool", {"stored_only": "ask"})
    edited = service.get_candidate(include_disabled_sources=True)
    assert edited["local"]["permissions"]["tools"] == {"stored_only": "ask"}
    assert edited["permissions"]["tools"] == {}

    def broken_builtin():
        raise ValueError("broken builtin")

    service._builtin_reader = broken_builtin
    unreadable = service.get_candidate(include_disabled_sources=True)
    assert unreadable["source_errors"]["builtin"] == "broken builtin"
    assert unreadable["permissions"]["tools"] == {}


def test_local_module_replacement_and_invalid_input_do_not_touch_live(service):
    service.initialize_from_live()
    before = service.config_path.read_bytes()
    previous = service.local_path.read_bytes()
    with pytest.raises(ValueError, match="read"):
        service.update_local("file", [{"path": "/private", "read": "invalid"}])
    with pytest.raises(ValueError, match="unknown"):
        service.update_settings({"policy_sources": [{"id": "unregistered", "enabled": True}]})
    assert service.local_path.read_bytes() == previous
    assert service.config_path.read_bytes() == before
    service.update_local("tool", {"mcp_custom": "deny"})
    service.update_local("network", {"example.com": "ask"})
    service.update_local("command", [{"id": "new", "tools": ["bash"], "pattern": "echo *", "action": "allow"}])
    service.update_local("file", [{"path": "/new", "read": "allow", "write": "deny"}])
    local = service.get_candidate()["local"]["permissions"]
    assert local["tools"] == {"mcp_custom": "deny"}
    assert local["net_guard"]["urls"] == {"example.com": "ask"}
    assert local["rules"][0]["id"] == "new"
    assert local["file_guard"]["paths"][0]["path"] == "/new"
    assert service.config_path.read_bytes() == before


def test_native_module_settings_stay_out_of_rule_sources(service):
    service.initialize_from_live()
    before = service.config_path.read_bytes()
    service.update_module_settings("file", {"defaults": {"write": "deny"}})
    service.update_module_settings("network", {
        "defaults": "ask", "enforce_host_exit": False,
        "max_request_mb": 20, "timeout_seconds": 90,
    })
    service.update_module_settings("command", {"builtin_rules_enabled": False})
    service.update_module_settings("permissions", {"enabled": False, "permission_mode": "normal"})
    snapshot = service.get_candidate()
    settings = snapshot["settings"]["permissions"]
    assert settings["file_guard"]["defaults"] == {"read": "ask", "write": "deny"}
    assert settings["net_guard"]["defaults"] == "ask"
    assert settings["net_guard"]["max_request_mb"] == 20
    assert settings["net_guard"]["timeout_seconds"] == 90
    assert settings["shell_guard"]["builtin_rules_enabled"] is False
    assert settings["enabled"] is False
    assert snapshot["local"]["permissions"]["file_guard"]["paths"]
    with pytest.raises(ValueError, match="belong"):
        service.update_module_settings("file", {"paths": []})
    with pytest.raises(ValueError, match="network settings"):
        service.update_module_settings("network", {"urls": {}})
    with pytest.raises(ValueError, match="max_request_mb"):
        service.update_module_settings("network", {"max_request_mb": 0})
    assert service.config_path.read_bytes() == before


def test_disabled_source_is_not_parsed_until_reenabled(service):
    service.initialize_from_live()
    service.replace_cloud_policy(1, {"tools": {"bash": "deny"}})
    service.update_settings({"policy_sources": [{"id": "user", "enabled": True}]})
    service.cloud_path.write_text("not: [valid", encoding="utf-8")
    assert service.get_candidate()["permissions"]["tools"] == {"bash": "allow"}
    snapshot = service.get_candidate(include_disabled_sources=True)
    assert snapshot["permissions"]["tools"] == {"bash": "allow"}
    assert "cloud" in snapshot["source_errors"]
    with pytest.raises(Exception):
        service.update_settings({"policy_sources": [{"id": "cloud", "enabled": True}]})
    assert service.get_candidate()["permissions"]["tools"] == {"bash": "allow"}


def test_corrupt_enabled_source_can_be_switched_off(service):
    service.initialize_from_live()
    service.replace_cloud_policy(1, {"tools": {"bash": "deny"}})
    service.cloud_path.write_text("not: [valid", encoding="utf-8")
    with pytest.raises(Exception):
        service.get_candidate()
    service.update_settings({"policy_sources": [{"id": "user", "enabled": True}]})
    assert service.get_candidate()["permissions"]["tools"] == {"bash": "allow"}


def test_empty_source_list_skips_builtin_reader_and_keeps_defaults(service):
    service.initialize_from_live()
    service._builtin_reader = lambda: pytest.fail("disabled builtin was read")
    service.update_settings({"policy_sources": []})
    effective = service.get_candidate()["permissions"]
    assert effective["composition"]["source_order"] == []
    assert effective["tools"] == {}
    assert effective["file_guard"]["defaults"] == {"read": "ask"}


def test_package_template_can_be_adopted_without_mutating_live(tmp_path):
    from jiuwenswarm.common.utils import get_package_resources_dir

    template = (get_package_resources_dir() / "config.yaml").read_bytes()
    path = tmp_path / "config.yaml"
    path.write_bytes(template)
    service = SecurityConfigService(path)
    service.initialize_from_live()
    candidate = service.get_candidate()
    assert candidate["local"]["permissions"]["rules"]
    assert candidate["permissions"]["rules"]
    delete_rule = next(
        rule for rule in candidate["permissions"]["rules"]
        if rule["id"] == "shell_delete_files" and rule["layer"] == "user"
    )
    assert delete_rule["profile_actions"] == {"full_access": "allow"}
    assert resolve_permission_profile_rules({"enabled": False, "rules": [delete_rule]})["rules"][0]["action"] == "allow"
    assert resolve_permission_profile_rules({"enabled": True, "rules": [delete_rule]})["rules"][0]["action"] == "ask"
    assert path.read_bytes() == template
    assert any(rule.get("profile_actions") for rule in candidate["local"]["permissions"]["rules"])
    assert service.publish_candidate()["config"]["permissions"]["rules"]


def test_profile_actions_are_preserved_and_validated(service):
    live = load_yaml_round_trip(service.config_path)
    live["permissions"]["rules"][0]["profile_actions"] = {"full_access": "allow"}
    dump_yaml_round_trip(service.config_path, live)
    service.initialize_from_live()
    assert service.get_candidate()["local"]["permissions"]["rules"][0]["profile_actions"] == {
        "full_access": "allow",
    }
    with pytest.raises(ValueError, match="invalid profile_actions"):
        service.update_local("command", [{
            "id": "invalid-profile", "tools": ["bash"], "pattern": "bad *",
            "action": "ask", "profile_actions": {"full_access": []},
        }])


def test_profile_actions_preserve_template_rule_and_reject_invalid_overrides():
    rule = {
        "id": "delete-files", "tools": ["shell"], "pattern": "rm *",
        "action": "ask", "profile_actions": {"full_access": "allow"},
    }
    source = validate_source({"permissions": {"rules": [rule]}})
    assert source["permissions"]["rules"][0]["profile_actions"] == {"full_access": "allow"}
    with pytest.raises(ValueError, match="invalid profile_actions"):
        validate_source({"permissions": {"rules": [{**rule, "profile_actions": {"full_access": "skip"}}]}})


@pytest.mark.parametrize("profile_actions", [
    [], {}, {"auto_approve": "allow"}, {"full_access": []}, {"full_access": "invalid"},
])
def test_rule_profile_actions_rejects_invalid_values(profile_actions):
    source = {"permissions": {"rules": [{
        "id": "delete", "tools": ["shell"], "pattern": "rm *", "action": "ask",
        "profile_actions": profile_actions,
    }]}}
    with pytest.raises(ValueError, match="invalid profile_actions"):
        validate_source(source)


def test_lost_state_and_interrupted_source_write_get_fresh_revision(service):
    first = service.initialize_from_live()["revision"]
    service.state_path.unlink()
    second = service.get_candidate()["apply"]["revision"]
    assert second != first
    service.update_local("tool", {"bash": "deny"})
    third = service.get_candidate()["apply"]["revision"]
    service.local_path.write_bytes(service.local_path.read_bytes() + b"\n")
    fourth = service.get_candidate()["apply"]["revision"]
    assert fourth not in (first, second, third)
    service.state_path.write_text("{broken", encoding="utf-8")
    fifth = service.get_candidate()["apply"]["revision"]
    assert fifth not in (first, second, third, fourth)


def test_publish_requires_composition_capable_core(service, monkeypatch):
    from openjiuwen.harness.security.permission_engine import core

    service.initialize_from_live()
    before = service.config_path.read_bytes()
    monkeypatch.setattr(core, "COMPOSITION_SCHEMA_VERSION", 0, raising=False)
    with pytest.raises(RuntimeError, match="does not support"):
        service.publish_candidate()
    assert service.config_path.read_bytes() == before


@pytest.mark.parametrize("canonical_path", [False, True])
def test_publish_and_record_apply_preserve_unmanaged_config(service, monkeypatch, canonical_path):
    from jiuwenswarm.common import config as config_module

    if canonical_path:
        monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path)
    else:
        monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", service.config_path.with_name("other.yaml"))
    live = load_yaml_round_trip(service.config_path)
    sandbox = {
        "enabled": False, "type": "jiuwenbox", "url": "http://127.0.0.1:8321",
        "startup_mode": "internal", "policy_file": "sandbox-policy.yaml",
        "limits": {"max_file_mb": 100, "exec_timeout_seconds": 600},
        "idle_ttl_seconds": 900, "custom": {"keep": ["nested", "values"]},
    }
    live["sandbox"] = deepcopy(sandbox)
    dump_yaml_round_trip(service.config_path, live)
    service.initialize_from_live()
    revision = service.update_settings({"fail_closed": {"enabled": True}})["revision"]
    result = service.publish_candidate(expected_revision=revision)
    live = load_yaml_round_trip(service.config_path)
    assert result["apply"]["revision"] == revision
    assert live["model"] == "untouched"
    assert live["sandbox"] == sandbox
    assert result["config"]["sandbox"] == sandbox
    assert result["previous"]["sandbox"] == sandbox
    assert live["security"]["fail_closed"] == {"enabled": True}
    assert live["permissions"]["composition"]["revision"] == revision
    failed = service.record_apply(revision, {"rail": "applied", "host_exit": "failed", "sandbox": "not_required"})
    assert failed["status"] == "failed" and not failed["applied"]
    applied = service.record_apply(revision, {"rail": "applied", "host_exit": "applied", "sandbox": "not_required"})
    assert applied["status"] == "applied" and applied["applied"]
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        service.record_apply("old", applied["targets"])


def test_composed_permanent_approval_stays_in_user_source(service):
    service.initialize_from_live()
    service.replace_cloud_policy(1, {"rules": [
        {"id": "cloud-rule", "tools": ["bash"], "pattern": "danger *", "action": "deny"},
    ]})
    grant = {"id": "remembered", "tools": ["bash"], "match_type": "command",
             "pattern": "safe *", "action": "allow"}
    assert service.persist_user_approval(grant)
    snapshot = service.get_candidate()
    assert snapshot["local"]["permissions"]["approval_overrides"][-1] == grant
    assert "rules" not in snapshot["local"]["permissions"] or all(
        rule["id"] != "cloud-rule" for rule in snapshot["local"]["permissions"]["rules"]
    )
    live = load_yaml_round_trip(service.config_path)
    assert live["permissions"]["approval_overrides"][-1] == grant
    assert snapshot["apply"]["applied"]
    service.update_settings({"policy_sources": [{"id": "cloud", "enabled": True}]})
    assert not service.persist_user_approval({**grant, "id": "disabled"})
    assert [item["id"] for item in service._read_yaml(service.local_path)["permissions"]["approval_overrides"]] == [
        "approved-1", "remembered",
    ]


@pytest.mark.parametrize("kind", ["compound", "single"])
def test_composed_shell_approval_batch_persists_together_and_rejects_invalid_batch(service, tmp_path, kind):
    from openjiuwen.harness.security.permission_engine.approve.operation_grants import (
        build_operation_grant, build_operation_grants,
    )

    service.initialize_from_live()
    args = {
        "command": "cd repo && git status" if kind == "compound" else "git status",
        "workdir": str(tmp_path),
    }
    grants = build_operation_grants(
        "bash", args, {},
        mode="allow_with_scope", scope="subcommands",
    )
    if kind == "single":
        grants.append(build_operation_grant("bash", args, {}))
    assert service.persist_user_approvals(grants)
    assert service.persist_user_approvals(grants)
    stored = load_yaml_round_trip(service.config_path)["permissions"]["approval_overrides"]
    assert stored[-2:] == grants
    assert len(stored) == 3  # Existing approval plus this complete, deduplicated batch.
    before = service.local_path.read_bytes()
    with pytest.raises(ValueError, match="requires an id"):
        service.persist_user_approvals([grants[0], {**grants[1], "id": ""}])
    assert service.local_path.read_bytes() == before


def test_legacy_edit_keeps_sandbox_retry_for_unchanged_network_axis(service):
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(service.config_path, live)
    service.initialize_from_live()
    pending = service.update_local("network", {"pending.example": "deny"})
    service.publish_candidate(expected_revision=pending["revision"])
    service.record_apply(
        pending["revision"], {"rail": "applied", "host_exit": "applied", "sandbox": "failed: unavailable"},
        sandbox_retry={"network": True},
    )

    _, edited = service.edit_native(
        lambda _settings, local: local["permissions"].setdefault("tools", {}).update({"bash": "ask"}),
    )
    assert edited["sandbox_retry"] == {"network": True}
    assert service.get_candidate()["apply"]["sandbox_retry"] == {"network": True}


def test_permanent_approval_does_not_mark_unfinished_sandbox_projection_applied(service):
    live = load_yaml_round_trip(service.config_path)
    live["sandbox"]["enabled"] = True
    dump_yaml_round_trip(service.config_path, live)
    service.initialize_from_live()
    pending = service.update_local("network", {"pending.example": "deny"})
    service.publish_candidate(expected_revision=pending["revision"])
    service.record_apply(
        pending["revision"], {"rail": "applied", "host_exit": "applied", "sandbox": "failed: unavailable"},
        sandbox_retry={"network": True},
    )

    grant = {"id": "remembered", "tools": ["bash"], "match_type": "command",
             "pattern": "safe *", "action": "allow"}
    assert service.persist_user_approval(grant)
    state = service.get_candidate()["apply"]
    assert not state["applied"]
    assert state["targets"]["sandbox"] == "pending"
    assert state["sandbox_retry"] == {"network": True}
    assert load_yaml_round_trip(service.config_path)["permissions"]["approval_overrides"][-1] == grant


def test_composed_file_projection_accepts_read_deny_implied_exec_on_windows(tmp_path):
    import sys
    from jiuwenswarm.common.file_guard_config import project_file_guard_to_sandbox

    if sys.platform != "win32":
        pytest.skip("Windows filesystem sandbox semantics")
    target = tmp_path / "private"
    target.mkdir()
    files, skipped = project_file_guard_to_sandbox({"paths": [
        {"path": str(target), "read": "deny", "layer": "user"},
    ]})
    assert not skipped
    assert files == [{"path": str(target), "read": "deny", "write": "deny"}]


def test_external_permission_write_invalidates_applied_state_without_overwrite(service):
    service.initialize_from_live()
    revision = service.publish_candidate()["apply"]["revision"]
    service.record_apply(revision, {"rail": "applied", "host_exit": "applied", "sandbox": "not_required"})
    live = load_yaml_round_trip(service.config_path)
    live["permissions"]["tools"]["external_tool"] = "deny"
    dump_yaml_round_trip(service.config_path, live)
    before = service.config_path.read_bytes()
    state = service.get_candidate()["apply"]
    assert state["revision"] != revision
    assert not state["applied"]
    assert service.config_path.read_bytes() == before
