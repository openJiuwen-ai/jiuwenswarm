# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""启动清理：permissions 下扩展段的用户自定义嵌套键不得被裁掉。"""

from __future__ import annotations

from jiuwenswarm.common.config import _prune_override_keys


def test_host_guard_section_kept_whole():
    template = {
        "permissions": {
            "enabled": False,
            "host_guard": {"enabled": True},
        }
    }
    override = {
        "permissions": {
            "enabled": True,
            "host_guard": {
                "enabled": True,
                "protected_patterns": [
                    {"pattern": "MATRIX_HG_001", "reason": "e2e host guard"},
                ],
            },
        }
    }

    pruned = _prune_override_keys(template, override)

    assert pruned["permissions"]["host_guard"] == override["permissions"]["host_guard"]


def test_other_nested_unknown_keys_still_pruned():
    template = {
        "permissions": {
            "enabled": False,
            "guard_actions": {"too_complex": "ask"},
        }
    }
    override = {
        "permissions": {
            "guard_actions": {"too_complex": "allow", "deprecated_key": "x"},
        }
    }

    pruned = _prune_override_keys(template, override)

    assert pruned["permissions"]["guard_actions"] == {"too_complex": "allow"}


def test_always_on_components_kept_explicit_empty_and_missing_template_key():
    """disabled 底线白名单不被清理链剪掉：显式 []（主动关闭底线）在模板有键时
    原样保留；旧模板缺键时显式白名单走 _PERMISSIONS_OVERRIDE_KEEP_KEYS 保键。"""
    template_with_key = {
        "permissions": {"enabled": False, "always_on_components": ["host_guard"]},
    }
    pruned = _prune_override_keys(
        template_with_key,
        {"permissions": {"always_on_components": []}},
    )
    assert pruned["permissions"]["always_on_components"] == []

    template_without_key = {"permissions": {"enabled": False}}
    pruned2 = _prune_override_keys(
        template_without_key,
        {"permissions": {"always_on_components": ["host_guard"]}},
    )
    assert pruned2["permissions"]["always_on_components"] == ["host_guard"]
