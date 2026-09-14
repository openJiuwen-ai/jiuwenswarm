# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight machine bootstrap; import Runtime only after stdout/cwd setup."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from jiuwenswarm.channels.process_cli.machine_io import (
    MachineInputError,
    OneShotWriter,
    read_run_input,
)
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunInput,
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
    WorkspaceSpec,
)

logger = logging.getLogger(__name__)


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
        if output_fd is not None:
            # Restore the caller's descriptor even after EPIPE; never flush the
            # broken data stream again during context-manager unwinding.
            os.dup2(output_fd, stdout_fd)
            with contextlib.suppress(OSError):
                output.close()


def _absolute_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def prepare_workspace(run_input: OneShotRunInput) -> OneShotRunInput:
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


def execute_source(
    source: str, *, conflicting_arguments: bool = False, json_lines: bool = False
) -> int:
    """Read exactly one request and exit; never enter REPL or spawn a worker."""
    with protocol_stdout() as output:
        writer = OneShotWriter(output, request_id=str(uuid.uuid4()))
        try:
            if conflicting_arguments:
                option = "--run-jsonl" if json_lines else "--run-json"
                raise MachineInputError(
                    f"{option} cannot be combined with legacy execution arguments."
                )
            if json_lines:
                result = asyncio.run(_execute_duplex(writer))
            else:
                run_input = read_run_input(source)
                writer.request_id = run_input.request_id or writer.request_id
                run_input = prepare_workspace(run_input)
                from jiuwenswarm.channels.process_cli.machine import run_with_signals

                result = asyncio.run(run_with_signals(run_input, writer))
        except MachineInputError as error:
            writer.request_id = error.request_id or writer.request_id
            result = _failure(writer, code=error.code, message=str(error), exit_code=2)
        except KeyboardInterrupt:
            result = _failure(
                writer, code="CANCELLED", message="Command interrupted.", exit_code=130
            )
        except Exception as error:  # noqa: BLE001 - bootstrap/asyncio shutdown boundary
            logger.warning("one-shot bootstrap failed (%s)", type(error).__name__)
            result = _failure(
                writer,
                code="STARTUP_FAILED",
                message="Command startup or shutdown failed.",
                exit_code=1,
            )
        if writer.broken:
            return result.exit_code or 1
        try:
            writer.write_result(result)
        except OSError:
            return 1
        return result.exit_code
