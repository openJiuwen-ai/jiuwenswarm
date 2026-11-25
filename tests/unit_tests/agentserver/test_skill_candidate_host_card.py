# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host card mapping for enterprise skills.source.search UI fields."""

from __future__ import annotations

from jiuwenswarm.extensions.sdk.skill_source import SkillCandidate
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def test_skill_candidate_to_host_card_keeps_spi_display_fields() -> None:
    candidate = SkillCandidate(
        source_id="swarm_skill_hub",
        skill_id="asset-1",
        version_id="ver-1",
        name="demo-skill",
        display_name="Demo Skill",
        short_description="SPI short description",
        version="1.2.0",
        tags=("docs", "pdf"),
        metadata={
            "namespace": "team-space",
            "owner_display_name": "Alice",
            "rating_avg": 4.5,
            "rating_count": 2,
            "download_count": 9,
            "accessible": True,
        },
    )

    card = SkillManager._skill_candidate_to_host_card(candidate)

    assert card["short_description"] == "SPI short description"
    assert card["tags"] == ["docs", "pdf"]
    assert "summary" not in card
    assert "labels" not in card
    assert card["namespace"] == "team-space"
    assert card["owner_display_name"] == "Alice"
    assert card["download_count"] == 9
    assert "downloadable" not in card


def test_skill_candidate_to_host_card_fills_spi_fields_from_metadata() -> None:
    candidate = SkillCandidate(
        source_id="swarm_skill_hub",
        skill_id="asset-2",
        version_id="ver-2",
        name="legacy-shape",
        metadata={
            "summary": "from metadata",
            "labels": [{"name": "legacy"}],
        },
    )

    card = SkillManager._skill_candidate_to_host_card(candidate)

    assert card["short_description"] == "from metadata"
    assert card["tags"] == ["legacy"]
    assert "summary" not in card
    assert "labels" not in card
