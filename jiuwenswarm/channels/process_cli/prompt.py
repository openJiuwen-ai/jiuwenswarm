# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interactive prompt with a Codex-style slash-command index."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style

from jiuwenswarm.channels.process_cli.commands import (
    matching_slash_arguments,
    matching_slash_commands,
)

PROMPT_TEXT = "jiuwenswarm> "
LIVE_INPUT_PROMPT = "输入栏\n补充输入> "
_COMMAND_COLUMN_WIDTH = 22


class SlashCommandCompleter(Completer):
    """Offer local commands only while the first token starts with ``/``."""

    def get_completions(self, document: Document, complete_event):
        prefix = document.current_line_before_cursor
        argument_fragment = prefix.rpartition(" ")[2]
        for argument in matching_slash_arguments(prefix):
            yield Completion(
                argument.value,
                start_position=-len(argument_fragment),
                display=argument.value.ljust(_COMMAND_COLUMN_WIDTH),
                display_meta=argument.description,
            )
        if " " in prefix:
            return
        for command in matching_slash_commands(prefix):
            yield Completion(
                command.name,
                start_position=-len(prefix),
                display=command.name.ljust(_COMMAND_COLUMN_WIDTH),
                display_meta=command.description,
            )


_PROMPT_STYLE = Style.from_dict(
    {
        "completion-menu": "fg:default bg:default",
        "completion-menu.completion": "fg:ansiwhite bg:default",
        "completion-menu.completion.current": ("fg:ansicyan bold noreverse bg:default"),
        "completion-menu.meta.completion": "fg:ansibrightblack bg:default",
        "completion-menu.meta.completion.current": "fg:ansicyan bold bg:default",
        "completion-menu.scrollbar": "bg:default",
        "completion-menu.scrollbar.button": "bg:default",
    }
)
_LIVE_PROMPT_STYLE = Style.from_dict(
    {
        "user-prefix": "fg:ansicyan bold",
        "supplement": "fg:ansicyan",
        "accepted": "fg:ansigreen",
        "rejected": "fg:ansired",
        "unknown": "fg:ansiyellow",
        "assistant": "fg:ansicyan bold",
        "processing": "fg:ansibrightblack",
        "input-prefix": "fg:ansicyan bold",
    }
)


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


def create_prompt_session(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> PromptSession[str] | None:
    """Create the enhanced reader only for a real interactive terminal."""
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    if not (_is_tty(input_stream) and _is_tty(output_stream)):
        return None

    return PromptSession(
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        erase_when_done=True,
        history=InMemoryHistory(),
        reserve_space_for_menu=4,
        style=None if os.getenv("NO_COLOR") is not None else _PROMPT_STYLE,
    )


def create_live_prompt_session() -> PromptSession[str]:
    """Create the parent-owned editor for one active worker turn."""

    session = PromptSession(
        history=InMemoryHistory(),
        erase_when_done=True,
        reserve_space_for_menu=0,
        style=None if os.getenv("NO_COLOR") is not None else _LIVE_PROMPT_STYLE,
    )
    # Keep live redraws out of normal terminal scrollback. On completion the
    # parent prints one complete immutable turn snapshot after this alternate
    # screen is restored.
    session.app.full_screen = True
    return session


async def read_prompt(
    session: PromptSession[str] | None,
    prompt_text: str = PROMPT_TEXT,
) -> str:
    """Read one instruction, preserving pipe/test support without TTY control."""
    if session is None:
        return await asyncio.to_thread(input, prompt_text)
    return await session.prompt_async(prompt_text)


__all__ = [
    "LIVE_INPUT_PROMPT",
    "PROMPT_TEXT",
    "SlashCommandCompleter",
    "create_live_prompt_session",
    "create_prompt_session",
    "read_prompt",
]
