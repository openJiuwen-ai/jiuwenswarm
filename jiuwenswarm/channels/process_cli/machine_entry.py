# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight machine bootstrap; import Runtime only after stdout/cwd setup."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import logging
import os
import sys
import uuid
from collections.abc import Coroutine, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO, TypeVar

from jiuwenswarm.channels.process_cli.machine_io import (
    MAX_RUN_INPUT_BYTES,
    MachineInputError,
    OneShotWriter,
    read_run_input,
)
from jiuwenswarm.channels.process_cli.machine_signals import (
    CommandSignals,
    command_signals,
)
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunInput,
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
    WorkspaceSpec,
)

from jiuwenswarm.channels.process_cli.protocol.query import (
    OneShotQueryInput,
    OneShotQueryResult,
)

logger = logging.getLogger(__name__)

_Input = TypeVar("_Input", OneShotRunInput, OneShotQueryInput)
_Result = TypeVar("_Result", OneShotRunResult, OneShotQueryResult)


@contextlib.contextmanager
def protocol_stdout() -> Iterator[TextIO]:
    """Keep both Python and inherited native stdout diagnostics off JSONL.

    The duplicated data handle stays with the writer; dependencies and their
    tool subprocesses inherit stderr instead. StringIO test streams need only
    Python-level redirection.
    """
    original = sys.stdout
    output = original
    output_fd = None
    try:
        stdout_fd = original.fileno()
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        stdout_fd = None
        stderr_fd = None
    try:
        if stdout_fd is not None and stderr_fd is not None:
            original.flush()
            output_fd = os.dup(stdout_fd)
            output = os.fdopen(output_fd, "w", encoding="utf-8", newline="\n")
            os.dup2(stderr_fd, stdout_fd)
        with contextlib.redirect_stdout(sys.stderr):
            yield output
    finally:
        if output_fd is not None and stdout_fd is not None:
            # Restore the caller's descriptor even after EPIPE; never flush the
            # broken data stream again during context-manager unwinding.
            os.dup2(output_fd, stdout_fd)
            with contextlib.suppress(OSError):
                output.close()


def _absolute_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def prepare_workspace(run_input: _Input) -> _Input:
    """Resolve all paths before chdir so project and cwd retain separate roles."""
    workspace = run_input.workspace
    if workspace is None:
        return run_input
    try:
        resolved = WorkspaceSpec(
            cwd=_absolute_path(workspace.cwd) if workspace.cwd is not None else None,
            project_dir=(
                _absolute_path(workspace.project_dir)
                if workspace.project_dir is not None
                else None
            ),
            trusted_dirs=tuple(_absolute_path(path) for path in workspace.trusted_dirs),
        )
        if resolved.cwd is not None:
            os.chdir(resolved.cwd)
    except (OSError, ValueError) as error:
        raise MachineInputError("Cannot access the requested workspace.") from error
    return replace(run_input, workspace=resolved)


def _failure(
    writer: OneShotWriter, *, code: str, message: str, exit_code: int
) -> OneShotRunResult:
    return OneShotRunResult(
        sequence=writer.sequence,
        request_id=writer.request_id,
        session_id=writer.session_id,
        status=RunStatus.CANCELLED if exit_code == 130 else RunStatus.FAILED,
        exit_code=exit_code,
        error=RuntimeErrorInfo(code=code, message=message),
    )


async def _execute_duplex(writer: OneShotWriter) -> OneShotRunResult:
    from jiuwenswarm.channels.process_cli.duplex_input import (
        DuplexInputError,
        DuplexLineReader,
    )

    try:
        async with DuplexLineReader() as reader:
            line = await reader.read_line()
            if line is None:
                raise MachineInputError(
                    "A run record is required before control input."
                )
            run_input = read_run_input("-", stdin=io.BytesIO(line))
            writer.request_id = run_input.request_id or writer.request_id
            run_input = prepare_workspace(run_input)
            from jiuwenswarm.channels.process_cli.duplex_control import DuplexController
            from jiuwenswarm.channels.process_cli.machine import run_with_signals

            control = DuplexController(reader, writer)
            return await run_with_signals(run_input, writer, control=control)
    except DuplexInputError as error:
        raise MachineInputError(str(error)) from error


async def _execute_document(source: str, writer: OneShotWriter) -> OneShotRunResult:
    if source == "-":
        from jiuwenswarm.channels.process_cli.duplex_input import (
            DuplexInputError,
            DuplexLineReader,
        )

        try:
            async with DuplexLineReader(max_line_bytes=MAX_RUN_INPUT_BYTES) as reader:
                document = await reader.read_document()
        except DuplexInputError as error:
            raise MachineInputError(str(error)) from error
        run_input = read_run_input("-", stdin=io.BytesIO(document))
    else:
        run_input = read_run_input(source)
    writer.request_id = run_input.request_id or writer.request_id
    run_input = prepare_workspace(run_input)
    await asyncio.sleep(0)
    from jiuwenswarm.channels.process_cli.machine import run_with_signals

    await asyncio.sleep(0)
    return await run_with_signals(run_input, writer)


def _run_async(
    operation: Coroutine[Any, Any, _Result], signals: CommandSignals
) -> _Result:
    """Observe failures that asyncio.run logs instead of raising on shutdown."""
    shutdown_failed = False

    async def guarded() -> _Result:
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()

        def report_error(event_loop: asyncio.AbstractEventLoop, context: dict) -> None:
            nonlocal shutdown_failed
            if signals.cleaning:
                shutdown_failed = True
                logger.warning("one-shot asyncio shutdown failed")
            elif previous is not None:
                previous(event_loop, context)
            else:
                event_loop.default_exception_handler(context)

        # This is a private one-shot loop, disposed by asyncio.run below. Keep
        # the handler installed through task and async-generator shutdown.
        loop.set_exception_handler(report_error)
        return await signals.run(operation)

    result = asyncio.run(guarded())
    if not shutdown_failed:
        return result
    error: RuntimeErrorInfo = result.error or RuntimeErrorInfo(
        code="SHUTDOWN_FAILED", message="Runtime cleanup failed."
    )
    existing: object = error.details.get("cleanup_errors", ())
    errors: tuple[Any, ...] = ("asyncio_shutdown",)
    if isinstance(existing, tuple):
        errors = existing + errors
    return replace(
        result,
        status=result.status if result.error else RunStatus.FAILED,
        exit_code=result.exit_code or 1,
        error=replace(error, details={**dict(error.details), "cleanup_errors": errors}),
    )


def execute_source(
    source: str, *, conflicting_arguments: bool = False, json_lines: bool = False
) -> int:
    """Read exactly one request and exit; never enter REPL or spawn a worker."""
    with protocol_stdout() as output, command_signals() as signals:
        writer = OneShotWriter(output, request_id=str(uuid.uuid4()))
        try:
            if conflicting_arguments:
                option = "--run-jsonl" if json_lines else "--run-json"
                raise MachineInputError(
                    f"{option} cannot be combined with legacy execution arguments."
                )
            if json_lines:
                result = _run_async(_execute_duplex(writer), signals)
            else:
                result = _run_async(_execute_document(source, writer), signals)
        except MachineInputError as error:
            writer.request_id = error.request_id or writer.request_id
            result = _failure(writer, code=error.code, message=str(error), exit_code=2)
        except (KeyboardInterrupt, asyncio.CancelledError):
            result = _failure(
                writer, code="CANCELLED", message="Command interrupted.", exit_code=130
            )
        except (Exception, builtins.SystemExit) as error:  # noqa: BLE001 - bootstrap/shutdown boundary
            logger.warning("one-shot bootstrap failed (%s)", type(error).__name__)
            result = _failure(
                writer,
                code="STARTUP_FAILED",
                message="Command startup or shutdown failed.",
                exit_code=1,
            )
        result = signals.finalize(result)
        if writer.broken:
            return result.exit_code or 1
        try:
            writer.write_result(result)
        except (OSError, ValueError):
            return 1
        return result.exit_code
