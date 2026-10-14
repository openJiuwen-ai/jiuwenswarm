# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for deep policy merge and update_policy.yaml store."""

from __future__ import annotations

import pytest
import yaml

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.policy_engine import PolicyEngine
from jiuwenbox.server.policy_update_store import PolicyUpdateStore


def _base_policy(**overrides) -> SecurityPolicy:
    data = {
        "name": "base",
        "environment": {"FOO": "1"},
        "filesystem_policy": {
            "read_only": ["/usr"],
            "read_write": ["/tmp"],
        },
        "process": {
            "run_as_user": "sandbox",
            "run_as_group": "sandbox",
        },
        "namespace": {
            "user": True,
            "pid": True,
        },
        "capabilities": {
            "add": ["CAP_NET_BIND_SERVICE"],
            "drop": ["CAP_SYS_ADMIN"],
        },
        "landlock": {"compatibility": "best_effort"},
        "syscall": {
            "x86_64": {"blocked": ["ptrace"]},
            "arm64": {"blocked": ["ptrace"]},
        },
        "network": {
            "mode": "isolated",
            "egress": {
                "default": "allow",
                "blocked_ips": ["10.0.0.1/32"],
                "allowed_domains": ["example.com"],
            },
            "ingress": {"default": "allow"},
        },
        "cgroup": {
            "memory_max": 256 * 1024 * 1024,
            "pids_max": 128,
        },
        "timeout": {
            "idle_timeout": 1800,
            "idle_check_interval": 60,
        },
        "conch": {
            "template_name": "default",
            "vcpu_num": 1,
            "network": {
                "egress": {
                    "default": "allow",
                    "blocked_ips": ["192.0.2.1"],
                },
            },
        },
    }
    data.update(overrides)
    return SecurityPolicy.model_validate(data)


def test_merge_append_dedupes_lists():
    engine = PolicyEngine()
    base = _base_policy()
    merged = engine.merge_policy(
        base,
        {
            "network": {
                "egress": {
                    "blocked_ips": ["10.0.0.1/32", "10.0.0.2/32"],
                },
            },
        },
        mode="append",
    )
    assert merged.network.egress.blocked_ips == [
        "10.0.0.1/32",
        "10.0.0.2/32",
    ]
    assert merged.network.egress.default == "allow"


def test_merge_override_replaces_lists_preserves_absent_leaves():
    engine = PolicyEngine()
    base = _base_policy()
    merged = engine.merge_policy(
        base,
        {
            "network": {
                "egress": {
                    "blocked_ips": ["203.0.113.1/32"],
                },
            },
        },
        mode="override",
    )
    assert merged.network.egress.blocked_ips == ["203.0.113.1/32"]
    assert merged.network.egress.allowed_domains == ["example.com"]
    assert merged.network.egress.default == "allow"
    assert merged.name == "base"


def test_merge_append_filesystem_process_capabilities_syscall():
    engine = PolicyEngine()
    base = _base_policy()
    merged = engine.merge_policy(
        base,
        {
            "filesystem_policy": {
                "read_only": ["/usr", "/lib"],
                "read_write": ["/home/work"],
            },
            "process": {"run_as_user": "nobody"},
            "capabilities": {
                "add": ["CAP_NET_BIND_SERVICE", "CAP_CHOWN"],
                "drop": ["CAP_SYS_PTRACE"],
            },
            "syscall": {
                "x86_64": {"blocked": ["ptrace", "reboot"]},
            },
        },
        mode="append",
    )
    assert merged.filesystem_policy.read_only == ["/usr", "/lib"]
    assert merged.filesystem_policy.read_write == ["/tmp", "/home/work"]
    assert merged.process.run_as_user == "nobody"
    assert merged.process.run_as_group == "sandbox"
    assert merged.capabilities.add == [
        "CAP_NET_BIND_SERVICE",
        "CAP_CHOWN",
    ]
    assert merged.capabilities.drop == [
        "CAP_SYS_ADMIN",
        "CAP_SYS_PTRACE",
    ]
    assert merged.syscall.x86_64.blocked == ["ptrace", "reboot"]
    assert merged.syscall.arm64.blocked == ["ptrace"]


def test_merge_override_filesystem_cgroup_timeout_namespace_landlock():
    engine = PolicyEngine()
    base = _base_policy()
    merged = engine.merge_policy(
        base,
        {
            "filesystem_policy": {
                "read_write": ["/var/tmp"],
            },
            "namespace": {"pid": False},
            "landlock": {"compatibility": "hard_requirement"},
            "cgroup": {
                "memory_max": 512 * 1024 * 1024,
            },
            "timeout": {
                "idle_timeout": 120,
            },
            "environment": {"BAR": "2"},
        },
        mode="override",
    )
    assert merged.filesystem_policy.read_write == ["/var/tmp"]
    assert merged.filesystem_policy.read_only == ["/usr"]
    assert merged.namespace.pid is False
    assert merged.namespace.user is True
    assert merged.landlock.compatibility == "hard_requirement"
    assert merged.cgroup.memory_max == 512 * 1024 * 1024
    assert merged.cgroup.pids_max == 128
    assert merged.timeout.idle_timeout == 120.0
    assert merged.timeout.idle_check_interval == 60.0
    assert merged.environment == {"FOO": "1", "BAR": "2"}


def test_merge_append_conch_network_and_scalars():
    engine = PolicyEngine()
    base = _base_policy()
    merged = engine.merge_policy(
        base,
        {
            "conch": {
                "vcpu_num": 2,
                "ram_mb": 1024,
                "network": {
                    "egress": {
                        "blocked_ips": ["192.0.2.1", "198.51.100.1"],
                    },
                },
            },
        },
        mode="append",
    )
    assert merged.conch.template_name == "default"
    assert merged.conch.vcpu_num == 2
    assert merged.conch.ram_mb == 1024
    assert merged.conch.network.egress.blocked_ips == [
        "192.0.2.1",
        "198.51.100.1",
    ]
    assert merged.conch.network.egress.default == "allow"


def test_merge_override_conch_list_replace_preserves_other_fields():
    engine = PolicyEngine()
    base = _base_policy()
    merged = engine.merge_policy(
        base,
        {
            "conch": {
                "network": {
                    "egress": {
                        "blocked_ips": ["203.0.113.9"],
                    },
                },
            },
        },
        mode="override",
    )
    assert merged.conch.network.egress.blocked_ips == ["203.0.113.9"]
    assert merged.conch.network.egress.default == "allow"
    assert merged.conch.template_name == "default"
    assert merged.conch.vcpu_num == 1


def test_update_store_save_overwrites_single_document(tmp_path):
    path = tmp_path / "update_policy.yaml"
    store = PolicyUpdateStore(path=path)
    engine = PolicyEngine()
    base = _base_policy()

    first = engine.merge_policy(
        base,
        {"network": {"egress": {"blocked_ips": ["198.51.100.1/32"]}}},
        mode="append",
    )
    store.save(first)
    second = engine.merge_policy(
        first,
        {"network": {"egress": {"blocked_ips": ["198.51.100.2/32"]}}},
        mode="override",
    )
    store.save(second)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "updates" not in raw
    assert raw["network"]["egress"]["blocked_ips"] == ["198.51.100.2/32"]
    assert raw["network"]["egress"]["allowed_domains"] == ["example.com"]

    loaded = store.load()
    assert loaded is not None
    assert loaded.network.egress.blocked_ips == ["198.51.100.2/32"]
    assert loaded.network.egress.default == "allow"


def test_update_store_save_keeps_merged_non_network_fields(tmp_path):
    path = tmp_path / "update_policy.yaml"
    store = PolicyUpdateStore(path=path)
    engine = PolicyEngine()
    base = _base_policy()

    merged = engine.merge_policy(
        base,
        {
            "filesystem_policy": {"read_write": ["/workspace"]},
            "capabilities": {"drop": ["CAP_SYS_PTRACE"]},
            "timeout": {"idle_timeout": 900},
        },
        mode="append",
    )
    merged = engine.merge_policy(
        merged,
        {
            "process": {"run_as_user": "nobody"},
            "cgroup": {"pids_max": 64},
            "conch": {
                "network": {
                    "egress": {"blocked_ips": ["203.0.113.7"]},
                },
            },
        },
        mode="override",
    )
    store.save(merged)

    loaded = store.resolve(_base_policy(name="unused-base"))
    assert loaded.filesystem_policy.read_write == ["/tmp", "/workspace"]
    assert loaded.capabilities.drop == ["CAP_SYS_ADMIN", "CAP_SYS_PTRACE"]
    assert loaded.timeout.idle_timeout == 900.0
    assert loaded.timeout.idle_check_interval == 60.0
    assert loaded.process.run_as_user == "nobody"
    assert loaded.process.run_as_group == "sandbox"
    assert loaded.cgroup.pids_max == 64
    assert loaded.cgroup.memory_max == 256 * 1024 * 1024
    assert loaded.conch.network.egress.blocked_ips == ["203.0.113.7"]
    assert loaded.conch.template_name == "default"


def test_update_store_missing_file_resolves_to_base(tmp_path):
    store = PolicyUpdateStore(path=tmp_path / "update_policy.yaml")
    base = _base_policy()
    assert store.load() is None
    assert store.resolve(base) is base


def test_update_store_legacy_updates_list_raises(tmp_path):
    path = tmp_path / "update_policy.yaml"
    path.write_text("updates: not-a-list\n", encoding="utf-8")
    store = PolicyUpdateStore(path=path)
    with pytest.raises(ValueError, match="Legacy multi-entry"):
        store.load()


def test_sandbox_manager_loads_merged_update_policy_on_init(tmp_path):
    from jiuwenbox.server.policy_reader import PolicyReader
    from jiuwenbox.server.sandbox_manager import SandboxManager

    base_yaml = tmp_path / "base.yaml"
    base_yaml.write_text(
        "name: base\n"
        "filesystem_policy:\n"
        "  read_only: [/usr]\n"
        "  read_write: [/tmp]\n"
        "process:\n"
        "  run_as_user: sandbox\n"
        "  run_as_group: sandbox\n"
        "network:\n"
        "  mode: host\n"
        "  egress:\n"
        "    default: allow\n"
        "    blocked_ips: []\n"
        "cgroup:\n"
        "  memory_max: 268435456\n"
        "timeout:\n"
        "  idle_timeout: 1800\n"
        "  idle_check_interval: 60\n",
        encoding="utf-8",
    )
    update_path = tmp_path / "update_policy.yaml"
    engine = PolicyEngine()
    base = PolicyReader(policy_path=base_yaml).load_policy()
    merged = engine.merge_policy(
        base,
        {
            "network": {"egress": {"blocked_ips": ["203.0.113.50/32"]}},
            "filesystem_policy": {"read_write": ["/workspace"]},
            "timeout": {"idle_timeout": 42},
            "cgroup": {"pids_max": 32},
        },
        mode="append",
    )
    PolicyUpdateStore(path=update_path).save(merged)

    mgr = SandboxManager(
        policy_reader=PolicyReader(policy_path=base_yaml),
        update_policy_path=update_path,
        state_dir=tmp_path / "sandboxes",
    )
    assert "203.0.113.50/32" in mgr.policy.network.egress.blocked_ips
    assert mgr.policy.filesystem_policy.read_write == ["/tmp", "/workspace"]
    assert mgr.policy.timeout.idle_timeout == 42.0
    assert mgr.policy.cgroup.memory_max == 268435456
    assert mgr.policy.cgroup.pids_max == 32
    assert mgr.policy.process.run_as_user == "sandbox"
