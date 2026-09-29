# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway Web RPC：workspace.tree / usage / entries.delete / preview / download。"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.utils import get_multi_tenant_user_workspace_dir
from jiuwenswarm.common.workspace.quota import snapshot_to_dict
from jiuwenswarm.common.workspace.service import WorkspaceError, WorkspaceService
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.workspace.quota import resolve_quota_snapshot, upsert_usage_cache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AgentForwardCall:
    channel: Any
    agent_client: Any
    ws: Any
    req_id: Any
    params: dict[str, Any]
    session_id: str | None
    req_method: Any


def _identity_from_params(params: dict[str, Any] | None) -> tuple[str, str, str]:
    p = params if isinstance(params, dict) else {}
    return (
        str(p.get("user_id") or "").strip(),
        str(p.get("group_id") or "").strip(),
        str(p.get("bot_id") or "").strip(),
    )


def _identity_for_forward(ws: Any, params: dict[str, Any]) -> tuple[str, str, str]:
    """合成转发身份：params（HTTP 头已 merge）优先，连接级 ``_web_routing`` 兜底。

    企业版 ``apply_invoke_ids_to_envelope`` 依赖信封 ``user_id`` + ``channel_context.routing``
    才能落到正确的 ``workspace_{md5}``；缺身份会落到 ``default_workspace_key``，树接口 404。
    """
    user_id, group_id, bot_id = _identity_from_params(params)
    routing = getattr(ws, "_web_routing", None)
    if not isinstance(routing, dict):
        routing = {}
    if not user_id:
        user_id = str(routing.get("user_id") or "").strip()
        if not user_id:
            try:
                from jiuwenswarm.gateway.channel_manager.web.web_connect import (
                    _WEB_CONNECTION_USER_ID_ATTR,
                )

                user_id = str(getattr(ws, _WEB_CONNECTION_USER_ID_ATTR, None) or "").strip()
            except Exception:  # noqa: BLE001
                user_id = ""
    if not group_id:
        group_id = str(routing.get("group_id") or "").strip()
    if not bot_id:
        bot_id = str(routing.get("bot_id") or "").strip()
    return user_id, group_id, bot_id


async def _forward_to_agent(
    call: _AgentForwardCall,
) -> tuple[bool, dict[str, Any], str | None, str | None]:
    """Forward via E2A; return (ok, payload, error, code)."""
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.request_identity import apply_routing_metadata

    agent_client = call.agent_client
    ac = agent_client() if callable(agent_client) else agent_client
    if ac is None or not getattr(ac, "server_ready", False):
        return False, {}, "agent not ready", "INTERNAL_ERROR"

    params = call.params
    user_id, group_id, bot_id = _identity_for_forward(call.ws, params)
    # 业务 params 不携带身份三元组（与 request_identity 约定一致）；身份走信封。
    forward_params = {
        k: v
        for k, v in (params or {}).items()
        if k not in {"user_id", "group_id", "bot_id", "gateway_id"}
    }
    routing_meta = apply_routing_metadata(
        {},
        {
            "user_id": user_id,
            "group_id": group_id,
            "bot_id": bot_id,
        },
    )
    env = e2a_from_agent_fields(
        request_id=str(call.req_id) if call.req_id else "",
        channel_id=getattr(call.channel, "channel_id", None) or "web",
        session_id=call.session_id,
        req_method=call.req_method,
        params=forward_params,
        user_id=user_id or None,
        metadata=routing_meta or None,
    )
    try:
        resp = await ac.send_request(env)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[workspace] forward to agent failed: %s", exc)
        return False, {}, str(exc), "INTERNAL_ERROR"
    pl = resp.payload if isinstance(resp.payload, dict) else {}
    if not resp.ok:
        return (
            False,
            pl,
            str(pl.get("error") or pl.get("message") or "request failed"),
            str(pl.get("code") or "BAD_REQUEST"),
        )
    return True, pl, None, None


def _local_service(params: dict[str, Any] | None) -> WorkspaceService:
    p = params if isinstance(params, dict) else {}
    if is_enterprise():
        key = str(p.get("workspace_key") or "default").strip() or "default"
        root = get_multi_tenant_user_workspace_dir(key)
    else:
        sid = p.get("service_id")
        aid = p.get("agent_id")
        root = get_multi_tenant_user_workspace_dir(
            service_id=str(sid).strip() if sid else None,
            agent_id=str(aid).strip() if aid else None,
        )
    return WorkspaceService(tenant_root=root)


async def _usage_payload_from_used(
    used_bytes: int,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    user_id, group_id, bot_id = _identity_from_params(params)
    snap = await resolve_quota_snapshot(
        used_bytes,
        user_id=user_id,
        group_id=group_id,
        bot_id=bot_id,
    )
    await upsert_usage_cache(
        user_id=user_id,
        group_id=group_id,
        bot_id=bot_id,
        used_bytes=used_bytes,
    )
    return snapshot_to_dict(snap, user_id=user_id, group_id=group_id, bot_id=bot_id)


def register_workspace_web_methods(channel: Any, *, agent_client: Any) -> None:
    """Register workspace.* WebChannel methods (HTTP mapped routes + WS)."""
    from jiuwenswarm.common.schema.message import ReqMethod

    def _resolve_ac() -> Any:
        if callable(agent_client):
            return agent_client()
        return agent_client

    async def _tree(ws, req_id, params, session_id):
        p = dict(params) if isinstance(params, dict) else {}
        relative_path = p.get("relative_path")
        try:
            if is_enterprise():
                ok, payload, err, code = await _forward_to_agent(
                    _AgentForwardCall(
                        channel=channel,
                        agent_client=_resolve_ac,
                        ws=ws,
                        req_id=req_id,
                        params=p,
                        session_id=session_id,
                        req_method=ReqMethod.WORKSPACE_TREE,
                    ),
                )
                if not ok:
                    await channel.send_response(
                        ws, req_id, ok=False, error=err or "failed", code=code or "BAD_REQUEST"
                    )
                    return
                used = payload.get("used_bytes")
                if isinstance(used, int):
                    await upsert_usage_cache(
                        user_id=str(p.get("user_id") or ""),
                        group_id=str(p.get("group_id") or ""),
                        bot_id=str(p.get("bot_id") or ""),
                        used_bytes=used,
                    )
                data = {k: v for k, v in payload.items() if k != "used_bytes"}
                await channel.send_response(ws, req_id, ok=True, payload=data)
                return

            svc = _local_service(p)
            data = svc.list_tree(relative_path)
            used = svc.measure_used_bytes()
            await upsert_usage_cache(
                user_id=str(p.get("user_id") or ""),
                group_id=str(p.get("group_id") or ""),
                bot_id=str(p.get("bot_id") or ""),
                used_bytes=used,
            )
            await channel.send_response(ws, req_id, ok=True, payload=data)
        except WorkspaceError as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=exc.message, code=exc.code, payload=exc.details or None
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workspace.tree] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )

    async def _usage(ws, req_id, params, session_id):
        p = dict(params) if isinstance(params, dict) else {}
        try:
            if is_enterprise():
                ok, payload, err, code = await _forward_to_agent(
                    _AgentForwardCall(
                        channel=channel,
                        agent_client=_resolve_ac,
                        ws=ws,
                        req_id=req_id,
                        params=p,
                        session_id=session_id,
                        req_method=ReqMethod.WORKSPACE_USAGE,
                    ),
                )
                if not ok:
                    await channel.send_response(
                        ws, req_id, ok=False, error=err or "failed", code=code or "BAD_REQUEST"
                    )
                    return
                used = int(payload.get("used_bytes") or 0)
            else:
                used = _local_service(p).measure_used_bytes()
            out = await _usage_payload_from_used(used, p)
            await channel.send_response(ws, req_id, ok=True, payload=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workspace.usage] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )

    async def _delete(ws, req_id, params, session_id):
        p = dict(params) if isinstance(params, dict) else {}
        paths = p.get("relative_paths")
        if not isinstance(paths, list) or not paths:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="relative_paths required",
                code="BAD_REQUEST",
            )
            return
        try:
            if is_enterprise():
                ok, payload, err, code = await _forward_to_agent(
                    _AgentForwardCall(
                        channel=channel,
                        agent_client=_resolve_ac,
                        ws=ws,
                        req_id=req_id,
                        params=p,
                        session_id=session_id,
                        req_method=ReqMethod.WORKSPACE_ENTRIES_DELETE,
                    ),
                )
                if not ok:
                    await channel.send_response(
                        ws, req_id, ok=False, error=err or "failed", code=code or "BAD_REQUEST"
                    )
                    return
                used = payload.get("used_bytes")
                data = {k: v for k, v in payload.items() if k != "used_bytes"}
                if isinstance(used, int):
                    await upsert_usage_cache(
                        user_id=str(p.get("user_id") or ""),
                        group_id=str(p.get("group_id") or ""),
                        bot_id=str(p.get("bot_id") or ""),
                        used_bytes=used,
                    )
                await channel.send_response(ws, req_id, ok=True, payload=data)
                return

            svc = _local_service(p)
            data = svc.delete_entries([str(x) for x in paths])
            used = svc.measure_used_bytes()
            await upsert_usage_cache(
                user_id=str(p.get("user_id") or ""),
                group_id=str(p.get("group_id") or ""),
                bot_id=str(p.get("bot_id") or ""),
                used_bytes=used,
            )
            await channel.send_response(ws, req_id, ok=True, payload=data)
        except WorkspaceError as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=exc.message, code=exc.code
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workspace.entries.delete] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )

    async def _preview(ws, req_id, params, session_id):
        p = dict(params) if isinstance(params, dict) else {}
        relative_path = p.get("relative_path")
        max_bytes = p.get("max_bytes")
        try:
            max_n = int(max_bytes) if max_bytes is not None else None
        except (TypeError, ValueError):
            max_n = None
        try:
            if is_enterprise():
                ok, payload, err, code = await _forward_to_agent(
                    _AgentForwardCall(
                        channel=channel,
                        agent_client=_resolve_ac,
                        ws=ws,
                        req_id=req_id,
                        params=p,
                        session_id=session_id,
                        req_method=ReqMethod.WORKSPACE_PREVIEW,
                    ),
                )
                if not ok:
                    await channel.send_response(
                        ws, req_id, ok=False, error=err or "failed", code=code or "BAD_REQUEST"
                    )
                    return
                await channel.send_response(ws, req_id, ok=True, payload=payload)
                return

            data = _local_service(p).preview_file(relative_path, max_bytes=max_n)
            await channel.send_response(ws, req_id, ok=True, payload=data)
        except WorkspaceError as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=exc.message, code=exc.code, payload=exc.details or None
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workspace.preview] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )

    async def _download(ws, req_id, params, session_id):
        """返回 filename / content_type / content_b64；HTTP 层再解包成文件流。"""
        p = dict(params) if isinstance(params, dict) else {}
        relative_path = p.get("relative_path")
        try:
            if is_enterprise():
                ok, payload, err, code = await _forward_to_agent(
                    _AgentForwardCall(
                        channel=channel,
                        agent_client=_resolve_ac,
                        ws=ws,
                        req_id=req_id,
                        params=p,
                        session_id=session_id,
                        req_method=ReqMethod.WORKSPACE_DOWNLOAD,
                    ),
                )
                if not ok:
                    await channel.send_response(
                        ws, req_id, ok=False, error=err or "failed", code=code or "BAD_REQUEST"
                    )
                    return
                await channel.send_response(ws, req_id, ok=True, payload=payload)
                return

            blob = _local_service(p).download_payload(relative_path)
            content = blob.get("content") or b""
            if not isinstance(content, (bytes, bytearray)):
                content = bytes(content)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={
                    "filename": blob.get("filename") or "download.bin",
                    "content_type": blob.get("content_type") or "application/octet-stream",
                    "size_bytes": len(content),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                },
            )
        except WorkspaceError as exc:
            await channel.send_response(
                ws, req_id, ok=False, error=exc.message, code=exc.code, payload=exc.details or None
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workspace.download] %s", exc)
            await channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR"
            )

    channel.register_method("workspace.tree", _tree)
    channel.register_method("workspace.usage", _usage)
    channel.register_method("workspace.entries.delete", _delete)
    channel.register_method("workspace.preview", _preview)
    channel.register_method("workspace.download", _download)
