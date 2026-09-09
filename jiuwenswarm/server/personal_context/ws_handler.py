# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PersonalContext WebSocket request dispatch on JiuwenSwarm's existing E2A wire path."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from openjiuwen.core.common.exception.errors import BaseError

from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.ws_send import send_wire_payload

if TYPE_CHECKING:
    from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


logger = logging.getLogger(__name__)

PERSONAL_CONTEXT_REQUEST_METHODS = frozenset(
    {
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_COLLECTION,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_COLLECTION,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_AGENT_USE,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_GET_CONFIG,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_PATCH_CONFIG,
        ReqMethod.PERSONAL_CONTEXT_RUNTIME_SELECT_MODEL,
        ReqMethod.PERSONAL_CONTEXT_FETCH_LIST_SERVICES,
        ReqMethod.PERSONAL_CONTEXT_FETCH_CREATE_SERVICE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_DELETE_SERVICE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_PATCH_SERVICE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_START_SERVICE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_STOP_SERVICE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_RUN_ALL,
        ReqMethod.PERSONAL_CONTEXT_FETCH_RUN_ONE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_STOP_RUN,
        ReqMethod.PERSONAL_CONTEXT_FETCH_GET_RUN_STATUS,
        ReqMethod.PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS,
        ReqMethod.PERSONAL_CONTEXT_FETCH_AUTHORIZE_PROVIDER,
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH,
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_TREE,
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_SEARCH_PAGES,
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_GET_NODE,
        ReqMethod.PERSONAL_CONTEXT_CONTEXT_GET_SOURCE,
    }
)


def _payload(value: object) -> dict[str, object]:
    if value is None:
        return {"ok": True}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    raise TypeError("PersonalContext Host result must be an object")


def _text(params: dict[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


async def _send_result(
    ws: Any,
    request: AgentRequest,
    send_lock: asyncio.Lock,
    payload: dict[str, object],
) -> None:
    if request.is_stream:
        wire = encode_agent_chunk_for_wire(
            AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=payload,
                is_complete=True,
                agent_ref=request.agent_ref,
            ),
            response_id=request.request_id,
            sequence=0,
        )
    else:
        wire = encode_agent_response_for_wire(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
                agent_ref=request.agent_ref,
            ),
            response_id=request.request_id,
        )
    async with send_lock:
        await send_wire_payload(ws, wire)


async def _send_error(
    ws: Any,
    request: AgentRequest,
    send_lock: asyncio.Lock,
    *,
    message: str,
    code: object,
    status: str | None = None,
) -> None:
    payload: dict[str, object] = {"error": message, "code": code}
    if status is not None:
        payload["status"] = status
    if request.is_stream:
        payload["event_type"] = "chat.error"
        wire = encode_agent_chunk_for_wire(
            AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=payload,
                is_complete=True,
                agent_ref=request.agent_ref,
            ),
            response_id=request.request_id,
            sequence=0,
        )
    else:
        wire = encode_agent_response_for_wire(
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
                agent_ref=request.agent_ref,
            ),
            response_id=request.request_id,
        )
    async with send_lock:
        await send_wire_payload(ws, wire)


async def _stream_graph(
    host: PersonalContextHostAPI,
    ws: Any,
    request: AgentRequest,
    send_lock: asyncio.Lock,
    *,
    tree: bool = False,
) -> None:
    root_id = request.params.get("root_id")
    if root_id is not None and (not isinstance(root_id, str) or not root_id.strip()):
        raise ValueError("root_id must be null or a non-empty string")
    depth = request.params.get("depth", 3)
    if type(depth) is not int or not 1 <= depth <= 10:
        raise ValueError("depth must be an integer between 1 and 10")
    normalized_root = root_id.strip() if isinstance(root_id, str) else None
    if tree:
        graph = await host.get_tree(root_id=normalized_root, depth=depth)
    else:
        graph = await host.get_graph(root_id=normalized_root, depth=depth)
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise TypeError("PersonalContext graph nodes and edges must be arrays")
    events: list[dict[str, object]] = [
        {
            "event_type": "personal_context.context.start",
            "context_ready": bool(graph.get("context_ready")),
            "root_id": normalized_root,
            "depth": depth,
        }
    ]
    for start in range(0, len(nodes), 200):
        stop = start + 200
        events.append(
            {
                "event_type": "personal_context.context.nodes",
                "nodes": nodes[start:stop],
            }
        )
    for start in range(0, len(edges), 200):
        stop = start + 200
        events.append(
            {
                "event_type": "personal_context.context.edges",
                "edges": edges[start:stop],
            }
        )
    events.append(
        {
            "event_type": "personal_context.context.end",
            "node_count": len(nodes),
            "edge_count": len(edges),
        }
    )
    for sequence, event in enumerate(events):
        wire = encode_agent_chunk_for_wire(
            AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=event,
                is_complete=sequence == len(events) - 1,
                agent_ref=request.agent_ref,
            ),
            response_id=request.request_id,
            sequence=sequence,
        )
        async with send_lock:
            await send_wire_payload(ws, wire)


async def _execute(
    host: PersonalContextHostAPI,
    request: AgentRequest,
    *,
    runtime_enabled_changed: Callable[[bool], Awaitable[None]] | None = None,
) -> dict[str, object]:
    method = request.req_method
    params = request.params
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_STATUS:
        return _payload(await host.get_status())
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_COLLECTION:
        return _payload(await host.set_collection_enabled(True))
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_COLLECTION:
        return _payload(await host.set_collection_enabled(False))
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_START_AGENT_USE:
        result = await host.set_agent_use_enabled(True)
        await _notify_runtime_enabled(runtime_enabled_changed, True)
        return _payload(result)
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_STOP_AGENT_USE:
        result = await host.set_agent_use_enabled(False)
        await _notify_runtime_enabled(runtime_enabled_changed, False)
        return _payload(result)
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_GET_CONFIG:
        return await host.get_runtime_config()
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_PATCH_CONFIG:
        return await host.patch_runtime_config(
            cast(dict[str, object], params.get("patch"))
        )
    if method == ReqMethod.PERSONAL_CONTEXT_RUNTIME_SELECT_MODEL:
        return await host.select_model(cast(int, params.get("model_index")))
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_LIST_SERVICES:
        return {"services": await host.list_fetch_services()}
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_CREATE_SERVICE:
        service = params.get("service")
        if not isinstance(service, dict):
            raise ValueError("service must be an object")
        return await host.create_fetch_service(cast(dict[str, object], service))
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_DELETE_SERVICE:
        await host.delete_fetch_service(_text(params, "service_id"))
        return {"ok": True}
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_PATCH_SERVICE:
        return await host.patch_fetch_service(
            _text(params, "service_id"),
            cast(dict[str, object], params.get("patch")),
        )
    if method in {
        ReqMethod.PERSONAL_CONTEXT_FETCH_START_SERVICE,
        ReqMethod.PERSONAL_CONTEXT_FETCH_STOP_SERVICE,
    }:
        await host.set_fetch_service_enabled(
            _text(params, "service_id"),
            method == ReqMethod.PERSONAL_CONTEXT_FETCH_START_SERVICE,
        )
        return {"ok": True}
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_RUN_ALL:
        return await host.run_fetch()
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_RUN_ONE:
        return await host.run_fetch(service_id=_text(params, "service_id"))
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_STOP_RUN:
        return await host.stop_fetch_run(_text(params, "service_id"))
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_GET_RUN_STATUS:
        return await host.get_fetch_run_status(
            cast(str | None, params.get("service_id"))
        )
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_GET_AUTHORIZATION_STATUS:
        return await host.get_authorization_status(_text(params, "provider"))
    if method == ReqMethod.PERSONAL_CONTEXT_FETCH_AUTHORIZE_PROVIDER:
        return await host.authorize_provider(_text(params, "provider"))
    if method == ReqMethod.PERSONAL_CONTEXT_CONTEXT_SEARCH_PAGES:
        return await host.search_graph(_text(params, "query"))
    if method == ReqMethod.PERSONAL_CONTEXT_CONTEXT_GET_NODE:
        return await host.get_graph_page(_text(params, "node_id"))
    if method == ReqMethod.PERSONAL_CONTEXT_CONTEXT_GET_SOURCE:
        return await host.get_source(_text(params, "source_id"))
    raise ValueError("unknown PersonalContext method")


async def _notify_runtime_enabled(
    callback: Callable[[bool], Awaitable[None]] | None,
    enabled: bool,
) -> None:
    """Best-effort notification; a Rail refresh must not fail the API call."""

    if callback is None:
        return
    cancelled: asyncio.CancelledError | None = None
    try:
        await callback(enabled)
    except asyncio.CancelledError as exc:
        cancelled = exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "PersonalContext runtime Rail refresh failed: %s",
            type(exc).__name__,
        )
    if cancelled is not None:
        raise cancelled


async def handle_personal_context_request(
    host: PersonalContextHostAPI,
    ws: Any,
    request: AgentRequest,
    send_lock: asyncio.Lock,
    *,
    runtime_enabled_changed: Callable[[bool], Awaitable[None]] | None = None,
) -> None:
    """Execute one parsed PersonalContext request without introducing a PersonalContext wire protocol."""

    try:
        if request.req_method in {
            ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_GRAPH,
            ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_TREE,
        }:
            if not request.is_stream:
                raise ValueError(f"{request.req_method.value} requires is_stream=true")
            await _stream_graph(
                host,
                ws,
                request,
                send_lock,
                tree=(
                    request.req_method == ReqMethod.PERSONAL_CONTEXT_CONTEXT_STREAM_TREE
                ),
            )
            return
        await _send_result(
            ws,
            request,
            send_lock,
            await _execute(
                host,
                request,
                runtime_enabled_changed=runtime_enabled_changed,
            ),
        )
    except asyncio.CancelledError:
        raise
    except ValueError as exc:
        await _send_error(
            ws,
            request,
            send_lock,
            message=str(exc),
            code="BAD_REQUEST",
        )
    except BaseError as exc:
        await _send_error(
            ws,
            request,
            send_lock,
            message=exc.message,
            code=exc.code,
            status=exc.status.name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PersonalContext request failed: %s", type(exc).__name__)
        await _send_error(
            ws,
            request,
            send_lock,
            message="PersonalContext request failed",
            code="INTERNAL_ERROR",
        )
