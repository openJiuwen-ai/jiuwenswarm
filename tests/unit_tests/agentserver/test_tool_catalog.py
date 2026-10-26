# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from unittest.mock import patch

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.server.runtime.tool_catalog import (
    get_registered_tools_catalog,
    resolve_short_description,
    short_description_from_description,
    tool_catalog_entry_from_card,
)
from jiuwenswarm.server.runtime import tool_catalog


class _FakeAbilityManager:
    def __init__(self, cards: list[ToolCard]) -> None:
        self._cards = cards

    def list(self) -> list[ToolCard]:
        return list(self._cards)


def test_short_description_from_description_first_sentence_english() -> None:
    text = short_description_from_description("Fetch data from an API.\nMore details.")
    assert text == "Fetch data from an API."


def test_short_description_from_description_bilingual_paragraphs() -> None:
    text = short_description_from_description(
        "在会话工作目录下执行 Shell 命令。更多说明在此。\n"
        "Execute commands in the session workspace. More details here."
    )
    assert text == "在会话工作目录下执行 Shell 命令。 Execute commands in the session workspace."


def test_resolve_short_description_uses_description_not_tool_name() -> None:
    short = resolve_short_description("bash", "Long model-facing description for bash execution.")
    assert short == "Long model-facing description for bash execution."


def test_tool_catalog_entry_uses_description_not_cached_properties() -> None:
    long_short = "向用户展示一组带选项的结构化问题" * 5
    card = ToolCard(
        name="ask_user_question",
        description="向用户展示选项并等待选择。Show options to the user.",
        properties={"short_description": long_short},
    )
    entry = tool_catalog_entry_from_card(card)
    assert entry["short_description"] == "向用户展示选项并等待选择。 Show options to the user."
    assert len(entry["short_description"]) <= 100


ASK_USER_QUESTION_DESCRIPTION = (
    "向用户展示一组带选项的结构化问题（可多题），并阻塞等待用户选择。仅当引导/交互已开启时推送选择 UI；"
    "未开启时不推送 chat.delta（由对话流展示），由用户在下一条消息中自由回复。"
    "questions 为 JSON 数组，每项含 question、options[{label, description?, id?}]、可选 header、multi_select；"
    "可选 preview{text,title?,format?,editable?,outline_ref?,meta?} 用于大纲等 Markdown 审阅"
    "（将注入 outline_confirm / outline_use_edited 选项）。"
)


RELOAD_CONTEXT_DESCRIPTION = (
    "Retrieve messages that were previously offloaded from the context window."
    "Provide the exact handle and storage type returned when the content was offloaded;"
    "the tool will fetch the complete original message list and inject it back into the conversation, "
    "allowing the model to see the full text as if it had never been removed."
)


def test_short_description_splits_period_before_uppercase_without_space() -> None:
    short = short_description_from_description(RELOAD_CONTEXT_DESCRIPTION)
    assert short == "Retrieve messages that were previously offloaded from the context window."
    assert "Provide" not in short
    assert not short.endswith("...")


def test_short_description_ignores_schema_optional_markers() -> None:
    short = short_description_from_description(ASK_USER_QUESTION_DESCRIPTION)
    assert short == "向用户展示一组带选项的结构化问题（可多题），并阻塞等待用户选择。"
    assert ", id?" not in short


def test_resolve_short_description_truncates_over_100_chars() -> None:
    long_line = "A" * 200
    short = resolve_short_description("unknown_tool_x", long_line)
    assert len(short) == 100
    assert short.endswith("...")


def test_get_registered_tools_catalog_dedupes_by_name() -> None:
    mgr = _FakeAbilityManager(
        [
            ToolCard(name="bash", description="a"),
            ToolCard(name="bash", description="b"),
            ToolCard(name="read_file", description="read"),
        ],
    )
    tools = get_registered_tools_catalog(mgr)
    assert len(tools) == 2
    names = [t["name"] for t in tools]
    assert names == ["bash", "read_file"]


def test_jiuwenswarm_get_registered_tools_catalog_member() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    class _DeepInstance:
        ability_manager = _FakeAbilityManager(
            [ToolCard(name="grep", description="Search in files.")],
        )

    swarm = JiuWenSwarm()
    with patch.object(swarm, "get_instance", return_value=_DeepInstance()):
        tools = swarm.get_registered_tools_catalog()
    assert len(tools) == 1
    assert tools[0]["name"] == "grep"
    assert tools[0]["short_description"] == "Search in files."


def test_stable_tools_catalog_includes_builtin_and_team_tools() -> None:
    get_stable_tools_catalog = getattr(tool_catalog, "get_stable_tools_catalog", None)
    assert callable(get_stable_tools_catalog)

    catalog = get_stable_tools_catalog("en")

    assert catalog["bash"]["description"]
    assert catalog["spawn_external_cli"]["description"]
    assert catalog["async_task_cancel"]["description"]
