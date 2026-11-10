# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""管理面能力开关盖到 YAML 上时，没下发的字段要留下来。"""

from __future__ import annotations

from jiuwenswarm.enterprise.config.effective.apply_agent_capabilities import (
    apply_capability_bodies,
)


def test_a2ui_overlay_keeps_fields_the_manager_did_not_send() -> None:
    merged = apply_capability_bodies(
        {
            "a2ui": {
                "enabled": False,
                "protocol_version": "0.9",
                "stream_validation_enabled": True,
            }
        },
        {"a2ui": {"enabled": True}},
    )
    a2ui = merged["a2ui"]
    assert a2ui["enabled"] is True
    assert a2ui["protocol_version"] == "0.9"
    assert a2ui["stream_validation_enabled"] is True
