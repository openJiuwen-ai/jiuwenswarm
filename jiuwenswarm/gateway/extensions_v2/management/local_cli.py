# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local CLI adapter owned exclusively by the Gateway runtime."""

from __future__ import annotations

import asyncio
import re
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.extension.errors import OperationError

from jiuwenswarm.common.extension_cli import METHODS, LocalControl, source_domain
from jiuwenswarm.gateway.extensions_v2.management.notifications import notify_installed


async def start_local_cli(runtime: Any) -> LocalControl:
    """Start a domain-restricted adapter for this live Gateway manager."""
    async def handle(action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "presentation.query":
            return runtime.browser.presentations.query(params)
        if action == "presentation.start":
            return await runtime.browser.presentations.start(params)
        manager = runtime.packages
        package_id = params.get("package_id")
        try:
            if action == "install":
                raw_paths = params.get("paths")
                if set(params) != {"paths"} or not isinstance(raw_paths, list) or not raw_paths:
                    raise ValueError("install requires paths")
                paths: list[Path] = []
                for value in raw_paths:
                    if not isinstance(value, str):
                        raise ValueError("path must be a string")
                    path = Path(value)
                    if not path.is_absolute() or path.suffix.lower() != ".zip" or not path.is_file():
                        raise ValueError("path must be an existing absolute ZIP")
                    if await asyncio.to_thread(source_domain, path) != "gateway":
                        return _error("DOMAIN_MISMATCH", "该 ZIP 不属于 Gateway。")
                    paths.append(path)
            else:
                if set(params) - {"package_id"}:
                    raise ValueError("only package_id is accepted")
                if "package_id" in params and (
                    not isinstance(package_id, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", package_id) is None
                    or ".." in package_id
                ):
                    raise ValueError("invalid package_id")
                if action != "query" and not package_id:
                    raise ValueError("package_id is required")
        except (OSError, ValueError, KeyError):
            return _error("INVALID_PARAMS", "包管理参数或 ZIP manifest 无效。")
        try:
            if action == "query":
                return {"ok": True, "payload": {"state": manager.query(package_id)}}
            if action == "install":
                reports = await manager.install(paths)
            else:
                if not manager.owns(package_id):
                    return _error("PACKAGE_NOT_FOUND", "Gateway 没有该包的安装副本。")
                operation = manager.reload if action == "reload" else manager.uninstall
                reports = [await operation(package_id)]
            payload = {"reports": [_encode(report) for report in reports]}
            succeeded = [report.package_id for report in reports if report.status == "SUCCESS"]
            if action in ("install", "reload") and succeeded:
                payload["ui_notified"] = await notify_installed(runtime, succeeded)
            return {"ok": True, "payload": payload}
        except Exception:  # noqa: BLE001 - the CLI must preserve an uncertain mutation result
            result = _error("INTERNAL_ERROR", "包管理结果未确认。")
            result["payload"]["outcome"] = "unknown"
            return result

    control = LocalControl("gateway", handle, methods=METHODS | {"presentation.query", "presentation.start"})
    try:
        await control.start()
    except BaseException:
        await control.close()
        raise
    return control


def _encode(value: Any) -> Any:
    if isinstance(value, OperationError):
        return {"code": value.code, "message": value.message, "params": _encode(value.params)}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "payload": {"code": code, "error": message}}
