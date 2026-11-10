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


def test_a4p_overlay_sets_the_confirm_switch() -> None:
    merged = apply_capability_bodies(
        {"a4p": {"enabled": False}},
        {"a4p": {"enabled": True}},
    )
    assert merged["a4p"]["enabled"] is True


def test_symphony_overlay_keeps_local_paths() -> None:
    merged = apply_capability_bodies(
        {
            "symphony": {
                "enabled": False,
                "paths": {"skills_root": "/data/skills", "graph_dir": "/data/graph"},
            }
        },
        {
            "symphony": {
                "enabled": True,
                "skill_retrieval": {"enabled": True},
            }
        },
    )
    symphony = merged["symphony"]
    assert symphony["enabled"] is True
    assert symphony["paths"]["skills_root"] == "/data/skills"
    assert symphony["paths"]["graph_dir"] == "/data/graph"
    assert symphony["skill_retrieval"]["enabled"] is True


def test_auto_memory_sets_the_top_level_switch() -> None:
    merged = apply_capability_bodies(
        {"auto_memory_enabled": False},
        {"auto_memory": {"enabled": True}},
    )
    assert merged["auto_memory_enabled"] is True


def test_missing_section_is_created_from_the_overlay() -> None:
    merged = apply_capability_bodies(
        {},
        {"proactive_recommendation": {"enabled": True, "max_recommend_per_day": 3}},
    )
    assert merged["proactive_recommendation"]["enabled"] is True
    assert merged["proactive_recommendation"]["max_recommend_per_day"] == 3
