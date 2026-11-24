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
