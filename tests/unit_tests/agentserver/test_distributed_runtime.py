"""Unit tests for distributed runtime config normalization helpers."""

import pytest

from jiuwenswarm.agents.harness.team.distributed_runtime import (
    DistributedDependencyError,
    fallback_distributed_to_local,
    normalize_distributed_transport_fields,
    parse_port,
)


def _pg_distributed_cfg():
    return {
        "team": {
            "runtime": {"mode": "distributed", "role": "leader"},
            "transport": {"type": "pyzmq", "params": {}},
            "storage": {
                "type": "postgresql",
                "params": {
                    "connection_string": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
                },
            },
        }
    }


def test_normalize_leader_applies_pool_policy_when_pubsub_prefilled():
    """Leader templates often pre-fill pubsub_*; pool policy must still run."""
    config_base = {
        "team": {
            "runtime": {"mode": "distributed", "role": "leader"},
        }
    }
    team_cfg = {
        "transport": {
            "type": "pyzmq",
            "params": {
                "direct_addr": "tcp://0.0.0.0:28555",
                "pubsub_publish_addr": "tcp://127.0.0.1:28556",
                "pubsub_subscribe_addr": "tcp://127.0.0.1:28557",
                "known_peers": [{"agent_id": "legacy", "addrs": ["tcp://127.0.0.1:1"]}],
                "metadata": {"pubsub_bind": True},
            },
        },
    }
    normalized = normalize_distributed_transport_fields(config_base, team_cfg)
    params = normalized["transport"]["params"]
    assert "known_peers" not in params
    assert "enforce_static_spawn_names" not in params.get("metadata", {})


def test_normalize_distributed_transport_fields_does_not_mutate_input():
    config_base = {
        "team": {
            "runtime": {"mode": "distributed", "role": "leader"},
        }
    }
    team_cfg = {
        "leader": {"member_name": "team_leader"},
        "transport": {
            "type": "pyzmq",
            "params": {
                "leader": {"host": "127.0.0.1", "direct_port": 18555, "pub_port": 18556, "sub_port": 18557},
                "teammate": {"host": "127.0.0.1", "direct_port": 18600},
            },
        },
        "predefined_members": [{"member_name": "teammate_1"}],
    }

    normalized = normalize_distributed_transport_fields(config_base, team_cfg)

    # Ensure helper keeps input immutable for better determinism in repeated calls.
    assert "direct_addr" not in team_cfg["transport"]["params"]
    assert "pubsub_publish_addr" not in team_cfg["transport"]["params"]
    assert "metadata" not in team_cfg["transport"]["params"]

    params = normalized["transport"]["params"]
    assert params["direct_addr"] == "tcp://0.0.0.0:18555"
    assert params["pubsub_publish_addr"] == "tcp://127.0.0.1:18556"
    assert params["metadata"]["pubsub_bind"] is True
    assert "enforce_static_spawn_names" not in params["metadata"]
    assert "known_peers" not in params
    assert "bootstrap_peers" not in params


def test_normalize_distributed_transport_fields_teammate_keeps_leader_peer():
    config_base = {
        "team": {
            "runtime": {"mode": "distributed", "role": "teammate"},
        }
    }
    team_cfg = {
        "leader": {"member_name": "team_leader"},
        "transport": {
            "type": "pyzmq",
            "params": {
                "leader": {"host": "10.0.0.1", "direct_port": 18555},
                "teammate": {"host": "10.0.0.2", "direct_port": 18600},
            },
        },
    }
    normalized = normalize_distributed_transport_fields(config_base, team_cfg)
    peers = normalized["transport"]["params"]["known_peers"]
    assert len(peers) == 1
    assert peers[0]["agent_id"] == "team_leader"
    assert peers[0]["addrs"] == ["tcp://10.0.0.1:18555"]


def test_parse_port_uses_default_for_blank_string():
    assert parse_port("  ", 18555, "team.transport.params.leader.direct_port") == 18555


def test_parse_port_raises_for_non_numeric_value():
    with pytest.raises(ValueError, match="team\\.transport\\.params\\.leader\\.pub_port"):
        parse_port("abc", 18556, "team.transport.params.leader.pub_port")


def test_parse_port_raises_for_out_of_range_value():
    with pytest.raises(ValueError, match="1\\.\\.65535"):
        parse_port(70000, 18557, "team.transport.params.leader.sub_port")


def test_fallback_pg_env_unset_silently_rewrites_to_sqlite(monkeypatch):
    """Unset JIUWENSWARM_ALLOW_SQLITE_FALLBACK keeps legacy silent PG→sqlite."""
    monkeypatch.delenv("JIUWENSWARM_ALLOW_SQLITE_FALLBACK", raising=False)
    out = fallback_distributed_to_local(_pg_distributed_cfg())
    assert out["team"]["runtime"]["mode"] == "local"
    assert out["team"]["transport"]["type"] == "inprocess"
    assert out["team"]["storage"]["type"] == "sqlite"
    assert out["team"]["storage"]["params"]["connection_string"] == "team.db"


def test_fallback_pg_env_true_rewrites_to_sqlite(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_ALLOW_SQLITE_FALLBACK", "1")
    out = fallback_distributed_to_local(_pg_distributed_cfg())
    assert out["team"]["storage"]["type"] == "sqlite"


def test_fallback_pg_env_false_raises(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_ALLOW_SQLITE_FALLBACK", "0")
    with pytest.raises(DistributedDependencyError, match="JIUWENSWARM_ALLOW_SQLITE_FALLBACK is false"):
        fallback_distributed_to_local(_pg_distributed_cfg())


def test_fallback_local_sqlite_storage_unchanged():
    cfg = {
        "team": {
            "runtime": {"mode": "distributed", "role": "leader"},
            "transport": {"type": "pyzmq"},
            "storage": {
                "type": "sqlite",
                "params": {"connection_string": "keep-me.db"},
            },
        }
    }
    out = fallback_distributed_to_local(cfg)
    assert out["team"]["runtime"]["mode"] == "local"
    assert out["team"]["storage"]["type"] == "sqlite"
    assert out["team"]["storage"]["params"]["connection_string"] == "keep-me.db"
