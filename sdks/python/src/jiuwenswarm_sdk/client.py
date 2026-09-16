# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async thin process host, with no Runtime dependency or resident worker."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import signal
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from .protocol import ProtocolError, Records, SCHEMA_VERSION, encode

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
InteractionHandler = Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]


class TransportError(RuntimeError):
    """The child could not start or be cleanly collected; no automatic retry."""


class InteractionRequired(RuntimeError):
    """An interaction needs a host handler; no permission has been granted."""


class Client:
    """Each run/query creates one child and waits for its result AND process exit.

    Handlers are awaited in event order. stderr is always drained separately.
    A slow handler applies backpressure; set a host deadline for untrusted hosts.
    """

    def __init__(
        self,
        command: Sequence[str] = ("jiuwenswarm-process",),
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        shutdown_grace_seconds: float = 30,
        max_record_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError(
                "command must be a nonempty argv sequence, not a shell string"
            )
        if any(not isinstance(arg, str) or not arg for arg in command):
            raise ValueError("command arguments must be nonempty strings")
        if not math.isfinite(shutdown_grace_seconds) or shutdown_grace_seconds <= 0:
            raise ValueError("shutdown grace must be positive and finite")
        if type(max_record_bytes) is not int or max_record_bytes < 1:
            raise ValueError("max_record_bytes must be positive")
        self.command = tuple(command)
        self.cwd = cwd
        self.env = {**os.environ, **env} if env is not None else None
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.max_record_bytes = max_record_bytes

    async def run(
        self,
        request: Mapping[str, Any],
        *,
        on_event: EventHandler | None = None,
        on_interaction: InteractionHandler | None = None,
        cancel: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run once. Interactions/Plan/permissions use the SAME child's stdin."""
        return await self._execute(
            dict(request),
            query=False,
            on_event=on_event,
            on_interaction=on_interaction,
            cancel=cancel,
            deadline_seconds=deadline_seconds,
        )

    async def query(
        self,
        operation: str,
        params: Mapping[str, Any] | None = None,
        *,
        workspace: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """One read-only query; returns a query_result, never a live Session."""
        request: dict[str, Any] = {"operation": operation, "params": dict(params or {})}
        if workspace is not None:
            request["workspace"] = dict(workspace)
        if timeout_seconds is not None:
            request["timeout_seconds"] = timeout_seconds
        return await self._execute(
            request, query=True, deadline_seconds=deadline_seconds
        )

    async def _execute(
        self,
        request: dict[str, Any],
        *,
        query: bool,
        on_event: EventHandler | None = None,
        on_interaction: InteractionHandler | None = None,
        cancel: asyncio.Event | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        if deadline_seconds is not None:
            if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
                raise ValueError("host deadline must be positive and finite")
        request.setdefault("schema_version", SCHEMA_VERSION)
        request.setdefault("type", "query" if query else "run")
        request.setdefault("request_id", str(uuid.uuid4()))
        request_id = request["request_id"]
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a nonempty string")
        request["request_id"] = request_id = request_id.strip()
        payload = encode(request)
        args = ["--query-json", "-"] if query else ["--run-jsonl"]
        options: dict[str, Any] = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                limit=self.max_record_bytes,
                **options,
            )
        except OSError as error:
            raise TransportError("could not launch the Process CLI") from error
        invocation = _Invocation(
            process,
            Records(request_id, query=query, operation=request.get("operation")),
            self,
        )
        stderr_task = asyncio.create_task(invocation.drain_stderr())
        cancel_task = (
            asyncio.create_task(invocation.watch_cancel(cancel))
            if cancel is not None
            else None
        )
        try:
            async with asyncio.timeout(deadline_seconds):
                await invocation.write(payload)
                if query:
                    invocation.stdin.close()
                result = await invocation.consume(on_event, on_interaction)
                exit_code = await process.wait()
                await stderr_task
                invocation.records.finish(exit_code)
                return result
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
            # Shield owned cleanup from a caller's cancellation. It is bounded;
            # force termination is a failure fallback, never the normal path.
            cleanup = asyncio.create_task(invocation.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)


class _Invocation:
    def __init__(
        self, process: asyncio.subprocess.Process, records: Records, client: Client
    ) -> None:
        self.process = process
        # create_subprocess_exec above unconditionally requests all three pipes.
        self.stdin = cast(asyncio.StreamWriter, process.stdin)
        self.stdout = cast(asyncio.StreamReader, process.stdout)
        self.stderr = cast(asyncio.StreamReader, process.stderr)
        self.records = records
        self.client = client
        self.write_lock = asyncio.Lock()
        self.cancel_sent = False
        self.cancelled = asyncio.Event()
        self.stderr_tail = bytearray()

    async def write(self, data: bytes) -> None:
        async with self.write_lock:
            if self.stdin.is_closing():
                return
            try:
                self.stdin.write(data)
                await self.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The child can reject input early; its terminal result and
                # actual exit status, not a racing EPIPE, decide the outcome.
                return

    async def cancel(self) -> None:
        if self.cancel_sent or self.records.result is not None:
            return
        self.cancel_sent = True
        self.cancelled.set()
        await self.write(
            encode(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "cancel",
                    "request_id": self.records.request_id,
                }
            )
        )

    async def watch_cancel(self, event: asyncio.Event) -> None:
        await event.wait()
        await self.cancel()

    async def drain_stderr(self) -> None:
        while True:
            chunk = await self.stderr.read(8192)
            if not chunk:
                break
            self.stderr_tail.extend(chunk)
            del self.stderr_tail[:-65536]

    async def consume(
        self, on_event: EventHandler | None, on_interaction: InteractionHandler | None
    ) -> dict[str, Any]:
        while True:
            try:
                line = await self.stdout.readline()
            except ValueError as error:
                raise ProtocolError(
                    "output record exceeds configured byte limit"
                ) from error
            if not line:
                break
            if len(line) > self.client.max_record_bytes:
                raise ProtocolError("output record exceeds configured byte limit")
            record = self.records.accept(line)
            if record["type"] != "event":
                continue
            if on_event is not None:
                await self.callback(on_event(record))
            if record["event_type"] == "interaction.requested" and not self.cancel_sent:
                if on_interaction is None:
                    raise InteractionRequired(
                        "host interaction handler required; no approval granted"
                    )
                answers = await self.callback(on_interaction(record))
                if self.cancel_sent:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("interaction_id"), str
                ):
                    raise ProtocolError("interaction has no correlation identity")
                await self.write(
                    encode(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "type": "answer",
                            "request_id": self.records.request_id,
                            "session_id": record["session_id"],
                            "interaction_id": payload["interaction_id"],
                            "answers": answers,
                        }
                    )
                )
        if self.records.result is None:
            raise ProtocolError("process output ended without a terminal result")
        return self.records.result

    async def callback(self, operation: Awaitable[Any]) -> Any:
        """Cancellation must not wait for an interactive UI to submit an answer."""
        callback = asyncio.ensure_future(operation)
        cancelled = asyncio.create_task(self.cancelled.wait())
        try:
            await asyncio.wait(
                (callback, cancelled), return_when=asyncio.FIRST_COMPLETED
            )
            if self.cancelled.is_set():
                return None
            return callback.result()
        finally:
            callback.cancel()
            cancelled.cancel()
            await asyncio.gather(callback, cancelled, return_exceptions=True)

    async def close(self) -> None:
        async def drain_and_wait() -> None:
            if not self.records.query:
                await self.cancel()
            elif self.process.returncode is None:
                graceful = (
                    getattr(signal, "CTRL_BREAK_EVENT")
                    if os.name == "nt"
                    else signal.SIGTERM
                )
                with contextlib.suppress(OSError, ProcessLookupError):
                    self.process.send_signal(graceful)
            self.stdin.close()
            while await self.stdout.read(8192):
                pass
            await self.process.wait()

        try:
            async with asyncio.timeout(self.client.shutdown_grace_seconds):
                await drain_and_wait()
        except TimeoutError:
            await self.force_stop()
            while await self.stdout.read(8192):
                pass
            await self.process.wait()
        finally:
            self.stdin.close()

    async def force_stop(self) -> None:
        """Failure-only fallback for the exact child tree owned by this call."""
        if self.process.returncode is not None:
            return
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill.exe",
                    "/PID",
                    str(self.process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                async with asyncio.timeout(5):
                    try:
                        await killer.wait()
                    finally:
                        if killer.returncode is None:
                            killer.kill()
                            await killer.wait()
            except OSError:
                pass
        else:
            with contextlib.suppress(ProcessLookupError):
                getattr(os, "killpg")(self.process.pid, getattr(signal, "SIGKILL"))
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
