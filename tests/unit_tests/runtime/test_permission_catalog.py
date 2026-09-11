# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from jiuwenswarm.runtime import permission_catalog
from jiuwenswarm.runtime.permission_catalog import (
    PermissionCatalogError,
    PermissionLayerSnapshot,
    PermissionLevel,
    PermissionRuleSnapshot,
    PermissionSnapshotInput,
    read_permission_snapshot,
)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | None, int]]:
    snapshot: dict[str, tuple[str, bytes | None, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            snapshot[relative] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
        else:
            snapshot[relative] = ("directory", None, path.stat().st_mtime_ns)
    return snapshot


def _tools(layer: PermissionLayerSnapshot) -> dict[str, PermissionLevel]:
    return {item.name: item.level for item in layer.tools}


def _rules(layer: PermissionLayerSnapshot) -> dict[str, PermissionRuleSnapshot]:
    return {item.rule_id: item for item in layer.rules}


def test_snapshot_reads_all_layers_with_effective_deny_and_ask_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import (
        permissions_layers,
    )
    from jiuwenswarm.common import config

    config_path = tmp_path / "config.yaml"
    user_path = tmp_path / "user_permissions.yaml"
    sessions_path = tmp_path / "sessions"
    session_path = sessions_path / "session_1" / "session_permissions.yaml"
    secret = "sk-permission-catalog-must-not-leak"
    _write_yaml(
        config_path,
        {
            "permissions": {
                "enabled": True,
                "package_builtin_rules": False,
                "tools": {
                    "dangerous": "deny",
                    "global_ask": {"*": "ask", "token": secret},
                },
                "rules": [
                    {
                        "id": "global-critical",
                        "tools": ["bash"],
                        "pattern": "rm -rf *",
                        "severity": "CRITICAL",
                        "private_note": secret,
                    }
                ],
                "approval_overrides": [{"id": "private", "token": secret}],
                "api_key": secret,
                "file_guard": {"paths": [{"path": secret}]},
            }
        },
    )
    _write_yaml(
        user_path,
        {
            "permissions": {
                "tools": {"dangerous": "allow", "inspect": "ask"},
                "rules": [
                    {
                        "id": "user-high",
                        "tools": "powershell",
                        "pattern": "invoke-*",
                        "severity": "HIGH",
                        "credential": secret,
                    }
                ],
                "owner_scopes": {"secret": secret},
            }
        },
    )
    _write_yaml(
        session_path,
        {
            "permissions": {
                "tools": {
                    "dangerous": "allow",
                    "session_only": "allow",
                    "session_cannot_ask": "ask",
                },
                "rules": [
                    {
                        "id": "session-explicit",
                        "tools": ["write"],
                        "pattern": "*.txt",
                        "action": "deny",
                    }
                ],
            }
        },
    )
    monkeypatch.setattr(config, "get_config_file", lambda: config_path)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(
        permissions_layers,
        "user_permissions_path",
        lambda: user_path,
    )
    monkeypatch.setattr(
        permissions_layers,
        "session_permissions_path",
        lambda session_id: sessions_path / session_id / "session_permissions.yaml",
    )

    def reject_writer_lock(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read-only catalog must not acquire a writer lock")

    monkeypatch.setattr(
        permissions_layers,
        "permission_storage_lock",
        reject_writer_lock,
    )
    before = _tree_snapshot(tmp_path)

    result = read_permission_snapshot(PermissionSnapshotInput(session_id="session_1"))

    after = _tree_snapshot(tmp_path)
    assert after == before
    assert result.scope == "session"
    assert result.session_id == "session_1"
    assert result.global_layer.enabled is True
    assert _tools(result.global_layer)["dangerous"] == "deny"
    assert _tools(result.user_layer)["inspect"] == "ask"
    assert _tools(result.session_layer)["session_cannot_ask"] == "ask"

    effective_tools = _tools(result.effective)
    assert effective_tools["dangerous"] == "deny"
    assert effective_tools["inspect"] == "ask"
    assert effective_tools["session_only"] == "allow"
    assert "session_cannot_ask" not in effective_tools

    effective_rules = _rules(result.effective)
    assert effective_rules["global-critical"].action == "deny"
    assert effective_rules["user-high"].action == "ask"
    assert effective_rules["session-explicit"].action == "deny"

    payload = result.to_dict()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert secret not in serialized
    assert "api_key" not in serialized
    assert "approval_overrides" not in serialized
    assert "owner_scopes" not in serialized
    assert "file_guard" not in serialized
    assert set(payload) == {
        "scope",
        "session_id",
        "global",
        "user",
        "session",
        "effective",
    }
    assert set(payload["effective"]["rules"][0]) == {
        "id",
        "tools",
        "pattern",
        "action",
        "severity",
        "description",
        "match_type",
    }


def test_host_snapshot_skips_session_storage_and_keeps_inputs_immutable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layers = (
        {"enabled": False, "tools": {"read": "allow"}},
        {"ask_tools": ["write"]},
        {},
    )
    original = deepcopy(layers)
    reader = Mock(return_value=layers)
    monkeypatch.setattr(permission_catalog, "_read_permission_layers", reader)

    result = read_permission_snapshot(PermissionSnapshotInput())

    reader.assert_called_once_with("")
    assert result.scope == "host"
    assert result.session_id == ""
    assert result.session_layer.tools == ()
    assert _tools(result.effective) == {"read": "allow", "write": "ask"}
    assert layers == original


@pytest.mark.parametrize(
    "session_id",
    ("../secret", "nested/session", ".", "a" * 81),
)
def test_snapshot_rejects_unsafe_session_id_before_storage_access(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
) -> None:
    reader = Mock(side_effect=AssertionError("storage must not be read"))
    monkeypatch.setattr(permission_catalog, "_read_permission_layers", reader)

    with pytest.raises(PermissionCatalogError, match="invalid session id") as caught:
        read_permission_snapshot(PermissionSnapshotInput(session_id=session_id))

    assert caught.value.code == "BAD_REQUEST"
    reader.assert_not_called()


def test_snapshot_maps_storage_failure_to_non_sensitive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "C:/private/config.yaml?token=must-not-leak"
    monkeypatch.setattr(
        permission_catalog,
        "_read_permission_layers",
        Mock(side_effect=OSError(secret)),
    )

    with pytest.raises(PermissionCatalogError) as caught:
        read_permission_snapshot(PermissionSnapshotInput())

    assert caught.value.code == "READ_FAILED"
    assert str(caught.value) == "failed to read permissions"
    assert secret not in str(caught.value)


def test_catalog_has_no_transport_or_mutation_dependencies() -> None:
    source = Path(permission_catalog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert imported_names.isdisjoint({"AgentRequest", "AgentResponse", "ReqMethod"})
    assert called_names.isdisjoint(
        {
            "capture_permission_layers",
            "permission_storage_lock",
            "save_user_permissions",
            "update_config",
            "reload_agents",
            "dispatch_permissions_config_request",
        }
    )
