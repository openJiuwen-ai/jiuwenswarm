from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any, Iterable


_TOOL_NAME_ALIASES = {
    "free_search": "mcp_free_search",
    "paid_search": "mcp_paid_search",
    "fetch_webpage": "mcp_fetch_webpage",
    "exec_command": "mcp_exec_command",
}

_LIST_TOOL_ALIASES = frozenset(
    {
        "list",
        "list_dir",
        "list_directories",
        "list_directory",
        "list_files",
        "list_paths",
        "ls",
        "glob",
    }
)

_SEARCH_TOOL_NAMES = frozenset(
    {
        "grep",
        "glob",
        "glob_file_search",
        "mcp_free_search",
        "mcp_paid_search",
        "search",
    }
)

_READ_TOOL_NAMES = frozenset(
    {
        "list",
        "list_dir",
        "list_directories",
        "list_directory",
        "list_files",
        "list_paths",
        "ls",
        "memory_get",
        "read",
        "read_file",
        "read_memory",
        "read_terminal_output",
        "read_text_file",
        "view",
    }
)

_EDIT_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "edit",
        "edit_file",
        "search_replace",
        "write",
        "write_file",
        "write_memory",
        "write_text_file",
    }
)

_DELETE_TOOL_NAMES = frozenset({"delete", "remove", "remove_file", "rm"})
_MOVE_TOOL_NAMES = frozenset({"move", "move_file", "rename", "rename_file"})
_EXECUTE_TOOL_NAMES = frozenset(
    {
        "bash",
        "create_terminal",
        "exec",
        "exec_command",
        "mcp_exec_command",
        "release_terminal",
        "run",
        "shell",
        "terminal_create",
        "wait_for_terminal_exit",
    }
)
_FETCH_TOOL_NAMES = frozenset({"fetch", "fetch_webpage", "mcp_fetch_webpage"})
_TERMINAL_CREATE_TOOL_NAMES = frozenset({"create_terminal", "terminal_create"})
_TERMINAL_WAIT_EXIT_TOOL_NAMES = frozenset({"wait_for_terminal_exit"})

_PATH_KEYS = (
    "path",
    "file_path",
    "file",
    "dir_path",
    "target_path",
    "destination",
    "source",
    "cwd",
    "root",
)


def is_reasoning_event(event_type: Any, payload: dict[str, Any]) -> bool:
    event_name = getattr(event_type, "value", event_type)
    if str(event_name or "") == "chat.reasoning":
        return True
    source_chunk_type = str(payload.get("source_chunk_type") or "")
    payload_event_type = str(payload.get("event_type") or "")
    return source_chunk_type == "llm_reasoning" or payload_event_type == "chat.reasoning"


def normalize_tool_name(tool_name: str) -> str:
    normalized = str(tool_name or "").strip()
    return _TOOL_NAME_ALIASES.get(normalized, normalized)


def build_acp_tool_descriptor(
    tool_name: str,
    arguments: Any,
    *,
    tool_call_id: str,
    status: str | None = None,
    raw_output: Any = None,
    title: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    raw_input = _normalize_arguments(arguments)
    normalized_name = normalize_tool_name(tool_name)
    resolved_kind = str(kind or _infer_tool_kind(normalized_name)).strip() or "other"
    resolved_title = str(title or _build_tool_title(normalized_name, raw_input)).strip()

    descriptor: dict[str, Any] = {
        "toolCallId": str(tool_call_id or "").strip(),
        "title": resolved_title,
        "kind": resolved_kind,
    }
    if status:
        descriptor["status"] = str(status)
    if raw_input:
        descriptor["rawInput"] = raw_input
    locations = _extract_locations(raw_input)
    if locations:
        descriptor["locations"] = locations
    if raw_output is not None and not _is_list_like_tool_name(normalized_name):
        descriptor["rawOutput"] = raw_output
    return descriptor


def build_acp_tool_call_update(
    payload: dict[str, Any],
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict):
        return None

    tool_call_id = _resolve_tool_call_id(tool_call)
    if not tool_call_id:
        return None

    original_name = str(tool_call.get("name") or "")
    arguments = tool_call.get("arguments", {})
    descriptor = build_acp_tool_descriptor(
        original_name,
        arguments,
        tool_call_id=tool_call_id,
        status=str(tool_call.get("status") or payload.get("status") or "pending"),
        title=tool_call.get("title") or payload.get("title"),
        kind=tool_call.get("kind") or payload.get("kind"),
    )

    update = {
        "sessionUpdate": "tool_call",
        "toolCall": {
            "id": tool_call_id,
            "name": original_name,
            "arguments": _legacy_arguments(arguments),
        },
        **descriptor,
    }

    if cache is not None:
        cache[tool_call_id] = {
            **descriptor,
            "toolName": original_name,
        }

    return update


def build_acp_tool_result_update(
    payload: dict[str, Any],
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: Any = payload.get("result")
    if result is None:
        result = payload.get("content", "")

    tool_call_id = _resolve_tool_call_id(payload)
    cached = cache.get(tool_call_id, {}) if cache and tool_call_id else {}
    tool_name = str(payload.get("tool_name") or payload.get("name") or "").strip()
    if not tool_name:
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict):
            tool_name = str(tool_call.get("name") or "").strip()
    if not tool_name:
        tool_name = str(cached.get("toolName") or "")
    arguments = payload.get("arguments") or payload.get("rawInput")
    if arguments in (None, "", [], {}):
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict):
            arguments = tool_call.get("arguments")
    raw_output = payload.get("rawOutput")
    if raw_output is None:
        raw_output = payload.get("raw_output")
    terminal_id = _extract_terminal_id(raw_output)

    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": tool_call_id,
    }
    if result not in (None, "") and not terminal_id:
        update["result"] = result
    if tool_name:
        update["toolName"] = tool_name

    if isinstance(cached, dict):
        for key in ("title", "kind", "rawInput", "locations"):
            value = cached.get(key)
            if value not in (None, "", [], {}):
                update[key] = value

    descriptor = build_acp_tool_descriptor(
        tool_name,
        arguments or update.get("rawInput") or {},
        tool_call_id=tool_call_id,
        status=_resolve_tool_result_status(payload, result, tool_name, raw_output),
        raw_output=raw_output,
        title=payload.get("title") or update.get("title"),
        kind=payload.get("kind") or update.get("kind"),
    )
    for key in ("title", "kind", "status", "rawOutput"):
        value = descriptor.get(key)
        if value not in (None, ""):
            update[key] = value

    content = _build_terminal_content_blocks(terminal_id) or _build_content_blocks(result)
    if content:
        update["content"] = content

    if cache is not None and tool_call_id:
        cache[tool_call_id] = {
            **cached,
            **{
                key: value
                for key, value in descriptor.items()
                if key in {"title", "kind", "rawInput", "locations", "rawOutput"}
            },
            "toolName": tool_name or cached.get("toolName", ""),
        }

    return update


def build_acp_todo_update(payload: dict[str, Any]) -> dict[str, Any] | None:
    todos = payload.get("todos")
    if not isinstance(todos, Iterable) or isinstance(todos, (str, bytes, dict)):
        return None

    normalized: list[Any] = []
    for item in todos:
        if isinstance(item, dict):
            normalized.append(dict(item))
        else:
            normalized.append(item)

    return {
        "sessionUpdate": "todo_update",
        "todos": normalized,
    }


def _resolve_tool_call_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("tool_call_id")
        or payload.get("toolCallId")
        or payload.get("id")
        or ""
    ).strip()


def _legacy_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    parsed = _normalize_arguments(arguments)
    return parsed or arguments or {}


def _normalize_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"input": arguments} if arguments.strip() else {}
        return dict(parsed) if isinstance(parsed, dict) else {"input": arguments}
    return {}


def _infer_tool_kind(tool_name: str) -> str:
    normalized = normalize_tool_name(tool_name).lower()
    if normalized in _DELETE_TOOL_NAMES:
        return "delete"
    if normalized in _MOVE_TOOL_NAMES:
        return "move"
    if normalized in _EDIT_TOOL_NAMES:
        return "edit"
    if normalized in _EXECUTE_TOOL_NAMES:
        return "execute"
    if normalized in _FETCH_TOOL_NAMES:
        return "fetch"
    if normalized in _SEARCH_TOOL_NAMES:
        return "search"
    if normalized in _READ_TOOL_NAMES or _is_list_like_tool_name(normalized):
        return "read"
    return "other"


def _build_tool_title(tool_name: str, arguments: dict[str, Any]) -> str:
    normalized = normalize_tool_name(tool_name).lower()
    path = _summarize_path(_first_string_value(arguments, *_PATH_KEYS))
    query = _first_string_value(arguments, "pattern", "query", "term", "text")
    url = _first_string_value(arguments, "url")
    command = _first_string_value(arguments, "command", "cmd")
    terminal_id = _first_string_value(arguments, "terminalId", "terminal_id")

    if normalized in _EXECUTE_TOOL_NAMES:
        if normalized in {"read_terminal_output", "wait_for_terminal_exit", "release_terminal"} and terminal_id:
            verb = {
                "read_terminal_output": "Reading terminal output",
                "wait_for_terminal_exit": "Waiting for terminal exit",
                "release_terminal": "Releasing terminal",
            }.get(normalized, "Running command")
            return f"{verb} {terminal_id}"
        if command:
            return f"Running {command}"
        if path:
            return f"Running in {path}"
        return "Running command"

    if normalized in _FETCH_TOOL_NAMES and url:
        return f"Fetching {url}"
    if normalized in _SEARCH_TOOL_NAMES:
        if query and path:
            return f"Searching {query} in {path}"
        if query:
            return f"Searching {query}"
        if path:
            return f"Searching in {path}"
        return "Searching"
    if normalized in _EDIT_TOOL_NAMES:
        return f"Editing {path}" if path else "Editing files"
    if normalized in _DELETE_TOOL_NAMES:
        return f"Deleting {path}" if path else "Deleting files"
    if normalized in _MOVE_TOOL_NAMES:
        destination = _summarize_path(
            _first_string_value(arguments, "destination", "dest", "target_path")
        )
        if path and destination:
            return f"Moving {path} to {destination}"
        return f"Moving {path}" if path else "Moving files"
    if _is_list_like_tool_name(normalized):
        return f"Listing {path}" if path else "Listing files"
    if normalized in _READ_TOOL_NAMES:
        return f"Reading {path}" if path else "Reading data"
    return _humanize_tool_name(tool_name)


def _build_content_blocks(result: Any) -> list[dict[str, Any]]:
    text = str(result or "").strip()
    if not text:
        return []
    return [
        {
            "type": "content",
            "content": {
                "type": "text",
                "text": text,
            },
        }
    ]


def _build_terminal_content_blocks(terminal_id: str | None) -> list[dict[str, Any]]:
    if not terminal_id:
        return []
    return [{"type": "terminal", "terminalId": terminal_id}]


def _extract_locations(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    line = _resolve_line(arguments)

    for key in _PATH_KEYS:
        location = _location_from_value(arguments.get(key), line=line)
        if location is None:
            continue
        marker = (location["path"], location.get("line"))
        if marker not in seen:
            seen.add(marker)
            locations.append(location)

    paths_value = arguments.get("paths")
    if isinstance(paths_value, list):
        for item in paths_value[:5]:
            location = _location_from_value(item, line=line)
            if location is None:
                continue
            marker = (location["path"], location.get("line"))
            if marker not in seen:
                seen.add(marker)
                locations.append(location)

    return locations


def _location_from_value(value: Any, *, line: int | None) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    location: dict[str, Any] = {"path": value.strip()}
    if isinstance(line, int) and line > 0:
        location["line"] = line
    return location


def _resolve_line(arguments: dict[str, Any]) -> int | None:
    for key in ("line", "lineno", "line_number"):
        value = arguments.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _first_string_value(arguments: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _summarize_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        pure = PurePath(path)
    except Exception:
        return path
    parts = [part for part in pure.parts if part not in ("\\", "/")]
    if not parts:
        return path
    return "/".join(parts[-3:])


def _humanize_tool_name(tool_name: str) -> str:
    parts = [part for part in str(tool_name or "").replace(".", "_").split("_") if part]
    if not parts:
        return "Using tool"
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _extract_terminal_id(raw_output: Any) -> str | None:
    if not isinstance(raw_output, dict):
        return None
    return _first_string_value(raw_output, "terminalId", "terminal_id")


def _resolve_tool_result_status(
    payload: dict[str, Any],
    result: Any,
    tool_name: str,
    raw_output: Any,
) -> str:
    status = str(payload.get("status") or "").strip()
    if status:
        return status
    terminal_id = _extract_terminal_id(raw_output)
    normalized_tool_name = normalize_tool_name(tool_name).strip().lower()
    if terminal_id and normalized_tool_name in _TERMINAL_CREATE_TOOL_NAMES:
        return "in_progress"
    if (
        isinstance(raw_output, dict)
        and bool(raw_output.get("timedOut"))
        and normalized_tool_name in _TERMINAL_WAIT_EXIT_TOOL_NAMES
    ):
        return "in_progress"
    if payload.get("error") not in (None, ""):
        return "failed"
    if result not in (None, ""):
        return "completed"
    return "in_progress"


def _is_list_like_tool_name(tool_name: str) -> bool:
    normalized = normalize_tool_name(tool_name).strip().lower()
    return normalized in _LIST_TOOL_ALIASES


__all__ = [
    "build_acp_todo_update",
    "build_acp_tool_call_update",
    "build_acp_tool_descriptor",
    "build_acp_tool_result_update",
    "is_reasoning_event",
    "normalize_tool_name",
]
