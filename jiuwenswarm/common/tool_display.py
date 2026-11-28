# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""工具调用展示辅助：call_goal schema 注入与参数剥离。

优先使用主模型在 tool_call 参数里一并给出的 call_goal（一句目标说明，
如「调研 openJiuwen 官网信息」）；执行前会从 arguments 剥掉该字段以免 schema 校验失败。
可读工具标题由前端根据 name 做 i18n，后端不再生成 display_name。

call_goal 是界面副标题，语言必须跟随当前 UI / preferred_language（zh|en），
而不是对话正文或 skill 文档的语言。

注意：绝不能用 display_name 作为 UI 字段名——team 的 spawn_teammate / build_team
等工具本身就有必填的 display_name（成员展示名），撞名会导致参数被误剥、持续报错。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

_CALL_GOAL_KEYS = ("call_goal", "callGoal")

# 用于识别「我们注入的」call_goal，便于按 UI 语言刷新 description，不覆盖工具自带定义。
_OUR_CALL_GOAL_MARKERS = (
    "仅界面展示",
    "UI display only",
    "不影响工具实际执行",
    "does not affect tool execution",
)

_CALL_GOAL_DESCRIPTION_ZH = (
    "每次调用都必须填写。一句简短说明，讲清这次工具调用要达成的目标（仅界面展示）。"
    "请用中文写，以匹配当前界面语言；即使上下文或技能文档是其他语言，也请用中文填写本字段，不要留空。"
    "示例：「调研 openJiuwen 官网信息」「查找 mcp_exec_command 工具」「定位 search_history.py 脚本」。"
    "不要只写工具名或裸 URL；不影响工具实际执行。"
    "与工具自带的 display_name（成员/团队展示名）以及 send_message.summary 都不是同一个字段，"
    "调用 spawn_member / spawn_teammate / send_message / build_team 时也必须填写。"
)

_CALL_GOAL_DESCRIPTION_EN = (
    "Always fill this field. One short sentence describing the goal of this tool call "
    "(UI display only). "
    "Write it in English to match the current UI language; even if the conversation "
    "context or skill documents are in another language, still fill this field in "
    "English rather than leaving it empty. "
    'Examples: "Research the openJiuwen website" "Find the mcp_exec_command tool" '
    '"Locate the search_history.py script". '
    "Do not write only a tool name or a bare URL; does not affect tool execution. "
    "This is not the same field as the tool's own display_name (member/team label) "
    "or send_message.summary; still required when calling spawn_member / "
    "spawn_teammate / send_message / build_team."
)


def call_goal_schema_for_language(language: object | None = None) -> dict[str, Any]:
    """Build the optional call_goal JSON Schema property for the given UI language."""
    from jiuwenswarm.common.i18n import normalize_preferred_language

    lang = normalize_preferred_language(language)
    description = (
        _CALL_GOAL_DESCRIPTION_ZH if lang == "zh" else _CALL_GOAL_DESCRIPTION_EN
    )
    return {"type": "string", "description": description}


def is_our_call_goal_schema(existing: object) -> bool:
    """True when ``existing`` looks like a call_goal schema we injected."""
    if not isinstance(existing, dict):
        return False
    desc = str(existing.get("description") or "")
    return any(marker in desc for marker in _OUR_CALL_GOAL_MARKERS)


def _truncate(value: str, max_len: int) -> str:
    one_line = " ".join(value.split())
    return one_line[:max_len] + "…" if len(one_line) > max_len else one_line


def inject_call_goal_schema(
    parameters: Any, language: object | None = None
) -> None:
    """给工具 JSON Schema 注入可选 call_goal，供主模型随 tool_call 一并产出。

    ``language`` 为当前 UI / preferred language（``zh`` / ``en`` 及别名）。
    未传时默认 ``en``。若已有我们注入的 call_goal，会按语言刷新 description；
    工具自带的 call_goal 定义不覆盖。永远不要碰 display_name。
    """
    if not isinstance(parameters, dict):
        return
    props = parameters.get("properties")
    if not isinstance(props, dict):
        props = {}
        parameters["properties"] = props
    schema = call_goal_schema_for_language(language)
    existing_call_goal = props.get("call_goal")
    if not isinstance(existing_call_goal, dict) or is_our_call_goal_schema(existing_call_goal):
        props["call_goal"] = schema
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
