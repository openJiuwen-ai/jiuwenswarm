# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The design profile's tool specs and the leader sanitizer keep-list stay paired.

A tool declared in ``_DESIGN_TOOL_NAMES`` but missing from the sanitizer's
``_KEEP_TOOL_NAMES`` is silently stripped off the leader's surface — the
spec and the keep-list must move together.
"""

from jiuwenswarm.agents.swarm import config_specs
from jiuwenswarm.agents.harness.design.rails.designer_surface_sanitizer_rail import (
    _KEEP_TOOL_NAMES,
)

# Spec element name -> the tool card names it expands to on the surface.
_DESIGN_TOOL_CARDS = {
    config_specs.registry.WEB_SEARCH: ("free_search",),
    config_specs.registry.WEB_FETCH: ("fetch_webpage",),
    config_specs.registry.WEB_PAID_SEARCH: ("paid_search",),
    config_specs.registry.VISION: ("image_ocr", "visual_question_answering"),
    config_specs.registry.AUDIO: (
        "audio_transcription",
        "audio_question_answering",
        "audio_metadata",
    ),
}


def test_design_profile_declares_web_lookup_and_keeps_session_rails():
    rails, _tools = config_specs.build_member_capability_specs(
        {}, "team.design", "teammate"
    )
    rail_types = {spec.type for spec in rails}
    # Web lookup is in (leader-only tuple; mirror #8874 split it out of the
    # worker set); the chat-only rails (avatar / heartbeat /
    # personal-context) are out.
    assert config_specs.registry.WEB_SEARCH in config_specs._DESIGN_LEADER_TOOL_NAMES
    assert config_specs.registry.WEB_FETCH in config_specs._DESIGN_LEADER_TOOL_NAMES
    assert config_specs.registry.WEB_PAID_SEARCH in config_specs._DESIGN_LEADER_TOOL_NAMES
    assert config_specs.registry.WEB_SEARCH not in config_specs._DESIGN_TOOL_NAMES
    assert config_specs.registry.AVATAR_PROMPT not in rail_types
    assert config_specs.registry.HEARTBEAT not in rail_types
    assert config_specs.registry.PERSONAL_CONTEXT not in rail_types
    # Workers keep the full sys_operation surface (file read/write for node work).
    assert config_specs.registry.SYS_OPERATION in rail_types


def test_every_design_profile_tool_card_survives_the_leader_sanitizer():
    for cards in _DESIGN_TOOL_CARDS.values():
        for card in cards:
            assert card in _KEEP_TOOL_NAMES, (
                f"design profile tool card {card!r} is missing from the "
                "sanitizer keep-list and would be stripped off the leader"
            )
