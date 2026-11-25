# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Classify the latest tool round into Claude and Codex panel sections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

BRANDS = {"claude": "Claude Code", "codex": "Codex"}
SECTIONS = {
    "claude": ("activity", "plan", "files"),
    "codex": ("changes", "files", "commands"),
}
SECTION_LABELS = {
    "activity": "Activity",
    "plan": "Plan",
    "files": "Files",
    "changes": "Changes",
    "commands": "Commands",
}
EMPTY_COPY = {
    "activity": "还没有 Read / Edit / Bash 活动",
    "plan": "还没有执行计划",
    "files": "还没有可列出的文件",
    "changes": "还没有代码变更",
    "commands": "还没有命令",
}
_READ = frozenset({"read", "read_file", "view", "cat", "open_file"})
_EDIT = frozenset({
    "edit", "write", "write_file", "str_replace", "strreplace", "apply_patch",
    "search_replace", "multiedit", "edit_file",
})
_BASH = frozenset({"bash", "shell", "run_command", "exec", "powershell", "command"})
_PLAN = frozenset({"plan", "todo", "todowrite", "update_plan", "exit_plan_mode"})
_MAX_ACTIVITIES = 40
_MAX_OUTPUT = 400
_MAX_DIFF = 30
_MAX_FILES = 60
_MAX_PLAN = 16


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One added or removed line taken from a tool argument."""

    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolActivity:
    """One tool call from the latest assistant round that used tools."""

    label: str
    tool: str
    target: str
    output: str
    diff: tuple[DiffLine, ...]
    error: bool


@dataclass(frozen=True, slots=True)
class WorkbenchSnapshot:
    """Observed tools and files for one bound harness session."""

    mode: str
    section: str
    activities: tuple[ToolActivity, ...]
    files: tuple[str, ...]
    plan: tuple[str, ...]
    permissions: tuple[str, ...]

    def document(self) -> dict[str, object]:
        """Return the JSON document embedded in the hosted panel page."""
        return {
            "brand": BRANDS[self.mode],
            "mode": self.mode,
            "section": self.section,
            "tabs": [{"id": key, "label": SECTION_LABELS[key]} for key in SECTIONS[self.mode]],
            "empty": {key: EMPTY_COPY[key] for key in SECTIONS[self.mode]},
            "permissionsEmpty": "还没有权限记录",
            "activities": [_activity_document(item) for item in self.activities],
            "changes": [_activity_document(item) for item in self.activities if item.label == "Edit"],
            "commands": [_activity_document(item) for item in self.activities if item.label == "Bash"],
            "files": list(self.files),
            "plan": list(self.plan),
            "permissions": list(self.permissions),
        }


def panel_has_content(snapshot: WorkbenchSnapshot) -> bool:
    """True when the Claude sandbox should occupy the right-hand slot."""
    return bool(snapshot.activities or snapshot.files or snapshot.plan or snapshot.permissions)


def normalize_section(mode: str, section: str) -> str:
    """Keep a section only when that harness style actually renders it."""
    allowed = SECTIONS[mode]
    if section in allowed:
        return section
    return allowed[0]


def build_snapshot(mode: str, messages: object, files: object, section: str = "") -> WorkbenchSnapshot:
    """Build a panel snapshot from session messages and a workspace listing."""
    if mode not in SECTIONS:
        raise ValueError(f"unsupported harness mode: {mode}")
    activities = _activities(messages)
    return WorkbenchSnapshot(
        mode=mode,
        section=normalize_section(mode, section),
        activities=tuple(activities),
        files=_file_paths(files, activities),
        plan=tuple(_plan_lines(messages)[:_MAX_PLAN]),
        permissions=tuple(_permissions(messages)[:_MAX_PLAN]),
    )


def _activity_document(item: ToolActivity) -> dict[str, object]:
    return {
        "label": item.label,
        "tool": item.tool,
        "target": item.target,
        "output": item.output,
        "error": item.error,
        "diff": [{"kind": line.kind, "text": line.text} for line in item.diff],
        "diffMissing": item.label == "Edit" and bool(item.target) and not item.diff,
    }


def _activities(messages: object) -> list[ToolActivity]:
    found: list[ToolActivity] = []
    for call in _latest_tools(messages):
        tool = call.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            continue
        arguments = call.get("input") if isinstance(call.get("input"), Mapping) else {}
        output = call.get("text") if isinstance(call.get("text"), str) else ""
        error = call.get("isError") is True
        changes = _file_changes(arguments)
        if changes:
            for path, diff in changes:
                found.append(ToolActivity(
                    label="Edit",
                    tool=tool.strip(),
                    target=path,
                    output=_clip(output, _MAX_OUTPUT),
                    diff=diff,
                    error=error,
                ))
                if len(found) == _MAX_ACTIVITIES:
                    return found
            continue
        found.append(ToolActivity(
            label=_label(tool),
            tool=tool.strip(),
            target=_target(arguments),
            output=_clip(output, _MAX_OUTPUT),
            diff=_structured_diff(arguments, call.get("result")),
            error=error,
        ))
        if len(found) == _MAX_ACTIVITIES:
            break
    return found


def _latest_tools(messages: object) -> list[Mapping]:
    found: list[Mapping] = []
    if not isinstance(messages, list):
        return found
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        uses = message.get("toolUses")
        if not isinstance(uses, list):
            continue
        calls = [item for item in uses if isinstance(item, Mapping)]
        if calls:
            found = calls
    return found


def _leaf(name: str) -> str:
    token = name.strip().lower().replace("-", "_").replace(" ", "_")
    return token.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _label(name: str) -> str:
    leaf = _leaf(name)
    if leaf in _PLAN or leaf.endswith("_plan"):
        return "Plan"
    if leaf in _BASH or leaf.endswith("_bash"):
        return "Bash"
    if leaf in _EDIT or any(part in leaf for part in ("edit", "write", "patch")):
        return "Edit"
    if leaf in _READ or leaf.startswith("read") or leaf.endswith("_read"):
        return "Read"
    return "Other"


def _file_changes(arguments: Mapping) -> list[tuple[str, tuple[DiffLine, ...]]]:
    """One Codex fileChange lists ``changes`` as ``{path, kind, diff}`` entries."""
    raw = arguments.get("changes")
    if not isinstance(raw, list):
        return []
    found: list[tuple[str, tuple[DiffLine, ...]]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path") if isinstance(item.get("path"), str) else ""
        diff = _file_change_diff(item)
        if not path.strip() and not diff:
            continue
        found.append((_clip(path.strip(), 180), diff))
    return found


def _file_change_diff(change: Mapping) -> tuple[DiffLine, ...]:
    """Read unified updates and the plain content used by Codex add/delete changes."""
    raw = change.get("diff")
    parsed = _patch_lines(raw)
    if parsed or not isinstance(raw, str):
        return parsed
    kind = change.get("kind")
    kind_name = kind.get("type") if isinstance(kind, Mapping) else kind
    if kind_name not in ("add", "delete"):
        return ()
    line_kind = "add" if kind_name == "add" else "del"
    return tuple(
        DiffLine(line_kind, _clip(line, 180))
        for line in raw.splitlines()[:_MAX_DIFF]
        if line
    )


def _target(arguments: Mapping) -> str:
    for key in ("path", "file_path", "file", "target"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip(), 180)
    for key in ("command", "cmd", "script"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip().splitlines()[0], 180)
    return ""


def _structured_diff(arguments: Mapping, result: object) -> tuple[DiffLine, ...]:
    """Use a structured patch only. Free text is never scanned for added or removed lines."""
    found = _diff_lines(arguments)
    if found or not isinstance(result, Mapping):
        return found
    return _diff_lines(result)


def _diff_lines(arguments: Mapping) -> tuple[DiffLine, ...]:
    """Added lines are green in the panel. Removed lines are red."""
    for key in ("diff", "patch", "unified_diff"):
        parsed = _patch_lines(arguments.get(key))
        if parsed:
            return parsed
    lines: list[DiffLine] = []
    old = arguments.get("old_string")
    if not isinstance(old, str):
        old = arguments.get("old_str")
    new = arguments.get("new_string")
    if not isinstance(new, str):
        new = arguments.get("new_str")
    if isinstance(old, str):
        lines.extend(DiffLine("del", _clip(line, 180)) for line in old.splitlines()[:_MAX_DIFF] if line)
    if isinstance(new, str):
        lines.extend(DiffLine("add", _clip(line, 180)) for line in new.splitlines()[:_MAX_DIFF] if line)
    if lines:
        return tuple(lines[:_MAX_DIFF])
    for key in ("contents", "content", "file_text", "new_content", "code"):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw.strip():
            return tuple(
                DiffLine("add", _clip(line, 180)) for line in raw.splitlines()[:_MAX_DIFF] if line
            )
    return ()


def _patch_lines(raw: object) -> tuple[DiffLine, ...]:
    if not isinstance(raw, str) or not raw.strip():
        return ()
    if not any(token in raw for token in ("@@", "***", "diff --git", "\n+", "\n-")) and not raw.startswith(("+", "-")):
        return ()
    return _unified(raw)


def _unified(raw: str) -> tuple[DiffLine, ...]:
    lines: list[DiffLine] = []
    for raw_line in raw.splitlines():
        if raw_line.startswith(("+++", "---")) or raw_line.startswith(("@@", "diff ")):
            continue
        if raw_line.startswith("+"):
            lines.append(DiffLine("add", _clip(raw_line[1:], 180)))
        elif raw_line.startswith("-"):
            lines.append(DiffLine("del", _clip(raw_line[1:], 180)))
        if len(lines) == _MAX_DIFF:
            break
    return tuple(lines)


def _plan_lines(messages: object) -> list[str]:
    lines: list[str] = []
    for call in _latest_tools(messages):
        arguments = call.get("input") if isinstance(call.get("input"), Mapping) else {}
        lines.extend(_plan_from(arguments))
    return lines


def _plan_from(arguments: Mapping) -> list[str]:
    lines: list[str] = []
    for key in ("plan", "todos", "steps"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            lines.extend(line.strip() for line in value.splitlines() if line.strip())
        elif isinstance(value, list):
            lines.extend(_plan_item(item) for item in value)
    return [line for line in lines if line]


def _plan_item(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, Mapping):
        return ""
    label = item.get("content") or item.get("title") or item.get("text") or item.get("step")
    if not isinstance(label, str) or not label.strip():
        return ""
    status = item.get("status")
    if isinstance(status, str) and status.strip():
        return f"{status.strip()}: {label.strip()}"
    return label.strip()


def _permissions(messages: object) -> list[str]:
    lines: list[str] = []
    for call in _latest_tools(messages):
        tool = call.get("tool") if isinstance(call.get("tool"), str) else "tool"
        for source in (call.get("input"), call.get("result")):
            line = _permission_line(tool, source)
            if line:
                lines.append(line)
    return lines


def _permission_line(tool: str, source: object) -> str:
    if not isinstance(source, Mapping):
        return ""
    for key in ("permission", "decision", "approved"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(f"{tool}: {value.strip()}", 180)
        if isinstance(value, bool):
            return f"{tool}: {'allowed' if value else 'denied'}"
    return ""


def _file_paths(files: object, activities: list[ToolActivity]) -> tuple[str, ...]:
    paths: list[str] = []
    _walk_files(files.get("treeData") if isinstance(files, Mapping) else None, paths)
    for activity in activities:
        if activity.label in ("Read", "Edit") and activity.target and activity.target not in paths:
            paths.append(activity.target)
    return tuple(paths[:_MAX_FILES])


def _walk_files(nodes: object, paths: list[str]) -> None:
    if not isinstance(nodes, list) or len(paths) >= _MAX_FILES:
        return
    for node in nodes:
        if not isinstance(node, Mapping) or len(paths) >= _MAX_FILES:
            continue
        path = node.get("path")
        if isinstance(path, str) and path.strip() and node.get("kind") == "file" and path.strip() not in paths:
            paths.append(path.strip())
        _walk_files(node.get("children"), paths)


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
