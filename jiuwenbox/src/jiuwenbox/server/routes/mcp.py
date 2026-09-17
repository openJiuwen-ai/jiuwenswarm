# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""MCP route: expose JiuwenBox sandbox capabilities via remote MCP."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.sandbox import SandboxSpec
from jiuwenbox.server.sandbox_manager import SandboxExecRequest

configure_logging()
logger = logging.getLogger(__name__)

ENV_MCP_ALLOWED_HOSTS = "JIUWENBOX_MCP_ALLOWED_HOSTS"

_DEFAULT_ALLOWED_HOSTS = [
    "localhost",
    "localhost:8321",
    "127.0.0.1",
    "127.0.0.1:8321",
]


def _build_transport_security() -> TransportSecuritySettings:
    allowed = list(_DEFAULT_ALLOWED_HOSTS)
    extra = os.environ.get(ENV_MCP_ALLOWED_HOSTS, "").strip()
    if extra:
        for host in extra.split(","):
            h = host.strip()
            if h and h not in allowed:
                allowed.append(h)
    logger.info("MCP allowed hosts: %s", allowed)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
    )


mcp_server = FastMCP(
    "jiuwenbox-mcp",
    instructions=(
        "JiuwenBox sandbox execution server. "
        "Use sandbox_run_command to execute commands inside isolated sandboxes."
    ),
    transport_security=_build_transport_security(),
)


class SandboxRunCommandParams(BaseModel):
    """Parameters for sandbox_run_command MCP tool.

    FastMCP automatically flattens BaseModel fields into individual MCP tool
    parameters in the JSON Schema, so MCP clients still pass each field as a
    separate argument (not a nested object).
    """

    command: list[str]
    timeout_seconds: int = 10
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin: str | None = None
    sandbox_id: str | None = None
    keep_sandbox: bool = False


@mcp_server.tool()
async def sandbox_run_command(
    params: SandboxRunCommandParams,
) -> dict[str, Any]:
    """Execute a command inside a JiuwenBox sandbox.

    If sandbox_id is provided, reuses that sandbox.
    Otherwise, creates a temporary sandbox and deletes it after execution
    (unless keep_sandbox is True).
    """
    command = params.command
    timeout_seconds = params.timeout_seconds
    workdir = params.workdir
    env = params.env
    stdin = params.stdin
    sandbox_id = params.sandbox_id
    keep_sandbox = params.keep_sandbox

    if not command:
        return {
            "sandbox_id": sandbox_id or "",
            "exit_code": -1,
            "stdout": "",
            "stderr": "command must not be empty",
            "duration_ms": 0,
            "created_sandbox": False,
            "deleted_sandbox": False,
        }

    if timeout_seconds < 1:
        timeout_seconds = 1
    elif timeout_seconds > 300:
        timeout_seconds = 300

    from jiuwenbox.server.app import get_manager

    mgr = get_manager()

    created_sandbox = False
    deleted_sandbox = False
    used_sandbox_id = sandbox_id

    if used_sandbox_id is None:
        ref = await mgr.create_sandbox(SandboxSpec(env=env or {}))
        used_sandbox_id = ref.id
        created_sandbox = True
        logger.info("MCP auto-created sandbox %s", used_sandbox_id)

    start_ms = time.monotonic()
    try:
        stdin_data = stdin.encode() if stdin else None
        result = await mgr.exec_in_sandbox(
            sandbox_id=used_sandbox_id,
            request=SandboxExecRequest(
                command=list(command),
                workdir=workdir,
                env=env,
                stdin_data=stdin_data,
                timeout=float(timeout_seconds),
            ),
        )
        duration_ms = round((time.monotonic() - start_ms) * 1000)
    except Exception as exc:
        duration_ms = round((time.monotonic() - start_ms) * 1000)
        logger.exception("MCP sandbox_run_command failed: %s", exc)
        if created_sandbox and not keep_sandbox:
            try:
                await mgr.delete_sandbox(used_sandbox_id)
                deleted_sandbox = True
            except Exception:
                logger.exception(
                    "MCP failed to delete sandbox %s after exec error",
                    used_sandbox_id,
                )
        return {
            "sandbox_id": used_sandbox_id,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": duration_ms,
            "created_sandbox": created_sandbox,
            "deleted_sandbox": deleted_sandbox,
        }

    if created_sandbox and not keep_sandbox:
        try:
            await mgr.delete_sandbox(used_sandbox_id)
            deleted_sandbox = True
        except Exception:
            logger.exception(
                "MCP failed to delete sandbox %s after exec",
                used_sandbox_id,
            )

    return {
        "sandbox_id": used_sandbox_id,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": duration_ms,
        "created_sandbox": created_sandbox,
        "deleted_sandbox": deleted_sandbox,
    }
