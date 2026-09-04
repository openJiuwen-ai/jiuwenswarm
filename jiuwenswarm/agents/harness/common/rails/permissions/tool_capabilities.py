# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Security-relevant tool capability taxonomy for auto permissions."""

from __future__ import annotations

from dataclasses import dataclass

from openjiuwen.harness.security.permission_engine.fileguard.file_tool_specs import (
    FileToolSpec,
    lookup_file_tool_specs,
    register_file_tool,
)

from jiuwenswarm.common.permission_tools import (
    PERMISSION_TOOL_ALIASES,
    PermissionToolNameResolution,
    normalize_permission_tool_name,
    resolve_permission_tool_name,
)

TOOL_CAPABILITY_FACTS_VERSION = "2"
HOST_STATIC_FACTS = "host_static"
NAME_CLASSIFICATION_HINT = "name_classification_hint"
UNKNOWN_FACTS = "unknown"

_READ_PDF_FILE_SPEC = FileToolSpec("read_pdf", "pdf_path", "read")


def install_permission_file_semantics() -> None:
    """Install JiuwenSwarm file semantics at the Permission composition boundary."""

    specs = lookup_file_tool_specs(_READ_PDF_FILE_SPEC.tool_name)
    if specs is None:
        register_file_tool(_READ_PDF_FILE_SPEC)
        specs = lookup_file_tool_specs(_READ_PDF_FILE_SPEC.tool_name)
    if specs != [_READ_PDF_FILE_SPEC]:
        raise RuntimeError("permission_file_semantics_conflict:read_pdf")


@dataclass(frozen=True)
class ToolCapability:
    """Immutable Host facts; this value never grants a permission decision."""

    tool_name: str
    registered_name: str
    aliases: tuple[str, ...]
    category: str
    operation_family: str
    resource_class: str
    static_side_effects: frozenset[str]
    risk_tier: str
    high_flex: bool
    display_key: str
    redaction_key: str
    facts_source: str
    facts_version: str
    alias_conflict: bool


_SHELL_TOOLS = {"bash", "cmd", "powershell", "mcp_exec_command", "create_terminal"}
_CODE_TOOLS = {"code"}
_PATH_TOOLS = {
    "apply_patch",
    "read_file",
    "read_pdf",
    "write_file",
    "edit_file",
    "read_text_file",
    "write_text_file",
    "write",
    "read",
    "glob_file_search",
    "glob",
    "list_dir",
    "list_files",
    "grep",
    "search_replace",
}
_PUBLIC_SEARCH_TOOLS = {"mcp_free_search", "mcp_paid_search"}
_PUBLIC_WEB_READ_TOOLS = {"mcp_fetch_webpage"}
_SUBAGENT_TOOLS = {"task_tool", "spawn_sub_agent", "session_new"}
_SUBAGENT_RUNTIME_CONTROL_TOOLS = {
    "subagent_close",
    "subagent_list",
    "subagent_resume",
    "subagent_send_input",
    "subagent_spawn",
    "subagent_wait",
}
_HOST_TODO_TOOLS = {
    "todo_create",
    "todo_list",
    "todo_get",
    "todo_modify",
}
_LEGACY_TODO_TOOLS = {
    "todo_insert",
    "todo_complete",
    "todo_remove",
}
_SESSION_STATUS_TOOLS = {"session_list"}
_ASK_USER_TOOLS = {"ask_user"}
_SKILL_READ_TOOLS = {"skill_tool"}
_SKILL_CHANGE_TOOLS = {"install_skill", "uninstall_skill"}
_SKILL_DISCOVERY_TOOLS = {"search_skill"}
_EXTERNAL_SEND_TOOLS = {
    "send_file_to_user",
    "upload_file",
    "upload_photo",
    "send_message",
}
_DEVICE_TOOLS = {
    "call_phone",
    "save_media_to_gallery",
    "save_file_to_file_manager",
}
_MEMORY_WRITE_TOOLS = {
    "write_memory",
    "edit_memory",
    "viking_remember",
    "viking_add_resource",
    "mem0_conclude",
    "coding_memory_write",
    "coding_memory_edit",
}
_LOCAL_MEMORY_READ_TOOLS = {
    "memory_get",
    "memory_search",
    "read_memory",
}
_REVIEWED_MEMORY_READ_TOOLS = {
    "coding_memory_read",
    "ltm_search",
    "ltm_search_summary",
    "mem0_profile",
    "mem0_search",
    "viking_browse",
    "viking_read",
    "viking_search",
}
_FIXED_BROWSER_TOOLS = {
    "browser_probe_cards",
    "browser_probe_interactives",
    "browser_recall_offload",
}
_DYNAMIC_REPRESENTATIVE_TOOLS = {
    "browser_click",
    "browser_open",
    "browser_run_task",
    "browser_snapshot",
    "cron_create_job",
    "cron_delete_job",
    "cron_preview_job",
    "mcp_unknown_tool",
    "todo_create",
    "todo_list",
}
_STATIC_CANONICAL_TOOL_NAMES = frozenset(
    set(PERMISSION_TOOL_ALIASES.values())
    | _SHELL_TOOLS
    | _CODE_TOOLS
    | _PATH_TOOLS
    | _PUBLIC_SEARCH_TOOLS
    | _PUBLIC_WEB_READ_TOOLS
    | _SUBAGENT_TOOLS
    | _SUBAGENT_RUNTIME_CONTROL_TOOLS
    | _HOST_TODO_TOOLS
    | _LEGACY_TODO_TOOLS
    | _SESSION_STATUS_TOOLS
    | _ASK_USER_TOOLS
    | _SKILL_READ_TOOLS
    | _SKILL_CHANGE_TOOLS
    | _SKILL_DISCOVERY_TOOLS
    | _EXTERNAL_SEND_TOOLS
    | _DEVICE_TOOLS
    | _MEMORY_WRITE_TOOLS
    | _LOCAL_MEMORY_READ_TOOLS
    | _REVIEWED_MEMORY_READ_TOOLS
    | _FIXED_BROWSER_TOOLS
    | _DYNAMIC_REPRESENTATIVE_TOOLS
    | {"xiaoyi_gui_agent"}
)
_TRUSTED_BROWSER_MCP_SERVER_PREFIXES = (
    "mcp_playwright-official_browser_",
    "mcp_playwright_official_browser_",
)
_TRUSTED_BROWSER_MCP_TOOL_SUFFIXES = frozenset(
    {
        "click",
        "close",
        "console_messages",
        "drag",
        "drop",
        "evaluate",
        "file_upload",
        "fill_form",
        "handle_dialog",
        "hover",
        "navigate",
        "navigate_back",
        "network_request",
        "network_requests",
        "press_key",
        "route",
        "resize",
        "run_code_unsafe",
        "select_option",
        "snapshot",
        "tabs",
        "take_screenshot",
        "type",
        "unroute",
        "wait_for",
    }
)


def normalize_tool_name(tool_name: str) -> str:
    """Normalize frontend or alias tool names to policy tool names."""
    return normalize_permission_tool_name(tool_name)


def classify_tool(tool_name: str) -> ToolCapability:
    """Return the configured capability for a normalized tool name."""
    resolution = resolve_permission_tool_name(
        tool_name,
        canonical_names=_STATIC_CANONICAL_TOOL_NAMES,
    )
    normalized = resolution.canonical_name
    lowered = normalized.lower()

    if resolution.conflict:
        return _capability(
            resolution, "unknown", "unknown", "unknown", set(), "high", True
        )
    if lowered in _SHELL_TOOLS:
        return _capability(
            resolution,
            "shell",
            "dynamic_execution",
            "command",
            {"dynamic_exec"},
            "high",
            True,
        )
    if lowered in _CODE_TOOLS:
        return _capability(
            resolution,
            "code",
            "dynamic_execution",
            "code",
            {"dynamic_exec"},
            "high",
            True,
        )
    if lowered in _PATH_TOOLS:
        side_effects = {"write_local"} if _is_path_write_tool(lowered) else set()
        return _capability(
            resolution,
            "path",
            "workspace_write" if side_effects else "workspace_read",
            "workspace_path",
            side_effects,
            "medium" if side_effects else "low",
            False,
        )
    if lowered in _PUBLIC_SEARCH_TOOLS:
        return _capability(
            resolution,
            "network",
            "public_search",
            "public_web",
            {"network_search"},
            "medium",
            False,
        )
    if lowered in _PUBLIC_WEB_READ_TOOLS:
        return _capability(
            resolution,
            "network",
            "public_web_read",
            "public_web",
            {"network_read"},
            "medium",
            False,
        )
    if lowered in _HOST_TODO_TOOLS:
        return _capability(
            resolution,
            "task_management",
            "task_state",
            "session_task_state",
            set(),
            "low",
            False,
        )
    if lowered in _LEGACY_TODO_TOOLS:
        return _capability(
            resolution,
            "task_management",
            "task_state",
            "session_task_state",
            set(),
            "medium",
            True,
            authoritative=False,
        )
    if lowered in _SESSION_STATUS_TOOLS:
        return _capability(
            resolution,
            "task_management",
            "session_status",
            "session_task_state",
            set(),
            "low",
            False,
        )
    if lowered in _ASK_USER_TOOLS:
        return _capability(
            resolution,
            "interaction",
            "ask_user",
            "user_interaction",
            set(),
            "low",
            False,
        )
    if lowered.startswith("cron_"):
        return _capability(
            resolution,
            "cron",
            "schedule_change",
            "schedule",
            {"schedule"},
            "high",
            True,
            authoritative=False,
        )
    if (
        isinstance(tool_name, str)
        and tool_name in _SUBAGENT_RUNTIME_CONTROL_TOOLS
        and resolution.registered_name == tool_name
        and resolution.canonical_name == tool_name
        and not resolution.aliases
    ):
        return _capability(
            resolution,
            "subagent",
            "subagent_runtime_control",
            "agent_runtime",
            {"delegation"},
            "low",
            False,
        )
    if lowered in _SUBAGENT_TOOLS:
        return _capability(
            resolution,
            "subagent",
            "subagent_control",
            "agent_runtime",
            {"delegation"},
            "high",
            True,
            authoritative=lowered == "task_tool",
        )
    if lowered in _FIXED_BROWSER_TOOLS:
        operation_family = (
            "browser_session_recall"
            if lowered == "browser_recall_offload"
            else "browser_session_observe"
        )
        static_side_effects = (
            set() if lowered == "browser_recall_offload" else {"session_cache_write"}
        )
        return _capability(
            resolution,
            "browser",
            operation_family,
            "browser_session",
            static_side_effects,
            "low",
            False,
        )
    if (
        lowered.startswith("browser_")
        or lowered == "xiaoyi_gui_agent"
        or _is_trusted_browser_mcp_tool(lowered)
    ):
        return _capability(
            resolution,
            "browser",
            "browser_action",
            "browser_session",
            {"browser_action"},
            "high",
            True,
            authoritative=False,
        )
    if lowered in _SKILL_DISCOVERY_TOOLS:
        return _capability(
            resolution,
            "skill",
            "skill_discovery",
            "skill_catalog",
            set(),
            "medium",
            False,
        )
    if lowered in _SKILL_READ_TOOLS:
        return _capability(
            resolution,
            "skill",
            "skill_read",
            "installed_skill",
            set(),
            "low",
            False,
        )
    if lowered in _SKILL_CHANGE_TOOLS:
        return _capability(
            resolution,
            "skill",
            "skill_change",
            "skill_catalog",
            {"skill_change"},
            "high",
            True,
        )
    if lowered in _EXTERNAL_SEND_TOOLS:
        return _capability(
            resolution,
            "external_send",
            "external_send",
            "external_recipient",
            {"external_send"},
            "high",
            True,
        )
    if lowered in _DEVICE_TOOLS:
        return _capability(
            resolution,
            "device",
            "device_action",
            "user_device",
            {"device_action"},
            "high",
            True,
        )
    if lowered in _MEMORY_WRITE_TOOLS:
        return _capability(
            resolution,
            "memory",
            "memory_write",
            "memory_store",
            {"memory_write"},
            "high",
            True,
        )
    if lowered in _LOCAL_MEMORY_READ_TOOLS:
        operation_family = (
            "memory_search" if lowered == "memory_search" else "memory_read"
        )
        return _capability(
            resolution,
            "memory",
            operation_family,
            "memory_store",
            set(),
            "low",
            False,
        )
    if lowered in _REVIEWED_MEMORY_READ_TOOLS:
        operation_family = (
            "memory_search"
            if lowered
            in {"ltm_search", "ltm_search_summary", "mem0_search", "viking_search"}
            else "memory_read"
        )
        return _capability(
            resolution,
            "memory",
            operation_family,
            "memory_store",
            set(),
            "medium",
            True,
            authoritative=False,
        )
    if lowered.startswith("mcp_"):
        return _capability(
            resolution,
            "mcp",
            "unknown_mcp",
            "external_resource",
            set(),
            "high",
            True,
            authoritative=False,
        )

    return _capability(
        resolution,
        "unknown",
        "unknown",
        "unknown",
        set(),
        "high",
        True,
        authoritative=False,
    )


def _capability(  # pylint: disable=huawei-too-many-arguments
    resolution: PermissionToolNameResolution,
    category: str,
    operation_family: str,
    resource_class: str,
    static_side_effects: set[str],
    risk_tier: str,
    high_flex: bool,
    *,
    authoritative: bool = True,
) -> ToolCapability:
    return ToolCapability(
        tool_name=resolution.canonical_name,
        registered_name=resolution.registered_name,
        aliases=resolution.aliases,
        category=category,
        operation_family=operation_family,
        resource_class=resource_class,
        static_side_effects=frozenset(static_side_effects),
        risk_tier=risk_tier,
        high_flex=high_flex,
        display_key=f"tool.{category}.{operation_family}",
        redaction_key=_redaction_key(resource_class),
        facts_source=(
            UNKNOWN_FACTS
            if resolution.conflict or category == "unknown"
            else HOST_STATIC_FACTS
            if authoritative
            else NAME_CLASSIFICATION_HINT
        ),
        facts_version=TOOL_CAPABILITY_FACTS_VERSION,
        alias_conflict=resolution.conflict,
    )


def _redaction_key(resource_class: str) -> str:
    if resource_class in {"command", "code"}:
        return "arguments_sensitive"
    if resource_class in {
        "workspace_path",
        "browser_session",
        "installed_skill",
        "memory_store",
    }:
        return "resource_sensitive"
    if resource_class in {"public_web", "external_recipient"}:
        return "network_sensitive"
    if resource_class in {"unknown", "external_resource"}:
        return "full_sensitive"
    return "default"


def _is_path_write_tool(tool_name: str) -> bool:
    return tool_name in {
        "apply_patch",
        "write_file",
        "edit_file",
        "write_text_file",
        "write",
        "search_replace",
    }


def _is_trusted_browser_mcp_tool(tool_name: str) -> bool:
    for prefix in _TRUSTED_BROWSER_MCP_SERVER_PREFIXES:
        if tool_name.startswith(prefix):
            suffix = tool_name.removeprefix(prefix)
            return suffix in _TRUSTED_BROWSER_MCP_TOOL_SUFFIXES
    return False
