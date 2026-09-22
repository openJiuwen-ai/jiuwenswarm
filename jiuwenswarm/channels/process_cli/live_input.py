# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human TTY supplemental input for one Process CLI Runtime worker."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import InMemoryHistory

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.duplex_input import DuplexLineReader
from jiuwenswarm.channels.process_cli.live_layout import FORWARDED_RECEIPT_PREFIX
from jiuwenswarm.channels.process_cli.prompt import LIVE_INPUT_PROMPT
from jiuwenswarm.common.mode_matrix import (
    canonicalize_mode_text,
    deprecate_mode,
    is_plan_mode,
    is_single_agent_mode,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent, TERMINAL_ERROR_EVENT_TYPES

_DELIVERY_TIMEOUT_SECONDS = 5.0
_STEER_CONTEXT_KEYS = (
    "mode",
    "work_mode",
    "cwd",
    "project_dir",
    "trusted_dirs",
    "supports_user_interaction",
)
_TARGET_CHANGED_MARKERS = (
    "supplemental input was not sent",
    "targeted execution has ended",
    "waiting for an interaction answer",
    "session is finishing",
    "session output is finishing",
)


@dataclass(frozen=True, slots=True)
class LiveInputReceipt:
    """One local delivery outcome; it never changes the root command result."""

    status: Literal["accepted", "rejected", "unknown"]
    message: str


def encode_forwarded_receipt(receipt: LiveInputReceipt) -> str:
    return FORWARDED_RECEIPT_PREFIX + json.dumps(
        {
            "status": receipt.status,
            "message": receipt.message,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class TtyLineReader:
    """Own one cancellable prompt-toolkit reader in the Runtime worker."""

    prompt_safe = True

    def __init__(self) -> None:
        self._session: PromptSession[str] = PromptSession(
            history=InMemoryHistory(),
        )

    async def __call__(self, prompt: str) -> str | None:
        try:
            return await self._session.prompt_async(prompt)
        except EOFError:
            return None

    async def close(self) -> None:
        return


class PipeLineReader:
    """Read forwarded parent-REPL lines without trying to control its terminal."""

    prompt_safe = False

    def __init__(self, stream: Any = None) -> None:
        self._reader = DuplexLineReader(stream)

    async def __call__(self, _prompt: str) -> str | None:
        line = await self._reader.read_line()
        return None if line is None else line.decode("utf-8")

    async def close(self) -> None:
        await self._reader.close()


def supports_live_session_input(args: Any, *, stdin: Any, stdout: Any) -> bool:
    """Return whether this human command can safely own live terminal input."""

    if getattr(args, "output", None) != "human":
        return False
    if str(getattr(args, "_operation", "chat") or "chat") != "chat":
        return False
    forwarded_repl = bool(getattr(args, "_forwarded_live_input", False))
    if not forwarded_repl and (not _is_tty(stdin) or not _is_tty(stdout)):
        return False
    mode = deprecate_mode(canonicalize_mode_text(getattr(args, "mode", None)))
    return is_single_agent_mode(mode) and not is_plan_mode(mode)


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


def execution_id_from_event(event: RuntimeEvent) -> str | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    value = payload.get("execution_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _copy_steer_context(params: dict[str, Any]) -> dict[str, Any]:
    trusted: dict[str, Any] = {}
    for key in _STEER_CONTEXT_KEYS:
        if key in params:
            trusted[key] = params[key]
    return trusted


def _message_marks_target_changed(normalized: str) -> bool:
    for marker in _TARGET_CHANGED_MARKERS:
        if marker in normalized:
            return True
    return False


def build_steer_request(
    root: AgentRequest,
    *,
    text: str,
    execution_id: str,
) -> AgentRequest:
    """Build a text-only steer bound to the execution visible in this worker."""

    params = root.params if isinstance(root.params, dict) else {}
    trusted = _copy_steer_context(params)
    trusted.update(
        {
            "query": text,
            "content": text,
            "input_mode": "steer",
            "expected_execution_id": execution_id,
        }
    )
    return AgentRequest(
        request_id=f"steer-{uuid.uuid4().hex[:12]}",
        channel_id=root.channel_id,
        session_id=root.session_id,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        timestamp=time.time(),
        params=trusted,
    )


def _error_details(error: BaseException) -> tuple[str, str]:
    code = str(getattr(error, "code", "") or "")
    return code, str(error)


def _event_error_details(event: RuntimeEvent) -> tuple[str, str]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    code = str(metadata.get("code") or payload.get("code") or "")
    message = str(payload.get("error") or payload.get("message") or "补充输入投递失败")
    return code, message


def classify_delivery_error(code: str, message: str) -> LiveInputReceipt:
    normalized = message.lower()
    if code == "SESSION_INPUT_DELIVERY_UNKNOWN" or "delivery is unknown" in normalized:
        return LiveInputReceipt(
            "unknown",
            "补充输入投递结果未知，请勿自动重试",
        )
    if code == "SESSION_INPUT_TARGET_CHANGED" or _message_marks_target_changed(normalized):
        return LiveInputReceipt("rejected", "补充输入未投递：当前执行状态已变化")
    return LiveInputReceipt("rejected", f"补充输入未投递：{message}")


async def observe_steer_stream(
    stream: AsyncIterator[RuntimeEvent],
) -> LiveInputReceipt:
    """Consume the short delivery stream without rendering it as root output."""

    try:
        async with aclosing(stream) as events:
            async for event in events:
                if event.event_type == "runtime.accepted":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    if payload.get("input_delivery") == "chat":
                        return LiveInputReceipt(
                            "rejected",
                            "补充输入未投递：当前执行已经结束",
                        )
                    return LiveInputReceipt("accepted", "补充输入已受理")
                if not event.ok or event.event_type in TERMINAL_ERROR_EVENT_TYPES:
                    return classify_delivery_error(*_event_error_details(event))
    except Exception as error:  # noqa: BLE001 - receipt classification boundary
        return classify_delivery_error(*_error_details(error))
    return LiveInputReceipt(
        "unknown",
        "补充输入投递结果未知，请勿自动重试",
    )


class LiveSessionInputController:
    """Read and deliver serial steers while the root stream keeps flowing."""

    def __init__(
        self,
        *,
        client: InProcessRuntimeClient,
        root_request: AgentRequest,
        read_line: Callable[[str], Awaitable[str | None]],
        on_ready: Callable[[], None],
        on_receipt: Callable[[LiveInputReceipt], None],
        delivery_timeout: float = _DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._root_request = root_request
        self._read_line = read_line
        self._on_ready = on_ready
        self._on_receipt = on_receipt
        self._delivery_timeout = delivery_timeout
        self._execution_id: str | None = None
        self._input_allowed = asyncio.Event()
        self._target_ready = asyncio.Event()
        self._stopping = False
        self._paused = False
        self._task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[str | None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._input_allowed.set()
            self._on_ready()
            self._task = asyncio.create_task(
                self._run(),
                name="process-cli-live-input",
            )

    def observe(self, event: RuntimeEvent) -> None:
        """Open admission only after Runtime identifies the owned root execution."""

        execution_id = execution_id_from_event(event)
        if self._execution_id is None and execution_id is not None:
            self._execution_id = execution_id
            self._target_ready.set()
        if not self._paused and not self._stopping and not event.is_complete:
            self._input_allowed.set()

    async def render_root_event(self, render: Callable[[], None]) -> None:
        """Render above an active prompt while preserving its edit buffer."""

        if (
            getattr(self._read_line, "prompt_safe", False)
            and self._read_task is not None
            and not self._read_task.done()
        ):
            await run_in_terminal(render)
            return
        render()

    async def pause(self) -> None:
        """Release stdin before an ask-user/approval interaction reads it."""

        self._paused = True
        self._input_allowed.clear()
        await self._cancel_read()

    def resume_after_event(self) -> None:
        """Wait for resumed Runtime output before accepting more steers."""

        self._paused = False
        self._input_allowed.clear()

    async def close(self) -> None:
        self._stopping = True
        self._input_allowed.set()
        self._target_ready.set()
        await self._cancel_read()
        task = self._task
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=self._delivery_timeout)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self._on_receipt(
                        LiveInputReceipt(
                            "unknown",
                            "补充输入投递结果未知，请勿自动重试",
                        )
                    )
        finally:
            close_reader = getattr(self._read_line, "close", None)
            if callable(close_reader):
                await close_reader()

    async def read_interaction_line(self) -> str:
        """Read one answer while steer admission is paused."""

        self._read_task = asyncio.create_task(
            self._read_line(""),
            name="process-cli-interaction-input-read",
        )
        try:
            return str(await self._read_task or "").strip()
        finally:
            self._read_task = None

    async def _cancel_read(self) -> None:
        task = self._read_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._read_task = None

    async def _run(self) -> None:
        while not self._stopping:
            await self._input_allowed.wait()
            if self._stopping:
                return
            if self._paused:
                self._input_allowed.clear()
                continue
            self._read_task = asyncio.create_task(
                self._read_line(LIVE_INPUT_PROMPT),
                name="process-cli-live-input-read",
            )
            try:
                text = await self._read_task
            except asyncio.CancelledError:
                if self._stopping:
                    return
                continue
            finally:
                self._read_task = None
            if text is None:
                return
            text = text.strip()
            if not text:
                continue
            await self._target_ready.wait()
            await self._input_allowed.wait()
            if self._stopping or self._execution_id is None:
                self._on_receipt(
                    LiveInputReceipt(
                        "rejected",
                        "补充输入未投递：当前执行已经结束",
                    )
                )
                return
            request = build_steer_request(
                self._root_request,
                text=text,
                execution_id=self._execution_id,
            )
            receipt = await observe_steer_stream(self._client.stream(request))
            self._on_receipt(receipt)


__all__ = [
    "LIVE_INPUT_PROMPT",
    "FORWARDED_RECEIPT_PREFIX",
    "LiveInputReceipt",
    "LiveSessionInputController",
    "PipeLineReader",
    "TtyLineReader",
    "build_steer_request",
    "classify_delivery_error",
    "execution_id_from_event",
    "encode_forwarded_receipt",
    "observe_steer_stream",
    "supports_live_session_input",
]
