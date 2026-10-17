# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only Runtime Public API dispatch for a single query process."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.channels.process_cli.machine_signals import defer_command_signals
from jiuwenswarm.channels.process_cli.protocol import RunStatus, RuntimeErrorInfo
from jiuwenswarm.channels.process_cli.protocol.query import (
    OneShotQueryInput,
    OneShotQueryResult,
)
from jiuwenswarm.runtime.permission_catalog import PermissionSnapshotInput
from jiuwenswarm.runtime.session_catalog import SessionGetInput, SessionListInput

logger = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT_SECONDS = 5.0
CHANNEL_ID = "process_cli"


def dispatch_query(
    client: InProcessRuntimeClient, request: OneShotQueryInput
) -> dict[str, Any]:
    """Only public queries; never acquire an Agent or adopt another Channel's Session."""
    params: dict[str, Any] = dict(request.params)
    catalogs = {
        "model.list": "list_model_capabilities",
        "mode.list": "list_mode_capabilities",
    }
    if request.operation in catalogs:
        return getattr(client, catalogs[request.operation])().to_dict()
    if request.operation == "session.get":
        session = client.get_session(SessionGetInput(channel_id=CHANNEL_ID, **params))
        return {"session": session.to_dict() if session is not None else None}
    operations: dict[str, Callable[[], Any]] = {
        "session.list": lambda: client.list_sessions(
            SessionListInput(channel_id=CHANNEL_ID, **params)
        ),
        "model.resolve": lambda: client.resolve_model_capability(**params),
        "mode.resolve": lambda: client.resolve_mode_capability(**params),
        "permission.get": lambda: client.get_permission_snapshot(
            PermissionSnapshotInput(channel_id=CHANNEL_ID, **params)
        ),
        "mcp.validate": lambda: client.validate_mcp_references(**params),
    }
    return operations[request.operation]().to_dict()


def query_failure(
    request_id: str, *, code: str, message: str, exit_code: int = 1
) -> OneShotQueryResult:
    status = {124: RunStatus.TIMED_OUT, 130: RunStatus.CANCELLED}.get(
        exit_code, RunStatus.FAILED
    )
    return OneShotQueryResult(
        request_id=request_id,
        status=status,
        exit_code=exit_code,
        error=RuntimeErrorInfo(code=code, message=message),
    )


async def run_query(
    request: OneShotQueryInput,
    request_id: str,
    *,
    client_factory: Callable[[], InProcessRuntimeClient] = InProcessRuntimeClient,
) -> OneShotQueryResult:
    """Start and close exactly one Runtime, even on partial startup failure."""
    client = None
    result = query_failure(
        request_id, code="RUNTIME_FAILED", message="Runtime query failed."
    )
    deadline = asyncio.timeout(request.timeout_seconds)
    try:
        async with deadline:
            client = client_factory()
            await client.start()
            data = dispatch_query(client, request)
            # Synchronous catalog I/O cannot yield to asyncio's deadline timer.
            # Check elapsed time before cancelling that timer on context exit.
            expires_at = deadline.when()
            expired = (
                expires_at is not None
                and asyncio.get_running_loop().time() >= expires_at
            )
            await asyncio.sleep(0)
            if expired:
                result = query_failure(
                    request_id,
                    code="TIMEOUT",
                    message="Command timed out.",
                    exit_code=124,
                )
            else:
                result = OneShotQueryResult(
                    request_id=request_id, operation=request.operation, data=data
                )
    except TimeoutError:
        if deadline.expired():
            result = query_failure(
                request_id, code="TIMEOUT", message="Command timed out.", exit_code=124
            )
    except asyncio.CancelledError:
        result = query_failure(
            request_id, code="CANCELLED", message="Command interrupted.", exit_code=130
        )
    except (Exception, builtins.SystemExit) as error:  # pylint: disable=broad-exception-caught
        code = getattr(error, "code", "RUNTIME_FAILED")
        if not isinstance(code, str) or not code.strip():
            code = "RUNTIME_FAILED"
        result = query_failure(request_id, code=code, message="Runtime query failed.")
        logger.warning("one-shot query failed (%s)", type(error).__name__)
    finally:
        defer_command_signals()
        if client is not None:
            try:
                async with asyncio.timeout(SHUTDOWN_TIMEOUT_SECONDS):
                    await client.close()
            except (Exception, asyncio.CancelledError, builtins.SystemExit) as error:  # pylint: disable=broad-exception-caught
                logger.warning("one-shot query close failed (%s)", type(error).__name__)
                info = result.error or RuntimeErrorInfo(
                    code="SHUTDOWN_FAILED", message="Runtime cleanup failed."
                )
                result = replace(
                    result,
                    status=result.status if result.error else RunStatus.FAILED,
                    exit_code=result.exit_code or 1,
                    data=None,
                    error=replace(
                        info,
                        details={
                            **dict(info.details),
                            "cleanup_errors": ("runtime_close",),
                        },
                    ),
                )
    return replace(result, operation=request.operation)
