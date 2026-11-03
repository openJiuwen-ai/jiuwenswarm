# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render trees for the Workspace Probe panel."""

from __future__ import annotations

import os
from pathlib import Path

from openjiuwen.core.events.ops_impl.fs.models import FsEntry, FsStatus

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .state import ProbeSnapshot

PACKAGE_ID = "example.fs-probe"
SLOT_PANEL = "conversation.panel.right"
INSTANCE_KEY = "fs-probe.panel"
ACTION_RESCAN = "fs-probe.rescan"
ACTION_WRITE = "fs-probe.write"
ACTION_EXPAND = "fs-probe.expand"
ACTION_SELECT = "fs-probe.select"
MARKER_RELATIVE = "fs-probe-marker.md"
CHECKED_FILES = ("README.md", "AGENTS.md", "pyproject.toml")
PREVIEW_MAX_BYTES = 12 * 1024
PREVIEW_CUT_NOTICE = "\n… (preview truncated)"


def panel_tree(
    snapshot: ProbeSnapshot | None,
    expanded: frozenset[str] | set[str] = frozenset(),
    children: dict[str, tuple[FsEntry, ...]] | None = None,
) -> RenderNode:
    """Build the panel root; an empty snapshot still mounts a usable panel."""

    if snapshot is None:
        body = RenderNode(
            "Text",
            {
                "value": "Waiting for the next chat round to scan the workspace.",
                "size": "sm",
                "variant": "secondary",
            },
        )
    else:
        body = _snapshot_body(snapshot, expanded, children or {})
    return RenderNode("Panel", {"title": "Workspace Probe"}, children=(body,))


def _snapshot_body(
    snapshot: ProbeSnapshot,
    expanded: frozenset[str] | set[str],
    children: dict[str, tuple[FsEntry, ...]],
) -> RenderNode:
    blocks: list[RenderNode] = [
        _header(snapshot),
        _status_line(snapshot),
        _meta_line(snapshot),
        _workspace_view(snapshot, expanded, children),
    ]
    write_line = _write_line(snapshot)
    if write_line is not None:
        blocks.append(write_line)
    blocks.append(_footer_line(snapshot))
    return RenderNode(
        "Box", {"direction": "column", "gap": "sm", "flex": 1}, children=tuple(blocks)
    )


def _workspace_view(
    snapshot: ProbeSnapshot,
    expanded: frozenset[str] | set[str],
    children: dict[str, tuple[FsEntry, ...]],
) -> RenderNode:
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "none", "flex": 1},
        children=(_file_tree(snapshot, expanded, children), _preview(snapshot)),
    )


def _header(snapshot: ProbeSnapshot) -> RenderNode:
    path_line = RenderNode(
        "Text",
        {
            "value": f"工作路径：{_display_path(snapshot.cwd)}",
            "size": "sm",
            "monospace": True,
            "max_lines": 1,
        },
    )
    buttons = RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "wrap": True},
        children=(
            _button("Rescan", ACTION_RESCAN, "secondary"),
            _button("Write marker", ACTION_WRITE, "primary"),
        ),
    )
    return RenderNode(
        "Box",
        {"direction": "column", "gap": "sm"},
        children=(buttons, path_line),
    )


def _button(label: str, action_id: str, variant: str) -> RenderNode:
    return RenderNode(
        "Button",
        {"label": label, "variant": variant, "size": "sm"},
        events={"ui.click": {"id": action_id, "args": {}}},
    )


def _status_line(snapshot: ProbeSnapshot) -> RenderNode:
    checks = (
        ("list", snapshot.listed.status),
        ("exists", snapshot.exists_status),
        ("ancestors", snapshot.ancestors.status),
    )
    value = " · ".join(f"{label} {status.value}" for label, status in checks)
    if all(status is FsStatus.OK for _, status in checks):
        tone = "success"
    elif any(status is FsStatus.ERROR for _, status in checks):
        tone = "danger"
    else:
        tone = "warning"
    return RenderNode("Text", {"value": f"接口状态：{value}", "size": "sm", "tone": tone, "max_lines": 1})


def _meta_line(snapshot: ProbeSnapshot) -> RenderNode:
    parts = [f"{snapshot.listed.total_count} entries"]
    if snapshot.listed.truncated:
        parts.append(f"showing {len(snapshot.listed.entries)}")
    parts.append(f"updated {snapshot.refreshed_at}")
    value = " · ".join(parts)
    return RenderNode(
        "Text", {"value": f"目录信息：{value}", "size": "sm", "variant": "secondary", "max_lines": 1}
    )


def _file_tree(
    snapshot: ProbeSnapshot,
    expanded: frozenset[str] | set[str],
    children: dict[str, tuple[FsEntry, ...]],
) -> RenderNode:
    tree_data = tuple(
        node
        for node in (_file_node(entry, snapshot.cwd, children) for entry in snapshot.listed.entries)
        if node is not None
    )
    return RenderNode(
        "FileTree",
        {
            "treeData": tree_data,
            "expandedKeys": tuple(sorted(_directory_keys(tree_data) & set(expanded))),
            "status": "ready" if snapshot.listed.status is FsStatus.OK else "unavailable",
            "message": snapshot.listed.error.message if snapshot.listed.error is not None else "",
            "truncated": snapshot.listed.truncated,
            "emptyText": "The session workspace has no files yet.",
        },
        events={
            "ui.change": {"id": ACTION_EXPAND, "args": {}},
            "ui.select": {"id": ACTION_SELECT, "args": {}},
        },
    )


def _directory_keys(nodes: tuple[dict[str, object], ...]) -> set[str]:
    keys: set[str] = set()
    pending = list(nodes)
    while pending:
        node = pending.pop()
        if node.get("kind") != "directory":
            continue
        keys.add(str(node["key"]))
        pending.extend(node.get("children", ()))
    return keys


def _file_node(
    entry: FsEntry,
    cwd: str,
    children: dict[str, tuple[FsEntry, ...]],
) -> dict[str, object] | None:
    relative = _relative(entry.path, cwd)
    if not relative or relative == ".." or relative.startswith("../") or ":" in relative:
        return None
    node: dict[str, object] = {
        "key": relative,
        "title": entry.name,
        "path": relative,
        "kind": "directory" if entry.is_directory else "file",
    }
    if entry.is_directory:
        listed = children.get(relative)
        if listed is None:
            node["loaded"] = False
        else:
            node["loaded"] = True
            node["children"] = tuple(
                child
                for child in (_file_node(item, cwd, children) for item in listed)
                if child is not None
            )
    return node


def _preview(snapshot: ProbeSnapshot) -> RenderNode:
    file = _preview_file(snapshot)
    if file is not None:
        status = "ready"
    elif snapshot.preview is None:
        status = "missing"
    elif snapshot.preview.status is FsStatus.DENIED:
        status = "unavailable"
    elif snapshot.preview.status is FsStatus.OK:
        status = "unsupported"
    else:
        status = "error"
    return RenderNode(
        "FilePreview",
        {"file": file, "status": status, "emptyText": "No preview file found."},
    )


def _preview_file(snapshot: ProbeSnapshot) -> dict[str, object] | None:
    preview = snapshot.preview
    if preview is None or preview.status is not FsStatus.OK or not isinstance(preview.content, str):
        return None
    relative = _relative(preview.path, snapshot.cwd)
    if not relative or relative == ".." or relative.startswith("../") or ":" in relative:
        return None
    size = snapshot.preview_stat.size if snapshot.preview_stat is not None else None
    content = _clip(preview.content)
    return {
        "name": os.path.basename(preview.path),
        "path": relative,
        "content": content,
        "size": size if isinstance(size, int) and size >= 0 else len(content.encode("utf-8")),
    }


def _clip(content: str) -> str:
    raw = content.encode("utf-8", errors="ignore")
    if len(raw) <= PREVIEW_MAX_BYTES:
        return content
    return raw[:PREVIEW_MAX_BYTES].decode("utf-8", errors="ignore") + PREVIEW_CUT_NOTICE


def _footer_line(snapshot: ProbeSnapshot) -> RenderNode:
    parts = [_stat_part(snapshot)]
    ancestors = _ancestors_part(snapshot)
    if ancestors is not None:
        parts.append(ancestors)
    value = " · ".join(parts)
    return RenderNode(
        "Text",
        {
            "value": value,
            "size": "sm",
            "variant": "secondary",
            "monospace": True,
            "max_lines": 1,
        },
    )


def _stat_part(snapshot: ProbeSnapshot) -> str:
    stat = snapshot.preview_stat
    if stat is None:
        return "stat 信息 : select a file"
    name = os.path.basename(stat.path)
    if stat.status is not FsStatus.OK:
        return f"stat 信息 : {name} · {stat.status.value}"
    size = _format_size(stat.size)
    modified = stat.modified_time or "unknown time"
    return f"stat 信息 : {name} · {size} · {modified}"


def _format_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def _ancestors_part(snapshot: ProbeSnapshot) -> str | None:
    ancestors = snapshot.ancestors
    if ancestors.status is not FsStatus.OK:
        return f"ancestors · {ancestors.status.value}"
    if not ancestors.entries:
        return None
    paths = ", ".join(_relative(entry.path, snapshot.cwd) for entry in ancestors.entries)
    return f"ancestors · {paths}"


def _write_line(snapshot: ProbeSnapshot) -> RenderNode | None:
    write = snapshot.write
    if write is None:
        return None
    if write.status is FsStatus.OK:
        return RenderNode(
            "Text",
            {
                "value": f"marker written · {_relative(write.path, snapshot.cwd)}",
                "size": "sm",
                "tone": "success",
                "max_lines": 1,
            },
        )
    message = write.error.message if write.error is not None else write.status.value
    return RenderNode(
        "Text",
        {
            "value": f"marker failed · {message}",
            "size": "sm",
            "tone": "danger",
            "max_lines": 1,
        },
    )


def _relative(path: str, cwd: str) -> str:
    try:
        value = os.path.relpath(path, cwd)
    except ValueError:
        return ""
    return value.replace(os.sep, "/")


def _display_path(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path
