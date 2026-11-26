# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapt the Worker's native text slash-command path to command.run semantics."""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from openjiuwen.core.events import SessionContext

from jiuwenswarm.extensions_v2.event_types.server_run_command import CommandOrigin, ServerCommandError
from jiuwenswarm.server.runtime.commands.run_command_events import (
    NativeCommandResult, dispatch_server_run_command,
)


def command_origin(channel_id: str | None) -> CommandOrigin:
    """Translate a host-established channel without trusting client metadata."""
    channel = str(channel_id or "").strip().lower()
    if channel == "web":
        return CommandOrigin("bridge")
    if channel in {"sdk", "cli", "process_cli", "acp"}:
        return CommandOrigin("sdk")
    if channel in {"cron", "heartbeat"}:
        return CommandOrigin("scheduled-trigger" if channel == "cron" else "task-notification")
    if channel in {"feishu", "welink", "slack", "telegram", "dingtalk", "wechat"}:
        return CommandOrigin("channel", server=channel)
    # A channel name alone cannot attest the human gesture at a terminal.
    return CommandOrigin("unclassified")


async def run_adapter_slash_command(
    adapter: Any, query: Any, session_id: str, mode: str, channel_id: str | None,
    native: Callable[..., Awaitable[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """Keep native payloads private while allowing argument and text policies."""
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus

    match = re.fullmatch(r"/([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?", query.lstrip()) if isinstance(query, str) else None
    bus = get_extension_bus()
    if match is None or bus is None:
        return await native(query, session_id, mode, channel_id=channel_id)
    command, args = match.group(1).casefold(), match.group(2) or ""
    # The original product only treats /goal text as a command on TUI.
    if command == "goal" and str(channel_id or "").lower() != "tui":
        return await native(query, session_id, mode, channel_id=channel_id)
    directory = getattr(adapter, "_project_dir", None)
    cwd = os.path.abspath(directory) if isinstance(directory, str) and directory.strip() else None
    context = SessionContext(session_id, cwd=cwd, metadata={"project_root": cwd} if cwd else {})

    async def execute(rewritten_args: str) -> NativeCommandResult:
        text = "/" + command + (" " + rewritten_args if rewritten_args else "")
        value = await native(text, session_id, mode, channel_id=channel_id)
        if value is None:
            raise ServerCommandError("COMMAND_UNKNOWN")
        output = value.get("output")
        return NativeCommandResult(value, output if isinstance(output, str) else None)

    async def record_context(notes: tuple[str, ...]) -> None:
        await append_command_context(adapter, session_id, notes)

    execution = await dispatch_server_run_command(
        bus, context, command=command, args=args, origin=command_origin(channel_id),
        execute=execute, record_context=record_context,
    )
    if execution.use_native:
        return execution.native
    if isinstance(execution.native, dict):
        # A text replacement cannot undo a panel, approval, goal or follow-up.
        result = copy.deepcopy(execution.native)
        if isinstance(result.get("output"), str) and execution.output.text is not None:
            result["output"] = execution.output.text
        return result
    return {"result_type": "text", "output": execution.output.text or "",
            "source": "extension", "slash_command": command, "display_level": "info"}


async def append_command_context(adapter: Any, session_id: str, notes: tuple[str, ...]) -> None:
    """Persist hidden user messages through the existing ContextEngine/Session."""
    from openjiuwen.core.foundation.llm import UserMessage
    from openjiuwen.core.single_agent import create_agent_session
    from jiuwenswarm.agents.harness.common.session_ops_service import resolve_live_agent_session
    from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_runtime import get_kv_cache_runtime

    instance = getattr(adapter, "_instance", None)
    react = getattr(instance, "react_agent", None)
    engine = getattr(react, "context_engine", None)
    if engine is None:
        raise ServerCommandError("COMMAND_CONTEXT_UNAVAILABLE")
    context = engine.get_context(session_id=session_id)
    session = resolve_live_agent_session(instance, session_id)
    session = session or (context.get_session_ref() if context is not None else None)
    if session is None:
        session = create_agent_session(
            session_id=session_id, card=getattr(instance, "card", None),
            kv_cache_runtime=get_kv_cache_runtime(),
        )
        await session.pre_run(inputs={})
    if context is None:
        config = getattr(react, "_config", None)
        context = await engine.create_context(session=session, processors=getattr(config, "context_processors", None))
    await context.add_messages([UserMessage(content=note) for note in notes])
    await engine.save_contexts(session)
    await session.commit()
