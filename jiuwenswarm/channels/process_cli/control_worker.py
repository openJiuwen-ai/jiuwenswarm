# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Private catalog worker for REPL controls, with command-line inputs only.

The result envelope is parent/worker IPC, not a public CLI or SDK protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from jiuwenswarm.channels.process_cli.diagnostics import capture_diagnostics

OPERATIONS = {
    "model.list": (),
    "model.resolve": ("requested",),
    "session.get": ("session_id",),
    "session.list": ("limit", "offset", "search"),
    "permission.get": ("session_id",),
}
SHUTDOWN_TIMEOUT_SECONDS = 5.0
logger = logging.getLogger(__name__)


def build_command(
    operation: str,
    *,
    cwd: str,
    params: dict[str, Any],
    timeout: float,
) -> list[str]:
    """Encode only the arguments used by trusted, read-only REPL controls."""
    if operation not in OPERATIONS or set(params) - set(OPERATIONS[operation]):
        raise ValueError("unsupported REPL catalog query")
    command = [operation, "--cwd", cwd, "--timeout", str(timeout)]
    for name, value in params.items():
        command.append("--" + name.replace("_", "-") + "=" + str(value))
    return command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private REPL catalog worker")
    parser.add_argument("operation", choices=tuple(OPERATIONS))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--requested", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--search", default="")
    return parser


def _dispatch(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    from jiuwenswarm.runtime.permission_catalog import PermissionSnapshotInput
    from jiuwenswarm.runtime.session_catalog import SessionGetInput, SessionListInput

    if args.operation == "model.list":
        return client.list_model_capabilities().to_dict()
    if args.operation == "model.resolve":
        return client.resolve_model_capability(args.requested).to_dict()
    if args.operation == "session.get":
        session = client.get_session(
            SessionGetInput(channel_id="process_cli", session_id=args.session_id)
        )
        return {"session": session.to_dict() if session is not None else None}
    if args.operation == "session.list":
        return client.list_sessions(
            SessionListInput(
                channel_id="process_cli",
                limit=args.limit,
                offset=args.offset,
                search=args.search,
            )
        ).to_dict()
    return client.get_permission_snapshot(
        PermissionSnapshotInput(
            channel_id="process_cli",
            session_id=args.session_id or None,
        )
    ).to_dict()


async def run(
    args: argparse.Namespace,
    *,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Own one Runtime and close it before returning the private IPC result."""
    if client_factory is None:
        from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient

        client_factory = InProcessRuntimeClient
    client = None
    result: dict[str, Any] = {
        "operation": args.operation,
        "status": "failed",
        "data": None,
        "error": {"code": "QUERY_FAILED"},
    }
    deadline = asyncio.timeout(args.timeout)
    try:
        async with deadline:
            client = client_factory()
            await client.start()
            data = _dispatch(client, args)
            # Catalog reads are synchronous, so also enforce their elapsed time.
            expires_at = deadline.when()
            if (
                expires_at is not None
                and asyncio.get_running_loop().time() >= expires_at
            ):
                raise TimeoutError
            result.update(status="completed", data=data, error=None)
    except (Exception, asyncio.CancelledError, SystemExit) as exc:
        code = (
            "TIMEOUT"
            if isinstance(exc, TimeoutError)
            else getattr(exc, "code", "QUERY_FAILED")
        )
        result["error"] = {"code": code if isinstance(code, str) else "QUERY_FAILED"}
        logger.warning("REPL catalog query failed (%s)", type(exc).__name__)
    finally:
        if client is not None:
            try:
                async with asyncio.timeout(SHUTDOWN_TIMEOUT_SECONDS):
                    await client.close()
            except (Exception, asyncio.CancelledError, SystemExit) as exc:
                result.update(
                    status="failed", data=None, error={"code": "SHUTDOWN_FAILED"}
                )
                logger.warning("REPL catalog cleanup failed (%s)", type(exc).__name__)
    return result


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    target = Path(args.cwd).expanduser().resolve(strict=True)
    if not target.is_dir():
        parser.error("cwd must be a directory")
    os.chdir(target)
    with capture_diagnostics(capture_stdout=True, debug=False) as streams:
        result = asyncio.run(run(args))
        streams.stdout.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
        streams.stdout.flush()
    raise SystemExit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
