# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""通用 HTTP 审计中间件（纯 ASGI，零 starlette/fastapi 依赖）。

任意 FastAPI/Starlette/ASGI 应用可通过 :func:`install_http_audit_middleware`
接入行为审计：请求身份 UA + 4xx/5xx EVT，经 ``emit`` 回调出口产出
（通常传 audit 的 ``log_event`` 或业务侧包装）。

路径映射 / 排除前缀 / 身份与上下文提取均以安装参数注入——SDK 不感知
具体业务的路径规则与信任策略。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = ["HTTPAuditMiddleware", "install_http_audit_middleware"]


class HTTPAuditMiddleware:
    """纯 ASGI 中间件：/api/v1 请求身份 UA + 4xx/5xx EVT。

    - 非 http scope（websocket 等）直通
    - send 包装捕获 ``http.response.start`` 的状态码
    - 路由未处理异常：记 EVT(500) 后原样上抛（由外层转 500 响应）
    - ``emit`` 回调与 ``ids_extractor`` 由安装参数注入，本类零业务依赖
    """

    def __init__(
        self,
        app: Any,
        *,
        emit: Callable[..., None],
        submdl: str,
        include_prefix: str | tuple[str, ...] = "/api/v1",
        exclude_prefixes: tuple[str, ...] = ("/api/v1/health",),
        proc_map: dict[str, str] | None = None,
        default_proc: str = "",
        error_proc: str = "",
        uid_extractor: Callable[[dict, dict], str | None] | None = None,
        ids_extractor: Callable[[dict, dict], dict[str, str]] | None = None,
    ) -> None:
        self.app = app
        self._emit_fn = emit
        self._submdl = submdl
        self._include_prefix = (
            (include_prefix,) if isinstance(include_prefix, str) else tuple(include_prefix)
        )
        self._exclude_prefixes = tuple(exclude_prefixes)
        self._proc_map = dict(proc_map or {})
        self._default_proc = default_proc
        self._error_proc = error_proc
        self._uid_extractor = uid_extractor
        self._ids_extractor = ids_extractor

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        holder: dict[str, int | None] = {"status": None}

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                holder["status"] = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 — 未处理异常先记 EVT 再上抛
            self._emit(path, scope, 500, error=exc)
            raise
        status = holder["status"]
        if status is not None:
            self._emit(path, scope, status)

    # ------------------------------------------------------------------

    def _emit(self, path: str, scope: dict, status: int, error: Exception | None = None) -> None:
        try:
            if not path.startswith(self._include_prefix) or path.startswith(
                self._exclude_prefixes
            ):
                return
            headers = self._flat_headers(scope)
            method = scope.get("method", "")
            success = status < 400
            proc = self._proc_for(path)
            if status >= 500 and self._error_proc:
                proc = self._error_proc  # 5xx 边界 EVT（路由未匹配时也覆盖）
            if not proc:
                return  # 未匹配 PROC 且非 5xx：不打点（避免轮询噪声）
            self._emit_fn(
                submdl=self._submdl,
                proc=proc,
                success=success,
                uid=self._uid_extractor(scope, headers) if self._uid_extractor else None,
                rspcd=None if success else f"E{status}",
                message=f"{method} {path} status={status}"
                + (f" error={error}" if error else ""),
                level=None if success else "WARN",
                extra={"method": method, "status_code": status},
                **(self._ids_extractor(scope, headers) if self._ids_extractor else {}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[audit] http middleware emit failed: %s: %s", type(exc).__name__, exc)

    def _proc_for(self, path: str) -> str:
        for fragment, proc in self._proc_map.items():
            if fragment in path:
                return proc
        return self._default_proc

    @staticmethod
    def _flat_headers(scope: dict) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }


def install_http_audit_middleware(
    app: Any,
    *,
    emit: Callable[..., None],
    submdl: str,
    include_prefix: str | tuple[str, ...] = "/api/v1",
    exclude_prefixes: tuple[str, ...] = ("/api/v1/health",),
    proc_map: dict[str, str] | None = None,
    default_proc: str = "",
    error_proc: str = "",
    uid_extractor: Callable[[dict, dict], str | None] | None = None,
    ids_extractor: Callable[[dict, dict], dict[str, str]] | None = None,
) -> None:
    """给 ASGI 应用挂全局 HTTP 审计中间件。

    Args:
        emit: 审计事件出口，签名 ``(**fields)``（通常传 audit ``log_event``
            或业务侧包装——企业版门控/上下文回退在出口内自理）。
        submdl: 固定子模块标识（如 ``gateway``）。
        proc_map: 路径片段 → PROC 映射（如 ``{"/file-api/upload": "file_upload"}``）。
        uid_extractor: ``(scope, headers) -> uid|None``，业务信任策略注入点。
        ids_extractor: ``(scope, headers) -> {"session_id": …, "request_id": …}``，
            上下文提取注入点（缺省字段由 SDK ContextVar/注册表回退）。
    """
    app.add_middleware(
        HTTPAuditMiddleware,
        emit=emit,
        submdl=submdl,
        include_prefix=include_prefix,
        exclude_prefixes=exclude_prefixes,
        proc_map=proc_map,
        default_proc=default_proc,
        error_proc=error_proc,
        uid_extractor=uid_extractor,
        ids_extractor=ids_extractor,
    )
