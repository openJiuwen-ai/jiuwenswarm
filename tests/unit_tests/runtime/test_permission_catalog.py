# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from inspect import Parameter, signature
from pathlib import Path
from unittest.mock import Mock

import pytest

from jiuwenswarm.runtime import mode_catalog, model_catalog, session_catalog
from jiuwenswarm.runtime import permission_catalog
from jiuwenswarm.runtime.permission_catalog import (
    PermissionCatalogError,
    PermissionLayerSnapshot,
    PermissionSnapshotInput,
    PermissionToolSnapshot,
    read_permission_snapshot,
)


def _tool_levels(layer: PermissionLayerSnapshot) -> dict[str, str]:
    return {item.name: item.level for item in layer.tools}


def test_snapshot_uses_one_consistent_capture_and_safe_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )

    secret = "sk-permission-catalog-must-not-leak"
    global_layer = {
        "enabled": True,
        "package_builtin_rules": False,
        "tools": {"dangerous": "deny", "inspect": {"*": "ask", "token": secret}},
        "rules": [
            {
                "id": "critical",
                "tools": ["bash"],
                "pattern": "rm -rf *",
                "severity": "CRITICAL",
                "private_note": secret,
            }
        ],
        "approval_overrides": [{"id": "private", "token": secret}],
        "api_key": secret,
    }
    user_layer = {"tools": {"dangerous": "allow", "write": "ask"}}
    session_layer = {
        "tools": {
            "dangerous": "allow",
            "session_only": "allow",
            "session_cannot_ask": "ask",
        }
    }
    original = deepcopy((global_layer, user_layer, session_layer))
    effective = compose_host_effective_permissions(
        global_permissions=global_layer,
        user_permissions=user_layer,
        session_permissions=session_layer,
    )
    capture = Mock(return_value=(global_layer, user_layer, session_layer, effective))
    owned_session = Mock()
    read_owned_session = Mock(return_value=owned_session)
    monkeypatch.setattr(permission_catalog, "_read_owned_session", read_owned_session)
    monkeypatch.setattr(permission_catalog, "_capture_permission_layers", capture)

    result = read_permission_snapshot(
        PermissionSnapshotInput(channel_id="process_cli", session_id="session_1")
    )

    read_owned_session.assert_called_once_with("process_cli", "session_1")
    capture.assert_called_once_with("session_1")
    assert (global_layer, user_layer, session_layer) == original
    assert result.scope == "session"
    assert _tool_levels(result.effective)["dangerous"] == "deny"
    assert _tool_levels(result.effective)["write"] == "ask"
    assert _tool_levels(result.effective)["session_only"] == "allow"
    assert "session_cannot_ask" not in _tool_levels(result.effective)

    serialized = json.dumps(result.to_dict())
    assert secret not in serialized
    assert "api_key" not in serialized
    assert "approval_overrides" not in serialized


def test_snapshot_result_is_deep_copy_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    layers = (
        {"enabled": True, "tools": {"read": "allow"}},
        {"tools": {"write": "ask"}},
        {},
        {"enabled": True, "tools": {"read": "allow", "write": "ask"}},
    )
    monkeypatch.setattr(
        permission_catalog,
        "_capture_permission_layers",
        Mock(return_value=layers),
    )

    result = read_permission_snapshot(PermissionSnapshotInput())
    first = result.to_dict()
    first["effective"]["tools"].clear()
    layers[3]["tools"]["read"] = "deny"

    assert _tool_levels(result.effective) == {"read": "allow", "write": "ask"}
    assert len(result.to_dict()["effective"]["tools"]) == 2


def test_capture_wrapper_reuses_consistent_permission_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    capture = Mock(return_value=({}, {}, {}, {}))
    monkeypatch.setattr(permissions_layers, "capture_permission_layers", capture)

    assert permission_catalog._capture_permission_layers("") == ({}, {}, {}, {})
    capture.assert_called_once_with(None)


@pytest.mark.parametrize("session_id", ("../secret", "nested/session", ".", "a" * 81))
def test_snapshot_rejects_unsafe_session_id_before_capture(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
) -> None:
    capture = Mock(side_effect=AssertionError("storage must not be read"))
    read_owned_session = Mock(side_effect=AssertionError("session must not be read"))
    monkeypatch.setattr(permission_catalog, "_read_owned_session", read_owned_session)
    monkeypatch.setattr(permission_catalog, "_capture_permission_layers", capture)

    with pytest.raises(PermissionCatalogError, match="invalid session id") as caught:
        read_permission_snapshot(PermissionSnapshotInput(session_id=session_id))

    assert caught.value.code == "BAD_REQUEST"
    read_owned_session.assert_not_called()
    capture.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {
            "session_id": "missing_session",
            "channel_id": "tui",
            "mode": "agent.code.normal",
        },
    ),
)
def test_session_snapshot_rejects_missing_or_foreign_before_locking(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, object],
) -> None:
    from jiuwenswarm.runtime import session_catalog

    read_metadata = Mock(return_value=metadata)
    capture = Mock(side_effect=AssertionError("permission lock must not be acquired"))
    monkeypatch.setattr(session_catalog, "_read_session_metadata", read_metadata)
    monkeypatch.setattr(permission_catalog, "_capture_permission_layers", capture)

    with pytest.raises(PermissionCatalogError, match="session not found") as caught:
        read_permission_snapshot(
            PermissionSnapshotInput(
                channel_id="process_cli",
                session_id="missing_session",
            )
        )

    assert caught.value.code == "NOT_FOUND"
    read_metadata.assert_called_once_with("missing_session")
    capture.assert_not_called()


def test_snapshot_maps_capture_failure_to_non_sensitive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "D:/private/config.yaml?token=must-not-leak"
    monkeypatch.setattr(
        permission_catalog,
        "_capture_permission_layers",
        Mock(side_effect=OSError(secret)),
    )

    with pytest.raises(PermissionCatalogError) as caught:
        read_permission_snapshot(PermissionSnapshotInput())

    assert caught.value.code == "READ_FAILED"
    assert str(caught.value) == "failed to read permissions"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_permission_dto_is_frozen_slotted_and_keyword_only() -> None:
    tool = PermissionToolSnapshot(name="read", level="allow")

    assert not hasattr(tool, "__dict__")
    with pytest.raises(FrozenInstanceError):
        tool.name = "changed"  # type: ignore[misc]
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        for parameter in signature(PermissionToolSnapshot).parameters.values()
    )


@pytest.mark.parametrize(
    "catalog_module",
    (model_catalog, mode_catalog, session_catalog, permission_catalog),
)
def test_catalogs_have_no_transport_dependencies(catalog_module: object) -> None:
    module_path = getattr(catalog_module, "__file__")
    source = Path(module_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported_names.isdisjoint({"AgentRequest", "AgentResponse", "ReqMethod"})
    lowered_source = source.lower()
    assert "agent_ws_server" not in lowered_source
    assert "jiuwenswarm.gateway" not in lowered_source
    assert "websocket" not in lowered_source
    assert "wire codec" not in lowered_source


def test_permission_catalog_has_no_mutation_dependencies() -> None:
    source = Path(permission_catalog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names.isdisjoint(
        {
            "save_user_permissions",
            "update_config",
            "reload_agents",
            "dispatch_permissions_config_request",
        }
    )
