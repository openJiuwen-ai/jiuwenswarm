# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DesignerAdapter: designer.graph.* / designer.run.* executed in AgentServer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import portalocker

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATE_HANDLER,
    DesignerGraphValidationError,
    apply_graph_patch,
    build_bootstrap_graph,
    node_pipeline,
    node_role,
    NODE_ROLE_COMPOSE,
    normalize_execution_graph,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.common.utils import get_agent_root_dir, get_agent_sessions_dir
from jiuwenswarm.common.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    DESIGN_WORK_MODE,
    is_default_project_id,
)
from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode

logger = logging.getLogger(__name__)

_store = DesignerGraphStore()
_executor = GraphExecutor(_store)


def _workspace_receipt_path(token: str) -> Path:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return get_agent_root_dir() / "designer" / "workspace_receipts" / f"{digest}.json"


@asynccontextmanager
async def _workspace_create_lock(token: str):
    """Serialize one creation token across coroutines and AgentServer processes."""
    receipt_path = _workspace_receipt_path(token)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = receipt_path.with_suffix(".lock")
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        while not acquired:
            try:
                portalocker.lock(
                    handle,
                    portalocker.LOCK_EX | portalocker.LOCK_NB,
                )
                acquired = True
            except portalocker.exceptions.LockException:
                await asyncio.sleep(0.05)
        yield
    finally:
        if acquired:
            portalocker.unlock(handle)
        handle.close()


def _read_workspace_receipt(
    token: str,
    signature: str,
) -> tuple[dict[str, Any] | None, str | None, str | None] | None:
    path = _workspace_receipt_path(token)
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    if str(receipt.get("signature") or "") != signature:
        return None, "create_token was already used with different parameters", "CONFLICT"
    project_id = str(receipt.get("project_id") or "")
    if not project_id:
        # Compatibility for receipts written by the initial implementation.
        payload = receipt.get("payload")
        project = payload.get("project") if isinstance(payload, dict) else None
        project_id = str(project.get("project_id") or "") if isinstance(project, dict) else ""
    current, error, _ = _get_design_workspace({"project_id": project_id})
    if error is not None or current is None:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return current, None, None


def _write_workspace_receipt(
    token: str,
    signature: str,
    payload: dict[str, Any],
) -> None:
    path = _workspace_receipt_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    project = payload.get("project")
    project_id = str(project.get("project_id") or "") if isinstance(project, dict) else ""
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            {"signature": signature, "project_id": project_id},
            handle,
            ensure_ascii=False,
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _ok_response(request: AgentRequest, payload: Any) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )


def _request_params(request: AgentRequest) -> dict[str, Any]:
    return request.params if isinstance(request.params, dict) else {}


def _error(
    request: AgentRequest,
    message: str,
    code: str = "BAD_REQUEST",
) -> AgentResponse:
    return build_error_response(request, message, code=code)


def hydrate_graph_node_outputs(
    graph: dict[str, Any],
    run: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy the latest run's node outputs onto the graph so switching graphs shows real previews."""
    if not graph or not run:
        return graph
    states = run.get("node_states") or {}
    if not isinstance(states, dict) or not states:
        return graph
    nodes = []
    changed = False
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            nodes.append(node)
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if config.get("user_replaced_output"):
            nodes.append(node)
            continue
        state = states.get(str(node.get("id") or "")) or {}
        ref = state.get("output_ref") if isinstance(state, dict) else None
        if isinstance(ref, dict) and str(ref.get("uri") or "").strip():
            if node.get("output_ref") != ref:
                node = {**node, "output_ref": dict(ref)}
                changed = True
        nodes.append(node)
    if not changed:
        return graph
    return {**graph, "nodes": nodes}


def _get_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    graph = _executor.reconcile_loaded_graph(graph)
    run = _store.get_latest_run_for_graph(graph_id)
    return {"graph": dict(hydrate_graph_node_outputs(graph, run))}, None, None


def _run_clip_summary(run: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not run:
        return False, None
    states = run.get("node_states") or {}
    ordered = []
    compose = states.get("n_compose")
    if isinstance(compose, dict):
        ordered.append(compose)
    ordered.extend(
        state
        for key, state in states.items()
        if key != "n_compose" and isinstance(state, dict)
    )
    for state in ordered:
        ref = state.get("output_ref") or {}
        if not isinstance(ref, dict):
            continue
        uri = str(ref.get("uri") or "")
        if ref.get("kind") == "video" and uri.startswith("file:"):
            label = str(ref.get("label") or "").strip() or None
            return True, label
    return False, None


def _summarize_graph(graph: dict[str, Any], run: dict[str, Any] | None = None) -> dict[str, Any]:
    if run is None:
        run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    has_video, clip_label = _run_clip_summary(run)
    return {
        "graph_id": graph.get("graph_id"),
        "project_id": graph.get("project_id"),
        "title": graph.get("title") or graph.get("graph_id"),
        "updated_at": graph.get("updated_at"),
        "run_id": run.get("run_id") if run else None,
        "run_status": run.get("status") if run else None,
        "has_video": has_video,
        "clip_label": clip_label,
    }


def _design_title(prompt: str) -> str:
    from jiuwenswarm.server.runtime.session.project_store import sanitize_project_dir_name

    return sanitize_project_dir_name(prompt, max_len=80)


def _design_workspace_messages(session_id: str) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.session.session_history import load_history_records

    messages: list[dict[str, Any]] = []
    for record in load_history_records(session_id):
        event_type = str(record.get("event_type") or "")
        if not event_type.startswith("design."):
            continue
        role = str(record.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(record.get("content") or "")
        if not content and event_type != "design.graph_updated":
            continue
        messages.append(
            {
                "id": str(record.get("id") or uuid.uuid4()),
                "role": role,
                "content": content,
                "kind": str(record.get("design_kind") or (
                    "user" if role == "user" else "chat_ack"
                )),
                "createdAt": int(float(record.get("timestamp") or 0) * 1000),
                **(
                    {"references": record["references"]}
                    if isinstance(record.get("references"), list)
                    else {}
                ),
            }
        )
    return messages


def _get_design_workspace(
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if project is None or project.hidden or project.work_mode != DESIGN_WORK_MODE:
        return None, "design project not found", "NOT_FOUND"
    sessions = [
        item
        for item in collect_all_sessions_metadata()
        if str(item.get("project_id") or "") == project_id
        and str(item.get("work_mode") or "") == DESIGN_WORK_MODE
    ]
    sessions.sort(key=lambda item: float(item.get("created_at") or 0))
    graphs = _store.list_graphs_for_project(project_id)
    if len(sessions) != 1 or len(graphs) != 1:
        return None, "design workspace invariant is not satisfied", "CONFLICT"
    session = sessions[0]
    graph = graphs[0]
    run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    return {
        "project": {
            "project_id": project.project_id,
            "name": project.name,
            "project_dir": project.project_dir,
            "work_mode": project.work_mode,
        },
        "session": {
            "session_id": session.get("session_id"),
            "title": session.get("title"),
            "project_id": project_id,
            "project_dir": project.project_dir,
            "work_mode": DESIGN_WORK_MODE,
        },
        "graph": dict(hydrate_graph_node_outputs(graph, run)),
        "messages": _design_workspace_messages(str(session.get("session_id") or "")),
    }, None, None


def _rollback_design_workspace(
    *,
    project_id: str,
    project_dir: str,
    session_id: str,
    graph_id: str,
) -> None:
    if graph_id:
        _store.delete_graph(graph_id)
    if session_id:
        shutil.rmtree(get_agent_sessions_dir() / session_id, ignore_errors=True)
    if project_id:
        try:
            project_store.purge_project(project_id)
        except Exception:
            logger.warning("Failed to roll back design project %s", project_id, exc_info=True)
    if project_dir:
        shutil.rmtree(project_dir, ignore_errors=True)


async def _create_design_workspace_once(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    from jiuwenswarm.server.runtime.session.session_history import write_history_records
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata,
        update_session_metadata,
    )

    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "prompt is required", "BAD_REQUEST"
    model_name = str(params.get("model_name") or "").strip()
    short_id = uuid.uuid4().hex[:8]
    title = _design_title(prompt)
    directory_name = f"{_design_title(prompt)}-{short_id}"
    project_dir = str(get_agent_root_dir() / "workspace" / DESIGN_WORK_MODE / directory_name)
    project_id = ""
    session_id = f"design_{uuid.uuid4().hex}"
    graph_id = ""
    committed = False
    try:
        Path(project_dir).mkdir(parents=True, exist_ok=False)
        try:
            project, _ = project_store.create_project_checked(
                title,
                project_dir,
                DESIGN_WORK_MODE,
            )
        except project_store.ProjectNameConflict:
            project, _ = project_store.create_project_checked(
                f"{title}-{short_id}",
                project_dir,
                DESIGN_WORK_MODE,
            )
        project_id = project.project_id
        init_session_metadata(
            session_id=session_id,
            channel_id=request.channel_id or "web",
            user_id=str(request.user_id or ""),
            title=title,
            mode="designer",
            project_dir=project_dir,
            project_id=project_id,
            persist_session=True,
            model=model_name,
            work_mode=DESIGN_WORK_MODE,
        )
        bootstrap_params = {
            "prompt": prompt,
            "project_id": project_id,
            "work_mode": DESIGN_WORK_MODE,
            "optimize_for": "quality",
            "session_id": session_id,
            **({"model_name": model_name} if model_name else {}),
            **(
                {"references": params["references"]}
                if isinstance(params.get("references"), list)
                else {}
            ),
        }
        bootstrap, error, code = await _bootstrap_graph_with_director(
            bootstrap_params,
            request.channel_id,
            _leader_progress_callback(request),
        )
        if error is not None or not isinstance(bootstrap, dict):
            return None, error or "failed to create graph", code or "INTERNAL_ERROR"
        graph = bootstrap.get("graph")
        if not isinstance(graph, dict) or not graph.get("graph_id"):
            return None, "bootstrap response missing graph", "INTERNAL_ERROR"
        graph_id = str(graph["graph_id"])
        graph = dict(graph)
        metadata = dict(graph.get("metadata") or {})
        metadata["session_id"] = session_id
        metadata["work_mode"] = DESIGN_WORK_MODE
        if model_name:
            metadata["model_name"] = model_name
        graph["metadata"] = metadata
        graph = dict(_store.save_graph(graph))

        generated_title = _design_title(str(graph.get("title") or title))
        if generated_title and generated_title != project.name:
            try:
                renamed = project_store.rename_project(project_id, generated_title)
                if renamed is not None:
                    project = renamed
                    title = generated_title
            except (project_store.ProjectNameConflict, ValueError):
                pass

        now = time.time()
        records = [
            {
                "id": f"{uuid.uuid4()}:user",
                "role": "user",
                "request_id": request.request_id,
                "channel_id": request.channel_id or "web",
                "timestamp": now,
                "content": prompt,
                "event_type": "design.user",
                "design_kind": "user",
                **(
                    {"references": params["references"]}
                    if isinstance(params.get("references"), list)
                    else {}
                ),
            },
            {
                "id": f"{uuid.uuid4()}:assistant",
                "role": "assistant",
                "request_id": request.request_id,
                "channel_id": request.channel_id or "web",
                "timestamp": now + 0.001,
                "content": "Director composed the workflow.",
                "event_type": "design.bootstrap_completed",
                "design_kind": "bootstrap_done",
                "graph_id": graph_id,
            },
        ]
        write_history_records(session_id, records, preserve_existing_format=False)
        update_session_metadata(
            session_id=session_id,
            title=title,
            set_message_count=2,
            last_user_message_at=now,
            touch_last_message_at=True,
            sync_write=True,
        )
        committed = True
        return {
            "project": {
                "project_id": project_id,
                "name": title,
                "project_dir": project_dir,
                "work_mode": DESIGN_WORK_MODE,
            },
            "session": {
                "session_id": session_id,
                "title": title,
                "project_id": project_id,
                "project_dir": project_dir,
                "work_mode": DESIGN_WORK_MODE,
            },
            "graph": graph,
            "messages": _design_workspace_messages(session_id),
        }, None, None
    except FileExistsError:
        return None, "design workspace directory already exists", "CONFLICT"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create design workspace")
        return None, str(exc), "INTERNAL_ERROR"
    finally:
        # A workspace is committed only when all four durable resources exist.
        if not committed:
            _rollback_design_workspace(
                project_id=project_id,
                project_dir=project_dir,
                session_id=session_id,
                graph_id=graph_id,
            )


async def _create_design_workspace(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    token = str(params.get("create_token") or "").strip()
    prompt = str(params.get("prompt") or "").strip()
    if not token:
        return None, "create_token is required", "BAD_REQUEST"
    signature = hashlib.sha256(
        json.dumps(
            {
                "prompt": prompt,
                "references": params.get("references") or [],
                "model_name": str(params.get("model_name") or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    async with _workspace_create_lock(token):
        receipt = _read_workspace_receipt(token, signature)
        if receipt is not None:
            return receipt
        result = await _create_design_workspace_once(request, params)
        payload, error, _ = result
        if payload is None or error is not None:
            return result
        try:
            _write_workspace_receipt(token, signature, payload)
        except OSError as exc:
            project = payload.get("project")
            session = payload.get("session")
            graph = payload.get("graph")
            _rollback_design_workspace(
                project_id=(
                    str(project.get("project_id") or "")
                    if isinstance(project, dict)
                    else ""
                ),
                project_dir=(
                    str(project.get("project_dir") or "")
                    if isinstance(project, dict)
                    else ""
                ),
                session_id=(
                    str(session.get("session_id") or "")
                    if isinstance(session, dict)
                    else ""
                ),
                graph_id=(
                    str(graph.get("graph_id") or "")
                    if isinstance(graph, dict)
                    else ""
                ),
            )
            logger.exception("Failed to persist Design idempotency receipt")
            return None, f"failed to persist workspace receipt: {exc}", "INTERNAL_ERROR"
        return result


def _list_graphs(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if is_default_project_id(project_id):
        project_id = ""
    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
        graphs = _store.list_graphs_for_project(project_id)
    else:
        graphs = _store.list_graphs()
    # Whole-graph bodies blow the WebSocket send budget once the store grows;
    # callers load the one graph they need through designer.graph.get.
    payload: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for graph in graphs:
        item = dict(graph)
        run = _store.get_latest_run_for_graph(str(item.get("graph_id") or ""))
        summaries.append(_summarize_graph(item, run))
        # Do not hydrate outputs or ship full node bodies here — Agent WS
        # send budget is 6MB; a page of Designer graphs with images exceeds it.
        payload.append(
            {
                "graph_id": item.get("graph_id"),
                "project_id": item.get("project_id"),
                "title": item.get("title"),
                "updated_at": item.get("updated_at"),
                "schema_version": item.get("schema_version"),
            }
        )
    return {
        "graphs": payload,
        "summaries": summaries,
    }, None, None


async def _push_designer_event(
    *,
    request: AgentRequest,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    try:
        from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

        body = {"event_type": event_type, **payload}
        await WebSocketGatewayPushTransport().send_push(
            {
                "request_id": request.request_id or f"designer-{event_type}-{utc_now_ms()}",
                "channel_id": request.channel_id or "web",
                "session_id": request.session_id,
                "payload": body,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DesignerAdapter] push %s failed: %s", event_type, exc)


def _run_update_callback(request: AgentRequest):
    loop = asyncio.get_running_loop()

    def on_update(updated: dict[str, Any], node_id: str | None = None) -> None:
        snapshot = deepcopy(updated)
        node_payload = dict(snapshot)

        async def _emit() -> None:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": snapshot},
            )
            if node_id:
                await _push_designer_event(
                    request=request,
                    event_type=EventType.DESIGNER_NODE_UPDATED.value,
                    payload={"run": node_payload, "node_id": node_id},
                )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_emit()))

    return on_update


def _leader_progress_callback(request: AgentRequest):
    loop = asyncio.get_running_loop()

    def on_progress(kind: str, text: str, tool: str = "") -> None:
        activity = {
            "kind": str(kind or "stage"),
            "text": str(text or ""),
            "tool": str(tool or ""),
            "at": utc_now_ms(),
        }

        async def _emit() -> None:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_LEADER_ACTIVITY.value,
                payload={"activity": activity},
            )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_emit()))

    return on_progress


def _graph_update_callback(request: AgentRequest):
    loop = asyncio.get_running_loop()

    def on_graph(graph: dict[str, Any]) -> None:
        snapshot = deepcopy(graph)

        async def _emit() -> None:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_GRAPH_UPDATED.value,
                payload={"graph": snapshot},
            )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_emit()))

    return on_graph


def _save_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    raw_graph = params.get("graph")
    if not isinstance(raw_graph, dict):
        return None, "graph is required", "BAD_REQUEST"
    try:
        graph = normalize_execution_graph(raw_graph)
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            Director().onboard_user_added_nodes(graph)
        except Exception:
            logger.debug("Director user-node onboard on save failed", exc_info=True)
        saved = _store.save_graph(graph)
        _persist_uploaded_outputs(saved)
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _persist_uploaded_outputs(graph: dict[str, Any]) -> None:
    """Keep the latest run aligned with a still the user uploaded onto a node."""
    from jiuwenswarm.server.runtime.designer.handlers.common import (
        apply_uploaded_outputs_to_run,
    )

    run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    if run is None or not apply_uploaded_outputs_to_run(run, graph):
        return
    run["updated_at"] = utc_now_ms()
    _store.save_run(run)


def _patch_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    raw_patch = params.get("patch")
    if raw_patch is None:
        raw_patch = {
            key: params[key]
            for key in (
                "title",
                "description",
                "upsert_nodes",
                "upsert_edges",
                "remove_node_ids",
                "remove_edge_ids",
            )
            if key in params
        }
    try:
        # Mark upserted nodes as user-added when the patch did not already set it
        # (canvas dock / successor add paths stamp this on the client).
        if isinstance(raw_patch, dict):
            upsert = raw_patch.get("upsert_nodes")
            if isinstance(upsert, list):
                stamped = []
                for item in upsert:
                    if not isinstance(item, dict):
                        stamped.append(item)
                        continue
                    node = dict(item)
                    cfg = dict(node.get("config") or {})
                    if "user_added" not in cfg:
                        cfg["user_added"] = True
                    node["config"] = cfg
                    stamped.append(node)
                raw_patch = {**raw_patch, "upsert_nodes": stamped}
        next_graph = apply_graph_patch(graph, raw_patch)
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            Director().onboard_user_added_nodes(next_graph)
        except Exception:
            logger.debug("Director user-node onboard on patch failed", exc_info=True)
        saved = _store.save_graph(next_graph)
        _persist_uploaded_outputs(saved)
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _bootstrap_graph(
    params: dict[str, Any],
    channel_id: str,
    analysis: dict[str, Any] | None = None,
    on_progress: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    prompt = str(params.get("prompt") or "").strip()
    raw_references = params.get("references")
    if raw_references is None:
        raw_references = params.get("user_references")
    has_references = isinstance(raw_references, list) and len(raw_references) > 0
    if not prompt and not has_references:
        return None, "prompt is required", "BAD_REQUEST"
    if not prompt:
        prompt = "根据参考素材创作"

    project_id = str(params.get("project_id") or "").strip()
    if is_default_project_id(project_id):
        project_id = ""
    project_payload: dict[str, Any] | None = None
    resolved_project_dir = ""

    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
        if (
            project.work_mode == DESIGN_WORK_MODE
            and _store.list_graphs_for_project(project_id)
        ):
            return None, "design workspace already has a graph", "CONFLICT"
        resolved_project_dir = str(project.project_dir or "")
    else:
        name = str(params.get("name") or "").strip()
        if not name:
            from jiuwenswarm.server.runtime.session.project_store import (
                sanitize_project_dir_name,
            )

            name = sanitize_project_dir_name(prompt[:80] or "Designer Project")
        else:
            from jiuwenswarm.server.runtime.session.project_store import (
                sanitize_project_dir_name,
            )

            name = sanitize_project_dir_name(name)
        work_mode, mode_error = resolve_request_work_mode(params, channel_id)
        if mode_error is not None:
            return None, f"invalid work_mode: {params.get('work_mode')!r}", mode_error
        project_dir = str(params.get("project_dir") or "").strip()
        if project_dir and not os.path.isabs(project_dir):
            return None, "project_dir must be an absolute path", "BAD_REQUEST"
        if project_dir and not os.path.isdir(project_dir):
            return None, "project directory does not exist", "PROJECT_DIR_MISSING"
        if not project_dir:
            try:
                project_dir = project_store.resolve_default_project_dir(name, work_mode)
            except ValueError as exc:
                return None, str(exc), "BAD_REQUEST"
            try:
                os.makedirs(project_dir, exist_ok=True)
            except OSError as exc:
                return None, f"failed to create project directory: {exc}", "INTERNAL_ERROR"
        try:
            project, restored = project_store.create_project_checked(
                name,
                project_dir,
                work_mode,
            )
        except project_store.ProjectDirConflict:
            existing = project_store.get_project_by_dir_and_mode(
                project_dir, work_mode, cache_bust=True
            )
            if existing is None or existing.hidden:
                return None, "project_dir already exists", "CONFLICT"
            project, restored = existing, True
        except project_store.ProjectNameConflict:
            return None, "project name already exists", "CONFLICT"
        except ValueError as exc:
            return None, str(exc), "BAD_REQUEST"
        project_id = project.project_id
        resolved_project_dir = str(project.project_dir or "")
        project_payload = {
            "project_id": project.project_id,
            "project_dir": project.project_dir,
            "restored": restored,
            "work_mode": project.work_mode or DEFAULT_WEB_WORK_MODE,
        }

    title = params.get("title")
    optimize_raw = str(params.get("optimize_for") or params.get("optimizeFor") or "quality")
    optimize_for = "cost" if optimize_raw.strip().lower() == "cost" else "quality"
    from pathlib import Path as _Path

    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_REQUIRED,
    )
    from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph
    from jiuwenswarm.server.runtime.designer.user_references import (
        UserReferenceError,
        attach_user_references_to_graph,
        normalize_user_references,
    )

    try:
        refs_dir = (
            _Path(resolved_project_dir) / ".designer" / "refs"
            if resolved_project_dir
            else _Path(".") / ".designer" / "refs"
        )
        user_refs = normalize_user_references(raw_references, dest_dir=refs_dir)
    except UserReferenceError as exc:
        return None, str(exc), exc.code

    # Catalog templates must not be dumped onto the canvas. Director/Leader
    # always owns topology via the smart video pipeline.
    if callable(on_progress):
        on_progress("thinking", "Director · Reading brief")
    # Never call LLM from this sync thread (asyncio.run breaks AsyncOpenAI).
    # Caller must pass LLM analysis from the main event loop.
    if not isinstance(analysis, dict) or str(analysis.get("source") or "") != "llm":
        return (
            None,
            "Designer requires a successful LLM cast/shot analysis before building the graph.",
            LLM_REQUIRED,
        )
    if callable(on_progress):
        on_progress(
            "tool_call",
            "Director · Materializing graph",
            "build_smart_video_graph",
        )
    try:
        from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate

        graph = apply_runtime_delegate(
            build_smart_video_graph(
                project_id=project_id,
                prompt=prompt,
                analysis=analysis,
                title=str(title).strip() if isinstance(title, str) else None,
                optimize_for=optimize_for,
            )
        )
    except DesignerLlmError as exc:
        return None, exc.user_message, exc.code
    meta = dict(graph.get("metadata") or {})
    meta["optimize_for"] = optimize_for
    meta["scenario"] = "video"
    meta["script_analysis"] = analysis
    # Enter already authored Brief→Storyboard→Graph — skip Play redesign.
    meta["director_composed_on_bootstrap"] = True
    # Keep the shot set built above. Storyboard completion must not replace nodes.
    meta["freeze_shot_topology"] = True
    if isinstance(analysis.get("scene_locks"), dict) and analysis["scene_locks"]:
        meta["scene_locks"] = analysis["scene_locks"]
    graph["metadata"] = meta
    graph = attach_skills_metadata(graph, prompt)
    if callable(on_progress):
        cast_n = sum(
            1
            for n in (graph.get("nodes") or [])
            if str(n.get("id") or "").startswith("n_character")
        )
        on_progress(
            "stage",
            f"Director · Graph materialised ({cast_n} solo cards)",
        )
    if user_refs:
        graph = attach_user_references_to_graph(graph, user_refs)
    if resolved_project_dir:
        metadata = dict(graph.get("metadata") or {})
        metadata["project_dir"] = resolved_project_dir
        graph["metadata"] = metadata
    session_id = str(params.get("session_id") or "").strip()
    if session_id:
        metadata = dict(graph.get("metadata") or {})
        metadata["session_id"] = session_id
        graph["metadata"] = metadata
    if callable(on_progress):
        on_progress("stage", "Director · Saving graph")
    saved = _store.save_graph(graph)
    payload: dict[str, Any] = {"graph": dict(saved), "project_id": project_id}
    if project_payload is not None:
        payload["project"] = project_payload
    return payload, None, None


def _get_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if run_id:
        run = _store.get_run(run_id)
        if run is None:
            return None, "run not found", "NOT_FOUND"
        return {"run": dict(run)}, None, None
    graph_id = str(params.get("graph_id") or "").strip()
    if graph_id:
        run = _store.get_latest_run_for_graph(graph_id)
        if run is None:
            return None, "run not found", "NOT_FOUND"
        return {"run": dict(run)}, None, None
    return None, "run_id or graph_id is required", "BAD_REQUEST"


def _rerun_error_message(graph: dict[str, Any] | None, node_id: str, exc: Exception) -> str:
    message = str(exc)
    if "upstream not ready" not in message:
        return message
    if graph is not None and _is_comfyui_target(graph, node_id):
        missing = message.split("upstream not ready:", 1)[-1].strip()
        label = next(
            (
                str(node.get("label") or missing)
                for node in graph.get("nodes") or []
                if isinstance(node, dict) and str(node.get("id") or "") == missing
            ),
            missing,
        )
        return f"ComfyUI reference has no file yet; upload or generate it first: {label}"
    role = ""
    if graph is not None:
        for node in graph.get("nodes") or []:
            if str(node.get("id") or "") == node_id:
                role = node_pipeline(node) or node_role(node)
                break
    if role == NODE_ROLE_COMPOSE or node_id in {"n_compose", "n_final"}:
        detail = message.split("upstream not ready:", 1)[-1].strip()
        if detail and detail != message:
            return f"已连接的视频「{detail}」还没有文件，无法合并。请先生成该片段。"
        return "已连接的视频片段还没有文件，无法合并。请先生成这些片段。"
    return message


def _is_comfyui_target(graph: dict[str, Any], node_id: str) -> bool:
    from jiuwenswarm.common.schema.designer_graph import is_comfyui_node

    return any(
        str(node.get("id") or "") == node_id and is_comfyui_node(node)
        for node in graph.get("nodes") or []
        if isinstance(node, dict)
    )


def _start_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    graph = _store.get_graph(graph_id) if graph_id else None
    contribution_warning = ""
    if graph is not None and not (node_id and _is_comfyui_target(graph, node_id)):
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            audit = Director().audit_contribution_for_run(graph)
            contribution_warning = str(audit.get("warning") or "")
            _store.save_graph(graph)
        except Exception:
            logger.debug("Director contribution audit failed", exc_info=True)
    if run_id:
        existing = _store.get_run(run_id)
        if existing is None:
            return None, "run not found", "NOT_FOUND"
        if graph is None:
            graph = _store.get_graph(existing["graph_id"])
        elif str(existing.get("graph_id") or "") != str(graph.get("graph_id") or ""):
            return None, "run does not belong to graph", "BAD_REQUEST"
        if node_id:
            if graph is None:
                return None, "graph not found", "NOT_FOUND"
            try:
                run = _executor.create_rerun(graph, source_run=existing, node_id=node_id)
            except ValueError as exc:
                return None, _rerun_error_message(graph, node_id, exc), "BAD_REQUEST"
            except KeyError:
                return None, "node not found", "NOT_FOUND"
            run_id = run["run_id"]
        elif (existing.get("metadata") or {}).get("target_node_id"):
            # The workflow Continue action explicitly leaves single-node scope.
            if _executor.has_active_tasks(existing["graph_id"]):
                return None, "任务正在运行或停止，请稍后继续工作流", "BAD_REQUEST"
            existing["metadata"].pop("target_node_id")
            _store.save_run(existing)
    elif graph is not None and node_id:
        source = _store.get_latest_run_for_graph(graph["graph_id"])
        # A ComfyUI node runs on its own, so it needs no earlier Play.
        if source is None and not _is_comfyui_target(graph, node_id):
            return None, "no previous run to rerun from", "BAD_REQUEST"
        try:
            run = _executor.create_rerun(graph, source_run=source, node_id=node_id)
        except ValueError as exc:
            return None, _rerun_error_message(graph, node_id, exc), "BAD_REQUEST"
        except KeyError:
            return None, "node not found", "NOT_FOUND"
        run_id = run["run_id"]
    elif graph is not None:
        run = _executor.create_run(graph)
        run_id = run["run_id"]
    else:
        return None, "graph_id or run_id is required", "BAD_REQUEST"
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    # Stamp warning onto the run record so the UI can show it without blocking.
    if contribution_warning:
        try:
            run_obj = _store.get_run(run_id)
            if isinstance(run_obj, dict):
                run_obj = dict(run_obj)
                run_obj["warning"] = contribution_warning
                warnings = list(run_obj.get("warnings") or [])
                if contribution_warning not in warnings:
                    warnings.append(contribution_warning)
                run_obj["warnings"] = warnings[:8]
                _store.save_run(run_obj)
        except Exception:
            logger.debug("Failed to stamp run contribution warning", exc_info=True)
    return {
        "run_id": run_id,
        "graph_id": graph["graph_id"],
        "warning": contribution_warning or None,
        "warnings": [contribution_warning] if contribution_warning else [],
    }, None, None


def _pause_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    try:
        run = _executor.pause_run(run_id)
    except KeyError:
        return None, "run not found", "NOT_FOUND"
    return {"run": dict(run)}, None, None


def _cancel_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    try:
        run = _executor.cancel_run(run_id)
    except KeyError:
        return None, "run not found", "NOT_FOUND"
    return {"run": dict(run)}, None, None


def _choose_output(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    choice = str(params.get("choice") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    if not node_id:
        return None, "node_id is required", "BAD_REQUEST"
    try:
        run = _executor.choose_output(run_id, node_id, choice)
    except KeyError:
        return None, "run or node not found", "NOT_FOUND"
    except ValueError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"run": dict(run)}, None, None


async def _chat_graph(request: AgentRequest, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    from jiuwenswarm.server.runtime.designer.leader_chat import run_leader_chat
    from jiuwenswarm.server.runtime.designer.chat_document_sync import (
        ChatDocumentConflict,
        read_chat_documents,
        save_document_update,
    )
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        require_llm,
        use_preferred_designer_model,
    )

    graph_id = str(params.get("graph_id") or "").strip()
    message = str(params.get("message") or params.get("prompt") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    if not message:
        return None, "message is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    selected_node_id = str(params.get("selected_node_id") or params.get("node_id") or "").strip()
    run_new_nodes = bool(params.get("run_new_nodes") or params.get("runNewNodes"))
    progress = _leader_progress_callback(request)
    try:
        require_llm()
        run = _store.get_latest_run_for_graph(graph_id)
        documents = read_chat_documents(graph, run)
        pending_documents = any(
            state.get("candidate_output_ref") or state.get("candidate_output_refs")
            for node_id, state in ((run or {}).get("node_states") or {}).items() if node_id in documents
        )
        was_active = _executor.has_active_tasks(graph_id)
        preferred_model = str((graph.get("metadata") or {}).get("model_name") or "")
        with use_preferred_designer_model(preferred_model):
            result = await run_leader_chat(
                graph,
                message,
                documents=documents,
                pending_documents=pending_documents,
                selected_node_id=selected_node_id,
                run_new_nodes=run_new_nodes,
                progress=progress,
            )
        updated_text_uris = []
        saved = graph
        if result.get("changed") or result.get("run_node_ids"):
            if was_active or _executor.has_active_tasks(graph_id):
                return None, "工作流任务尚未结束；运行中请先停止，任务正在停止时请稍后重试。", "CONFLICT"
            current_graph = _store.get_graph(graph_id)
            current_run = _store.get_latest_run_for_graph(graph_id)
            if (current_graph != graph or current_run != run
                    or read_chat_documents(current_graph, current_run) != documents):
                return None, "工作流或文本已在本次请求期间发生变化，请重新提交。", "CONFLICT"
        if result.get("changed"):
            saved, run, updated_text_uris = save_document_update(
                _store, result["graph"], run, documents, result["texts"]
            )
        progress("stage", str(result.get("summary") or "done"))
    except DesignerLlmError as exc:
        return None, exc.user_message, exc.code
    except ChatDocumentConflict as exc:
        return None, str(exc), "CONFLICT"
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DesignerAdapter] graph chat failed: %s", exc)
        return None, str(exc), "INTERNAL_ERROR"

    summary = str(result.get("summary") or "")
    session_id = str((saved.get("metadata") or {}).get("session_id") or "").strip()
    if session_id:
        from jiuwenswarm.server.runtime.session.session_history import append_history_record

        now = time.time()
        history_request_id = request.request_id or str(uuid.uuid4())
        append_history_record(
            session_id=session_id,
            request_id=history_request_id,
            channel_id=request.channel_id or "web",
            role="user",
            content=message,
            timestamp=now,
            event_type="design.user",
            extra={"design_kind": "user", "graph_id": graph_id},
            mode="designer",
        )
        append_history_record(
            session_id=session_id,
            request_id=history_request_id,
            channel_id=request.channel_id or "web",
            role="assistant",
            content=summary or "Updated the workflow.",
            timestamp=now + 0.001,
            event_type="design.graph_updated",
            extra={"design_kind": "chat_ack", "graph_id": graph_id},
            mode="designer",
        )
    run_payload = dict(run) if run is not None and result.get("changed") else None
    run_ids = list(result.get("run_node_ids") or [])
    if run_ids:
        start_params: dict[str, Any] = {"graph_id": saved["graph_id"], "node_id": run_ids[0]}
        latest = _store.get_latest_run_for_graph(saved["graph_id"])
        if latest is not None:
            start_params["run_id"] = str(latest.get("run_id") or "")
        payload, error, code = _start_run(start_params)
        if error is None and payload is not None:
            run = await _executor.start_run(
                str(payload["run_id"]),
                on_update=_run_update_callback(request),
                on_graph_update=_graph_update_callback(request),
            )
            run_payload = dict(run)
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": run_payload},
            )
        elif error:
            result["summary"] = f"{result.get('summary') or ''} ({error})".strip()
            summary = str(result["summary"])
    return {
        "graph": dict(saved),
        "updated_text_uris": updated_text_uris,
        "summary": summary,
        "intent": result.get("intent") or "answer",
        "run_node_ids": run_ids,
        "run": run_payload,
    }, None, None


async def _bootstrap_graph_with_director(
    params: dict[str, Any],
    channel_id: str,
    on_progress: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    from jiuwenswarm.server.runtime.designer.model_tools import (
        use_preferred_designer_model,
    )

    with use_preferred_designer_model(str(params.get("model_name") or "").strip()):
        return await _bootstrap_graph_with_director_impl(
            params,
            channel_id,
            on_progress,
        )


async def _bootstrap_graph_with_director_impl(
    params: dict[str, Any],
    channel_id: str,
    on_progress: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Enter: seed cast → Brief/Director → Storyboard/Director → Graph/Director.

    Never runs AsyncOpenAI inside ``asyncio.to_thread`` / ``asyncio.run``.
    """
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        require_llm,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import analyze_creative_brief
    from jiuwenswarm.server.runtime.designer.user_references import (
        analysis_prompt_with_references,
        classify_reference_images,
        normalize_user_references,
    )

    prompt = str(params.get("prompt") or "").strip() or "根据参考素材创作"
    raw_references = params.get("references")
    if raw_references is None:
        raw_references = params.get("user_references")
    # One Work/Code-style local credential gate at the user-visible Enter boundary.
    # Nested helpers call the model bluntly; billing/API failures raise there.
    try:
        require_llm()
    except DesignerLlmError as exc:
        return None, exc.user_message, exc.code
    try:
        user_refs_preview = (
            normalize_user_references(raw_references, dest_dir=None)
            if isinstance(raw_references, list) and raw_references
            else []
        )
    except Exception:  # noqa: BLE001
        user_refs_preview = []
    analysis_prompt = analysis_prompt_with_references(prompt, user_refs_preview)

    if callable(on_progress):
        on_progress("thinking", "Director · Extracting cast and scenes (LLM)")
    try:
        analysis = await analyze_creative_brief(
            analysis_prompt,
            timeout_sec=120.0,
            reference_images=[
                str(item.get("path") or "")
                for item in user_refs_preview
                if str(item.get("kind") or "") == "image"
                and str(item.get("path") or "").strip()
            ],
        )
    except DesignerLlmError as exc:
        return None, exc.user_message, exc.code
    if not isinstance(analysis, dict) or str(analysis.get("source") or "") != "llm":
        return (
            None,
            "Chat model did not return a usable cast/shot analysis.",
            "LLM_API_ERROR",
        )
    if callable(on_progress):
        n = len(analysis.get("characters") or [])
        on_progress(
            "stage",
            f"Director · LLM cast locked ({n} characters)",
        )
    image_refs = [
        item
        for item in user_refs_preview
        if str(item.get("kind") or "") == "image" and str(item.get("path") or "").strip()
    ]
    if image_refs:
        if callable(on_progress):
            on_progress("thinking", "Supervisor · Reading reference images")
        reads = await classify_reference_images(prompt, image_refs)
        if not reads:
            return (
                None,
                "Attached stills could not be assigned a reference role.",
                "LLM_API_ERROR",
            )
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            ReferenceIntentError,
            stamp_creative_intent,
        )

        try:
            analysis = stamp_creative_intent(analysis, reads, image_refs)
        except ReferenceIntentError as exc:
            return None, str(exc), "LLM_API_ERROR"

    payload, error, code = await asyncio.to_thread(
        _bootstrap_graph,
        params,
        channel_id,
        analysis,
        on_progress,
    )
    if error is not None or not isinstance(payload, dict):
        return payload, error, code
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        return payload, error, code

    from jiuwenswarm.server.runtime.designer.orchestration import Director
    from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate

    optimize_for = str(
        params.get("optimize_for")
        or (graph.get("metadata") or {}).get("optimize_for")
        or "quality"
    )
    try:
        if callable(on_progress):
            on_progress("thinking", "Director · Authoring brief (LLM)")
        await Director().author_creative_brief(graph)
        if callable(on_progress):
            on_progress("thinking", "Director · Reviewing / approving brief")
        await Director().review_brief(graph)
        if callable(on_progress):
            on_progress("thinking", "Director · Designing storyboard (LLM)")
        await Director().author_storyboard(graph)
        if callable(on_progress):
            on_progress("thinking", "Director · Reviewing / approving storyboard")
        await Director().review_storyboard(graph)
        if callable(on_progress):
            on_progress(
                "tool_call",
                "Director · Designing execution graph (LLM)",
                "design_execution_graph",
            )
        await Director().design_execution_graph(
            graph,
            optimize_for=optimize_for,
        )
        if callable(on_progress):
            on_progress("thinking", "Director · Validating / approving graph + locks")
        await Director().validate_plan(graph)
        graph = apply_runtime_delegate(graph)
        meta = dict(graph.get("metadata") or {})
        cast_n = sum(
            1
            for n in (graph.get("nodes") or [])
            if str(n.get("id") or "").startswith("n_character")
        )
        meta["director_composed_on_bootstrap"] = True
        graph["metadata"] = meta
        if callable(on_progress):
            on_progress(
                "stage",
                f"Director · Approved — {cast_n} solo cards",
            )
        saved = _store.save_graph(graph)
        payload = dict(payload)
        payload["graph"] = dict(saved)
        return payload, None, None
    except DesignerLlmError as exc:
        logger.info(
            "Director bootstrap approval chain failed (LLM): %s",
            exc,
            exc_info=True,
        )
        return None, exc.user_message, exc.code
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Director bootstrap approval chain failed: %s",
            exc,
            exc_info=True,
        )
        return (
            None,
            f"Director approval chain failed: {exc}",
            "LLM_API_ERROR",
        )


class DesignerAdapter(GatewayAdapter):
    """Designer execution graph adapter."""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.DESIGNER_WORKSPACE_CREATE.value,
            ReqMethod.DESIGNER_WORKSPACE_GET.value,
            ReqMethod.DESIGNER_GRAPH_GET.value,
            ReqMethod.DESIGNER_GRAPH_LIST.value,
            ReqMethod.DESIGNER_GRAPH_SAVE.value,
            ReqMethod.DESIGNER_GRAPH_BOOTSTRAP.value,
            ReqMethod.DESIGNER_GRAPH_PATCH.value,
            ReqMethod.DESIGNER_GRAPH_CHAT.value,
            ReqMethod.DESIGNER_RUN_START.value,
            ReqMethod.DESIGNER_RUN_GET.value,
            ReqMethod.DESIGNER_RUN_PAUSE.value,
            ReqMethod.DESIGNER_RUN_CANCEL.value,
            ReqMethod.DESIGNER_RUN_CHOOSE_OUTPUT.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method
        params = _request_params(request)
        try:
            if method == ReqMethod.DESIGNER_WORKSPACE_CREATE:
                payload, error, code = await _create_design_workspace(request, params)
            elif method == ReqMethod.DESIGNER_WORKSPACE_GET:
                payload, error, code = await asyncio.to_thread(
                    _get_design_workspace, params
                )
            elif method == ReqMethod.DESIGNER_GRAPH_GET:
                payload, error, code = await asyncio.to_thread(_get_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_LIST:
                payload, error, code = await asyncio.to_thread(_list_graphs, params)
            elif method == ReqMethod.DESIGNER_GRAPH_SAVE:
                payload, error, code = await asyncio.to_thread(_save_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_BOOTSTRAP:
                # Enter: provisional graph, then Director LLM approval chain.
                payload, error, code = await _bootstrap_graph_with_director(
                    params,
                    request.channel_id,
                    _leader_progress_callback(request),
                )
            elif method == ReqMethod.DESIGNER_GRAPH_PATCH:
                payload, error, code = await asyncio.to_thread(_patch_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_CHAT:
                payload, error, code = await _chat_graph(request, params)
            elif method == ReqMethod.DESIGNER_RUN_GET:
                payload, error, code = await asyncio.to_thread(_get_run, params)
            elif method == ReqMethod.DESIGNER_RUN_START:
                payload, error, code = await asyncio.to_thread(_start_run, params)
                if error is None and payload is not None:
                    run = await _executor.start_run(
                        str(payload["run_id"]),
                        on_update=_run_update_callback(request),
                        on_graph_update=_graph_update_callback(request),
                    )
                    run_out = dict(run)
                    warning = str(payload.get("warning") or "").strip()
                    warnings = [
                        str(x)
                        for x in (payload.get("warnings") or [])
                        if str(x).strip()
                    ]
                    if warning:
                        run_out["warning"] = warning
                        if warning not in warnings:
                            warnings = [warning, *warnings]
                    if warnings:
                        run_out["warnings"] = warnings[:8]
                    await _push_designer_event(
                        request=request,
                        event_type=EventType.DESIGNER_RUN_UPDATED.value,
                        payload={"run": run_out},
                    )
                    return _ok_response(
                        request,
                        {
                            "run": run_out,
                            "warning": warning or None,
                            "warnings": warnings,
                        },
                    )
            elif method == ReqMethod.DESIGNER_RUN_PAUSE:
                payload, error, code = await asyncio.to_thread(_pause_run, params)
            elif method == ReqMethod.DESIGNER_RUN_CANCEL:
                payload, error, code = await asyncio.to_thread(_cancel_run, params)
            elif method == ReqMethod.DESIGNER_RUN_CHOOSE_OUTPUT:
                payload, error, code = await asyncio.to_thread(_choose_output, params)
            else:
                return build_error_response(
                    request,
                    f"unsupported method: {method}",
                    code="NOT_IMPLEMENTED",
                )
        except DesignerLlmError as exc:
            logger.warning("[DesignerAdapter] %s LLM failed: %s", method, exc)
            return build_error_response(request, exc.user_message, code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DesignerAdapter] %s failed: %s", method, exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")

        if error is not None:
            return build_error_response(request, error, code=code or "BAD_REQUEST")
        if isinstance(payload, dict) and payload.get("graph") is not None and method in {
            ReqMethod.DESIGNER_GRAPH_BOOTSTRAP,
            ReqMethod.DESIGNER_GRAPH_PATCH,
            ReqMethod.DESIGNER_GRAPH_CHAT,
        }:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_GRAPH_UPDATED.value,
                payload={"graph": payload["graph"]},
            )
        if isinstance(payload, dict) and payload.get("run") is not None and method in {
            ReqMethod.DESIGNER_RUN_PAUSE,
            ReqMethod.DESIGNER_RUN_CANCEL,
            ReqMethod.DESIGNER_RUN_CHOOSE_OUTPUT,
        }:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": payload["run"]},
            )
        return _ok_response(request, payload)
