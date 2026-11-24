# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""permissions 配置加载：用户 yaml 的 ``host_guard`` 段必须补回生效配置。"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

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
        "permissions_config_loader_host_guard_under_test", _LOADER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


loader = _load_config_loader()

# 模板 ∪ 用户 override 的合并结果：模板 host_guard 只有 enabled，
# 用户独有的 protected_patterns 在这一步已被丢掉。
_MERGED: dict[str, Any] = {
    "enabled": True,
    "schema": "tiered_policy",
    "shell_allowlist": None,
    "tools": {"bash": "allow"},
    "host_guard": {"enabled": True},
}

# 用户 config.yaml 原文（稀疏 override）
_USER_YAML: dict[str, Any] = {
    "permissions": {
        "shell_allowlist": None,
        "tools": {"bash": "allow"},
        "host_guard": {
            "enabled": True,
            "protected_patterns": [
                {"pattern": "MATRIX_HG_001", "reason": "e2e host guard"},
            ],
        },
    }
}


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch):
    import jiuwenswarm.common.config as common_config
    import jiuwenswarm.common.utils as common_utils

    monkeypatch.setattr(loader, "is_enterprise", lambda: False)
    monkeypatch.setattr(
        common_config,
        "get_config",
        lambda: {"permissions": copy.deepcopy(_MERGED)},
    )
    monkeypatch.setattr(
        common_utils, "load_yaml_dict", lambda _path: copy.deepcopy(_USER_YAML)
    )
    monkeypatch.setattr(loader, "_permissions_yaml_path", lambda: Path("config.yaml"))
    loader.clear_permissions_config_cache()
    yield
    loader.clear_permissions_config_cache()


def test_user_host_guard_protected_patterns_restored():
    effective = loader.get_effective_permissions_config()

    assert effective["host_guard"]["enabled"] is True
    assert effective["host_guard"]["protected_patterns"] == [
        {"pattern": "MATRIX_HG_001", "reason": "e2e host guard"},
    ]


def test_user_host_guard_without_enabled_keeps_template_default(monkeypatch):
    user_yaml = copy.deepcopy(_USER_YAML)
    user_yaml["permissions"]["host_guard"] = {
        "protected_patterns": [
            {"pattern": "MATRIX_HG_001", "reason": "e2e host guard"},
        ],
    }
    import jiuwenswarm.common.utils as common_utils

    monkeypatch.setattr(
        common_utils, "load_yaml_dict", lambda _path: copy.deepcopy(user_yaml)
    )

    effective = loader.get_effective_permissions_config()

    assert effective["host_guard"]["enabled"] is True
    assert effective["host_guard"]["protected_patterns"] == [
        {"pattern": "MATRIX_HG_001", "reason": "e2e host guard"},
    ]


def test_user_yaml_without_host_guard_keeps_merged_section(monkeypatch):
    user_yaml = copy.deepcopy(_USER_YAML)
    user_yaml["permissions"].pop("host_guard")
    import jiuwenswarm.common.utils as common_utils

    monkeypatch.setattr(
        common_utils, "load_yaml_dict", lambda _path: copy.deepcopy(user_yaml)
    )

    effective = loader.get_effective_permissions_config()

    assert effective["host_guard"] == {"enabled": True}
    assert "protected_patterns" not in effective["host_guard"]
