# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer requests for the already assembled V2 package manager."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, ClassVar

from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.data.json_values import valid_package_id
from openjiuwen.core.extension.errors import OperationError
from openjiuwen.core.extension.management import QueryResult

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.assembly import get_extension_manager
from jiuwenswarm.extensions_v2.config import resolve_examples_dir, resolve_install_dir
from jiuwenswarm.extensions_v2.management.catalog import (
    PackageNotFound,
    find_entry,
    list_catalog,
    list_mine,
    project_detail,
    resolve_install_source,
)
from jiuwenswarm.extensions_v2.management.recovery import query_after_error
from jiuwenswarm.extensions_v2.management.registry import CODE_MANAGER_NOT_INITIALIZED, ExtensionManager
from jiuwenswarm.extensions_v2.manifest_io import (
    DOMAIN_AGENT_SERVER,
    package_domain,
    read_manifest_document,
)
from jiuwenswarm.server.runtime.extension_package_manager import (
    list_package_files,
    read_package_file,
)
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)

_PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class _Request:
    paths: tuple[Path, ...] = ()
    package_ids: tuple[str, ...] = ()
    package_id: str = ""
    path: str = ""
    scope: str = "catalog"
    enable: bool | None = None


class PackageManagementAdapter(GatewayAdapter):
    """Validate management requests without reimplementing the Core lifecycle."""

    methods: ClassVar[frozenset[str]] = frozenset(
        {
            ReqMethod.EXTENSIONS_V2_QUERY.value,
            ReqMethod.EXTENSIONS_V2_CATALOG.value,
            ReqMethod.EXTENSIONS_V2_SHOW.value,
            ReqMethod.EXTENSIONS_V2_INSTALL.value,
            ReqMethod.EXTENSIONS_V2_UNINSTALL.value,
            ReqMethod.EXTENSIONS_V2_RELOAD.value,
            ReqMethod.EXTENSIONS_V2_SET_ENABLED.value,
            ReqMethod.EXTENSIONS_V2_FILE_LIST.value,
            ReqMethod.EXTENSIONS_V2_FILE_READ.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method.value if isinstance(request.req_method, ReqMethod) else request.req_method
        try:
            parsed = _parse_request(method, request.params)
        except ValueError as exc:
            return _error_response(request, str(exc), code="INVALID_PARAMS")

        manager = get_extension_manager()
        if manager is None or not manager.initialized:
            return _error_response(request, "V2 package manager is unavailable", code=CODE_MANAGER_NOT_INITIALIZED)

        try:
            payload = await _dispatch(manager, method, parsed)
        except PackageNotFound as exc:
            return _error_response(request, f"package {exc} not found", code=_PACKAGE_NOT_FOUND)
        except ValueError as exc:
            return _error_response(request, str(exc), code="INVALID_PARAMS")
        except ExtensionFailure as exc:
            return await _failed_request(request, manager, parsed.package_id, exc.message, code=exc.status.name)
        except Exception:  # noqa: BLE001 - keep management failures at the request boundary
            logger.exception("[ExtensionsV2] management request failed: %s", method)
            return await _failed_request(request, manager, parsed.package_id, "V2 package management failed")
        if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
            payload["ui_notified"] = await _notify_installed_packages(payload.get("reports", ()))
        try:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=_encode(payload),
                metadata=request.metadata,
                agent_ref=request.agent_ref,
            )
        except Exception:  # noqa: BLE001 - a received Report is not an unknown operation result
            logger.exception("[ExtensionsV2] could not encode management result")
            return _error_response(request, "Could not encode V2 package management result")


async def _dispatch(manager: ExtensionManager, method: str, parsed: _Request) -> dict[str, Any]:
    if method == ReqMethod.EXTENSIONS_V2_CATALOG.value:
        state = await manager.query()
        examples_dir = resolve_examples_dir()
        packages = (
            list_catalog(examples_dir, state, domain=DOMAIN_AGENT_SERVER)
            if parsed.scope == "catalog"
            else list_mine(examples_dir, state, domain=DOMAIN_AGENT_SERVER)
        )
        return {"packages": packages}
    if method == ReqMethod.EXTENSIONS_V2_SHOW.value:
        state = await manager.query()
        entry = find_entry(
            parsed.package_id,
            resolve_examples_dir(),
            state,
            domain=DOMAIN_AGENT_SERVER,
            install_dir=resolve_install_dir(),
        )
        return {"package": project_detail(entry, state)}
    if method in (ReqMethod.EXTENSIONS_V2_FILE_LIST.value, ReqMethod.EXTENSIONS_V2_FILE_READ.value):
        state = await manager.query()
        entry = find_entry(
            parsed.package_id,
            resolve_examples_dir(),
            state,
            install_dir=resolve_install_dir(),
        )
        if method == ReqMethod.EXTENSIONS_V2_FILE_LIST.value:
            return {"tree": list_package_files(entry.root)}
        return read_package_file(entry.root, parsed.path)
    if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
        paths = list(parsed.paths)
        if parsed.package_ids:
            examples_dir = resolve_examples_dir()
            install_dir = resolve_install_dir()
            paths = [
                resolve_install_source(package_id, examples_dir=examples_dir, install_dir=install_dir)
                for package_id in parsed.package_ids
            ]
        reports = await manager.install(paths)
        return {"reports": reports, "installed_packages": _installed_packages(manager, reports)}
    if method == ReqMethod.EXTENSIONS_V2_UNINSTALL.value:
        return {"reports": [await manager.uninstall(parsed.package_id)]}
    if method == ReqMethod.EXTENSIONS_V2_RELOAD.value:
        return {"reports": [await manager.reload(parsed.package_id)]}
    if method == ReqMethod.EXTENSIONS_V2_SET_ENABLED.value:
        return {"reports": [await manager.set_enabled(parsed.package_id, parsed.enable is True)]}
    return {"state": await manager.query(parsed.package_id or None)}


async def _notify_installed_packages(reports: Sequence[ActivationReport]) -> bool:
    """Refresh mounted WebUI slots after a confirmed install."""
    succeeded = [report.package_id for report in reports if report.status == "SUCCESS"]
    if not succeeded:
        return False
    from jiuwenswarm.extensions_v2.assembly import get_webui_runtime

    try:
        runtime = get_webui_runtime()
        return await runtime.notify_packages_changed(succeeded) if runtime is not None else False
    except Exception:  # noqa: BLE001 - notification failure does not undo a successful install
        logger.warning("[ExtensionsV2] package change UI notification failed", exc_info=True)
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


def _parse_request(method: object, params: object) -> _Request:
    if not isinstance(method, str) or method not in PackageManagementAdapter.methods:
        raise ValueError("unsupported V2 package management method")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    if method == ReqMethod.EXTENSIONS_V2_INSTALL.value:
        if set(params) == {"paths"}:
            return _Request(paths=tuple(_zip_paths(params["paths"])))
        if set(params) == {"package_ids"}:
            return _Request(package_ids=tuple(_package_ids(params["package_ids"])))
        raise ValueError("install requires exactly one of paths or package_ids")

    if method == ReqMethod.EXTENSIONS_V2_CATALOG.value:
        if set(params) - {"scope"}:
            raise ValueError("catalog accepts scope only")
        scope = params.get("scope", "catalog")
        if scope not in {"catalog", "mine"}:
            raise ValueError("scope must be 'catalog' or 'mine'")
        return _Request(scope=scope)

    if method == ReqMethod.EXTENSIONS_V2_SHOW.value:
        if set(params) != {"package_id"}:
            raise ValueError("show requires only package_id")
        return _Request(package_id=_package_id(params["package_id"]))

    if method == ReqMethod.EXTENSIONS_V2_FILE_LIST.value:
        if set(params) != {"package_id"}:
            raise ValueError("file list requires only package_id")
        return _Request(package_id=_package_id(params["package_id"]))

    if method == ReqMethod.EXTENSIONS_V2_FILE_READ.value:
        if set(params) != {"package_id", "path"}:
            raise ValueError("file read requires package_id and path")
        relative = params.get("path")
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("path must be a nonempty string")
        return _Request(package_id=_package_id(params["package_id"]), path=relative.strip())

    if method == ReqMethod.EXTENSIONS_V2_SET_ENABLED.value:
        if set(params) != {"package_id", "enable"}:
            raise ValueError("set enabled requires package_id and enable")
        enable = params.get("enable")
        if not isinstance(enable, bool):
            raise ValueError("enable must be a boolean")
        return _Request(package_id=_package_id(params["package_id"]), enable=enable)

    if set(params) - {"package_id"}:
        raise ValueError("only package_id is accepted")
    if method == ReqMethod.EXTENSIONS_V2_QUERY.value and "package_id" not in params:
        return _Request()
    return _Request(package_id=_package_id(params.get("package_id")))


def _package_id(value: object) -> str:
    if not isinstance(value, str) or not valid_package_id(value):
        raise ValueError(
            "package_id must use 1-128 letters, digits, dots, underscores or hyphens, without spaces or '..'"
        )
    return value


def _package_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("package_ids must be a nonempty array of string ids")
    return [_package_id(item) for item in value]


def _zip_paths(value: object) -> list[Path]:
    if not isinstance(value, list) or not value:
        raise ValueError("paths must be a nonempty array of absolute paths")
    paths: list[Path] = []
    for raw_path in value:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("each path must be a nonempty string")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("install requires an absolute ZIP file path")
        if path.suffix.lower() != ".zip" or not path.is_file():
            raise ValueError("install accepts existing .zip files only; directories are not supported")
        paths.append(path)
    return paths


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
        state["package_ids"] = sorted(record.package_id for record in value.packages if record.successful)
        state["installed_packages"] = [
            {
                "package_id": root.parent.name,
                "version": root.name,
                "domain": _installed_package_domain(root),
                "install_path": str(root),
            }
            for root in value.installed
        ]
        return state
    if isinstance(value, OperationError):
        return {"code": value.code, "message": value.message, "params": _encode(value.params)}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported management result type: {type(value).__name__}")


def _installed_package_domain(root: Path) -> str | None:
    """Read one installed package domain for the slash-command presentation."""
    try:
        return package_domain(read_manifest_document(root))
    except (OSError, UnicodeError, ValueError):
        return None
