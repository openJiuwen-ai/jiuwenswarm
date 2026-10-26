# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer：工作区列目录 / 用量 / 删除 / 预览 / 下载。"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_multi_tenant_user_workspace_dir
from jiuwenswarm.common.workspace.quota import (
    get_cached_used,
    mark_usage_active,
    measure_and_cache,
)
from jiuwenswarm.common.workspace.service import WorkspaceError, WorkspaceService
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


def _tenant_root_from_request(request: Any) -> Any:
    wk = getattr(request, "workspace_key", None) or None
    if isinstance(getattr(request, "params", None), dict):
        wk = wk or request.params.get("workspace_key")
    sid = getattr(request, "service_id", None)
    aid = getattr(request, "agent_id", None)
    if is_enterprise():
        key = str(wk or "default").strip() or "default"
        return get_multi_tenant_user_workspace_dir(key)
    return get_multi_tenant_user_workspace_dir(
        service_id=str(sid).strip() if sid else None,
        agent_id=str(aid).strip() if aid else None,
    )


def _identity_from_params(params: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(params.get("user_id") or "").strip(),
        str(params.get("group_id") or "").strip(),
        str(params.get("bot_id") or "").strip(),
    )


def _force_refresh(params: dict[str, Any]) -> bool:
    for key in ("force_refresh", "forceRefresh", "refresh"):
        raw = params.get(key)
        if raw is True:
            return True
        if isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _resolve_used_bytes(root: Any, params: dict[str, Any]) -> int:
    uid, gid, bid = _identity_from_params(params)
    if _force_refresh(params):
        return measure_and_cache(root, user_id=uid, group_id=gid, bot_id=bid)
    mark_usage_active(root, user_id=uid, group_id=gid, bot_id=bid)
    return get_cached_used(root)


def _error_response(request: Any, *, code: str, message: str) -> AgentResponse:
    return AgentResponse(
        request_id=getattr(request, "request_id", "") or "",
        channel_id=getattr(request, "channel_id", "") or "",
        ok=False,
        payload={"error": message, "code": code},
    )


def _ok_response(request: Any, payload: dict[str, Any]) -> AgentResponse:
    return AgentResponse(
        request_id=getattr(request, "request_id", "") or "",
        channel_id=getattr(request, "channel_id", "") or "",
        ok=True,
        payload=payload,
    )


async def handle_workspace(ctx: RequestContext) -> None:
    """Dispatch workspace.* 文件与用量方法。"""
    request = ctx.request
    method = request.req_method
    params = request.params if isinstance(request.params, dict) else {}
    try:
        root = _tenant_root_from_request(request)
        svc = WorkspaceService(tenant_root=root)
        if method == ReqMethod.WORKSPACE_TREE:
            data = svc.list_tree(params.get("relative_path"))
            # 不再附带全量 du；用量由独立 usage 接口 / 后台校准提供。
            resp = _ok_response(request, data)
        elif method == ReqMethod.WORKSPACE_USAGE:
            used = await asyncio.to_thread(_resolve_used_bytes, root, params)
            resp = _ok_response(request, {"used_bytes": used})
        elif method == ReqMethod.WORKSPACE_ENTRIES_DELETE:
            paths = params.get("relative_paths")
            if not isinstance(paths, list) or not paths:
                resp = _error_response(
                    request, code="BAD_REQUEST", message="relative_paths required"
                )
            else:
                data = svc.delete_entries([str(x) for x in paths])
                used = await asyncio.to_thread(_resolve_used_bytes, root, params)
                data = {
                    **data,
                    "used_bytes": used,
                }
                resp = _ok_response(request, data)
        elif method == ReqMethod.WORKSPACE_PREVIEW:
            max_bytes = params.get("max_bytes")
            try:
                max_n = int(max_bytes) if max_bytes is not None else None
            except (TypeError, ValueError):
                max_n = None
            # 预览正文不写日志（§2.6）
            data = svc.preview_file(params.get("relative_path"), max_bytes=max_n)
            resp = _ok_response(request, data)
        elif method == ReqMethod.WORKSPACE_DOWNLOAD:
            payload = svc.download_payload(params.get("relative_path"))
            content = payload.get("content") or b""
            if not isinstance(content, (bytes, bytearray)):
                content = bytes(content)
            resp = _ok_response(
                request,
                {
                    "filename": payload.get("filename") or "download.bin",
                    "content_type": payload.get("content_type")
                    or "application/octet-stream",
                    "size_bytes": len(content),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                },
            )
        else:
            resp = _error_response(request, code="BAD_REQUEST", message="unknown method")
    except WorkspaceError as exc:
        resp = _error_response(request, code=exc.code, message=exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[workspace] handler failed: %s", exc)
        resp = _error_response(request, code="INTERNAL_ERROR", message=str(exc))

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)
