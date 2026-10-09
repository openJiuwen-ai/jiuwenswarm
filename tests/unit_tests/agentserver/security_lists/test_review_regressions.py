# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Integration regressions found during the 2026-10-08 security review."""
import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.agents.harness.common.rails.security_lists import api, audit, matcher, store
from jiuwenswarm.agents.harness.common.rails.security_lists.models import SecurityListRecord
from jiuwenswarm.server import sandbox_policy_render as spr, security_lists_render as render


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("permissions:\n  enabled: true\n  permission_mode: normal\n  net_guard:\n    enabled: true\n    urls: {}\nsandbox:\n  type: jiuwenbox\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(spr, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(audit, "_audit_file", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit, "log_event", lambda *a, **kw: True)
    from jiuwenswarm.server import security_lists_rpc
    monkeypatch.setattr(security_lists_rpc, "_publish_enforcement", lambda: "published")
    return path


def domain(pattern, action="deny"):
    return SecurityListRecord(type="domain", pattern=pattern, match="exact", cells={"*": {"*": action}})


@pytest.mark.parametrize("command", ["echo ok && curl https://x.example", "curl https://x.example", '"C:/tools/curl.exe" https://x.example'])
def test_command_api_checks_all_executables(env, command):
    store.upsert_record(SecurityListRecord(type="command", pattern="curl", match="exact", cells={"*": {"*": "deny"}}))
    assert api.check_command("", command, mode="auto_approve").action == "deny"


def test_target_extraction_error_must_propagate(monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("extractor unavailable")
    monkeypatch.setattr(matcher, "extract_accesses_native", broken)
    with pytest.raises(RuntimeError, match="extractor unavailable"):
        matcher.extract_targets("read_file", {"file_path": "secret.txt"})


def test_shell_parser_error_must_propagate(monkeypatch):
    monkeypatch.setattr(matcher, "extract_accesses_native", lambda *a: [])
    def broken(*a, **kw):
        raise RuntimeError("parser unavailable")
    monkeypatch.setattr(matcher, "parse_shell_for_permission", broken)
    with pytest.raises(RuntimeError, match="parser unavailable"):
        matcher.extract_targets("bash", {"command": "echo ok && curl https://x.example"})


def test_glob_checks_canonical_path(env, tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    pattern = protected.as_posix() + "/**"
    record = SecurityListRecord(type="file_path", pattern=pattern, match="glob", cells={"*": {"read": "deny"}})
    assert matcher.match_record(record, str(tmp_path / "public" / ".." / "protected" / "secret.txt"))


@pytest.mark.parametrize("windows", [True, False])
def test_restart_keeps_unified_and_panel_rules(env, monkeypatch, windows):
    from jiuwenswarm.common.net_guard_config import render_saved_sandbox_urls
    monkeypatch.setattr(spr, "_is_windows", lambda: windows)
    store.upsert_record(domain("center.example"))
    spr.set_sandbox_network_config(False, [], ["panel.example"])
    config.update_config(lambda data: {**data, "sandbox": {"type": "jiuwenbox", "urls": {"snapshot.example": "deny"}}})
    policy_path = spr._runtime_copy_path() if windows else spr._linux_runtime_copy_path()
    render_saved_sandbox_urls(policy_path)
    assert set(spr.get_sandbox_network_config()["deny_domains"]) == {"center.example", "*.center.example", "panel.example", "*.panel.example", "snapshot.example", "*.snapshot.example"}


def test_absent_saved_urls_does_not_clear_panel(env):
    from jiuwenswarm.common.net_guard_config import render_saved_sandbox_urls
    spr.set_sandbox_network_config(False, [], ["panel.example"])
    render_saved_sandbox_urls(spr._runtime_copy_path())
    assert spr.get_sandbox_network_config()["deny_domains"] == ["panel.example", "*.panel.example"]


@pytest.mark.parametrize("linux", [False, True])
def test_policy_write_failure_is_visible(env, monkeypatch, linux):
    store.upsert_record(domain("blocked.example"))
    save = spr._save_linux_copy if linux else spr._save_copy
    if linux:
        spr.ensure_linux_copy_exists()
    else:
        spr._ensure_copy_exists()
    monkeypatch.setattr(spr.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        save(spr._linux_empty_skeleton() if linux else spr._empty_skeleton())


def test_panel_deny_wins_duplicate_allow(env):
    spr.set_sandbox_network_config(False, ["both.example"], ["both.example"])
    assert api.check_domain("both.example").action == "deny"
    assert spr.get_sandbox_network_config()["deny_domains"] == ["both.example", "*.both.example"]


def test_panel_collision_is_reported(env):
    store.upsert_record(domain("owned.example", "allow"))
    result = spr.set_sandbox_network_config(False, [], ["owned.example"])
    assert result["skipped"] == ["owned.example"]


def test_cloud_defaults_do_not_override_user_defaults(env):
    store.set_defaults({"*": {"domain": "deny"}})
    store.cloud_sync(sync_version="v1", synced_at="", records=[], defaults={"*": {"domain": "allow"}})
    assert api.check_domain("unlisted.example").action == "deny"


@pytest.mark.parametrize("section", [
    {"user": {}}, {"cloud": []}, {"defaults": []},
    {"cloud": {"records": {}}}, {"cloud": {"defaults": []}},
])
def test_falsy_malformed_config_fails_closed(env, section):
    from jiuwenswarm.agents.harness.common.rails.security_lists.models import SecurityListsCorruptedError
    config.update_config(lambda data: {**data, "security_lists": section})
    with pytest.raises(SecurityListsCorruptedError):
        store.get_security_lists()


def test_user_generic_default_beats_cloud_mode_specific(env):
    store.set_defaults({"*": {"domain": "deny"}})
    store.cloud_sync(sync_version="v1", synced_at="", records=[],
                     defaults={"auto_approve": {"domain": "allow"}})
    assert api.check_domain("unlisted.example", mode="auto_approve").action == "deny"
    data = config.get_config()["security_lists"]
    assert data["defaults"] == {"*": {"domain": "deny"}}
    assert data["cloud"]["defaults"] == {"auto_approve": {"domain": "allow"}}
    store.cloud_sync(sync_version="v2", synced_at="", records=[], defaults={})
    assert api.check_domain("unlisted.example", mode="auto_approve").action == "deny"


@pytest.mark.parametrize("target", ["blocked.example.", "https://%62locked.example/x", "sub.blocked.example"])
def test_domain_normalization_cannot_bypass_deny(env, target):
    store.upsert_record(domain("blocked.example"))
    assert api.check_domain(target).action == "deny"


def test_idna_projection_matches_runtime_proxy(env):
    from jiuwenbox.supervisor.win_proxy import EgressFilter
    store.upsert_record(domain("例子.测试"))
    projected = render.collect_sandbox_lists()["blocked_domains"]
    assert projected == ["xn--fsqu00a.xn--0zwm56d", "*.xn--fsqu00a.xn--0zwm56d"]
    assert EgressFilter._domain_matches(projected[0], "xn--fsqu00a.xn--0zwm56d.")
    assert EgressFilter._domain_matches(projected[0], "例子.测试")
    assert EgressFilter._domain_matches(projected[1], "sub.xn--fsqu00a.xn--0zwm56d")
    assert not EgressFilter._domain_matches(projected[1], "xn--fsqu00a.xn--0zwm56d")
    render.render_sandbox_copy()
    assert spr.get_sandbox_network_config()["deny_domains"] == projected
    from jiuwenswarm.agents.harness.common.rails.security_lists.bridge import merge_domain_rules_into_net_guard
    merged = merge_domain_rules_into_net_guard({"net_guard": {"enabled": True, "urls": {}}})
    assert merged["net_guard"]["urls"][projected[0]] == "deny"
    assert merged["net_guard"]["urls"]["例子.测试"] == "deny"


@pytest.mark.parametrize("pattern", ["localhost", "127.0.0.1"])
def test_valid_host_rule_is_not_silently_removed_from_runtime(env, pattern):
    store.upsert_record(domain(pattern))
    render.render_sandbox_copy()
    assert pattern in spr.get_sandbox_network_config()["deny_domains"]


def test_legacy_panel_invalid_deny_does_not_erase_existing_rule(env):
    spr.set_sandbox_network_config(False, [], ["blocked.example"])
    before = env.read_bytes()
    with pytest.raises(ValueError):
        spr.set_sandbox_network_config(False, [], ["http://invalid.example/path"])
    assert env.read_bytes() == before


def test_restart_does_not_resurrect_deleted_snapshot_record(env):
    from jiuwenswarm.common.net_guard_config import render_saved_sandbox_urls
    config.update_config(lambda data: {**data, "sandbox": {"type": "jiuwenbox", "urls": {"snapshot.example": "deny"}}})
    render_saved_sandbox_urls(spr._runtime_copy_path())
    record = next(r for r in store.get_security_lists()["user"] if r.pattern == "snapshot.example")
    store.delete_record(record.id)
    render_saved_sandbox_urls(spr._runtime_copy_path())
    assert not store.get_security_lists()["user"]
    config.update_config(lambda data: {**data, "sandbox": {**data["sandbox"], "urls_revision": "new-sync"}})
    render_saved_sandbox_urls(spr._runtime_copy_path())
    assert api.check_domain("snapshot.example").action == "deny"


def test_snapshot_collision_rejects_entire_transaction(env):
    from jiuwenswarm.common.net_guard_config import render_saved_sandbox_urls
    from jiuwenswarm.agents.harness.common.rails.security_lists.models import DuplicateRecordError
    store.upsert_record(domain("owned.example", "allow"))
    config.update_config(lambda data: {**data, "sandbox": {"type": "jiuwenbox", "urls": {"new.example": "deny", "owned.example": "deny"}}})
    before = env.read_bytes()
    with pytest.raises(DuplicateRecordError):
        render_saved_sandbox_urls(spr._runtime_copy_path())
    assert env.read_bytes() == before


def test_linux_copy_migration_is_independent_and_idempotent(env):
    path = spr._linux_runtime_copy_path()
    path.write_text(yaml.safe_dump({"network": {"egress": {"blocked_domains": ["legacy.example"]}}}), encoding="utf-8")
    assert store.migrate_sandbox_copy_once(path) == 1
    assert store.migrate_sandbox_copy_once(path) == 0
    assert api.check_domain("legacy.example").action == "deny"


def test_cloud_web_channel_rejected_without_writes(env):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.security_lists_rpc import dispatch_security_lists_request
    before = env.read_bytes()
    response = dispatch_security_lists_request(AgentRequest(
        request_id="review", channel_id="web", req_method=ReqMethod.SECURITY_LISTS_CLOUD_SYNC,
        params={"sync_version": "v1", "records": []}))
    assert not response.ok
    assert response.payload["code"] == "FORBIDDEN"
    assert env.read_bytes() == before


def test_defaults_rpc_separates_editable_user_from_cloud_view(env):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.security_lists_rpc import dispatch_security_lists_request
    store.cloud_sync(sync_version="v1", synced_at="", records=[], defaults={"*": {"domain": "deny"}})
    response = dispatch_security_lists_request(AgentRequest(
        request_id="review", channel_id="web", req_method=ReqMethod.SECURITY_LISTS_DEFAULTS_GET))
    assert response.payload == {"defaults": {"*": {"domain": "deny"}},
                                "user_defaults": {}, "cloud_defaults": {"*": {"domain": "deny"}}}


def test_rpc_reports_saved_but_sync_failed(env, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import security_lists_rpc as rpc
    monkeypatch.setattr(rpc, "_trigger_sandbox_sync", lambda: "failed")
    monkeypatch.setattr(rpc, "_publish_enforcement", lambda: "published")
    response = rpc.dispatch_security_lists_request(AgentRequest(
        request_id="review", channel_id="web", req_method=ReqMethod.SECURITY_LISTS_UPSERT,
        params={"record": {"type": "domain", "pattern": "blocked.example", "match": "exact", "cells": {"*": {"*": "deny"}}}}))
    assert response.ok and response.payload["saved"]
    assert response.payload["sync"] == {"sandbox": "failed", "host_exit": "published"}
    assert api.check_domain("blocked.example").action == "deny"


@pytest.mark.parametrize("mode", ["", "bogus", "normal"])
def test_invalid_explicit_mode_does_not_skip_deny(env, mode):
    store.upsert_record(domain("blocked.example"))
    with pytest.raises(ValueError, match="未知模式"):
        api.check_domain("blocked.example", mode=mode)


def test_template_upgrade_preserves_saved_security_configuration(tmp_path):
    from jiuwenswarm.common.config_split import sync_system_files_from_package
    package = tmp_path / "package.yaml"
    package.write_text("sandbox:\n  type: jiuwenbox\n", encoding="utf-8")
    user = tmp_path / "config.yaml"
    saved = {
        "sandbox": {"type": "jiuwenbox", "urls": {"blocked.example": "deny"},
                    "urls_revision": "v1", "files": [{"path": "C:/secret", "read": "deny"}]},
        "security_lists": {"cloud": {"sync_version": "v1", "records": [], "defaults": {"*": {"domain": "deny"}}},
                           "migrations": {"sandbox_urls_stamp": "stamp", "sandbox_copy_linux": "time"}},
    }
    user.write_text(yaml.safe_dump(saved), encoding="utf-8")
    assert sync_system_files_from_package(user_yaml=user, overlay_yaml=tmp_path / "overlay.yaml", package_yaml=package)
    restored = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert restored["sandbox"] == saved["sandbox"]
    assert restored["security_lists"] == saved["security_lists"]
