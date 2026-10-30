# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Forward file data requests through the public session-scoped Ops."""

from __future__ import annotations

from collections.abc import Mapping

from openjiuwen.core.events import EventHandlerOps

from .state import WorkspaceState


async def load_workspace(ops: EventHandlerOps, state: WorkspaceState) -> None:
    """Read the root snapshot when the contribution is mounted."""
    try:
        result = await ops.session.files(expanded_paths=[])
    except Exception:  # The host may not yet expose file capabilities.
        result = {"status": "unavailable"}
    state.tree_result = dict(result) if isinstance(result, Mapping) else {"status": "error"}
    state.preview_result = {"status": "ready", "file": None}


async def apply_file_action(
    ops: EventHandlerOps,
    state: WorkspaceState,
    action: str,
    event: str,
    data: Mapping,
) -> bool:
    """Resolve component keys against the current platform data snapshot."""
    nodes = state.nodes()
    key = data.get("key")
    item = nodes.get(key) if isinstance(key, str) else None
    if item is None:
        return False
    if action == "workbench.files.select" and event == "ui.select" and item["kind"] == "file":
        try:
            result = await ops.session.read_file(path=item["path"])
        except Exception:
            result = {"status": "error"}
        state.preview_result = dict(result) if isinstance(result, Mapping) else {"status": "error"}
        return True
    if (action != "workbench.files.expand" or event != "ui.change"
            or item["kind"] != "directory" or type(data.get("expanded")) is not bool):
        return False
    expanded_keys = data.get("expandedKeys")
    if not isinstance(expanded_keys, list) or any(
        not isinstance(value, str) or value not in nodes or nodes[value]["kind"] != "directory"
        for value in expanded_keys
    ):
        return False
    paths = [nodes[value]["path"] for value in expanded_keys]
    try:
        result = await ops.session.files(expanded_paths=paths)
    except Exception:
        result = {"status": "error"}
    snapshot = dict(result) if isinstance(result, Mapping) else {"status": "error"}
    if snapshot.get("status") == "error" and state.tree_result.get("status") == "ready":
        snapshot = dict(state.tree_result)
        snapshot["readFailed"] = True
    state.tree_result = snapshot
    return True
