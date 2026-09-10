# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application lifecycle for the process-style CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.display_context import resolve_cli_work_mode
from jiuwenswarm.channels.process_cli.render import EventRenderer
from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import (
    SessionCreateInput,
    SessionDescriptor,
    SessionForkInput,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
    SessionSwitchInput,
)
from jiuwenswarm.runtime.events import RuntimeEvent

if TYPE_CHECKING:
    import argparse

CHANNEL_ID = "process_cli"
CHAT_OPERATION = "chat"
SKILLS_LIST_OPERATION = "skills.list"
SESSION_CREATE_OPERATION = "session.create"
SESSION_SWITCH_OPERATION = "session.switch"
SESSION_FORK_OPERATION = "session.fork"
SESSION_DELETE_OPERATION = "session.delete"
SESSION_OPERATIONS = frozenset(
    {
        SESSION_CREATE_OPERATION,
        SESSION_SWITCH_OPERATION,
        SESSION_FORK_OPERATION,
        SESSION_DELETE_OPERATION,
    }
)
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
logger = logging.getLogger(__name__)


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
    work_mode = resolve_cli_work_mode(args.mode, args.work_mode)
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
            "work_mode": work_mode,
            "cwd": cwd,
            "project_dir": project_dir,
            "trusted_dirs": trusted_dirs,
            "supports_user_interaction": (
                bool(getattr(args, "_interactive_worker", False))
                or (args.output == "human" and sys.stdin.isatty())
            ),
        },
    )


def _build_skills_list_request(
    args: argparse.Namespace,
    *,
    request_id: str,
) -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id=CHANNEL_ID,
        session_id=args.session,
        req_method=ReqMethod.SKILLS_LIST,
        is_stream=False,
        timestamp=time.time(),
        params={},
    )


def _resolved_workspace(args: argparse.Namespace) -> tuple[str, str]:
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    project_dir = str(Path(args.project_dir or cwd).resolve())
    return cwd, project_dir


def _write_worker_result(
    args: argparse.Namespace,
    *,
    operation: str,
    session_id: str,
    mode: str,
    work_mode: str,
    project_dir: str = "",
) -> None:
    """Publish committed worker state to the parent REPL without a transport."""
    session_result_file = getattr(args, "_session_result_file", None)
    if session_result_file:
        Path(session_result_file).write_text(session_id, encoding="utf-8")
    worker_result_file = getattr(args, "_worker_result_file", None)
    if worker_result_file:
        Path(worker_result_file).write_text(
            json.dumps(
                {
                    "operation": operation,
                    "session_id": session_id,
                    "mode": mode,
                    "work_mode": work_mode,
                    "project_dir": project_dir,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def _render_session_event(
    renderer: EventRenderer,
    *,
    request_id: str,
    session_id: str,
    payload: dict[str, Any],
) -> None:
    renderer.render(
        RuntimeEvent.control(
            request_id=request_id,
            channel_id=CHANNEL_ID,
            session_id=session_id,
            payload=payload,
        )
    )


async def _abort_prepared_session(
    client: InProcessRuntimeClient,
    prepared: Any,
    primary_error: BaseException | None,
) -> None:
    if prepared is None or prepared.state is not SessionProvisionState.PREPARED:
        return
    try:
        await client.abort_session_provision(prepared)
    except asyncio.CancelledError:
        if primary_error is None:
            raise
        logger.warning(
            "process CLI Session abort was cancelled while preserving %s",
            type(primary_error).__name__,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the primary operation error
        logger.warning("process CLI Session abort failed: %s", exc)


async def _owned_session_descriptor(
    client: InProcessRuntimeClient,
    session_id: str,
) -> SessionDescriptor | None:
    target = str(session_id or "").strip()
    if not target:
        return None
    descriptor = await client.describe_session(session_id=target)
    if descriptor is None:
        return None
    if descriptor.channel_id.strip().lower() != CHANNEL_ID:
        return None
    return descriptor


def _descriptor_work_mode(descriptor: SessionDescriptor, *, fallback: str) -> str:
    return resolve_cli_work_mode(
        descriptor.mode,
        descriptor.work_mode or fallback,
    )


async def _create_session(
    client: InProcessRuntimeClient,
    args: argparse.Namespace,
    renderer: EventRenderer,
    *,
    request_id: str,
) -> str:
    arguments = str(args.prompt or "").strip().lower()
    if arguments not in {"", "--persist", "--persist-session"}:
        raise SessionProvisionError(
            "usage: /new [--persist|--persist-session]",
            code="BAD_REQUEST",
        )
    cwd, project_dir = _resolved_workspace(args)
    previous = await _owned_session_descriptor(
        client,
        str(args.session or ""),
    )
    prepared = None
    try:
        prepared = await client.prepare_session_create(
            SessionCreateInput(
                channel_id=CHANNEL_ID,
                previous_session_id=(previous.session_id if previous else ""),
                create_token=f"process-cli:{request_id}",
                persist_session=bool(arguments),
                persist_session_supplied=bool(arguments),
                mode=args.mode,
                previous_mode=(previous.mode or None) if previous else None,
                is_swarm=is_team_mode(args.mode),
                team_hint=is_team_mode(args.mode),
                # Process CLI workspaces are intentionally projectless: cwd
                # remains the command's execution location, while a registered
                # Project binding is not fabricated merely from a filesystem
                # path.  The following chat request still carries project_dir.
                project_dir="",
                cwd=cwd,
                work_mode=args.work_mode,
            )
        )
        result = prepared.result
        payload = {
            "event_type": "session.created",
            "session_id": result.session_id,
            "mode": result.canonical_mode,
            "work_mode": result.work_mode,
            "project_dir": result.project_dir,
            "persist_session": result.persist_session,
            "prewarm_hit": result.prewarm_hit,
            "prewarm_status": result.prewarm_status,
            "created": result.created,
        }
        # Create's established contract delivers success before its deferred
        # KVC commit.  Both terminal output and the parent result file are
        # flushed before invoking the AFTER_RESULT_DELIVERY finalizer.
        _render_session_event(
            renderer,
            request_id=request_id,
            session_id=result.session_id,
            payload=payload,
        )
        _write_worker_result(
            args,
            operation=SESSION_CREATE_OPERATION,
            session_id=result.session_id,
            mode=result.canonical_mode,
            work_mode=result.work_mode,
            project_dir=result.project_dir or project_dir,
        )
        try:
            await client.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
                context=SessionProvisionCommitContext(
                    foreground_scope_id=f"process-cli:{os.getpid()}:{request_id}"
                ),
            )
        except asyncio.CancelledError as exc:
            # The successful result is already visible to the parent REPL.
            # Match AgentServer's post-delivery contract: do not emit a second,
            # contradictory failure.  The finally block aborts only if Runtime
            # never entered its terminal commit state.
            logger.warning(
                "process CLI session.create post-delivery commit cancelled: %s",
                exc,
            )
        except Exception as exc:  # noqa: BLE001 - success is already delivered
            logger.warning(
                "process CLI session.create post-delivery commit failed: %s",
                exc,
            )
        return result.session_id
    finally:
        # A cancellation can arrive after the local result was flushed but
        # before commit enters Runtime.  Abort any still-PREPARED lease so
        # Runtime close never inherits an unfinished create transaction.
        await _abort_prepared_session(client, prepared, sys.exception())


async def _switch_session(
    client: InProcessRuntimeClient,
    args: argparse.Namespace,
    renderer: EventRenderer,
    *,
    request_id: str,
) -> str:
    target = str(args.prompt or "").strip()
    if not target:
        raise SessionProvisionError("session_id is required", code="BAD_REQUEST")
    target_descriptor = await _owned_session_descriptor(client, target)
    if target_descriptor is None:
        raise SessionProvisionError("session not found", code="NOT_FOUND")
    previous = await _owned_session_descriptor(
        client,
        str(args.session or ""),
    )
    target_mode = target_descriptor.mode or args.mode
    target_work_mode = _descriptor_work_mode(
        target_descriptor,
        fallback=args.work_mode,
    )

    prepared = None
    try:
        prepared = await client.prepare_session_switch(
            SessionSwitchInput(
                channel_id=CHANNEL_ID,
                target_session_id=target,
                previous_session_id=(previous.session_id if previous else ""),
                mode=target_mode,
                previous_mode=(previous.mode or None) if previous else None,
                team_hint=is_team_mode(target_mode),
            )
        )
        result = await client.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
            context=SessionProvisionCommitContext(
                foreground_scope_id=f"process-cli:{os.getpid()}:{request_id}"
            ),
        )
        _write_worker_result(
            args,
            operation=SESSION_SWITCH_OPERATION,
            session_id=result.session_id,
            mode=target_mode,
            work_mode=target_work_mode,
            project_dir=target_descriptor.project_dir,
        )
        _render_session_event(
            renderer,
            request_id=request_id,
            session_id=result.session_id,
            payload={
                "event_type": "session.switched",
                "session_id": result.session_id,
                "mode": target_mode,
                "switched": result.switched,
            },
        )
        return result.session_id
    finally:
        await _abort_prepared_session(client, prepared, sys.exception())


async def _fork_session(
    client: InProcessRuntimeClient,
    args: argparse.Namespace,
    renderer: EventRenderer,
    *,
    request_id: str,
) -> str:
    source = str(args.session or "").strip()
    if not source:
        raise SessionProvisionError(
            "no current session to branch",
            code="BAD_REQUEST",
        )
    source_descriptor = await _owned_session_descriptor(client, source)
    if source_descriptor is None:
        raise SessionProvisionError("source session not found", code="NOT_FOUND")

    prepared = None
    try:
        prepared = await client.prepare_session_fork(
            SessionForkInput(
                channel_id=CHANNEL_ID,
                source_session_id=source,
                title=str(args.prompt or "").strip(),
            )
        )
        result = await client.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )
        _write_worker_result(
            args,
            operation=SESSION_FORK_OPERATION,
            session_id=result.session_id,
            mode=source_descriptor.mode or args.mode,
            work_mode=_descriptor_work_mode(
                source_descriptor,
                fallback=args.work_mode,
            ),
            project_dir=source_descriptor.project_dir,
        )
        _render_session_event(
            renderer,
            request_id=request_id,
            session_id=result.session_id,
            payload={
                "event_type": "session.forked",
                "source_session_id": result.source_session_id,
                "session_id": result.session_id,
                "title": result.title,
            },
        )
        return result.session_id
    finally:
        await _abort_prepared_session(client, prepared, sys.exception())


async def _delete_session(
    client: InProcessRuntimeClient,
    args: argparse.Namespace,
    renderer: EventRenderer,
    *,
    request_id: str,
) -> str:
    target = str(args.prompt or "").strip()
    if not target:
        raise SessionProvisionError("session_id is required", code="BAD_REQUEST")
    if await _owned_session_descriptor(client, target) is None:
        raise SessionProvisionError("session not found", code="NOT_FOUND")
    result = await client.delete_session(
        channel_id=CHANNEL_ID,
        session_id=target,
    )
    if not result.ok:
        raise SessionProvisionError(
            result.error_message or "session delete failed",
            code=result.error_code,
        )
    _write_worker_result(
        args,
        operation=SESSION_DELETE_OPERATION,
        session_id=str(args.session or "").strip(),
        mode=args.mode,
        work_mode=args.work_mode,
        project_dir=str(args.project_dir or ""),
    )
    _render_session_event(
        renderer,
        request_id=request_id,
        session_id=target,
        payload={"event_type": "session.deleted", "session_id": target},
    )
    return str(args.session or "").strip()


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


def _is_terminal_team_event(
    request: AgentRequest,
    event: RuntimeEvent,
) -> bool:
    """Return whether a persistent Team stream completed the current round."""
    params = request.params if isinstance(request.params, dict) else {}
    payload = event.payload if isinstance(event.payload, dict) else {}
    return (
        is_team_mode(params.get("mode"))
        and event.event_type == "chat.processing_status"
        and payload.get("is_processing") is False
        and payload.get("is_complete") is True
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

    events = client.stream(request)
    try:
        async for event in events:
            renderer.render(event)
            if event.event_type in INTERACTION_EVENTS:
                nested = await handle_interaction(request, event)
                if nested != 0:
                    return nested
            if _is_terminal_team_event(request, event):
                return 1 if renderer.failed else 0
        return 1 if renderer.failed else 0
    finally:
        close_stream = getattr(events, "aclose", None)
        if callable(close_stream):
            await _bounded_cleanup(close_stream())


async def _invoke_skills_list(
    client: InProcessRuntimeClient,
    args: argparse.Namespace,
    renderer: EventRenderer,
    *,
    request_id: str,
) -> tuple[int, AgentRequest, str]:
    """Execute the stateless skills query without provisioning a Session."""
    session_id = str(args.session or "")
    request = _build_skills_list_request(args, request_id=request_id)
    renderer.working()
    for event in await client.invoke(request):
        renderer.render(event, view=SKILLS_LIST_OPERATION)
    return (1 if renderer.failed else 0), request, session_id


async def run(
    args: argparse.Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Own exactly one Runtime lifecycle for one CLI command."""
    args.work_mode = resolve_cli_work_mode(args.mode, args.work_mode)
    client = InProcessRuntimeClient()
    request: AgentRequest | None = None
    session_id = str(args.session or "").strip()
    request_id = _new_request_id()
    operation = str(getattr(args, "_operation", CHAT_OPERATION) or CHAT_OPERATION)
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
        if operation == SKILLS_LIST_OPERATION:
            result, request, session_id = await _invoke_skills_list(
                client,
                args,
                renderer,
                request_id=request_id,
            )
            return result
        if operation in SESSION_OPERATIONS:
            renderer.working()
            if operation == SESSION_CREATE_OPERATION:
                session_id = await _create_session(
                    client,
                    args,
                    renderer,
                    request_id=request_id,
                )
            elif operation == SESSION_SWITCH_OPERATION:
                session_id = await _switch_session(
                    client,
                    args,
                    renderer,
                    request_id=request_id,
                )
            elif operation == SESSION_FORK_OPERATION:
                session_id = await _fork_session(
                    client,
                    args,
                    renderer,
                    request_id=request_id,
                )
            else:
                session_id = await _delete_session(
                    client,
                    args,
                    renderer,
                    request_id=request_id,
                )
            return 0
        if operation != CHAT_OPERATION:
            raise ValueError(f"unsupported process CLI operation: {operation}")
        session_id = await client.create_or_resume_session(
            channel_id=CHANNEL_ID,
            session_id=args.session,
        )
        _write_worker_result(
            args,
            operation=CHAT_OPERATION,
            session_id=session_id,
            mode=args.mode,
            work_mode=args.work_mode,
            project_dir=str(args.project_dir or ""),
        )
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
        renderer.finish(
            session_id=session_id,
            request_id=request_id,
            show_completion=operation == CHAT_OPERATION,
        )
        return result
    except TimeoutError:
        if request is not None and operation == CHAT_OPERATION:
            await _bounded_cleanup(client.cancel(_cancel_request(request)))
        renderer.render(
            RuntimeEvent.error(
                request_id=request_id,
                channel_id=CHANNEL_ID,
                session_id=session_id or None,
                error=TimeoutError("process CLI execution timed out"),
            )
        )
        renderer.finish(
            session_id=session_id,
            request_id=request_id,
            show_completion=operation == CHAT_OPERATION,
        )
        return 124
    except asyncio.CancelledError:
        if request is not None and operation == CHAT_OPERATION:
            await _bounded_cleanup(client.cancel(_cancel_request(request)))
        renderer.interrupted()
        raise
    except Exception as exc:  # noqa: BLE001 - CLI converts failures to events
        error_metadata = None
        if isinstance(exc, SessionProvisionError) and exc.code is not None:
            error_metadata = {"code": exc.code}
        renderer.render(
            RuntimeEvent.error(
                request_id=request_id,
                channel_id=CHANNEL_ID,
                session_id=session_id or None,
                error=exc,
                metadata=error_metadata,
            )
        )
        renderer.finish(
            session_id=session_id,
            request_id=request_id,
            show_completion=operation == CHAT_OPERATION,
        )
        return 1
    finally:
        try:
            if session_id and operation == CHAT_OPERATION:
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


__all__ = [
    "CHANNEL_ID",
    "CHAT_OPERATION",
    "SESSION_CREATE_OPERATION",
    "SESSION_DELETE_OPERATION",
    "SESSION_FORK_OPERATION",
    "SESSION_SWITCH_OPERATION",
    "SKILLS_LIST_OPERATION",
    "run",
]
