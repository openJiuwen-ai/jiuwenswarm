"""Network sync preserves default deny and domain exceptions without base bypasses."""

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.net_guard_config import (
    project_net_guard_to_sandbox,
    render_saved_sandbox_urls,
    sync_net_guard_to_sandbox,
    validate_sandbox_urls,
)
from jiuwenswarm.server import sandbox_policy_render as render


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "permissions:\n  enabled: true\n  net_guard:\n    enabled: true\n"
        "    urls:\n      allow.example.com: allow\n      deny.example.com: deny\n"
        "      ask.example.com: ask\nsandbox:\n  type: jiuwenbox\n  files: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    return path


def test_projection_skips_ask_and_urls_instead_of_widening_them():
    network, skipped = project_net_guard_to_sandbox(
        {
            "defaults": "ask",
            "urls": {
                "ask.example.com": "ask",
                "deny.example.com": "deny",
                "*.allow.example.com": "allow",
                "https://example.com/private": "deny",
            },
        }
    )
    assert network == {
        "default": "allow",
        "allow_domains": ["*.allow.example.com"],
        "deny_domains": ["deny.example.com"],
    }
    assert {item["pattern"] for item in skipped} == {
        "ask.example.com",
        "https://example.com/private",
        "defaults",
    }


@pytest.mark.parametrize("windows", [True, False])
def test_sync_roundtrip_and_removal(config_file, monkeypatch, windows):
    monkeypatch.setattr(render, "_is_windows", lambda: windows)
    previous = render.set_sandbox_network_config(windows, [], ["old.example.com"])
    policy_path = render._runtime_copy_path() if windows else render._linux_runtime_copy_path()
    result = sync_net_guard_to_sandbox()
    assert result["urls"] == {"allow.example.com": "allow", "deny.example.com": "deny"}
    assert config.load_yaml_round_trip(config_file)["sandbox"]["urls"] == result["urls"]
    assert config.get_sandbox_runtime()["urls"] == result["urls"]
    assert render.get_sandbox_network_config() == previous
    assert [s["pattern"] for s in result["skipped"]] == ["ask.example.com"]
    # Applying consumes the saved sandbox snapshot, not later Guard edits.
    stored = config.load_yaml_round_trip(config_file)
    stored["permissions"]["net_guard"]["urls"] = {}
    config.dump_yaml_round_trip(config_file, stored)
    render_saved_sandbox_urls(policy_path)
    applied = render.get_sandbox_network_config()
    assert applied == {
        "disable_all": windows,
        "allow_domains": ["allow.example.com"],
        "deny_domains": ["deny.example.com"],
    }
    assert sync_net_guard_to_sandbox()["urls"] == {}
    assert render.get_sandbox_network_config() == applied
    render_saved_sandbox_urls(policy_path)
    assert render.get_sandbox_network_config()["deny_domains"] == []
    assert render.get_sandbox_network_config()["allow_domains"] == []


def test_rpc_only_saves_network_until_explicit_restart(config_file, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import sandbox_config_rpc as rpc

    applied = []
    monkeypatch.setattr(rpc, "_trigger_apply", applied.append)
    request = AgentRequest(
        request_id="sync",
        channel_id="web",
        req_method=ReqMethod.SANDBOX_NETWORK_SYNC,
        params={},
    )
    result = rpc.dispatch_sandbox_config_request(request)
    assert result.ok
    assert applied == []
    assert result.payload["restart_required"] is True
    assert result.payload["urls"] == {"allow.example.com": "allow", "deny.example.com": "deny"}
    assert result.payload["skipped"][0]["pattern"] == "ask.example.com"
    request.params = {"unexpected": True}
    assert not rpc.dispatch_sandbox_config_request(request).ok
    assert applied == []


def test_failed_runtime_write_does_not_report_success(config_file, monkeypatch):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    sync_net_guard_to_sandbox()
    render.get_sandbox_network_config()
    monkeypatch.setattr(render, "_save_copy", lambda _: None)
    with pytest.raises(OSError):
        render_saved_sandbox_urls(render._runtime_copy_path())


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_empty_urls_applies_empty_domain_lists(config_file, monkeypatch, explicit_empty):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    render.set_sandbox_network_config(False, ["allowed.example.com"], ["blocked.example.com"])
    if explicit_empty:
        data = config.load_yaml_round_trip(config_file)
        data["sandbox"]["urls"] = {}
        config.dump_yaml_round_trip(config_file, data)
    render_saved_sandbox_urls(render._runtime_copy_path())
    assert render.get_sandbox_network_config() == {
        "disable_all": False, "allow_domains": [], "deny_domains": [],
    }


def test_explicit_empty_sync_clears_legacy_rules_only_when_applied(config_file, monkeypatch):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    previous = render.set_sandbox_network_config(False, [], ["old.example.com"])
    data = config.load_yaml_round_trip(config_file)
    data["permissions"]["net_guard"]["urls"] = {}
    config.dump_yaml_round_trip(config_file, data)
    assert sync_net_guard_to_sandbox()["urls"] == {}
    assert config.load_yaml_round_trip(config_file)["sandbox"]["urls"] == {}
    assert render.get_sandbox_network_config() == previous
    render_saved_sandbox_urls(render._runtime_copy_path())
    assert render.get_sandbox_network_config()["deny_domains"] == []


def test_runtime_updates_preserve_synced_urls(config_file):
    saved = sync_net_guard_to_sandbox()["urls"]
    config.update_sandbox_runtime({"enabled": True})
    assert config.load_yaml_round_trip(config_file)["sandbox"]["urls"] == saved
    assert config.get_sandbox_runtime()["network_default"] == "allow"
    assert config.load_yaml_round_trip(config_file)["sandbox"]["network_default"] == "allow"
    with pytest.raises(ValueError, match="sandbox.network.sync"):
        config.update_sandbox_runtime({"urls": {}})
    with pytest.raises(ValueError, match="sandbox.network.sync"):
        config.update_sandbox_runtime({"network_default": "deny"})


@pytest.mark.parametrize("urls", [None, [], {"example.com": "ask"}, {"https://example.com/path": "deny"}])
def test_invalid_saved_rules_are_rejected(urls):
    with pytest.raises(ValueError):
        validate_sandbox_urls(urls)


@pytest.mark.parametrize("default", ["allow", "deny"])
def test_supported_default_is_synchronized_without_a_skipped_receipt(default):
    network, skipped = project_net_guard_to_sandbox({"defaults": default, "urls": {}})
    assert network["default"] == default
    assert skipped == []


@pytest.mark.parametrize("windows", [True, False])
@pytest.mark.parametrize("domains", [{}, {
    "allowed.example.com": "allow", "denied.example.com": "deny", "ip.me": "allow",
}])
def test_default_deny_survives_base_merge_and_allows_only_domain_exceptions(
    config_file, monkeypatch, windows, domains,
):
    from jiuwenbox.models import policy as policy_module
    from jiuwenbox.models.policy import SecurityPolicy
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.supervisor import win_proxy

    monkeypatch.setattr(render, "_is_windows", lambda: windows)
    # DNS is deterministic; this regression opens no sockets.
    monkeypatch.setattr(win_proxy.socket, "getaddrinfo", lambda *_args: [(0, 0, 0, "", ("203.0.113.10", 0))])
    data = config.load_yaml_round_trip(config_file)
    data["permissions"]["net_guard"].update(defaults="deny", urls=domains)
    config.dump_yaml_round_trip(config_file, data)
    synced = sync_net_guard_to_sandbox()
    assert synced["default"] == "deny"
    assert synced["skipped"] == []
    policy_path = render._runtime_copy_path() if windows else render._linux_runtime_copy_path()
    assert not policy_path.exists()  # Sync only saves the snapshot.

    # Applying must consume the snapshot even if NetGuard changes afterwards.
    data = config.load_yaml_round_trip(config_file)
    data["permissions"]["net_guard"].update(defaults="allow", urls={})
    config.dump_yaml_round_trip(config_file, data)
    render_saved_sandbox_urls(policy_path)
    override = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    base_path = Path(policy_module.__file__).resolve().parents[1] / "configs" / "windows-policy.yaml"
    base = SecurityPolicy.model_validate(yaml.safe_load(base_path.read_text(encoding="utf-8")))
    if not windows:
        # Exercise the same allowlist merge contract for the Linux projection.
        base = base.model_copy(update={"network": base.network.model_copy(update={"egress": base.windows.network.egress})})
    merged = PolicyEngine().merge_policy(base, override)
    egress = merged.windows.network.egress if windows else merged.network.egress
    assert egress.default == "deny"
    assert egress.allowed_domains == [domain for domain, action in domains.items() if action == "allow"]
    assert egress.allowed_ports == []
    assert egress.allowed_ips == []
    assert egress.blocked_ports == base.windows.network.egress.blocked_ports
    gate = win_proxy.EgressFilter(egress)
    # ip.me stays blocked: it is on the base blocklist, which outranks an allow projection.
    for host in ("203.0.113.10", "127.0.0.1", "pypi.org", "other.example.com", "denied.example.com", "ip.me"):
        for port in (80, 443, 8443):
            assert not gate.allow(host, port)[0], (host, port)
    if domains:
        for port in (80, 443, 8443):
            assert gate.allow("allowed.example.com", port)[0]

    # Switching back restores base allowances; repeated rendering is idempotent.
    assert sync_net_guard_to_sandbox()["default"] == "allow"
    render_saved_sandbox_urls(policy_path)
    render_saved_sandbox_urls(policy_path)
    restored = PolicyEngine().merge_policy(base, yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    restored_egress = restored.windows.network.egress if windows else restored.network.egress
    assert restored_egress.default == "allow"
    assert restored_egress.allowed_ports == base.windows.network.egress.allowed_ports
    assert restored_egress.allowed_ips == base.windows.network.egress.allowed_ips
    assert win_proxy.EgressFilter(restored_egress).allow("203.0.113.10", 443)[0]


def test_default_deny_rpc_receipt_preserves_only_unsupported_skips(config_file):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.sandbox_config_rpc import dispatch_sandbox_config_request

    data = config.load_yaml_round_trip(config_file)
    data["permissions"]["net_guard"].update(defaults="deny", urls={
        "allowed.example.com": "allow", "review.example.com": "ask",
        "https://example.com/private": "deny",
    })
    config.dump_yaml_round_trip(config_file, data)
    result = dispatch_sandbox_config_request(AgentRequest(
        request_id="deny-sync", channel_id="web", req_method=ReqMethod.SANDBOX_NETWORK_SYNC, params={},
    ))
    assert result.ok
    assert result.payload["default"] == "deny"
    assert result.payload["urls"] == {"allowed.example.com": "allow"}
    assert {item["pattern"] for item in result.payload["skipped"]} == {
        "review.example.com", "https://example.com/private",
    }


def test_default_only_runtime_write_failure_is_not_reported_as_success(config_file, monkeypatch):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    render.set_sandbox_network_config(False, [], [])
    data = config.load_yaml_round_trip(config_file)
    data["permissions"]["net_guard"].update(defaults="deny", urls={})
    config.dump_yaml_round_trip(config_file, data)
    sync_net_guard_to_sandbox()
    monkeypatch.setattr(render, "_save_copy", lambda _data: None)
    with pytest.raises(OSError, match="could not be saved"):
        render_saved_sandbox_urls(render._runtime_copy_path())


def test_legacy_snapshot_preserves_existing_default_and_allowances(config_file, monkeypatch):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    policy_path = render._runtime_copy_path()
    render.set_sandbox_network_config(False, [], [])
    data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    data["windows"]["network"]["egress"].update(default="deny", allowed_ports=[8443])
    policy_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    render_saved_sandbox_urls(policy_path)
    applied = yaml.safe_load(policy_path.read_text(encoding="utf-8"))["windows"]["network"]["egress"]
    assert applied["default"] == "deny"
    assert applied["allowed_ports"] == [8443]
    assert "replace_allowed" not in applied
