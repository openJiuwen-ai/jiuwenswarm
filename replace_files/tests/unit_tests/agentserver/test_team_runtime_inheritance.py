# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team runtime inheritance helpers."""

from types import SimpleNamespace

from openjiuwen.core.foundation.tool import ToolCard

from jiuwenclaw.agentserver.team.team_runtime_inheritance import (
    filter_inheritable_ability_cards,
)


class _FakeAbilityManager:
    def __init__(self, abilities):
        self._abilities = abilities

    def list(self):
        return list(self._abilities)


def _make_tool_card(name: str) -> ToolCard:
    return ToolCard(
        id=name,
        name=name,
        description=f"{name} description",
        input_params={"type": "object"},
    )


def test_filter_inheritable_ability_cards_includes_extended_claw_tools():
    main_agent = SimpleNamespace(
        ability_manager=_FakeAbilityManager(
            [
                _make_tool_card("visual_question_answering"),
                _make_tool_card("audio_question_answering"),
                _make_tool_card("audio_metadata"),
                _make_tool_card("user_todos"),
                _make_tool_card("task_tool"),
                _make_tool_card("send_file_to_user"),
            ]
        )
    )

    inherited = filter_inheritable_ability_cards(main_agent)
    inherited_names = {card.name for card in inherited}

    assert "visual_question_answering" in inherited_names
    assert "audio_question_answering" in inherited_names
    assert "audio_metadata" in inherited_names
    assert "user_todos" in inherited_names
    assert "task_tool" in inherited_names
    assert "send_file_to_user" not in inherited_names
