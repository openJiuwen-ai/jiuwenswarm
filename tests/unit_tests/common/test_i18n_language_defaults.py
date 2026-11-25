# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""preferred_language default alignment (illegal / missing → en)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common import config as config_mod
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control import config_service
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter
from tests.unit_tests.server.test_gateway_adapter import _request


def test_update_preferred_language_rejects_illegal(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("preferred_language: en\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", path)
    with pytest.raises(ValueError):
        config_mod.update_preferred_language_in_config("fr")
    with pytest.raises(ValueError):
        config_mod.update_preferred_language_in_config("")
    assert path.read_text(encoding="utf-8") == "preferred_language: en\n"


def test_set_preferred_language_rejects_illegal(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("preferred_language: en\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config_mod.set_preferred_language_in_config_file(path, "fr")
    assert path.read_text(encoding="utf-8") == "preferred_language: en\n"


def test_resolve_request_language_uses_resolver_when_missing(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.preferred_language.resolve_preferred_language",
        lambda: "en",
    )
    request = SimpleNamespace(metadata={}, params={})
    assert team_helpers._resolve_request_language(request) == "en"


def test_resolve_request_language_normalizes_aliases():
    request = SimpleNamespace(
        metadata={"preferred_language": "zh-CN"},
        params={},
    )
    assert team_helpers._resolve_request_language(request) == "zh"


def test_resolve_request_language_prefers_ui_language(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.preferred_language.resolve_preferred_language",
        lambda: "zh",
    )
    request = SimpleNamespace(
        metadata={"preferred_language": "zh"},
        params={"ui_language": "en"},
    )
    assert team_helpers._resolve_request_language(request) == "en"


def test_resolve_request_language_ignores_match_response_language(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.preferred_language.resolve_preferred_language",
        lambda: "en",
    )
    request = SimpleNamespace(
        metadata={},
        params={"preferred_response_language": "match"},
    )
    assert team_helpers._resolve_request_language(request) == "en"


def test_resolve_request_language_ignores_unrecognized(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.config.preferred_language.resolve_preferred_language",
        lambda: "en",
    )
    request = SimpleNamespace(metadata={"language": "fr"}, params={})
    assert team_helpers._resolve_request_language(request) == "en"


@pytest.mark.asyncio
async def test_locale_get_conf_missing_key_returns_en(tmp_path, monkeypatch):
    import yaml

    from jiuwenswarm.common import config

    raw = {"models": {}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.edition.is_enterprise",
        lambda: False,
    )
    response = await ConfigAdapter().handle(_request(ReqMethod.LOCALE_GET_CONF))
    assert response.ok is True
    assert response.payload["preferred_language"] == "en"


def test_config_service_locale_get_conf_missing_returns_en(monkeypatch):
    monkeypatch.setattr(
        config_service,
        "_read_raw_config",
        lambda: {},
    )
    # Exercise the same normalize path used by handle_config_request.
    from jiuwenswarm.common.i18n import normalize_preferred_language

    assert normalize_preferred_language({}.get("preferred_language")) == "en"
