# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application lifecycle for the process-style CLI."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.render import EventRenderer
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent

if TYPE_CHECKING:
    import argparse

CHANNEL_ID = "process_cli"
INTERRUPT_RESUME_SOURCES = frozenset(
    {
        "confirm_interrupt",
        "permission_interrupt",
        "ask_user_interrupt",
        "evolution_interrupt",
    }
)
INTERACTION_EVENTS = frozenset({"chat.ask_user_question", "plan.approval_required"})
SHUTDOWN_STEP_TIMEOUT_SECONDS = 5.0
INTERACTIVE_INPUT_REQUIRED = (
    "process CLI received an interaction request but interactive input is unavailable"
)


def _new_request_id(prefix: str = "cli") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _build_request(
    args: argparse.Namespace,
    *,
    session_id: str,
    request_id: str,
) -> AgentRequest:
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    project_dir = str(Path(args.project_dir or cwd).resolve())
    trusted_dirs = [str(Path(path).resolve()) for path in args.trusted_dir]
    if not trusted_dirs:
        trusted_dirs = [project_dir]
    return AgentRequest(
        request_id=request_id,
        channel_id=CHANNEL_ID,
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        timestamp=time.time(),
        params={
            "query": args.prompt,
            "content": args.prompt,
            "mode": args.mode,
            "work_mode": args.work_mode,
            "cwd": cwd,
            "project_dir": project_dir,
            "trusted_dirs": trusted_dirs,
            "supports_user_interaction": (
                bool(getattr(args, "_interactive_worker", False))
                or (args.output == "human" and sys.stdin.isatty())
            ),
        },
    )


def _interaction_answer(
    payload: dict[str, Any],
    stream: TextIO,
) -> tuple[str, list[dict[str, Any]]]:
    prompt = str(payload.get("question") or payload.get("message") or "需要输入")
    stream.write(f"\n? {prompt}\n")
    options = [item for item in payload.get("options", []) if isinstance(item, dict)]
    for index, option in enumerate(options, 1):
        label = option.get("label") or option.get("value") or "?"
        description = option.get("description") or ""
        suffix = f" — {description}" if description else ""
        stream.write(f"  {index}. {label}{suffix}\n")
    stream.write("请输入选项或自定义内容：")
    stream.flush()
    answer = sys.stdin.readline().strip()
    selected = answer
    if options and answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < len(options):
            selected = str(
                options[index].get("value") or options[index].get("label") or answer
            )
    return answer, [{"selected_options": [selected], "custom_input": answer}]


def _answer_request(
    original: AgentRequest,
    interaction: RuntimeEvent,
    answers: list[dict[str, Any]],
) -> tuple[AgentRequest, bool]:
    payload = interaction.payload or {}
    source = str(payload.get("source") or "")
    interaction_request_id = str(payload.get("request_id") or "")
    resume = source in INTERRUPT_RESUME_SOURCES and bool(interaction_request_id)
    params = {
        "session_id": original.session_id,
        "request_id": interaction_request_id,
        "answers": answers,
        "source": source,
        "mode": original.params.get("mode"),
        "work_mode": original.params.get("work_mode"),
        "project_dir": original.params.get("project_dir"),
        "cwd": original.params.get("cwd"),
        "trusted_dirs": original.params.get("trusted_dirs", []),
        "supports_user_interaction": True,
        "query": "" if resume else None,
    }
    return (
        AgentRequest(
            request_id=_new_request_id("answer"),
            channel_id=original.channel_id,
            session_id=original.session_id,
            req_method=ReqMethod.CHAT_SEND if resume else ReqMethod.CHAT_ANSWER,
            is_stream=resume,
            timestamp=time.time(),
            params=params,
        ),
        resume,
    )


def _cancel_request(original: AgentRequest) -> AgentRequest:
    return AgentRequest(
        # CHAT_CANCEL identifies the in-flight Runtime request itself; a new
        # transport-style correlation id would lose that precise target.
        request_id=original.request_id,
        channel_id=original.channel_id,
        session_id=original.session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        timestamp=time.time(),
        params={
            "intent": "cancel",
            "target_request_id": original.request_id,
            "mode": original.params.get("mode"),
            "work_mode": original.params.get("work_mode"),
            "project_dir": original.params.get("project_dir"),
        },
    )


async def _consume(
    client: InProcessRuntimeClient,
    request: AgentRequest,
    renderer: EventRenderer,
    *,
    interactive: bool,
) -> int:
    async def handle_interaction(
        original_request: AgentRequest,
        interaction: RuntimeEvent,
    ) -> int:
        if not interactive:
            renderer.render(
                RuntimeEvent.error(
                    request_id=interaction.request_id or original_request.request_id,
                    channel_id=original_request.channel_id,
                    session_id=original_request.session_id,
                    error=RuntimeError(INTERACTIVE_INPUT_REQUIRED),
                )
            )
            return 4

        renderer.prepare_interaction()
        _answer, answers = await asyncio.to_thread(
            _interaction_answer,
            interaction.payload or {},
            renderer.stdout,
        )
        answer_request, resumes_stream = _answer_request(
            original_request,
            interaction,
            answers,
        )
        if resumes_stream:
            return await _consume(
                client,
                answer_request,
                renderer,
                interactive=interactive,
            )

        for answer_event in await client.answer_interaction(answer_request):
            renderer.render(answer_event)
            if answer_event.event_type in INTERACTION_EVENTS:
                nested = await handle_interaction(answer_request, answer_event)
                if nested != 0:
                    return nested
        return 1 if renderer.failed else 0

    async for event in client.stream(request):
        renderer.render(event)
        if event.event_type in INTERACTION_EVENTS:
            nested = await handle_interaction(request, event)
            if nested != 0:
                return nested
    return 1 if renderer.failed else 0


async def run(
    args: argparse.Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Own exactly one Runtime lifecycle for one CLI command."""
    client = InProcessRuntimeClient()
    request: AgentRequest | None = None
    session_id = ""
    request_id = _new_request_id()
    renderer = EventRenderer(
        args.output,
        stdout=stdout,
        stderr=stderr,
        show_reasoning=args.show_reasoning,
        show_tools=args.show_tools,
    )
    renderer.start()

    async def execute() -> int:
        nonlocal request, session_id
        await client.start()
        session_id = await client.create_or_resume_session(
            channel_id=CHANNEL_ID,
            session_id=args.session,
        )
        session_result_file = getattr(args, "_session_result_file", None)
        if session_result_file:
            Path(session_result_file).write_text(session_id, encoding="utf-8")
        request = _build_request(
            args,
            session_id=session_id,
            request_id=request_id,
        )
        renderer.working()
        interactive = bool(getattr(args, "_interactive_worker", False)) or (
            args.output == "human" and sys.stdin.isatty()
        )
        return await _consume(
            client,
            request,
            renderer,
            interactive=interactive,
        )

    try:
        if args.timeout is not None:
            async with asyncio.timeout(args.timeout):
                result = await execute()
        else:
            result = await execute()
        renderer.finish(session_id=session_id, request_id=request_id)
        return result
    except TimeoutError:
        if request is not None:
            await _bounded_cleanup(client.cancel(_cancel_request(request)))
        renderer.render(
            RuntimeEvent.error(
                request_id=request_id,
                channel_id=CHANNEL_ID,
                session_id=session_id or None,
                error=TimeoutError("process CLI execution timed out"),
            )
        )
        renderer.finish(session_id=session_id, request_id=request_id)
        return 124
    except asyncio.CancelledError:
        if request is not None:
            await _bounded_cleanup(client.cancel(_cancel_request(request)))
        renderer.interrupted()
        raise
    except Exception as exc:  # noqa: BLE001 - CLI converts failures to events
        renderer.render(
            RuntimeEvent.error(
                request_id=request_id,
                channel_id=CHANNEL_ID,
                session_id=session_id or None,
                error=exc,
            )
        )
        renderer.finish(session_id=session_id, request_id=request_id)
        return 1
    finally:
        try:
            if session_id:
                await _bounded_cleanup(
                    client.cleanup_session(
                        channel_id=CHANNEL_ID,
                        session_id=session_id,
                    )
                )
        finally:
            # Runtime close must run even when session cleanup itself is
            # cancelled.  The original cancellation still propagates after
            # this finally block; only resource ownership is made reliable.
            await _bounded_cleanup(client.close())


async def _bounded_cleanup(awaitable: Any) -> None:
    """Bound every cleanup step so process exit cannot hang indefinitely."""
    try:
        await asyncio.wait_for(awaitable, timeout=SHUTDOWN_STEP_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001
        return


__all__ = ["CHANNEL_ID", "run"]
