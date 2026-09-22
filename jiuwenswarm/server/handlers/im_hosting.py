# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""数字分身 / 用户态 IM 托管 RPC."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.im.im_hosting.policy import CHANNEL_IDS
from jiuwenswarm.server.im.im_hosting.service import get_hosting_service

logger = logging.getLogger(__name__)


async def _send(ctx: RequestContext, *, ok: bool, payload: dict[str, Any]) -> None:
    request = ctx.request
    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
    )
    await ctx.sink.send_wire(encode_agent_response_for_wire(resp, response_id=request.request_id))


def _params(ctx: RequestContext) -> dict[str, Any]:
    raw = ctx.request.params or {}
    return raw if isinstance(raw, dict) else {}


async def handle_im_hosting(ctx: RequestContext) -> None:
    method = ctx.request.req_method
    params = _params(ctx)
    svc = get_hosting_service()
    try:
        payload = await _dispatch(svc, method, params)
    except KeyError as exc:
        await _send(ctx, ok=False, payload={"error": f"not found: {exc}", "code": "NOT_FOUND"})
        return
    except ValueError as exc:
        await _send(ctx, ok=False, payload={"error": str(exc), "code": "BAD_REQUEST"})
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("[im_hosting] %s failed", method)
        await _send(ctx, ok=False, payload={"error": str(exc)})
        return
    await _send(ctx, ok=True, payload=payload)


async def _dispatch(svc, method: ReqMethod, params: dict[str, Any]) -> dict[str, Any]:
    if method == ReqMethod.IM_HOSTING_STATUS:
        return svc.status()
    if method == ReqMethod.IM_HOSTING_DISCOVER:
        channel_id = str(params.get("channel_id") or "").strip()
        if channel_id not in CHANNEL_IDS:
            raise ValueError("channel_id required")
        count = params.get("query_count")
        return await svc.discover(channel_id, query_count=int(count) if count else None)
    if method == ReqMethod.IM_HOSTING_TARGETS_LIST:
        channel_id = str(params.get("channel_id") or "").strip() or None
        return {"targets": svc.store.list_targets(channel_id)}
    if method == ReqMethod.IM_HOSTING_TARGETS_ADD:
        channel_id = str(params.get("channel_id") or "").strip()
        kind = str(params.get("target_kind") or params.get("kind") or "").strip()
        external_id = str(params.get("external_id") or "").strip()
        if channel_id not in CHANNEL_IDS or kind not in {"group", "user"} or not external_id:
            raise ValueError("channel_id, target_kind, external_id required")
        target = svc.store.add_target(
            channel_id=channel_id,
            target_kind=kind,
            external_id=external_id,
            title=str(params.get("title") or ""),
            source="manual",
            rule_override=params.get("rule_override"),
            expert_service_id=str(params.get("expert_service_id") or "default").strip() or "default",
            expert_agent_id=str(params.get("expert_agent_id") or "default").strip() or "default",
            expert_persona=str(params.get("expert_persona") or "").strip() or None,
        )
        return {"target": target}
    if method == ReqMethod.IM_HOSTING_TARGETS_PATCH:
        target_id = str(params.get("id") or params.get("target_id") or "").strip()
        if not target_id:
            raise ValueError("id required")
        patch = {k: params[k] for k in (
            "enabled", "title", "poll_interval_seconds", "fetch_count", "rule_override",
            "expert_service_id", "expert_agent_id",
            "expert_persona",
        ) if k in params}
        updated = svc.store.patch_target(target_id, patch)
        if updated is None:
            raise KeyError(target_id)
        return {"target": updated}
    if method == ReqMethod.IM_HOSTING_TARGETS_DELETE:
        target_id = str(params.get("id") or params.get("target_id") or "").strip()
        if not target_id:
            raise ValueError("id required")
        if not svc.store.delete_target(target_id):
            raise KeyError(target_id)
        return {"deleted": True, "id": target_id}
    if method == ReqMethod.IM_HOSTING_POLICY_GET:
        return {"policy": svc.policy.load()}
    if method == ReqMethod.IM_HOSTING_POLICY_PATCH:
        channel_id = str(params.get("channel_id") or "").strip()
        patch = params.get("patch") if isinstance(params.get("patch"), dict) else {
            k: v for k, v in params.items() if k != "channel_id"
        }
        return {"policy": await svc.apply_channel_policy(channel_id, patch)}
    if method == ReqMethod.IM_HOSTING_POLL_NOW:
        target_id = params.get("target_id") or params.get("id")
        channel_id = str(params.get("channel_id") or "").strip() or None
        if target_id:
            results = await svc.poll_now(str(target_id))
        elif channel_id:
            policy = svc.policy.load()
            results = []
            for target in svc.store.list_targets(channel_id):
                if not target.get("enabled"):
                    continue
                ch_policy = policy.get(target["channel_id"]) or {}
                results.append(await svc._poll_target(target, ch_policy))
        else:
            results = await svc.poll_now(None)
        return {"results": results}
    if method == ReqMethod.IM_HOSTING_HISTORY:
        target_id = str(params.get("id") or params.get("target_id") or "").strip()
        if not target_id:
            raise ValueError("id required")
        target = svc.store.get_target(target_id)
        if target is None:
            raise KeyError(target_id)
        from jiuwenswarm.server.im.im_hosting.reply_bridge import load_hosting_history

        return load_hosting_history(target)
    raise ValueError(f"unsupported method {method}")
