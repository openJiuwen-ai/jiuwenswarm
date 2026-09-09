# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local slash-command registry for the process-style CLI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """A command shown by completion and accepted by the local REPL."""

    name: str
    description: str
    aliases: tuple[str, ...] = ()


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "查看所有命令"),
    SlashCommand("/new", "创建新会话"),
    SlashCommand("/session", "查看当前会话"),
    SlashCommand("/exit", "退出 JiuwenSwarm", aliases=("/quit",)),
)

_COMMAND_NAMES = {
    name: command.name
    for command in SLASH_COMMANDS
    for name in (command.name, *command.aliases)
}


def resolve_slash_command(value: str) -> str | None:
    """Return the canonical command name for an exact command or alias."""
    return _COMMAND_NAMES.get(value.strip().lower())


def matching_slash_commands(prefix: str) -> tuple[SlashCommand, ...]:
    """Return canonical commands matching a slash-prefixed input fragment."""
    normalized = prefix.lower()
    if not normalized.startswith("/") or any(
        character.isspace() for character in normalized
    ):
        return ()
    return tuple(
        command for command in SLASH_COMMANDS if command.name.startswith(normalized)
    )


__all__ = [
    "SLASH_COMMANDS",
    "SlashCommand",
    "matching_slash_commands",
    "resolve_slash_command",
]
