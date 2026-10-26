# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""标准版 ``permissions.agents[agent_id]`` 稀疏覆盖 + 包内模板缺省底。"""

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

# 用户全局（可被 UI 改写）
_RAW: dict[str, Any] = {
    "enabled": False,
    "schema": "tiered_policy",
    "tools": {"bash": "allow", "write_file": "deny"},
    "agents": {
        "office-excel": {
            "enabled": True,
            "tools": {"bash": "deny"},
            "agents": {"nested": {"enabled": False}},
        },
        "bad": "not-a-dict",
    },
}

# 包内模板缺省底（与用户全局刻意不同，用于证明不跟全局走）
_TEMPLATE: dict[str, Any] = {
    "enabled": False,
    "schema": "tiered_policy",
    "permission_mode": "normal",
    "defaults": {"*": "allow"},
    "tools": {"bash": "allow", "write_file": "allow", "mcp_exec_command": "ask"},
}


@pytest.fixture(autouse=True)
def _reset_permissions_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        loader,
        "get_shipped_template_permissions_config",
        lambda *, force_reload=False: copy.deepcopy(_TEMPLATE),
    )
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
    # 生效 = 包内模板 ∪ 稀疏覆盖；write_file 跟模板 allow，不跟用户全局 deny
    assert body["enabled"] is True
    assert body["schema"] == "tiered_policy"
    assert body["tools"]["bash"] == "deny"
    assert body["tools"]["write_file"] == "allow"
    assert body["tools"]["mcp_exec_command"] == "ask"
    assert "agents" not in body

    overlay = loader.get_yaml_agent_permissions_overlay("office-excel")
    assert overlay is not None
    assert overlay["enabled"] is True
    assert "schema" not in overlay
    assert "write_file" not in overlay.get("tools", {})

    assert loader.resolve_yaml_agent_permissions_body("missing") is None
    assert loader.resolve_yaml_agent_permissions_body("bad") is None
    assert loader.resolve_yaml_agent_permissions_body(None) is None
    assert loader.resolve_yaml_agent_permissions_body("  ") is None

    global_cfg = loader.get_global_permissions_config()
    assert "agents" not in global_cfg
    assert global_cfg["enabled"] is False
    assert global_cfg["tools"]["write_file"] == "deny"

    # 回归：全局段剥掉 agents 后，覆盖表仍须从 _cached_agents 可读（不得 .get("agents")）
    overlay_after_global = loader.get_yaml_agent_permissions_overlay("office-excel")
    assert overlay_after_global is not None
    assert overlay_after_global["tools"]["bash"] == "deny"
    assert loader.get_yaml_agent_permissions_overlay("missing") is None


def test_overlay_not_readable_from_stripped_global_return(monkeypatch, standard_edition):
    """评审回归：_load_yaml_cache 返回值不含 agents，只能读 _cached_agents。"""
    monkeypatch.setattr(loader, "_load_permissions_from_yaml", lambda: copy.deepcopy(_RAW))
    loader.clear_permissions_config_cache()

    stripped = loader._load_yaml_cache()
    assert "agents" not in stripped
    assert stripped.get(loader.PERMISSIONS_AGENTS_KEY, {}) == {}

    # 错误写法会恒为空；正确写法读进程侧 _cached_agents
    assert loader._cached_agents.get("office-excel") is not None
    assert loader.get_yaml_agent_permissions_overlay("office-excel") is not None


def test_seed_without_overlay_uses_template(monkeypatch, standard_edition):
    monkeypatch.setattr(
        loader,
        "_load_permissions_from_yaml",
        lambda: {"enabled": False, "tools": {"write_file": "deny"}},
    )
    body = loader.resolve_yaml_agent_permissions_body("office-excel")
    assert body is not None
    assert body["tools"]["write_file"] == "allow"
    assert body["tools"]["mcp_exec_command"] == "ask"
    assert loader.get_yaml_agent_permissions_overlay("office-excel") is None


def test_enterprise_ignores_yaml_agents(monkeypatch):
    monkeypatch.setattr(loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(loader, "_load_permissions_from_yaml", lambda: copy.deepcopy(_RAW))

    assert loader.resolve_yaml_agent_permissions_body("office-excel") is None
    assert loader.get_yaml_agent_permissions_overlay("office-excel") is None
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
    monkeypatch.setattr(loader, "_permissions_yaml_path", lambda: yaml_path)
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


def test_persist_global_does_not_seed_office_excel(tmp_path, monkeypatch, standard_edition):
    payload = {
        "enabled": False,
        "schema": "tiered_policy",
        "permission_mode": "normal",
        "tools": {"bash": "allow", "write_file": "ask"},
        "file_guard": {"enabled": True},
    }
    yaml_path = _install_yaml(tmp_path, monkeypatch, payload)

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = True

    loader.persist_permissions_mutate(mutate, persist_scope="base", source="guardrail")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    perms = data["permissions"]
    assert perms["enabled"] is True
    assert "agents" not in perms


def test_persist_global_does_not_overwrite_existing_office_excel(
    tmp_path, monkeypatch, standard_edition
):
    yaml_path = _install_yaml(tmp_path, monkeypatch, copy.deepcopy(_RAW))

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = True
        perms.setdefault("tools", {})["write_file"] = "deny"

    loader.persist_permissions_mutate(mutate, persist_scope="base", source="test")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    agent = data["permissions"]["agents"]["office-excel"]
    assert agent["tools"]["bash"] == "deny"
    assert "write_file" not in agent["tools"]


def test_persist_office_excel_miss_sparse_create(tmp_path, monkeypatch, standard_edition):
    payload = {
        "enabled": False,
        "schema": "tiered_policy",
        "tools": {"bash": "allow", "write_file": "deny"},
    }
    yaml_path = _install_yaml(tmp_path, monkeypatch, payload)

    def mutate(perms: dict[str, Any]) -> None:
        perms.setdefault("tools", {})["write_file"] = "ask"

    loader.persist_permissions_mutate(
        mutate,
        persist_scope="base",
        persist_target_agent_id="office-excel",
        source="test",
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    perms = data["permissions"]
    assert perms["tools"]["write_file"] == "deny"
    agent = perms["agents"]["office-excel"]
    assert agent == {"tools": {"write_file": "ask"}}

    effective = loader.resolve_yaml_agent_permissions_body("office-excel")
    assert effective is not None
    # bash / mcp 来自模板，不来自用户全局
    assert effective["tools"]["bash"] == "allow"
    assert effective["tools"]["write_file"] == "ask"
    assert effective["tools"]["mcp_exec_command"] == "ask"
    assert effective["schema"] == "tiered_policy"


def test_persist_office_excel_enabled_toggle_sparse(
    tmp_path, monkeypatch, standard_edition
):
    payload = {
        "enabled": False,
        "schema": "tiered_policy",
        "defaults": {"*": "allow"},
        "tools": {"bash": "deny", "mcp_exec_command": "deny"},
    }
    yaml_path = _install_yaml(tmp_path, monkeypatch, payload)

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = True

    loader.persist_permissions_mutate(
        mutate,
        persist_scope="base",
        persist_target_agent_id="office-excel",
        source="permissions_config_rpc",
    )
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    agent = data["permissions"]["agents"]["office-excel"]
    assert agent == {"enabled": True}

    effective = loader.resolve_yaml_agent_permissions_body("office-excel")
    assert effective is not None
    assert effective["enabled"] is True
    # 用户全局改成 deny 也不影响：仍读模板
    assert effective["tools"]["bash"] == "allow"
    assert effective["tools"]["mcp_exec_command"] == "ask"


def test_lookup_standard_permissions_agent_id():
    lookup = loader.lookup_standard_permissions_agent_id
    assert lookup("office-excel", "default") == "office-excel"
    assert lookup("office-excel", None) == "office-excel"
    assert lookup("default", "office") == "office"
    assert lookup(None, "office-excel") == "office-excel"
    assert lookup(None, None) is None
    assert lookup("  ", "") is None
    assert lookup(None, "default") == "default"


def test_resolve_permissions_persist_target(monkeypatch, standard_edition):
    monkeypatch.setattr(loader, "_load_permissions_from_yaml", lambda: copy.deepcopy(_RAW))
    loader.clear_permissions_config_cache()
    assert loader.resolve_permissions_persist_target("office-excel") == "office-excel"
    assert loader.resolve_permissions_persist_target("missing") is None
    assert loader.resolve_permissions_persist_target("bad") is None
    assert loader.resolve_permissions_persist_target(None) is None


def test_resolve_permissions_persist_target_seeds_office_excel(
    monkeypatch, standard_edition
):
    monkeypatch.setattr(
        loader,
        "_load_permissions_from_yaml",
        lambda: {"enabled": False, "tools": {"bash": "allow"}},
    )
    loader.clear_permissions_config_cache()
    assert loader.resolve_permissions_persist_target("office-excel") == "office-excel"
    assert loader.resolve_permissions_persist_target("office") is None


def test_enterprise_persist_target_is_none(monkeypatch):
    monkeypatch.setattr(loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(loader, "_load_permissions_from_yaml", lambda: copy.deepcopy(_RAW))
    loader.clear_permissions_config_cache()
    assert loader.resolve_permissions_persist_target("office-excel") is None
    assert loader.lookup_standard_permissions_agent_id(None, None) is None


def test_enterprise_persist_does_not_seed_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "is_enterprise", lambda: True)
    yaml_path = _install_yaml(
        tmp_path,
        monkeypatch,
        {"enabled": False, "tools": {"bash": "allow"}},
    )
    original = yaml_path.read_text(encoding="utf-8")

    def mutate(perms: dict[str, Any]) -> None:
        perms["enabled"] = True

    loader.persist_permissions_mutate(mutate, persist_scope="base", source="test")
    assert yaml_path.read_text(encoding="utf-8") == original
    data = yaml.safe_load(original)
    assert "agents" not in data["permissions"]
