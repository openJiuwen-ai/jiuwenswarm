# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The bundled icon set: what ``search_icons`` finds, and what an ``insert_icon`` call sends the task pane."""

from __future__ import annotations

import json

import pytest
from openjiuwen.core.foundation.llm import ToolCall

from jiuwenswarm.office.icons import icon_set


@pytest.mark.parametrize(("query", "first"), [
    ("team", "people_team"),
    ("data pie", "data_pie"),
    ("growth", "arrow_trending"),
    ("团队协作", "people_team"),
    ("物流", "vehicle_truck"),
])
def test_a_search_finds_icons_by_their_name_and_by_english_or_chinese_aliases(query: str, first: str) -> None:
    found = icon_set().search(query, 3)
    assert found[0] == {"icon": first, "styles": ["filled", "regular"]}
    assert len(found) <= 3


def test_a_search_for_nothing_the_set_has_finds_nothing_and_every_alias_names_an_icon() -> None:
    icons = icon_set()
    assert icons.search("xyzzy", 5) == []
    assert set(icons.aliases) <= set(icons.icons)


async def test_an_icon_call_carries_the_icon_s_svg_and_an_unknown_icon_fails_without_pausing(
        conversation, attach_pane) -> None:
    attach_pane("deck-a", ["insert_icon"])
    known, unknown = ({"slide_id": "256#", "icon": icon, "style": "filled", "x": 40, "y": 40}
                      for icon in ("people_team", "no_such_icon"))
    calls = [ToolCall(id=f"icon-{at}", type="function", name="insert_icon", arguments=json.dumps(arguments))
             for at, arguments in enumerate((unknown, known))]

    async with conversation([calls]) as pane:
        [request] = await pane.turn("put a team icon on slide 1")
        assert request["client_tool"]["icon_svg"] == icon_set().svg("people_team", "filled")
        await pane.answer((request, {"content": [{"type": "text", "text": '{"success": true}'}]}))
        assert "find icons with search_icons" in pane.model.tool_results()["icon-0"]
        assert pane.agent.ability_manager.get("search_icons") is not None


async def test_the_icon_search_goes_with_insert_icon_when_the_task_pane_cannot_insert_icons(
        conversation, attach_pane) -> None:
    attach_pane("deck-a", ["read_presentation_context"])

    async with conversation([]) as pane:
        await pane.turn("hello")
        assert pane.agent.ability_manager.get("search_icons") is None
