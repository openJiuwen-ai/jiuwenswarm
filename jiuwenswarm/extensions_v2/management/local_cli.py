# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local CLI adapter for the already assembled AgentServer manager."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from jiuwenswarm.common.extension_cli import LocalControl, source_domain
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter, _parse_params


async def start_local_cli() -> LocalControl:
    """Publish the current host's capability to its terminal subprocesses."""
    control = LocalControl("agent", handle)
    try:
        await control.start()
    except BaseException:
        await control.close()
        raise
    return control


async def handle(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate host ownership, then reuse the existing management adapter."""
    method = ReqMethod(f"extensions.v2.{action}")
    try:
        paths, package_id = _parse_params(method.value, params)
        for path in paths:
            if await asyncio.to_thread(source_domain, path) != "agent":
                return _error("DOMAIN_MISMATCH", "该 ZIP 属于 Gateway，请使用对应宿主的管理入口。")
        if package_id and action in {"reload", "uninstall"}:
            from jiuwenswarm.extensions_v2.assembly import get_extension_manager

            manager = get_extension_manager()
            if manager is not None and manager.initialized:
                state = await manager.query(package_id)
                for path in state.installed:
                    if await asyncio.to_thread(source_domain, Path(path)) != "agent":
                        return _error("DOMAIN_MISMATCH", "该安装副本属于 Gateway。")
    except (OSError, ValueError, KeyError):
        return _error("INVALID_PARAMS", "包管理参数或 ZIP manifest 无效。")
    request = AgentRequest(
        request_id=f"extension-cli-{uuid.uuid4().hex}", channel_id="local-extension-cli",
        req_method=method, params=params,
    )
    response = await PackageManagementAdapter().handle(request)
    return {"ok": response.ok, "payload": response.payload}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "payload": {"code": code, "error": message}}
