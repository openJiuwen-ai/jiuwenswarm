# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""标准版 ``permissions.agents[agent_id]`` 精确整段替换隔离。"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

_LOADER_PATH = (
    Path(__file__).resolve().parents[4]
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "rails"
    / "permissions"
    / "config_loader.py"
)


def _load_config_loader():
    spec = importlib.util.spec_from_file_location(
        "permissions_config_loader_under_test", _LOADER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


loader = _load_config_loader()

_RAW: dict[str, Any] = {
    "enabled": False,
    "schema": "tiered_policy",
    "tools": {"bash": "allow"},
    "agents": {
        "office-excel": {
            "enabled": True,
            "schema": "tiered_policy",
            "tools": {"bash": "deny"},
            "agents": {"nested": {"enabled": False}},
        },
        "bad": "not-a-dict",
    },
}


@pytest.fixture(autouse=True)
def _reset_permissions_cache():
    loader.clear_permissions_config_cache()
    yield
    loader.clear_permissions_config_cache()


@pytest.fixture
def standard_edition(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loader, "is_enterprise", lambda: False)


def test_strip_and_sanitize_agents(standard_edition):
    stripped = loader.strip_permissions_agents(_RAW)
    assert "agents" not in stripped
    assert stripped["tools"]["bash"] == "allow"
    assert "agents" in _RAW

    nested = loader.sanitize_agent_permissions_body(_RAW["agents"]["office-excel"])
    assert "agents" not in nested
    assert nested["tools"]["bash"] == "deny"


def test_resolve_yaml_agent_permissions_hit_miss(monkeypatch, standard_edition):
    monkeypatch.setattr(loader, "_load_permissions_from_yaml", lambda: copy.deepcopy(_RAW))

    body = loader.resolve_yaml_agent_permissions_body("office-excel")
    assert body is not None
    assert body["enabled"] is True
    assert body["tools"]["bash"] == "deny"
    assert "agents" not in body

    assert loader.resolve_yaml_agent_permissions_body("missing") is None
    assert loader.resolve_yaml_agent_permissions_body("bad") is None
    assert loader.resolve_yaml_agent_permissions_body(None) is None
    assert loader.resolve_yaml_agent_permissions_body("  ") is None

    global_cfg = loader.get_global_permissions_config()
    assert "agents" not in global_cfg
    assert global_cfg["enabled"] is False
    assert global_cfg["tools"]["bash"] == "allow"


def test_enterprise_ignores_yaml_agents(monkeypatch):
    monkeypatch.setattr(loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(loader, "_load_permissions_from_yaml", lambda: copy.deepcopy(_RAW))

    assert loader.resolve_yaml_agent_permissions_body("office-excel") is None
    global_cfg = loader.get_global_permissions_config()
    assert "agents" not in global_cfg


def test_agent_base_context_uses_sanitized_body(standard_edition):
    token = loader.setup_permissions_agent_base(
        {"enabled": True, "tools": {"bash": "deny"}, "agents": {"x": {}}}
    )
    try:
        base = loader.get_base_permissions_config()
        assert base["enabled"] is True
        assert base["tools"]["bash"] == "deny"
        assert "agents" not in base
    finally:
        loader.reset_permissions_agent_base(token)


def _install_yaml(tmp_path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"permissions": payload}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    def _get_config():
        return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", yaml_path)
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", _get_config)
    loader.clear_permissions_config_cache()
    return yaml_path


def test_persist_agent_does_not_touch_global(tmp_path, monkeypatch, standard_edition):
    yaml_path = _install_yaml(tmp_path, monkeypatch, copy.deepcopy(_RAW))

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = False
        perms.setdefault("tools", {})["write_file"] = "ask"

    loader.persist_permissions_mutate(
        mutate,
        persist_scope="base",
        persist_target_agent_id="office-excel",
        source="test",
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    perms = data["permissions"]
    assert perms["tools"]["bash"] == "allow"
    agent = perms["agents"]["office-excel"]
    assert agent["enabled"] is False
    assert agent["tools"]["bash"] == "deny"
    assert agent["tools"]["write_file"] == "ask"
    assert "agents" not in agent


def test_persist_global_preserves_agents_table(tmp_path, monkeypatch, standard_edition):
    yaml_path = _install_yaml(tmp_path, monkeypatch, copy.deepcopy(_RAW))

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = True
        perms["agents"] = {"should-not-write": {"enabled": True}}

    loader.persist_permissions_mutate(mutate, persist_scope="base", source="test")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    perms = data["permissions"]
    assert perms["enabled"] is True
    assert "should-not-write" not in perms["agents"]
    assert perms["agents"]["office-excel"]["tools"]["bash"] == "deny"


def test_persist_miss_does_not_create_agent_bucket(tmp_path, monkeypatch, standard_edition):
    yaml_path = _install_yaml(tmp_path, monkeypatch, copy.deepcopy(_RAW))

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = True

    loader.persist_permissions_mutate(
        mutate,
        persist_scope="base",
        persist_target_agent_id="no-such-agent",
        source="test",
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    perms = data["permissions"]
    assert perms["enabled"] is True
    assert "no-such-agent" not in perms["agents"]
    assert "office-excel" in perms["agents"]
