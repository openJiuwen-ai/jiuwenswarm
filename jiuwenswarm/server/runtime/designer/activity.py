# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Throttled node/leader activity updates for Designer peek UI."""

from __future__ import annotations

import threading
from typing import Any, Callable

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    LEADER_NODE_ID,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_RUNNING,
    DesignerExecutionRun,
    apply_node_activity,
    is_leader_node_id,
    node_pipeline,
    utc_now_ms,
)

_PIPELINE_STAGE_TEXT = {
    "brief": "writing the brief",
    "character_design": "generating character image",
    "character": "generating character image",
    "scene": "generating scene image",
    "storyboard": "building the storyboard",
    "frame": "generating keyframe",
    "keyframe": "generating keyframe",
    "clip": "generating clip video",
    "compose": "composing the film",
    "speech": "generating speech",
    "music": "generating music",
    "image": "generating image",
    "video": "generating video",
    "audio": "generating audio",
    "text": "writing text",
    "table": "filling the table",
}


def stage_text_for_node(node: dict[str, Any] | None) -> str:
    pipeline = ""
    if isinstance(node, dict):
        try:
            pipeline = str(node_pipeline(node) or "").strip()
        except Exception:  # noqa: BLE001
            pipeline = str((node.get("config") or {}).get("pipeline") or "")
        if not pipeline:
            pipeline = str(node.get("type") or "").strip()
    return _PIPELINE_STAGE_TEXT.get(pipeline, "working")

PublishFn = Callable[[DesignerExecutionRun, str | None], None]

_THROTTLE_MS = 320
_lock = threading.Lock()
_last_publish_at: dict[tuple[str, str], int] = {}


def graph_node_states(run: dict[str, Any] | None) -> dict[str, Any]:
    states = (run or {}).get("node_states") or {}
    if not isinstance(states, dict):
        return {}
    return {
        str(node_id): state
        for node_id, state in states.items()
        if not is_leader_node_id(node_id)
    }


def should_publish(run_id: str, node_id: str, *, now: int | None = None, force: bool = False) -> bool:
    stamp = now if isinstance(now, int) else utc_now_ms()
    key = (str(run_id or ""), str(node_id or ""))
    with _lock:
        previous = _last_publish_at.get(key, 0)
        if not force and stamp - previous < _THROTTLE_MS:
            return False
        _last_publish_at[key] = stamp
        return True


def apply_run_activity(
    run: DesignerExecutionRun | dict[str, Any],
    node_id: str,
    *,
    kind: str,
    text: str,
    tool: str = "",
    status: str | None = None,
) -> DesignerExecutionRun:
    nid = str(node_id or "").strip() or LEADER_NODE_ID
    states = run.setdefault("node_states", {})
    current = dict(states.get(nid) or {})
    next_state = apply_node_activity(current, kind=kind, text=text, tool=tool)
    if status:
        next_state["status"] = status
    elif is_leader_node_id(nid) and not current.get("status"):
        next_state["status"] = NODE_STATUS_RUNNING
    states[nid] = next_state
    run["updated_at"] = utc_now_ms()
    return run  # type: ignore[return-value]


def complete_leader_activity(run: DesignerExecutionRun | dict[str, Any], text: str = "") -> None:
    states = run.setdefault("node_states", {})
    current = dict(states.get(LEADER_NODE_ID) or {"status": NODE_STATUS_RUNNING})
    if text:
        current = apply_node_activity(
            current,
            kind=ACTIVITY_KIND_STAGE,
            text=text,
        )
    current["status"] = NODE_STATUS_COMPLETED
    current["completed_at"] = utc_now_ms()
    states[LEADER_NODE_ID] = current
    run["updated_at"] = utc_now_ms()


def emit_run_activity(
    run: DesignerExecutionRun | dict[str, Any],
    node_id: str,
    *,
    kind: str,
    text: str,
    tool: str = "",
    status: str | None = None,
    on_update: PublishFn | None = None,
    force: bool = False,
) -> None:
    apply_run_activity(run, node_id, kind=kind, text=text, tool=tool, status=status)
    run_id = str(run.get("run_id") or "")
    if on_update is None:
        return
    if should_publish(run_id, node_id, force=force):
        on_update(run, node_id)  # type: ignore[arg-type]
