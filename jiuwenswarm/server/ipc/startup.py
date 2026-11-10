# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit acceptance of one validated Worker connection before data changes."""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.server.ipc.protocol import KIND_STARTUP_ACCEPTED, PROTOCOL_VERSION


def startup_acceptance(hello: dict[str, Any]) -> dict[str, Any]:
    return {"kind": KIND_STARTUP_ACCEPTED, "protocol": PROTOCOL_VERSION,
            **{key: hello.get(key) for key in ("generation", "instance", "data_scope", "code_identity")}}


async def await_startup_acceptance(conn: Any, hello: dict[str, Any], *, timeout: float = 10.0) -> None:
    message = await asyncio.wait_for(conn.recv(), timeout=timeout)
    if message != startup_acceptance(hello):
        raise RuntimeError("Worker startup was not accepted for this deployment and connection")
