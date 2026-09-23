# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""工具调用展示辅助：call_goal schema 注入与参数剥离。

优先使用主模型在 tool_call 参数里一并给出的 call_goal（一句目标说明，
如「调研 openJiuwen 官网信息」）；执行前会从 arguments 剥掉该字段以免 schema 校验失败。
可读工具标题由前端根据 name 做 i18n，后端不再生成 display_name。

注意：绝不能用 display_name 作为 UI 字段名——team 的 spawn_teammate / build_team
等工具本身就有必填的 display_name（成员展示名），撞名会导致参数被误剥、持续报错。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

_CALL_GOAL_KEYS = ("call_goal", "callGoal")

_CALL_GOAL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "一句简短说明，讲清这次工具调用要达成的目标（仅界面展示）。"
        "必须使用当前对话所用的语言，不要固定用中文。"
        "Write it in the same language as the conversation with the user; "
        "do not default to Chinese. "
        "中文对话写「调研 openJiuwen 官网信息」「创建三子棋对战团队」「通知 player-x 落子」，"
        "英文对话写 \"Research the openJiuwen website\" \"Create a tic-tac-toe team\"。"
        "不要只写工具名或裸 URL；不影响工具实际执行。"
        "与工具自带的 display_name（成员/团队展示名）以及 send_message.summary 都不是同一个字段，"
        "调用 spawn_member / spawn_teammate / send_message / build_team 时也必须填写。"
    ),
}


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
