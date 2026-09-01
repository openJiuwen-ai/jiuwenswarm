# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""工具调用可读展示名。

优先使用主模型在 tool_call 参数里一并给出的 call_goal（一句目标说明，
如「调研 openJiuwen 官网信息」）；执行前会从 arguments 剥掉该字段以免 schema 校验失败。
若模型未填，再用规则从工具名+参数兜底。

注意：绝不能用 display_name 作为 UI 字段名——team 的 spawn_teammate / build_team
等工具本身就有必填的 display_name（成员展示名），撞名会导致参数被误剥、持续报错。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

_CALL_GOAL_KEYS = ("call_goal", "callGoal")

_VERB_BY_TOOL: dict[str, str] = {
    "write": "写入", "write_file": "写入", "write_text_file": "写入",
    "create": "写入", "create_file": "写入", "write_memory": "写入",
    "coding_memory_write": "写入", "upload_file": "上传", "send_file_to_user": "发送文件",
    "generate_image": "生成图片", "generate_video": "生成视频",
    "read": "读取", "read_file": "读取", "read_text_file": "读取", "view": "读取",
    "read_memory": "读取", "memory_get": "读取", "coding_memory_read": "读取",
    "read_mcp_resource": "读取",
    "edit": "编辑", "edit_file": "编辑", "search_replace": "编辑", "apply_patch": "编辑",
    "str_replace": "编辑", "edit_memory": "编辑", "coding_memory_edit": "编辑",
    "delete": "删除", "delete_file": "删除", "remove": "删除", "remove_file": "删除", "rm": "删除",
    "move": "移动", "move_file": "移动", "rename": "重命名", "rename_file": "重命名",
    "list": "列出", "ls": "列出", "list_files": "列出", "list_dir": "列出",
    "list_directory": "列出", "list_directories": "列出",
    "grep": "搜索", "rg": "搜索", "ripgrep": "搜索", "search": "搜索", "search_file": "搜索",
    "memory_search": "搜索", "ltm_search": "搜索", "mem0_search": "搜索", "viking_search": "搜索",
    "glob": "查找", "glob_files": "查找", "glob_file_search": "查找",
    "web_search": "联网搜索", "web_free_search": "联网搜索", "free_search": "联网搜索",
    "web_paid_search": "联网搜索", "paid_search": "联网搜索",
    "web_fetch": "抓取", "web_fetch_webpage": "抓取", "fetch": "抓取", "fetch_webpage": "抓取",
    "run_command": "执行", "run": "执行", "bash": "执行", "shell": "执行", "sh": "执行",
    "exec": "执行", "exec_command": "执行", "execute_bash": "执行", "command": "执行",
    "sandbox_run_command": "执行",
    "run_python": "运行代码", "python": "运行代码", "execute_python": "运行代码",
    "run_code": "运行代码", "execute_code": "运行代码", "code_interpreter": "运行代码", "code": "运行代码",
    "todo_create": "创建待办", "todo_modify": "更新待办", "todo_list": "查看待办", "todo_get": "读取待办",
    "skill_tool": "查看技能",
    # team tools often omit call_goal
    "spawn_member": "创建成员", "spawn_teammate": "创建成员",
    "send_message": "发送消息",
    "build_team": "创建团队",
    "shutdown_member": "关闭成员", "create_task": "创建任务", "update_task": "更新任务",
}

_FILE_VERBS = frozenset(["写入", "读取", "编辑", "删除", "移动", "重命名", "列出", "上传", "发送文件"])
_QUERY_VERBS = frozenset(["搜索", "查找", "联网搜索"])
_COMMAND_VERBS = frozenset(["执行", "运行代码"])
_SKILL_VERBS = frozenset(["查看技能"])
_TODO_VERBS = frozenset(["创建待办", "更新待办", "查看待办", "读取待办"])

_FILE_ARG_KEYS = (
    "target_file", "file_path", "filepath", "filePath", "path",
    "file", "filename", "fileName", "dir", "directory",
    "abs_file_path_list",
)
_QUERY_ARG_KEYS = ("query", "q", "search_query", "searchQuery", "pattern", "keyword", "keywords")
_COMMAND_ARG_KEYS = ("command", "cmd", "script", "code", "source")
_URL_ARG_KEYS = ("url", "uri", "link", "webpage")
_SKILL_ARG_KEYS = ("skill_name", "skillName", "name")

_CALL_GOAL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "一句简短中文，说明这次工具调用要达成的目标（仅界面展示），"
        "例如「调研 openJiuwen 官网信息」「创建三子棋对战团队」「通知 player-x 落子」。"
        "不要只写工具名或裸 URL；不影响工具实际执行。"
        "与工具自带的 display_name（成员/团队展示名）以及 send_message.summary 都不是同一个字段，"
        "调用 spawn_member / spawn_teammate / send_message / build_team 时也必须填写。"
    ),
}


def _normalize(name: str) -> str:
    result = name.strip().lower()
    for ch in (" ", "-"):
        result = result.replace(ch, "_")
    while "__" in result:
        result = result.replace("__", "_")
    return result


def _as_mapping(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, Mapping):
                return dict(parsed)
        except (ValueError, TypeError):
            return {}
    return {}


def _first_string(args: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            trimmed = value.strip()
            if trimmed.startswith("["):
                try:
                    parsed = json.loads(trimmed)
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, str) and item.strip():
                                return item.strip()
                except (ValueError, TypeError):
                    pass
            return trimmed
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""


def _basename(path: str) -> str:
    trimmed = path.rstrip("/\\")
    for sep in ("/", "\\"):
        if sep in trimmed:
            trimmed = trimmed.rsplit(sep, 1)[-1]
    return trimmed


def _truncate(value: str, max_len: int) -> str:
    one_line = " ".join(value.split())
    return one_line[:max_len] + "…" if len(one_line) > max_len else one_line


def inject_call_goal_schema(parameters: Any) -> None:
    """给工具 JSON Schema 注入可选 call_goal，供主模型随 tool_call 一并产出。"""
    if not isinstance(parameters, dict):
        return
    props = parameters.get("properties")
    if not isinstance(props, dict):
        props = {}
        parameters["properties"] = props
    # 若工具已有 call_goal 定义则不覆盖；永远不要碰 display_name。
    if "call_goal" not in props:
        props["call_goal"] = dict(_CALL_GOAL_SCHEMA)
    # 清理此前误注入的 UI 用 display_name（仅当描述像我们的 UI 文案时）。
    existing = props.get("display_name")
    if isinstance(existing, dict):
        desc = str(existing.get("description") or "")
        if "界面展示" in desc or "UI only" in desc or "不影响工具实际执行" in desc:
            props.pop("display_name", None)


_CALL_GOAL_MAX_LEN = 200


def extract_call_goal(arguments: Any) -> tuple[str, Any]:
    """从 arguments 取出 call_goal，并返回剥掉该字段后的 arguments（保持原类型风格）。

    不会触碰 display_name（team 成员名等业务字段）。
    call_goal 截断到 _CALL_GOAL_MAX_LEN，避免异常超长撑爆展示/事件负载。
    """
    if isinstance(arguments, Mapping):
        cleaned = dict(arguments)
        name = ""
        for key in _CALL_GOAL_KEYS:
            raw = cleaned.pop(key, None)
            if isinstance(raw, str) and raw.strip() and not name:
                name = _truncate(raw.strip(), _CALL_GOAL_MAX_LEN)
        return name, cleaned

    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return "", arguments
        if not isinstance(parsed, Mapping):
            return "", arguments
        name, cleaned_map = extract_call_goal(parsed)
        return name, json.dumps(cleaned_map, ensure_ascii=False)

    return "", arguments


def _format_message_to(to_value: Any) -> str:
    if isinstance(to_value, list):
        names = [str(item).strip() for item in to_value if str(item).strip()]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) <= 3:
            return "、".join(names)
        return f"{names[0]} 等{len(names)}人"
    if isinstance(to_value, str) and to_value.strip():
        raw = to_value.strip()
        return "全员" if raw == "*" else raw
    return ""


def build_tool_display_name(name: str, arguments: Any) -> str:
    """规则兜底：根据工具名+参数组可读展示名；无法可靠组出时返回空串。"""
    if not name:
        return ""
    verb = _VERB_BY_TOOL.get(_normalize(name))
    if not verb:
        return ""

    args = _as_mapping(arguments)
    for key in _CALL_GOAL_KEYS:
        args.pop(key, None)

    file = _first_string(args, _FILE_ARG_KEYS)
    query = _first_string(args, _QUERY_ARG_KEYS)
    command = _first_string(args, _COMMAND_ARG_KEYS)
    url = _first_string(args, _URL_ARG_KEYS)
    skill = _first_string(args, _SKILL_ARG_KEYS)

    # team：优先复用工具自身语义字段（summary / desc），避免漏 call_goal 时 UI 空白。
    if verb == "发送消息":
        summary = _first_string(args, ("summary",))
        if summary:
            return _truncate(summary, 48)
        to_label = _format_message_to(args.get("to"))
        content = _first_string(args, ("content", "message", "text"))
        if to_label and content:
            return f"{verb}给 {to_label}：{_truncate(content, 32)}"
        if to_label:
            return f"{verb}给 {to_label}"
        if content:
            return f"{verb} {_truncate(content, 40)}"
        return verb

    if verb == "创建成员":
        desc = _first_string(args, ("desc", "description", "prompt"))
        member = _first_string(args, ("display_name", "member_name", "name"))
        if desc:
            return _truncate(desc, 48)
        if member:
            return f"{verb} {member}"
        return verb

    if verb == "创建团队":
        team = _first_string(args, ("display_name", "team_desc", "team_name"))
        return f"{verb} {team}" if team else verb

    if verb == "关闭成员":
        member = _first_string(args, ("member_name", "display_name", "name"))
        return f"{verb} {member}" if member else verb

    if verb in ("创建任务", "更新任务"):
        title = _first_string(args, ("title", "content", "task", "description", "desc"))
        return f"{verb} {_truncate(title, 40)}" if title else verb

    if verb in _FILE_VERBS and file:
        return f"{verb} {_basename(file)}"
    if verb in _QUERY_VERBS and query:
        return f'{verb} "{_truncate(query, 40)}"'
    if verb == "抓取" and url:
        return f"{verb} {_truncate(url, 48)}"
    if verb in _COMMAND_VERBS and command:
        return f"{verb} {_truncate(command, 40)}"
    if verb in _SKILL_VERBS and skill:
        return f"{verb} {skill}"
    if verb == "创建待办":
        tasks = args.get("tasks")
        count = len(tasks) if isinstance(tasks, list) else 0
        return f"{verb} {count}" if count else verb
    if verb == "更新待办":
        action = args.get("action")
        if isinstance(action, str) and action.strip():
            return f"{verb} {action.strip()}"
        return verb
    if verb in _TODO_VERBS:
        return verb

    fallback = (
        _basename(file) if file
        else f'"{_truncate(query, 40)}"' if query
        else _truncate(command, 40) if command
        else _truncate(url, 48) if url
        else skill if skill
        else ""
    )
    return f"{verb} {fallback}" if fallback else ""
