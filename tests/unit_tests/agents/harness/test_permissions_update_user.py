# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-layer single-tool updates revoke remembered allows without new fields."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common import config
from jiuwenswarm.common import utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
    compose_host_effective_permissions,
)
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
    dispatch_permissions_config_request,
)


def _setup(tmp_path: Path, monkeypatch) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config, "get_config_file", lambda: config_path)
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    return sessions


def _write_global(tools: dict[str, str]) -> None:
    config.dump_yaml_round_trip(config.CONFIG_YAML_PATH, {
        "unrelated": "global-kept",
        "permissions": {
            "enabled": True,
            "defaults": {"*": "ask"},
            "tools": tools,
            "file_guard": {"enabled": True, "paths": [{"path": "/global", "write": "ask"}]},
        },
    })


def _request(method: ReqMethod, params: dict) -> AgentRequest:
    return AgentRequest(request_id="update-user", req_method=method, params=params)


def test_ask_revokes_remembered_allows_and_keeps_other_tools(tmp_path: Path, monkeypatch) -> None:
    sessions = _setup(tmp_path, monkeypatch)
    _write_global({"write_file": "ask", "bash": "ask"})
    assert layers._dump_yaml_dict(layers.user_permissions_path(), {
        "custom": "user-kept",
        "allow_tools": ["write_file", "bash"],
        "approval_overrides": [
            {"id": "only_write", "tools": ["write_file"], "action": "allow"},
            {"id": "shared", "tools": ["write_file", "edit_file"], "action": "allow"},
            {"id": "bash_only", "tools": ["bash"], "action": "allow"},
            {"id": "unscoped", "action": "allow"},
        ],
        "file_guard": {"paths": [{"path": "/tmp/user", "write": "allow"}]},
    })
    assert layers._dump_yaml_dict(sessions / "s1" / "session_permissions.yaml", {
        "allow_tools": ["write_file", "read_file"],
        "tools": {"write_file": "allow"},
        "approval_overrides": [{"id": "session_write", "tools": ["write_file"], "action": "allow"}],
        "file_guard": {"paths": [{"path": "/tmp/session", "write": "allow"}]},
    })
    assert layers._dump_yaml_dict(sessions / "s2" / "session_permissions.yaml", {
        "allow_tools": ["bash"],
    })
    before_global = config.CONFIG_YAML_PATH.read_bytes()
    before_other = (sessions / "s2" / "session_permissions.yaml").read_bytes()

    response = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE_USER,
        {"tool": "write_file", "level": "ask"},
    ))

    assert response.ok
    assert response.payload["level"] == "ask"
    assert response.payload["effective_level"] == "ask"
    assert response.payload["revoked"] == {
        "user_allow": 1,
        "session_allow": 1,
        "approval_overrides": 3,
    }
    user = layers.load_user_permissions()
    assert user["custom"] == "user-kept"
    assert user["allow_tools"] == ["bash"]
    assert user["ask_tools"] == ["write_file"]
    assert user["file_guard"]["paths"] == [{"path": "/tmp/user", "write": "allow"}]
    assert user["approval_overrides"] == [
        {"id": "shared", "tools": ["edit_file"], "action": "allow"},
        {"id": "bash_only", "tools": ["bash"], "action": "allow"},
        {"id": "unscoped", "action": "allow"},
    ]
    session = layers.load_session_permissions("s1")
    assert session["allow_tools"] == ["read_file"]
    assert "tools" not in session
    assert "approval_overrides" not in session
    assert session["file_guard"]["paths"] == [{"path": "/tmp/session", "write": "allow"}]
    assert (sessions / "s2" / "session_permissions.yaml").read_bytes() == before_other
    assert config.CONFIG_YAML_PATH.read_bytes() == before_global

    effective = compose_host_effective_permissions(
        global_permissions=layers.load_global_permissions(),
        user_permissions=user,
        session_permissions=session,
    )
    assert effective["tools"]["write_file"] == "ask"
    assert effective["tools"]["bash"] == "allow"
    assert effective["tools"]["read_file"] == "allow"


def test_ask_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    _write_global({"write_file": "ask"})
    assert layers._dump_yaml_dict(layers.user_permissions_path(), {"allow_tools": ["write_file"]})

    first = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE_USER, {"tool": "write_file", "level": "ask"},
    ))
    second = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE_USER, {"tool": "write_file", "level": "ask"},
    ))

    assert first.ok and second.ok
    assert second.payload["revoked"] == {
        "user_allow": 0,
        "session_allow": 0,
        "approval_overrides": 0,
    }
    assert layers.load_user_permissions()["ask_tools"] == ["write_file"]
    assert "allow_tools" not in layers.load_user_permissions()


def test_global_deny_stays_deny(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    _write_global({"write_file": "deny"})
    assert layers._dump_yaml_dict(layers.user_permissions_path(), {"allow_tools": ["write_file"]})

    response = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE_USER, {"tool": "write_file", "level": "ask"},
    ))

    assert response.ok
    assert response.payload["level"] == "ask"
    assert response.payload["effective_level"] == "deny"
    assert layers.load_global_permissions()["tools"]["write_file"] == "deny"


def test_allow_updates_user_lists_without_touching_sessions(tmp_path: Path, monkeypatch) -> None:
    sessions = _setup(tmp_path, monkeypatch)
    _write_global({"write_file": "ask"})
    assert layers._dump_yaml_dict(layers.user_permissions_path(), {
        "ask_tools": ["write_file"],
        "approval_overrides": [{"id": "kept", "tools": ["write_file"], "action": "allow"}],
    })
    session_path = sessions / "s1" / "session_permissions.yaml"
    assert layers._dump_yaml_dict(session_path, {"allow_tools": ["write_file"]})
    before = session_path.read_bytes()

    response = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE_USER, {"tool": "write_file", "level": "allow"},
    ))

    assert response.ok
    assert response.payload["effective_level"] == "allow"
    user = layers.load_user_permissions()
    assert user["allow_tools"] == ["write_file"]
    assert "ask_tools" not in user
    assert user["approval_overrides"][0]["tools"] == ["write_file"]
    assert session_path.read_bytes() == before


def test_global_update_does_not_revoke_user_allow(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    _write_global({"write_file": "ask"})
    assert layers._dump_yaml_dict(layers.user_permissions_path(), {"allow_tools": ["write_file"]})

    response = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE, {"tool": "write_file", "level": "ask"},
    ))

    assert response.ok
    assert layers.load_user_permissions()["allow_tools"] == ["write_file"]
    effective = compose_host_effective_permissions(
        global_permissions=layers.load_global_permissions(),
        user_permissions=layers.load_user_permissions(),
        session_permissions={},
    )
    assert effective["tools"]["write_file"] == "allow"


def test_update_user_rejects_unknown_level(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    _write_global({})
    response = dispatch_permissions_config_request(_request(
        ReqMethod.PERMISSIONS_TOOLS_UPDATE_USER, {"tool": "write_file", "level": "permit"},
    ))
    assert not response.ok
