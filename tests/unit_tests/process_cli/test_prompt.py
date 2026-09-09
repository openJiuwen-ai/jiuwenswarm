# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from io import StringIO

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.styles import merge_styles
from prompt_toolkit.styles.defaults import default_ui_style

from jiuwenswarm.channels.process_cli.commands import (
    SLASH_COMMANDS,
    matching_slash_commands,
    resolve_slash_command,
)
from jiuwenswarm.channels.process_cli.prompt import (
    PROMPT_TEXT,
    SlashCommandCompleter,
    _PROMPT_STYLE,
    create_prompt_session,
)


def _completions(text: str):
    document = Document(text=text, cursor_position=len(text))
    event = CompleteEvent(completion_requested=False)
    return list(SlashCommandCompleter().get_completions(document, event))


def test_slash_command_registry_has_unique_canonical_names() -> None:
    names = [command.name for command in SLASH_COMMANDS]

    assert names == ["/help", "/new", "/session", "/exit"]
    assert len(names) == len(set(names))
    assert all(command.description for command in SLASH_COMMANDS)


def test_slash_command_alias_resolves_to_canonical_command() -> None:
    assert resolve_slash_command("/QUIT") == "/exit"
    assert resolve_slash_command("/unknown") is None


def test_slash_prefix_filters_command_index() -> None:
    assert matching_slash_commands("/") == SLASH_COMMANDS
    assert [command.name for command in matching_slash_commands("/se")] == ["/session"]
    assert matching_slash_commands("ask /") == ()
    assert matching_slash_commands("/session now") == ()


def test_completer_displays_all_commands_and_chinese_descriptions_for_slash() -> None:
    completions = _completions("/")

    assert [completion.text for completion in completions] == [
        "/help",
        "/new",
        "/session",
        "/exit",
    ]
    assert [completion.start_position for completion in completions] == [-1] * 4
    assert all(len(completion.display_text) == 22 for completion in completions)
    assert completions[0].display_text.startswith("/help")
    assert [completion.display_meta_text for completion in completions] == [
        "查看所有命令",
        "创建新会话",
        "查看当前会话",
        "退出 JiuwenSwarm",
    ]


def test_completer_updates_matches_while_prefix_is_typed() -> None:
    completions = _completions("/he")

    assert [completion.text for completion in completions] == ["/help"]
    assert completions[0].start_position == -3


def test_prompt_style_removes_default_menu_background_and_reverse() -> None:
    style = merge_styles([default_ui_style(), _PROMPT_STYLE])
    normal = style.get_attrs_for_style_str("class:completion-menu.completion")
    selected = style.get_attrs_for_style_str("class:completion-menu.completion.current")
    description = style.get_attrs_for_style_str("class:completion-menu.meta.completion")

    assert normal.color == "ansiwhite"
    assert normal.bgcolor == "default"
    assert selected.color == "ansicyan"
    assert selected.bgcolor == "default"
    assert selected.bold is True
    assert selected.reverse is False
    assert description.color == "ansibrightblack"
    assert description.bgcolor == "default"


def test_non_tty_uses_plain_input_fallback() -> None:
    assert create_prompt_session(stdin=StringIO(), stdout=StringIO()) is None
    assert PROMPT_TEXT == "jiuwenswarm> "
