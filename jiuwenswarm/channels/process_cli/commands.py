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


@dataclass(frozen=True, slots=True)
class ParsedSlashCommand:
    """One recognized local command and its unparsed argument text."""

    name: str
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class SlashCommandArgument:
    """One argument offered by slash-command completion."""

    value: str
    description: str


MODE_ARGUMENTS: tuple[SlashCommandArgument, ...] = (
    SlashCommandArgument("agent.work", "单 Agent 通用模式"),
    SlashCommandArgument("agent.code", "单 Agent 代码模式"),
    SlashCommandArgument("team.work", "Team 通用模式"),
    SlashCommandArgument("team.code", "Team 代码模式"),
)

_MODE_TARGETS = {
    "agent": "agent.work.normal",
    "code": "agent.code.normal",
    "team": "team.work.normal",
    "team.normal": "team.work.normal",
    "code.normal": "agent.code.normal",
    "code.team": "team.code.normal",
    **{
        alias: f"{alias}.normal"
        for alias in (argument.value for argument in MODE_ARGUMENTS)
    },
    **{
        f"{argument.value}.normal": f"{argument.value}.normal"
        for argument in MODE_ARGUMENTS
    },
}

_SKILLS_ARGUMENTS: tuple[SlashCommandArgument, ...] = (
    SlashCommandArgument("list", "列出当前可用技能"),
)

_NEW_ARGUMENTS: tuple[SlashCommandArgument, ...] = (
    SlashCommandArgument("--persist", "为新会话启用持久记忆"),
    SlashCommandArgument("--persist-session", "为新会话启用持久记忆"),
)


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "查看所有命令"),
    SlashCommand("/mode", "查看或切换运行模式"),
    SlashCommand("/status", "查看当前状态"),
    SlashCommand("/skills", "查看可用技能"),
    SlashCommand("/new", "创建并切换到新会话"),
    SlashCommand("/resume", "按 ID 恢复会话", aliases=("/continue",)),
    SlashCommand("/branch", "从当前会话创建分支", aliases=("/fork",)),
    SlashCommand("/delete", "删除指定会话"),
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


def parse_slash_command(value: str) -> ParsedSlashCommand | None:
    """Parse a recognized local command without interpreting its arguments."""
    parts = value.strip().split(maxsplit=1)
    if not parts:
        return None
    name = _COMMAND_NAMES.get(parts[0].lower())
    if name is None:
        return None
    return ParsedSlashCommand(
        name=name,
        arguments=parts[1].strip() if len(parts) > 1 else "",
    )


def resolve_mode_target(value: str) -> str | None:
    """Resolve one non-plan Process CLI mode to Runtime's canonical value."""
    return _MODE_TARGETS.get(value.strip().lower())


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


def matching_slash_arguments(prefix: str) -> tuple[SlashCommandArgument, ...]:
    """Return argument suggestions for the first local command argument."""
    normalized = prefix.lower()
    command_name, separator, fragment = normalized.partition(" ")
    if not separator or any(character.isspace() for character in fragment):
        return ()
    if command_name == "/mode":
        options = MODE_ARGUMENTS
    elif command_name == "/skills":
        options = _SKILLS_ARGUMENTS
    elif command_name == "/new":
        options = _NEW_ARGUMENTS
    else:
        return ()
    return tuple(option for option in options if option.value.startswith(fragment))


__all__ = [
    "MODE_ARGUMENTS",
    "ParsedSlashCommand",
    "SLASH_COMMANDS",
    "SlashCommand",
    "SlashCommandArgument",
    "matching_slash_arguments",
    "matching_slash_commands",
    "parse_slash_command",
    "resolve_mode_target",
    "resolve_slash_command",
]
