# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DesignerAdapter: designer.graph.* / designer.run.* executed in AgentServer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import portalocker

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse

from jiuwenswarm.common.schema.designer_graph import (
    GRAPH_SOURCE_MANUAL,
    SCHEMA_VERSION,
    DesignerGraphValidationError,
    apply_graph_patch,
    new_graph_id,
    normalize_execution_graph,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.common.utils import get_agent_root_dir, get_agent_sessions_dir
from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_WORK,
    DESIGN_WORK_MODE,
    is_default_project_id,
)
from jiuwenswarm.server.runtime.design import RunCoordinator
from jiuwenswarm.server.runtime.design.context import get_graph_coordinator
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.media_settings import (
    MediaSettingsError,
    media_capabilities,
    update_media_settings,
)
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.server.runtime.session import project_store
logger = logging.getLogger(__name__)

_store = DesignerGraphStore()

DESIGNER_AGENT_GROUP = "designer"


def _coordinator_for(graph_id: str) -> RunCoordinator:
    """One RunCoordinator per graph (its run_id lives as long as the graph).

    Shared process-wide with DesignTeamBridge and the capability provider so
    every entry point observes the same in-flight trigger.
    """
    return get_graph_coordinator(_store, graph_id)


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
    except (OSError, ValueError):
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


def _summarize_graph(
    graph: dict[str, Any],
    run: dict[str, Any] | None = None,
    project: Any | None = None,
) -> dict[str, Any]:
    if run is None:
        run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    has_video, clip_label = _run_clip_summary(run)
    project_id = str(graph.get("project_id") or "")
    if project is None and project_id and not is_default_project_id(project_id):
        project = project_store.get_project_by_id(project_id, cache_bust=True)
    return {
        "graph_id": graph.get("graph_id"),
        "session_id": _graph_session_id(graph),
        "project_id": project_id,
        "project_dir": project.project_dir if project is not None else None,
        "project_name": project.name if project is not None else None,
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


_DESIGN_PLACEHOLDER_TITLE = "设计项目"


_retitled_design_projects: set[str] = set()


def _replacement_display_title(current: str, prompt: str, story_name: str) -> str:
    """Short model title when the current label is still the user prompt."""
    from jiuwenswarm.common.schema.designer_node_labels import usable_story_title

    reference = prompt or current
    summary = usable_story_title(story_name, reference)
    if not summary:
        return ""
    current_flat = " ".join(str(current or "").split())
    if usable_story_title(current, reference) and len(current_flat) <= 40:
        return ""
    if prompt or len(current_flat) > 40:
        return summary
    return ""


def _graph_story_prompt(graph: dict[str, Any]) -> tuple[str, str]:
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    return str(analysis.get("story_name") or ""), str(graph.get("description") or "")


def sync_design_display_name(project: Any) -> Any:
    """Rename a design project and its canvases from the stored model title."""
    project_id = str(getattr(project, "project_id", "") or "")
    if not project_id or project_id in _retitled_design_projects:
        return project
    if str(getattr(project, "work_mode", "") or "") != DESIGN_WORK_MODE:
        return project
    try:
        pairs = _pair_design_canvases(
            _design_sessions(project_id),
            _store.list_graphs_for_project(project_id),
        )
    except Exception:
        logger.debug("Skip design title sync for %s", project_id, exc_info=True)
        return project
    _retitled_design_projects.add(project_id)
    if not pairs:
        return project
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

    ordered = sorted(pairs, key=lambda item: int(item["graph"].get("updated_at") or 0))
    project_summary = ""
    for pair in ordered:
        graph = pair["graph"]
        story_name, prompt = _graph_story_prompt(graph)
        if not project_summary:
            project_summary = _replacement_display_title(
                str(getattr(project, "name", "") or ""),
                prompt,
                story_name,
            )
        session = pair["session"]
        session_id = str(session.get("session_id") or "")
        session_title = _replacement_display_title(
            str(session.get("title") or ""),
            prompt,
            story_name,
        )
        if session_id and session_title and session_title != str(session.get("title") or ""):
            update_session_metadata(
                session_id=session_id,
                title=session_title,
                touch_last_message_at=False,
                sync_write=True,
            )
    if project_summary:
        renamed_name = _design_title(project_summary) or project_summary
        if renamed_name and renamed_name != project.name:
            try:
                renamed = project_store.rename_project(project_id, renamed_name)
                if renamed is not None:
                    return renamed
            except (project_store.ProjectNameConflict, ValueError):
                logger.debug(
                    "Design project %s kept its name; %r is taken",
                    project_id,
                    renamed_name,
                    exc_info=True,
                )
    return project


def sync_design_display_names(projects: list[Any]) -> list[Any]:
    return [sync_design_display_name(project) for project in projects]


def _stamp_canvas_title(graph: dict[str, Any], prompt: str) -> str:
    """Set the canvas title from the model's short name, never the raw prompt."""
    from jiuwenswarm.common.schema.designer_node_labels import usable_story_title

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    name = usable_story_title(analysis.get("story_name"), prompt) or usable_story_title(
        graph.get("title"), prompt
    )
    graph["title"] = name or _DESIGN_PLACEHOLDER_TITLE
    return name


def _design_workspace_messages(session_id: str) -> list[dict[str, Any]]:
    """Chat history for one design session.

    Leader turns are standard ``chat.send(mode='team.design')`` rounds: the
    leader's own finals are persisted with role ``leader`` and belong to the
    canvas chat like any assistant reply. Teammate output stays off the main
    thread, as in work/code team mode. Records of one turn share the id
    ``<request_id>:<role>``, so repeats get a suffix to stay addressable.
    """
    from jiuwenswarm.server.runtime.session.session_history import load_history_records

    messages: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for record in load_history_records(session_id):
        role = str(record.get("role") or "")
        if role == "leader":
            role = "assistant"
        if role not in {"user", "assistant"}:
            continue
        content = str(record.get("content") or "")
        if not content:
            continue
        message_id = str(record.get("id") or uuid.uuid4())
        repeat = seen_ids.get(message_id, 0)
        seen_ids[message_id] = repeat + 1
        if repeat:
            message_id = f"{message_id}#{repeat}"
        messages.append(
            {
                "id": message_id,
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
                **({"graph_id": record["graph_id"]} if record.get("graph_id") else {}),
            }
        )
    return messages


def _graph_session_id(graph: dict[str, Any]) -> str:
    metadata = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    return str((metadata or {}).get("session_id") or "").strip()


def _design_sessions(project_id: str) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    sessions: list[dict[str, Any]] = []
    for item in collect_all_sessions_metadata():
        if str(item.get("project_id") or "") != project_id:
            continue
        if str(item.get("work_mode") or "") == DESIGN_WORK_MODE:
            sessions.append(item)
    return sessions


def _pair_design_canvases(
    sessions: list[dict[str, Any]],
    graphs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind each design session to one canvas.

    A project may hold many sessions. Chat history stays on the session, and
    the canvas is the graph whose metadata.session_id matches it. Graphs saved
    before sessions were explicit fill sessions that do not yet have one.
    """
    ordered = sorted(sessions, key=lambda item: float(item.get("created_at") or 0))
    owned: dict[str, list[dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []
    for graph in graphs:
        session_id = _graph_session_id(graph)
        if session_id:
            owned.setdefault(session_id, []).append(graph)
        else:
            unmatched.append(graph)
    unmatched.sort(key=lambda item: int(item.get("created_at") or item.get("updated_at") or 0))
    pairs: list[dict[str, Any]] = []
    for session in ordered:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            continue
        candidates = list(owned.get(session_id) or [])
        if not candidates and unmatched:
            candidates = [unmatched.pop(0)]
        if not candidates:
            continue
        graph = max(candidates, key=lambda item: int(item.get("updated_at") or 0))
        pairs.append({"session": session, "graph": graph})
    return pairs


def _canvas_summary(session: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(session.get("session_id") or ""),
        "title": str(session.get("title") or graph.get("title") or "画布"),
        "graph_id": str(graph.get("graph_id") or ""),
        "project_id": str(graph.get("project_id") or session.get("project_id") or ""),
        "updated_at": int(graph.get("updated_at") or 0),
    }


def _workspace_from_pair(
    project: Any,
    pair: dict[str, Any],
    pairs: list[dict[str, Any]],
) -> dict[str, Any]:
    session = pair["session"]
    graph = pair["graph"]
    project_id = str(project.project_id)
    run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    return {
        "project": {
            "project_id": project_id,
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
        "sessions": [
            _canvas_summary(item["session"], item["graph"])
            for item in pairs
        ],
    }


def _virtual_default_design_project() -> Any:
    """Design tasks with no chosen directory live on the shared default project."""
    return project_store.Project(
        project_id=DEFAULT_PROJECT_ID_WORK,
        name="默认项目",
        project_dir="",
        work_mode=DESIGN_WORK_MODE,
    )


def _hide_abandoned_design_project(project_id: str, keep_project_id: str) -> None:
    """Hide a project that bootstrap created and then left without a canvas."""
    if (
        not project_id
        or project_id == keep_project_id
        or is_default_project_id(project_id)
    ):
        return
    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if project is None or project.hidden or project.work_mode != DESIGN_WORK_MODE:
        return
    if _design_sessions(project_id) or _store.list_graphs_for_project(project_id):
        return
    try:
        project_store.hide_project(project_id)
    except Exception:
        logger.warning(
            "Failed to hide abandoned design project %s",
            project_id,
            exc_info=True,
        )


def _graphs_for_design_project(
    project_id: str,
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return canvases for these sessions, including ones saved under another project.

    Composing a default-project session used to clear the project id and create
    a new design project. The session stayed on ``default`` while the graph
    was stored on that new id, so opening the session found no canvas.
    """
    graphs = list(_store.list_graphs_for_project(project_id))
    session_ids = {
        str(item.get("session_id") or "").strip()
        for item in sessions
        if str(item.get("session_id") or "").strip()
    }
    covered = {_graph_session_id(graph) for graph in graphs}
    missing = session_ids - covered
    if not missing:
        return graphs
    known_ids = {str(graph.get("graph_id") or "") for graph in graphs}
    for graph in _store.list_graphs():
        session_id = _graph_session_id(graph)
        graph_id = str(graph.get("graph_id") or "")
        if session_id not in missing or not graph_id or graph_id in known_ids:
            continue
        previous_project_id = str(graph.get("project_id") or "")
        repaired = dict(graph)
        repaired["project_id"] = project_id
        try:
            repaired = dict(_store.save_graph(repaired))
        except Exception:
            logger.warning(
                "Failed to move design graph %s onto %s",
                graph_id,
                project_id,
                exc_info=True,
            )
        graphs.append(repaired)
        known_ids.add(graph_id)
        missing.discard(session_id)
        _hide_abandoned_design_project(previous_project_id, project_id)
    return graphs


def _design_pairs(
    project_id: str,
) -> tuple[Any, list[dict[str, Any]], str | None, str | None]:
    if is_default_project_id(project_id):
        project = _virtual_default_design_project()
        project_id = project.project_id
    else:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden or project.work_mode != DESIGN_WORK_MODE:
            return None, [], "design project not found", "NOT_FOUND"
    sessions = _design_sessions(project_id)
    pairs = _pair_design_canvases(
        sessions,
        _graphs_for_design_project(project_id, sessions),
    )
    return project, pairs, None, None


def _select_design_pair(
    pairs: list[dict[str, Any]],
    session_id: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not pairs:
        return None, "design session not found", "NOT_FOUND"
    wanted = str(session_id or "").strip()
    if wanted:
        pair = next(
            (
                item
                for item in pairs
                if str(item["session"].get("session_id") or "") == wanted
            ),
            None,
        )
        if pair is None:
            return None, "design session not found", "NOT_FOUND"
        return pair, None, None
    pair = max(pairs, key=lambda item: int(item["graph"].get("updated_at") or 0))
    return pair, None, None


def _get_design_workspace(
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    project, pairs, error, code = _design_pairs(project_id)
    if error is not None or project is None:
        return None, error, code
    pair, error, code = _select_design_pair(
        pairs,
        str(params.get("session_id") or ""),
    )
    if error is not None or pair is None:
        return None, error, code
    return _workspace_from_pair(project, pair, pairs), None, None


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
    if project_id and not is_default_project_id(project_id):
        try:
            project_store.purge_project(project_id)
        except Exception:
            logger.warning("Failed to roll back design project %s", project_id, exc_info=True)
    if project_dir and not is_default_project_id(project_id):
        shutil.rmtree(project_dir, ignore_errors=True)


def _attach_design_references(
    graph: dict[str, Any],
    raw_references: Any,
) -> dict[str, Any]:
    """Store uploads next to the canvas outputs and seed their canvas nodes."""
    from jiuwenswarm.server.runtime.designer.handlers.common import canvas_output_dir
    from jiuwenswarm.server.runtime.designer.user_references import (
        attach_user_references_to_graph,
        normalize_user_references,
    )

    if not isinstance(raw_references, list) or not raw_references:
        return graph
    refs_dir = canvas_output_dir(graph) / ".designer" / "refs"
    user_refs = normalize_user_references(raw_references, dest_dir=refs_dir)
    if not user_refs:
        return graph
    return attach_user_references_to_graph(graph, user_refs)


async def _create_design_workspace_once(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Create one design session on the default project with an empty canvas.

    No leader round runs here: the first user message arrives as a standard
    ``chat.send(mode='team.design')``, so the leader turn (streaming, history
    persistence) rides the team pipeline and the canvas gets its graph via the
    design tools' ``designer.*`` pushes.
    """
    from jiuwenswarm.server.runtime.designer.user_references import UserReferenceError
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata,
    )

    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "prompt is required", "BAD_REQUEST"
    model_name = str(params.get("model_name") or "").strip()
    project_id = DEFAULT_PROJECT_ID_WORK
    session_id = f"design_{uuid.uuid4().hex}"
    graph_id = ""
    committed = False
    try:
        init_session_metadata(
            session_id=session_id,
            channel_id=request.channel_id or "web",
            user_id=str(request.user_id or ""),
            title="画布 1",
            mode="designer",
            project_dir="",
            project_id=project_id,
            persist_session=True,
            model=model_name,
            work_mode=DESIGN_WORK_MODE,
            agent_group_name=DESIGNER_AGENT_GROUP,
        )
        graph = normalize_execution_graph(
            {
                "schema_version": SCHEMA_VERSION,
                "graph_id": new_graph_id(),
                "project_id": project_id,
                "title": _DESIGN_PLACEHOLDER_TITLE,
                "description": prompt,
                "source": GRAPH_SOURCE_MANUAL,
                "nodes": [],
                "edges": [],
                "metadata": {
                    "session_id": session_id,
                    "work_mode": DESIGN_WORK_MODE,
                    **({"model_name": model_name} if model_name else {}),
                },
            }
        )
        try:
            graph = _attach_design_references(graph, params.get("references"))
        except UserReferenceError as exc:
            return None, str(exc), exc.code
        graph = dict(_store.save_graph(graph))
        graph_id = str(graph["graph_id"])
        committed = True
        return {
            "project": {
                "project_id": project_id,
                "name": "默认项目",
                "project_dir": "",
                "work_mode": DESIGN_WORK_MODE,
            },
            "session": {
                "session_id": session_id,
                "title": "画布 1",
                "project_id": project_id,
                "project_dir": "",
                "work_mode": DESIGN_WORK_MODE,
            },
            "graph": graph,
            "messages": [],
        }, None, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create design workspace")
        return None, str(exc), "INTERNAL_ERROR"
    finally:
        if not committed:
            _rollback_design_workspace(
                project_id="",
                project_dir="",
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


def _session_title(raw: Any, fallback: str) -> str:
    title = " ".join(str(raw or "").split())
    if not title:
        title = fallback
    return title[:80]


def _create_design_session(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Open another canvas under an existing design project."""
    from jiuwenswarm.server.runtime.session.session_metadata import (
        init_session_metadata,
    )

    project_id = str(params.get("project_id") or "").strip()
    if not project_id:
        return None, "project_id is required", "BAD_REQUEST"
    project, _pairs, error, code = _design_pairs(project_id)
    if error is not None or project is None:
        return None, error, code
    existing = _design_sessions(project_id)
    title = _session_title(params.get("title"), f"画布 {len(existing) + 1}")
    session_id = f"design_{uuid.uuid4().hex}"
    init_session_metadata(
        session_id=session_id,
        channel_id=request.channel_id or "web",
        user_id=str(request.user_id or ""),
        title=title,
        mode="designer",
        project_dir=str(project.project_dir or ""),
        project_id=project_id,
        persist_session=True,
        work_mode=DESIGN_WORK_MODE,
        agent_group_name=DESIGNER_AGENT_GROUP,
    )
    try:
        graph = normalize_execution_graph(
            {
                "schema_version": SCHEMA_VERSION,
                "graph_id": new_graph_id(),
                "project_id": project_id,
                "title": title,
                "source": GRAPH_SOURCE_MANUAL,
                "nodes": [],
                "edges": [],
                "metadata": {
                    "session_id": session_id,
                    "work_mode": DESIGN_WORK_MODE,
                    "project_dir": str(project.project_dir or ""),
                },
            }
        )
        _store.save_graph(graph)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(get_agent_sessions_dir() / session_id, ignore_errors=True)
        logger.exception("Failed to create design session")
        return None, str(exc), "INTERNAL_ERROR"
    return _get_design_workspace({"project_id": project_id, "session_id": session_id})


def _compose_design_session(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Prepare an empty session canvas for its first leader message.

    Attaches the uploads and the chosen model to the session's canvas. The
    prompt itself is sent by the client as ``chat.send(mode='team.design')``;
    the leader builds the graph from it.
    """
    from jiuwenswarm.server.runtime.designer.user_references import (
        UserReferenceError,
        is_user_reference_node,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        update_session_metadata,
    )

    project_id = str(params.get("project_id") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    prompt = str(params.get("prompt") or "").strip()
    references = params.get("references")
    if not project_id or not session_id:
        return None, "project_id and session_id are required", "BAD_REQUEST"
    if not prompt and not (isinstance(references, list) and references):
        return None, "prompt is required", "BAD_REQUEST"
    current, error, code = _get_design_workspace(
        {"project_id": project_id, "session_id": session_id}
    )
    if error is not None or current is None:
        return None, error, code
    graph = current.get("graph") if isinstance(current.get("graph"), dict) else {}
    if graph.get("nodes") and not all(
        is_user_reference_node(node) for node in graph.get("nodes") or []
    ):
        return None, "design session already has a canvas", "CONFLICT"
    graph = dict(graph)
    metadata = dict(graph.get("metadata") or {})
    model_name = str(params.get("model_name") or "").strip()
    if model_name:
        metadata["model_name"] = model_name
        # Team rounds resolve their chat model from the session, not the graph.
        # Persist the canvas's chosen model here as well, otherwise the leader
        # turn (``chat.send(mode='team.design')`` carries no model_name) falls
        # back to the configured default even though the canvas was created
        # with another model selected.
        update_session_metadata(
            session_id=session_id,
            model=model_name,
            touch_last_message_at=False,
            sync_write=True,
        )
    graph["metadata"] = metadata
    if prompt:
        graph["description"] = prompt
    try:
        graph = _attach_design_references(graph, references)
        _store.save_graph(graph)
    except UserReferenceError as exc:
        return None, str(exc), exc.code
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to prepare design session")
        return None, str(exc), "INTERNAL_ERROR"
    # Sessions created before the designer AgentGroup existed carry no group
    # binding; the team path reads it on the first team.design round.
    if not str(get_session_metadata(session_id).get("agent_group_name") or "").strip():
        update_session_metadata(
            session_id=session_id,
            agent_group_name=DESIGNER_AGENT_GROUP,
            touch_last_message_at=False,
            sync_write=True,
        )
    _ = request
    return _get_design_workspace({"project_id": project_id, "session_id": session_id})


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
    latest_runs = _store.latest_runs_by_graph()
    projects: dict[str, Any] = {}

    def _project(pid: str) -> Any:
        if pid not in projects:
            projects[pid] = (
                project_store.get_project_by_id(pid, cache_bust=True)
                if pid and not is_default_project_id(pid)
                else None
            )
        return projects[pid]

    for graph in graphs:
        item = dict(graph)
        item_project_id = str(item.get("project_id") or "")
        project = _project(item_project_id)
        run = latest_runs.get(str(item.get("graph_id") or ""))
        summaries.append(_summarize_graph(item, run, project))
        # Do not hydrate outputs or ship full node bodies here — Agent WS
        # send budget is 6MB; a page of Designer graphs with images exceeds it.
        payload.append(
            {
                "graph_id": item.get("graph_id"),
                "project_id": item_project_id,
                "project_dir": project.project_dir if project is not None else None,
                "project_name": project.name if project is not None else None,
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


def _save_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    raw_graph = params.get("graph")
    if not isinstance(raw_graph, dict):
        return None, "graph is required", "BAD_REQUEST"
    try:
        graph = normalize_execution_graph(raw_graph)
        # The server-minted run id must survive client saves: the canvas's
        # in-memory graph predates the first trigger's mint, so its save
        # would wipe team_design_run_id and the next trigger would mint a
        # fresh run (empty node_states — the canvas loses every result).
        stored = _store.get_graph(str(graph.get("graph_id") or ""))
        stored_run = (
            str((stored.get("metadata") or {}).get("team_design_run_id") or "").strip()
            if stored is not None
            else ""
        )
        meta = dict(graph.get("metadata") or {})
        if stored_run and not str(meta.get("team_design_run_id") or "").strip():
            meta["team_design_run_id"] = stored_run
            graph["metadata"] = meta
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


_GRAPH_PATCH_KEYS = (
    "title",
    "description",
    "upsert_nodes",
    "upsert_edges",
    "remove_node_ids",
    "remove_edge_ids",
    "video_settings",
    "image_settings",
)


def _patch_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    raw_patch = params.get("patch")
    if raw_patch is None:
        raw_patch = {key: params[key] for key in _GRAPH_PATCH_KEYS if key in params}
    video_patch = None
    image_patch = None
    if isinstance(raw_patch, dict):
        raw_patch = dict(raw_patch)
        video_patch = raw_patch.pop("video_settings", None)
        image_patch = raw_patch.pop("image_settings", None)
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
        if video_patch is not None or image_patch is not None:
            update_media_settings(next_graph, video=video_patch, image=image_patch)
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            Director().onboard_user_added_nodes(next_graph)
        except Exception:
            logger.debug("Director user-node onboard on patch failed", exc_info=True)
        saved = _store.save_graph(next_graph)
        _persist_uploaded_outputs(saved)
    except (DesignerGraphValidationError, MediaSettingsError) as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _media_capabilities(
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    graph = None
    if graph_id:
        graph = _store.get_graph(graph_id)
        if graph is None:
            return None, "graph not found", "NOT_FOUND"
        return media_capabilities(graph, _store.get_latest_run_for_graph(graph_id)), None, None
    return media_capabilities(), None, None


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


def _is_comfyui_target(graph: dict[str, Any], node_id: str) -> bool:
    from jiuwenswarm.common.schema.designer_graph import is_comfyui_node

    return any(
        str(node.get("id") or "") == node_id and is_comfyui_node(node)
        for node in graph.get("nodes") or []
        if isinstance(node, dict)
    )


def _start_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve the run target (graph + optional node); no execution here.

    ``node_id`` present → single-node trigger (only that node re-executes,
    downstream frozen; ``cascade=true`` turns it into "rerun from X" — X plus
    its downstream closure re-execute, upstream stays frozen at current
    values); absent → full-graph trigger (unfrozen nodes execute in
    topological order). The run_id is the graph's lifelong identity, minted
    by the coordinator on first trigger.
    """
    graph_id = str(params.get("graph_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    cascade = bool(params.get("cascade"))
    graph = _store.get_graph(graph_id) if graph_id else None
    if run_id:
        existing = _store.get_run(run_id)
        if existing is None:
            return None, "run not found", "NOT_FOUND"
        if graph is None:
            graph = _store.get_graph(str(existing.get("graph_id") or ""))
        elif str(existing.get("graph_id") or "") != str(graph.get("graph_id") or ""):
            return None, "run does not belong to graph", "BAD_REQUEST"
    if graph is None:
        if graph_id or run_id:
            return None, "graph not found", "NOT_FOUND"
        return None, "graph_id or run_id is required", "BAD_REQUEST"
    node_ids: list[str] | None = None
    if node_id:
        if not any(str(n.get("id") or "") == node_id for n in graph.get("nodes") or []):
            return None, "node not found", "NOT_FOUND"
        node_ids = [node_id]
    elif cascade:
        return None, "cascade requires node_id", "BAD_REQUEST"
    contribution_warning = ""
    if not (node_id and _is_comfyui_target(graph, node_id)):
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            audit = Director().audit_contribution_for_run(graph)
            contribution_warning = str(audit.get("warning") or "")
            graph = dict(_store.save_graph(graph))
        except Exception:
            logger.debug("Director contribution audit failed", exc_info=True)
    return {
        "graph": dict(graph),
        "graph_id": graph["graph_id"],
        "node_ids": node_ids,
        "cascade": cascade,
        "warning": contribution_warning or None,
        "warnings": [contribution_warning] if contribution_warning else [],
    }, None, None


_RUN_TASKS: set[asyncio.Task[None]] = set()


async def _ensure_design_context(graph: dict[str, Any]) -> None:
    """Warm the design context for a canvas trigger (best-effort).

    A rerun after a server restart arrives with a cold in-process registry —
    the chat round that normally binds the worker base spec never ran in
    this process. Bind it here from the same enriched-spec source. A real
    setup failure stays visible as the coordinator's own RuntimeError.
    """
    try:
        from jiuwenswarm.server.runtime.design.team_bridge import ensure_design_context

        await ensure_design_context(graph)
    except Exception:  # noqa: BLE001 — warm-up must not block the trigger path
        logger.exception(
            "[DesignerAdapter] design context warm-up failed: graph_id=%s",
            graph.get("graph_id"),
        )


def _launch_run_trigger(
    coordinator: RunCoordinator,
    payload: dict[str, Any],
    on_update: Any,
) -> None:
    """Run the trigger on a background task; failures land on the run record.

    The trigger's own error handling writes a failed run and pushes it
    through ``on_update``; a task-level exception (outside the coordinator)
    is logged — the record keeps its last persisted state.
    """

    async def _run() -> None:
        try:
            await coordinator.start(
                payload["graph"],
                node_ids=payload["node_ids"],
                cascade=bool(payload.get("cascade")),
                on_update=on_update,
            )
        except Exception:  # noqa: BLE001 — background task must never raise
            logger.exception(
                "[DesignerAdapter] run trigger crashed: graph_id=%s",
                payload.get("graph_id"),
            )

    task = asyncio.create_task(_run())
    _RUN_TASKS.add(task)
    task.add_done_callback(_RUN_TASKS.discard)


def _initial_running_run(payload: dict[str, Any]) -> dict[str, Any]:
    """The 'running' snapshot the run.start response answers with.

    Mirrors the first ``on_update`` the coordinator emits once the lock is
    taken without racing the background task: read the minted run record, or
    synthesize a draft-shaped one when the run_id is brand new.
    """
    graph = payload["graph"]
    graph_id = str(graph.get("graph_id") or "")
    meta = dict(graph.get("metadata") or {})
    run_id = str(meta.get("team_design_run_id") or "").strip()
    run = _store.get_run(run_id) if run_id else None
    if run is not None:
        return dict(run)
    now = utc_now_ms()
    return {
        "schema_version": "designer-execution-run.v1",
        "run_id": run_id,
        "graph_id": graph_id,
        "project_id": str(graph.get("project_id") or ""),
        "status": "running",
        "node_states": {
            node_id: {
                "status": "pending",
                "started_at": None,
                "completed_at": None,
            }
            for node_id in (payload.get("node_ids") or [])
        },
        "current_node_ids": [],
        "created_at": now,
        "updated_at": now,
    }


def _pause_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    return (
        None,
        "team.design runs do not support pause; cancel the trigger and re-run",
        "NOT_IMPLEMENTED",
    )


def _cancel_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    run = _store.get_run(run_id)
    if run is None:
        return None, "run not found", "NOT_FOUND"
    graph_id = str(run.get("graph_id") or "")
    if graph_id:
        _coordinator_for(graph_id).cancel(run_id)
        run = _store.get_run(run_id) or run
    return {"run": dict(run)}, None, None


class DesignerAdapter(GatewayAdapter):
    """Designer execution graph adapter.

    The canvas conversation (graph authoring and edits) rides the standard
    ``chat.send(mode='team.design')`` pipeline; this adapter only serves the
    workspace, graph CRUD and the run triggers.
    """

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.DESIGNER_WORKSPACE_CREATE.value,
            ReqMethod.DESIGNER_WORKSPACE_GET.value,
            ReqMethod.DESIGNER_WORKSPACE_SESSION_CREATE.value,
            ReqMethod.DESIGNER_WORKSPACE_SESSION_COMPOSE.value,
            ReqMethod.DESIGNER_GRAPH_GET.value,
            ReqMethod.DESIGNER_GRAPH_LIST.value,
            ReqMethod.DESIGNER_GRAPH_SAVE.value,
            ReqMethod.DESIGNER_GRAPH_PATCH.value,
            ReqMethod.DESIGNER_MEDIA_CAPABILITIES.value,
            ReqMethod.DESIGNER_RUN_START.value,
            ReqMethod.DESIGNER_RUN_GET.value,
            ReqMethod.DESIGNER_RUN_PAUSE.value,
            ReqMethod.DESIGNER_RUN_CANCEL.value,
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
            elif method == ReqMethod.DESIGNER_WORKSPACE_SESSION_CREATE:
                payload, error, code = await asyncio.to_thread(
                    _create_design_session, request, params
                )
            elif method == ReqMethod.DESIGNER_WORKSPACE_SESSION_COMPOSE:
                payload, error, code = await asyncio.to_thread(
                    _compose_design_session, request, params
                )
            elif method == ReqMethod.DESIGNER_GRAPH_GET:
                payload, error, code = await asyncio.to_thread(_get_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_LIST:
                payload, error, code = await asyncio.to_thread(_list_graphs, params)
            elif method == ReqMethod.DESIGNER_GRAPH_SAVE:
                payload, error, code = await asyncio.to_thread(_save_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_PATCH:
                payload, error, code = await asyncio.to_thread(_patch_graph, params)
            elif method == ReqMethod.DESIGNER_MEDIA_CAPABILITIES:
                payload, error, code = await asyncio.to_thread(_media_capabilities, params)
            elif method == ReqMethod.DESIGNER_RUN_GET:
                payload, error, code = await asyncio.to_thread(_get_run, params)
            elif method == ReqMethod.DESIGNER_RUN_START:
                payload, error, code = await asyncio.to_thread(_start_run, params)
                if error is None and payload is not None:
                    coordinator = _coordinator_for(str(payload["graph_id"]))
                    if coordinator.is_active():
                        return build_error_response(
                            request,
                            "a trigger is already executing on this graph; wait for "
                            "it to finish (the canvas shows its progress)",
                            code="CONFLICT",
                        )
                    # Accept-and-trigger: a run spans many worker LLM turns, so
                    # answer with the initial 'running' record at once; the
                    # canvas follows progress via designer.run.updated /
                    # designer.node.updated pushes and designer.run.get.
                    # A canvas rerun can arrive with a cold in-process registry
                    # (server restarted since the last chat round); warm the
                    # design context so the trigger finds its worker base spec.
                    await _ensure_design_context(payload["graph"])
                    _launch_run_trigger(coordinator, payload, _run_update_callback(request))
                    run_out = _initial_running_run(payload)
                    # Accept-and-trigger contract: answer with a RUNNING
                    # snapshot. The stored record can still carry the previous
                    # trigger's terminal status (the background task may not
                    # have taken the coordinator lock yet), and the frontend's
                    # applyRun only starts its 400ms poll on an active status —
                    # a stale 'completed' leaves a rerun with no feedback
                    # channel at all (pushes are its only other leg).
                    if run_out is not None:
                        run_out["status"] = "running"
                        run_out["current_node_ids"] = []
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
            ReqMethod.DESIGNER_GRAPH_PATCH,
        }:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_GRAPH_UPDATED.value,
                payload={"graph": payload["graph"]},
            )
        if isinstance(payload, dict) and payload.get("run") is not None and method in {
            ReqMethod.DESIGNER_RUN_PAUSE,
            ReqMethod.DESIGNER_RUN_CANCEL,
        }:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": payload["run"]},
            )
        return _ok_response(request, payload)
