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

from jiuwenswarm.channels.process_cli.commands import matching_slash_commands

PROMPT_TEXT = "jiuwenswarm> "
_COMMAND_COLUMN_WIDTH = 22


class SlashCommandCompleter(Completer):
    """Offer local commands only while the first token starts with ``/``."""

    def get_completions(self, document: Document, complete_event):
        prefix = document.current_line_before_cursor
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
        history=InMemoryHistory(),
        reserve_space_for_menu=4,
        style=None if os.getenv("NO_COLOR") is not None else _PROMPT_STYLE,
    )


async def read_prompt(session: PromptSession[str] | None) -> str:
    """Read one instruction, preserving pipe/test support without TTY control."""
    if session is None:
        return await asyncio.to_thread(input, PROMPT_TEXT)
    return await session.prompt_async(PROMPT_TEXT)


__all__ = [
    "PROMPT_TEXT",
    "SlashCommandCompleter",
    "create_prompt_session",
    "read_prompt",
]
