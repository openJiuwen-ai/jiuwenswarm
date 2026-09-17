# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight query bootstrap: validate before importing the Runtime."""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import uuid

from jiuwenswarm.channels.process_cli.machine_entry import (
    _run_async,
    prepare_workspace,
    protocol_stdout,
)
from jiuwenswarm.channels.process_cli.machine_io import (
    MAX_RUN_INPUT_BYTES,
    MachineInputError,
    read_machine_document,
)
from jiuwenswarm.channels.process_cli.machine_signals import command_signals
from jiuwenswarm.channels.process_cli.protocol import RunStatus, RuntimeErrorInfo
from jiuwenswarm.channels.process_cli.protocol.query import (
    OneShotQueryInput,
    OneShotQueryResult,
)


async def _read_query(source: str) -> OneShotQueryInput:
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
        value = read_machine_document("-", stdin=io.BytesIO(document))
    else:
        value = read_machine_document(source)
    try:
        return OneShotQueryInput.from_dict(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        request_id = value.get("request_id")
        raise MachineInputError(
            "Query input does not match the schema.",
            request_id=request_id.strip()
            if isinstance(request_id, str) and request_id.strip()
            else None,
        ) from None


def execute_query_source(source: str, *, conflicting_arguments: bool = False) -> int:
    """Publish one distinct query_result after shared signal/asyncio cleanup."""
    request_id = str(uuid.uuid4())

    async def execute() -> OneShotQueryResult:
        nonlocal request_id
        request = await _read_query(source)
        request_id = request.request_id or request_id
        request = prepare_workspace(request)
        await asyncio.sleep(0)
        from jiuwenswarm.channels.process_cli.query import run_query

        return await run_query(request, request_id)

    with protocol_stdout() as output, command_signals() as signals:
        try:
            if conflicting_arguments:
                raise MachineInputError("--query-json must be used on its own.")
            result = _run_async(execute(), signals)
        except MachineInputError as error:
            result = OneShotQueryResult(
                request_id=error.request_id or request_id,
                status=RunStatus.FAILED,
                exit_code=2,
                error=RuntimeErrorInfo(code=error.code, message=str(error)),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            result = OneShotQueryResult(
                request_id=request_id,
                status=RunStatus.CANCELLED,
                exit_code=130,
                error=RuntimeErrorInfo(
                    code="CANCELLED", message="Command interrupted."
                ),
            )
        except (Exception, builtins.SystemExit):  # pylint: disable=broad-exception-caught
            result = OneShotQueryResult(
                request_id=request_id,
                status=RunStatus.FAILED,
                exit_code=1,
                error=RuntimeErrorInfo(
                    code="STARTUP_FAILED", message="Command startup or shutdown failed."
                ),
            )
        result = signals.finalize(result)
        try:
            line = (
                json.dumps(
                    result.to_dict(),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            if output.write(line) != len(line):
                return 1
            output.flush()
        except (OSError, ValueError):
            return 1
        return result.exit_code
