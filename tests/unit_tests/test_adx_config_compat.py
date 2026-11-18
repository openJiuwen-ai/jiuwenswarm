"""Managed runtime reads prepared settings and refreshes node connectivity."""
import copy
import json

import pytest


def test_runtime_merge_is_scoped_and_tracks_current_deployment(monkeypatch, adx_workspace_binding):
    from jiuwenswarm.common import config

    original = {"models": [{"name": "keep"}], "sandbox": {"type": "adx-environment", "runtime": "conch", "startup_mode": "external", "url": "http://old", "cpu": 900}}
    monkeypatch.setattr(config, "_read_with_retry", lambda path: copy.deepcopy(original))
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(adx_workspace_binding))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "")
    assert config.get_config()["sandbox"]["url"] == "http://old"
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", "https://worker:8443")
    first = config.get_config()
    assert first["models"] == original["models"]
    assert first["sandbox"]["type"] == "adx-environment"
    assert first["sandbox"]["cpu"] == 900
    assert "policy_file" not in first["sandbox"]
    assert first["sandbox"]["url"] == "https://worker:8443"
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", "https://other-node:8443")
    adx_workspace_binding["url"] = "https://other-node:8443"
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(adx_workspace_binding))
    assert config.get_sandbox_endpoint()["url"] == adx_workspace_binding["url"]
    assert config.get_sandbox_startup_mode() == "external"
    assert config.get_sandbox_startup_mode_explicit() == "external"


def test_managed_runtime_rejects_missing_injection_instead_of_using_old_addresses(monkeypatch, adx_workspace_binding):
    from jiuwenswarm.common import config

    monkeypatch.setattr(config, "_read_with_retry", lambda path: {"sandbox": {"type": "adx-environment", "url": "https://stale:8443"}})
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(adx_workspace_binding))
    monkeypatch.delenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", raising=False)
    with pytest.raises(ValueError, match="launcher-injected"):
        config.get_config()


def test_node_context_file_wins_over_old_launcher_and_workspace_urls(tmp_path, monkeypatch, adx_workspace_binding):
    from jiuwenswarm.common import config
    context = tmp_path / "node.json"
    context.write_text(json.dumps({"ingress_url": "http://10.89.4.1:8080"}))
    monkeypatch.setattr(config, "_read_with_retry", lambda path: {"sandbox": {"type": "adx-environment", "url": "https://stale:8443"}})
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(adx_workspace_binding))
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_CONTEXT_FILE", str(context))
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", "https://stale:8443")
    assert config.get_config()["sandbox"]["url"] == "http://10.89.4.1:8080"
    assert config.get_config()["sandbox"]["endpoint_file"] == str(context)
    context.write_text("{}")
    with pytest.raises(ValueError):
        config.get_config()


@pytest.mark.parametrize('backend', ['jiuwenbox', 'adx-environment', 'jiuwenbox-conch'])
@pytest.mark.parametrize('mode', [None, 'internal', 'external'])
def test_startup_mode_reads_prepared_value_without_backend_overrides(monkeypatch, backend, mode):
    from jiuwenswarm.common import config
    sandbox = {'type': backend}
    if mode is not None:
        sandbox['startup_mode'] = mode
    monkeypatch.setattr(config, 'get_config', lambda: {'sandbox': sandbox})
    assert config.get_sandbox_startup_mode() == (mode or 'internal')
    assert config.get_sandbox_startup_mode_explicit() == mode
    assert config.get_sandbox_endpoint()['startup_mode'] == (mode or 'internal')



@pytest.mark.parametrize('mode', [0o600, 0o640])
def test_agentos_config_updates_keep_the_prepared_mode(tmp_path, monkeypatch, mode):
    from jiuwenswarm.common.config import dump_yaml_round_trip

    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    path = tmp_path / 'config.yaml'
    path.write_text('models: {}\n')
    path.chmod(mode)
    dump_yaml_round_trip(path, {'models': {'defaults': []}})
    assert path.stat().st_mode & 0o777 == mode
