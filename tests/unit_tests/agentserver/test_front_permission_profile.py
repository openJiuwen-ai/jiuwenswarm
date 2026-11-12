# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front config reads preserve the permission profile used by the Web composer."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from jiuwenswarm.common.permission_mode import is_auto_permission_enabled
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control import config_service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions,expected_profile,expected_enabled",
    [
        (None, "full_access", "false"),
        ({}, "full_access", "false"),
        ({"enabled": False, "mode": "auto"}, "full_access", "false"),
        ({"enabled": "true", "mode": "auto"}, "full_access", "false"),
        ({"enabled": "1", "mode": "auto"}, "full_access", "false"),
        ({"enabled": "yes", "mode": "auto"}, "full_access", "false"),
        ({"enabled": "on", "mode": "auto"}, "full_access", "false"),
        ({"enabled": 1, "mode": "auto"}, "full_access", "false"),
        ({"enabled": "false", "mode": "auto"}, "full_access", "false"),
        ({"enabled": 0, "mode": "auto"}, "full_access", "false"),
        ({"enabled": None, "mode": "auto"}, "full_access", "false"),
        ({"enabled": True}, "default", "true"),
        ({"enabled": True, "mode": "manual"}, "default", "true"),
        ({"enabled": True, "mode": "auto"}, "automatic", "true"),
        ({"enabled": True, "mode": " AUTO "}, "automatic", "true"),
        ({"enabled": True, "mode": "unknown"}, "default", "true"),
    ],
)
async def test_config_get_restores_saved_permission_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    permissions: dict[str, object] | None,
    expected_profile: str,
    expected_enabled: str,
) -> None:
    path = tmp_path / "config.yaml"
    saved = yaml.safe_dump({"permissions": permissions, "unrelated": {"keep": True}})
    path.write_text(saved, encoding="utf-8")
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "_browser_credential_environment", lambda: {})
    request = AgentRequest(
        request_id="permission-profile-refresh",
        channel_id="web",
        req_method=ReqMethod.CONFIG_GET,
        params={},
    )

    # Each refresh must reconstruct the profile from disk, without changing it.
    for _ in range(2):
        response = await config_service.handle_config_request(request)
        assert response.ok is True
        assert response.payload["permissions_profile"] == expected_profile
        assert response.payload["permissions_enabled"] == expected_enabled
        assert path.read_text(encoding="utf-8") == saved

    assert is_auto_permission_enabled(permissions or {}) is (
        expected_profile == "automatic"
    )


def test_config_get_defaults_permission_profile_when_section_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_service, "_read_raw_config", lambda: {})
    monkeypatch.setattr(config_service, "_browser_credential_environment", lambda: {})

    payload = config_service.get_panel()

    assert payload["permissions_profile"] == "full_access"
    assert payload["permissions_enabled"] == "false"


def test_config_get_fallback_includes_permission_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_read() -> None:
        raise OSError("config read failed")

    monkeypatch.setattr(config_service, "_read_raw_config", fail_read)
    monkeypatch.setattr(config_service, "_browser_credential_environment", lambda: {})

    payload = config_service.get_panel()

    assert payload["permissions_profile"] == "full_access"
    assert payload["permissions_enabled"] == "false"


@pytest.mark.asyncio
async def test_config_get_permission_profile_handles_invalid_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    saved = "permissions:\n  enabled: true\n  mode: auto\nbroken: [\n"
    path.write_text(saved, encoding="utf-8")
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "_browser_credential_environment", lambda: {})
    warning = Mock()
    monkeypatch.setattr(config_service.logger, "warning", warning)
    request = AgentRequest(
        request_id="permission-profile-invalid-yaml",
        channel_id="web",
        req_method=ReqMethod.CONFIG_GET,
        params={},
    )

    response = await config_service.handle_config_request(request)

    assert response.ok is True
    assert response.payload["permissions_profile"] == "full_access"
    assert response.payload["permissions_enabled"] == "false"
    assert path.read_text(encoding="utf-8") == saved
    assert any(
        len(call.args) > 1 and isinstance(call.args[1], yaml.YAMLError)
        for call in warning.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions,expected_profile,expected_enabled",
    [
        ({"enabled": True, "mode": "auto"}, "automatic", "true"),
        ({"enabled": True, "mode": "manual"}, "default", "true"),
        ({"enabled": False, "mode": "auto"}, "full_access", "false"),
        ({"enabled": "true", "mode": "auto"}, "full_access", "false"),
    ],
)
async def test_config_get_permission_profile_survives_later_field_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    permissions: dict[str, object],
    expected_profile: str,
    expected_enabled: str,
) -> None:
    path = tmp_path / "config.yaml"
    # Evolution is read after the permission profile; a list has no get method.
    saved = yaml.safe_dump({
        "permissions": permissions,
        "react": {"evolution": ["invalid"]},
        "unrelated": {"keep": True},
    })
    path.write_text(saved, encoding="utf-8")
    monkeypatch.setattr(config_service, "get_config_file", lambda: path)
    monkeypatch.setattr(config_service, "_browser_credential_environment", lambda: {})
    warning = Mock()
    monkeypatch.setattr(config_service.logger, "warning", warning)
    request = AgentRequest(
        request_id="permission-profile-later-field-failure",
        channel_id="web",
        req_method=ReqMethod.CONFIG_GET,
        params={},
    )

    response = await config_service.handle_config_request(request)

    assert response.ok is True
    assert response.payload["permissions_profile"] == expected_profile
    assert response.payload["permissions_enabled"] == expected_enabled
    assert path.read_text(encoding="utf-8") == saved
    warning.assert_called_once()
    assert isinstance(warning.call_args.args[1], AttributeError)
    persisted = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert is_auto_permission_enabled(persisted["permissions"]) is (
        expected_profile == "automatic"
    )


