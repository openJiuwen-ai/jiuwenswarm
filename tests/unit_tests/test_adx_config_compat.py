# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Historical workspace configuration remains portable across ADX nodes."""
import copy
import json

import pytest

from jiuwenswarm.common.adx_config import managed_adx_sandbox


@pytest.fixture
def deployment():
    return {"type": "adx-environment", "url": "https://new-node:8443", "enabled": True,
            "image": "tools:v1", "runtime": "conch", "user": "1000", "group": "1000",
            "cpu": 2000, "cpu_limit": 2000, "memory": 4096, "mem_limit": 4096}


def test_legacy_resources_mounts_and_identity_are_migrated_without_mutating_input(deployment):
    mount = {"source": "/shared/alice", "readonly": False}
    local = {"type": "yuanrong", "enabled": False, "url": "http://old:18093",
             "executor": "docker", "image": "old-docker-image", "cpu": 3000,
             "rootfs": {"workdir": "/workspace", "user": "1001:1002", "mounts": [mount]},
             "mounts": [mount]}
    original = copy.deepcopy(local)
    result = managed_adx_sandbox(local, deployment)
    assert local == original
    assert result["type"] == "adx-environment"
    assert result["url"] == deployment["url"]
    assert result["enabled"] is True
    assert result["image"] == deployment["image"]
    assert result["cpu"] == result["cpu_limit"] == 3000
    assert (result["user"], result["group"]) == ("1001", "1002")
    assert result["workdir"] == "/workspace"
    assert result["mounts"] == [{"host_path": "/shared/alice", "target": "/shared/alice", "options": ["rw"]}]
    assert not ({"rootfs", "executor", "startup_mode"} & result.keys())
    assert managed_adx_sandbox(result, deployment) == result


def test_native_customization_cannot_redirect_routing_or_disable_tls(deployment):
    local = {"type": "adx-environment", "image": "custom:v2", "runtime": "runsc", "cpu": 800,
             "connection": {"server_address": "old:8443", "gateway_address": "old:8443",
                            "token": "old-secret", "verify_tls": False, "use_tls": False,
                            "gateway_use_tls": False, "token_provider": None},
             "startup_mode": "internal", "env": {"APP": "value"}}
    result = managed_adx_sandbox(local, deployment)
    assert result["connection"] == {"verify_tls": True}
    assert (result["image"], result["runtime"], result["cpu_limit"]) == ("custom:v2", "runsc", 800)
    assert result["env"] == local["env"]
    assert result["url"] == deployment["url"]


@pytest.mark.parametrize("local", [
    {"policy_file": "/old/policy.yaml"}, {"files": {"deny": ["/secret"]}},
    {"excluded_commands": ["rm"]}, {"rootfs": {"workspace": {"id": "old"}}},
    {"mounts": [{"source": "sfs://bucket", "target": "/data"}]},
    {"mounts": [{"source": "/data", "readonly": "false"}]},
    {"user": "1001:1002", "group": "1003"},
])
def test_non_equivalent_legacy_options_need_explicit_migration(local, deployment):
    with pytest.raises(ValueError):
        managed_adx_sandbox(local, deployment)


def test_runtime_merge_is_scoped_and_tracks_current_deployment(monkeypatch, deployment):
    from jiuwenswarm.common import config

    original = {"models": [{"name": "keep"}], "sandbox": {"type": "yuanrong", "url": "http://old", "cpu": 900}}
    monkeypatch.setattr(config, "_read_with_retry", lambda path: copy.deepcopy(original))
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(deployment))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "")
    assert config.get_config()["sandbox"]["type"] == "yuanrong"
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", "https://worker:8443")
    first = config.get_config()
    assert first["models"] == original["models"]
    assert first["sandbox"]["type"] == "adx-environment"
    assert first["sandbox"]["cpu"] == 900
    assert first["sandbox"]["url"] == "https://worker:8443"
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", "https://other-node:8443")
    deployment["url"] = "https://other-node:8443"
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(deployment))
    assert config.get_sandbox_endpoint()["url"] == deployment["url"]
    assert config.get_sandbox_startup_mode() == "external"
    assert config.get_sandbox_startup_mode_explicit() == "external"


def test_invalid_migration_is_not_swallowed_as_malformed_json(monkeypatch, deployment):
    from jiuwenswarm.common import config

    monkeypatch.setattr(config, "_read_with_retry", lambda path: {"sandbox": {"policy_file": "/old/policy"}})
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(deployment))
    monkeypatch.setenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", "https://worker:8443")
    with pytest.raises(ValueError, match="policy_file"):
        config.get_config()


def test_managed_template_has_no_address_without_runtime_injection(deployment):
    deployment.pop("url")
    result = managed_adx_sandbox({"type": "adx-environment", "url": "https://old:8443"}, deployment)
    assert "url" not in result


def test_managed_runtime_rejects_missing_injection_instead_of_using_old_addresses(monkeypatch, deployment):
    from jiuwenswarm.common import config

    monkeypatch.setattr(config, "_read_with_retry", lambda path: {"sandbox": {"url": "https://stale:8443"}})
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_AGENTOS_SANDBOX_CONFIG", json.dumps(deployment))
    monkeypatch.delenv("JIUWENSWARM_ADX_NODE_INGRESS_URL", raising=False)
    with pytest.raises(ValueError, match="launcher-injected"):
        config.get_config()
