# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer requests for the already assembled V2 package manager."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, ClassVar

from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.errors import OperationError
from openjiuwen.core.extension.management import QueryResult

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.assembly import get_extension_manager
from jiuwenswarm.extensions_v2.management.recovery import query_after_error
from jiuwenswarm.extensions_v2.management.registry import CODE_MANAGER_NOT_INITIALIZED, ExtensionManager
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)


class PackageManagementAdapter(GatewayAdapter):
    """Validate management requests without reimplementing the Core lifecycle."""

    methods: ClassVar[frozenset[str]] = frozenset(
        {
            ReqMethod.EXTENSIONS_V2_QUERY.value,
            ReqMethod.EXTENSIONS_V2_INSTALL.value,
            ReqMethod.EXTENSIONS_V2_UNINSTALL.value,
            ReqMethod.EXTENSIONS_V2_RELOAD.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method.value if isinstance(request.req_method, ReqMethod) else request.req_method
        try:
            paths, package_id = _parse_params(method, request.params)
        except ValueError as exc:
            return _error_response(request, str(exc), code="INVALID_PARAMS")

        manager = get_extension_manager()
        if manager is None or not manager.initialized:
            return _error_response(request, "V2 package manager is unavailable", code=CODE_MANAGER_NOT_INITIALIZED)

        result: object
        install_reports: list[ActivationReport] | None = None
        try:
            if method == ReqMethod.EXTENSIONS_V2_QUERY.value:
                key, result = "state", await manager.query(package_id or None)
            elif method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
                install_reports = await manager.install(paths)
                key, result = "reports", install_reports
            elif method == ReqMethod.EXTENSIONS_V2_UNINSTALL.value:
                key, result = "reports", [await manager.uninstall(package_id)]
            else:
                key, result = "reports", [await manager.reload(package_id)]
        except ExtensionFailure as exc:
            return await _failed_request(request, manager, package_id, exc.message, code=exc.status.name)
        except Exception:  # noqa: BLE001 - keep management failures at the request boundary
            logger.exception("[ExtensionsV2] management request failed: %s", method)
            return await _failed_request(request, manager, package_id, "V2 package management failed")
        try:
            payload = {key: _encode(result)}
            if install_reports is not None:
                payload["installed_packages"] = _installed_packages(manager, install_reports)
        except Exception:  # noqa: BLE001 - a received Report is not an unknown operation result
            logger.exception("[ExtensionsV2] could not encode management result")
            return _error_response(request, "Could not encode V2 package management result")
        if install_reports is not None:
            payload["ui_notified"] = await _notify_installed_packages(install_reports)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
            agent_ref=request.agent_ref,
        )


async def _notify_installed_packages(reports: Sequence[ActivationReport]) -> bool:
    """Refresh mounted WebUI slots after a confirmed install from any entry point."""
    succeeded = [report.package_id for report in reports if report.status == "SUCCESS"]
    if not succeeded:
        return False
    from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

    try:
        runtime = get_webui_runtime()
        return await runtime.notify_packages_installed(succeeded) if runtime is not None else False
    except Exception:  # noqa: BLE001 - notification failure does not undo a successful install
        logger.warning("[ExtensionsV2] installation UI notification failed", exc_info=True)
        return False


def _installed_packages(
    manager: ExtensionManager,
    reports: Sequence[ActivationReport],
) -> list[dict[str, str | int]]:
    """Return actual roots for the generations this install activated."""
    succeeded = {
        (report.package_id, report.current_generation)
        for report in reports
        if report.status == "SUCCESS"
    }
    if not succeeded:
        return []
    return [
        {
            "package_id": record.package_id,
            "version": record.version,
            "install_path": str(record.root),
            "generation": record.generation,
        }
        for record in manager.active_packages()
        if (record.package_id, record.generation) in succeeded
    ]


def _parse_params(method: object, params: object) -> tuple[list[Path], str]:
    allowed = PackageManagementAdapter.methods
    if not isinstance(method, str) or method not in allowed:
        raise ValueError("unsupported V2 package management method")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
        if set(params) != {"paths"}:
            raise ValueError("install requires only paths")
        raw_paths = params["paths"]
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("paths must be a nonempty array of absolute paths")
        paths: list[Path] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("each path must be a nonempty string")
            path = Path(raw_path)
            if not path.is_absolute():
                raise ValueError("install requires an absolute ZIP file path")
            if path.suffix.lower() != ".zip" or not path.is_file():
                raise ValueError("install accepts existing .zip files only; directories are not supported")
            paths.append(path)
        return paths, ""
    if set(params) - {"package_id"}:
        raise ValueError("only package_id is accepted")
    if method == ReqMethod.EXTENSIONS_V2_QUERY.value and "package_id" not in params:
        return [], ""
    package_id = params.get("package_id")
    if (
        not isinstance(package_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", package_id) is None
        or ".." in package_id
    ):
        raise ValueError(
            "package_id must use 1-128 letters, digits, dots, underscores or hyphens, without spaces or '..'"
        )
    return [], package_id


def _error_response(
    request: AgentRequest,
    error: str,
    *,
    code: str = "INTERNAL_ERROR",
    extra: dict[str, Any] | None = None,
) -> AgentResponse:
    response = build_error_response(request, error, code=code, extra=extra)
    response.agent_ref = request.agent_ref
    return response


async def _failed_request(
    request: AgentRequest,
    manager: ExtensionManager,
    package_id: str,
    error: str,
    *,
    code: str = "INTERNAL_ERROR",
) -> AgentResponse:
    if request.req_method in (ReqMethod.EXTENSIONS_V2_QUERY, ReqMethod.EXTENSIONS_V2_QUERY.value):
        return _error_response(request, error, code=code)
    extra: dict[str, Any] = {"outcome": "unknown"}
    state, query_error = await query_after_error(manager, package_id or None)
    if query_error is not None:
        extra["query_error"] = query_error
    else:
        try:
            extra["state"] = _encode(state)
        except Exception:  # noqa: BLE001 - keep the original operation error if state encoding fails
            logger.exception("[ExtensionsV2] could not encode current package state")
            extra["query_error"] = "Current package state could not be confirmed"
    return _error_response(request, error, code=code, extra=extra)


def _encode(value: object) -> Any:
    """Encode Core value objects without publishing their implementation enums."""
    if isinstance(value, QueryResult):
        state = {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
        state["installed_packages"] = [
            {"package_id": root.parent.name, "version": root.name, "install_path": str(root)}
            for root in value.installed
        ]
        return state
    if isinstance(value, OperationError):
        return {"code": value.code, "message": value.message, "params": _encode(value.params)}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported management result type: {type(value).__name__}")
