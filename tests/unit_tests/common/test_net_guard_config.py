"""Network sync must skip ASK and preserve sandbox allow/deny semantics."""

import pytest

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
    render.set_sandbox_network_config(windows, [], ["old.example.com"])
    previous = render.get_sandbox_network_config()
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
        "allow_domains": ["allow.example.com", "*.allow.example.com"],
        "deny_domains": ["old.example.com", "*.old.example.com", "deny.example.com", "*.deny.example.com"],
    }
    assert sync_net_guard_to_sandbox()["urls"] == {}
    assert render.get_sandbox_network_config() == applied
    render_saved_sandbox_urls(policy_path)
    # Empty sync clears its own snapshot, never the independent panel source.
    assert render.get_sandbox_network_config()["deny_domains"] == ["old.example.com", "*.old.example.com"]
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
def test_empty_or_missing_urls_preserves_independent_panel_rules(config_file, monkeypatch, explicit_empty):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    render.set_sandbox_network_config(False, ["allowed.example.com"], ["blocked.example.com"])
    if explicit_empty:
        data = config.load_yaml_round_trip(config_file)
        data["sandbox"]["urls"] = {}
        config.dump_yaml_round_trip(config_file, data)
    render_saved_sandbox_urls(render._runtime_copy_path())
    assert render.get_sandbox_network_config() == {
        "disable_all": False, "allow_domains": ["allowed.example.com", "*.allowed.example.com"],
        "deny_domains": ["blocked.example.com", "*.blocked.example.com"],
    }


def test_explicit_empty_sync_preserves_rules_owned_by_panel(config_file, monkeypatch):
    monkeypatch.setattr(render, "_is_windows", lambda: True)
    render.set_sandbox_network_config(False, [], ["old.example.com"])
    previous = render.get_sandbox_network_config()
    data = config.load_yaml_round_trip(config_file)
    data["permissions"]["net_guard"]["urls"] = {}
    config.dump_yaml_round_trip(config_file, data)
    assert sync_net_guard_to_sandbox()["urls"] == {}
    assert config.load_yaml_round_trip(config_file)["sandbox"]["urls"] == {}
    assert render.get_sandbox_network_config() == previous
    render_saved_sandbox_urls(render._runtime_copy_path())
    assert render.get_sandbox_network_config()["deny_domains"] == ["old.example.com", "*.old.example.com"]


def test_runtime_updates_preserve_synced_urls(config_file):
    saved = sync_net_guard_to_sandbox()["urls"]
    config.update_sandbox_runtime({"enabled": True})
    assert config.load_yaml_round_trip(config_file)["sandbox"]["urls"] == saved
    with pytest.raises(ValueError, match="sandbox.network.sync"):
        config.update_sandbox_runtime({"urls": {}})


@pytest.mark.parametrize("urls", [None, [], {"example.com": "ask"}, {"https://example.com/path": "deny"}])
def test_invalid_saved_rules_are_rejected(urls):
    with pytest.raises(ValueError):
        validate_sandbox_urls(urls)
