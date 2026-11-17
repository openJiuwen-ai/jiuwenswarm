# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRepeatCallGuardRail —— 重复调用守卫（只提醒，不否决）。

dsh ``repeat-tool-reminder`` 的 flash 移植：按 ``(tool_name, 规范化参数)`` 观测
连续重复调用链，命中阈值时通过 ``prompt_attachment`` 注入一条瞬态提醒
（RUNTIME kind）。与 circuit_breaker_rail（``execution_guard``）分工：

- circuit_breaker_rail：**熔断**（critical 时 ``force_finish``），是硬闸；
- 本 rail：**只提醒不否决**，永不拦截/改写任何工具调用，也不消费熔断逻辑。

机制要点（对齐 dsh 原文）：

1. **深度键排序规范化**：两个仅属性顺序不同的参数对象规范化为同一键；
2. **阈值阶梯**：``thresholds[0]`` 命中给温和提醒，后续阈值给带工具名/次数/参数的详细提醒；
3. **include/exclude 通配**：只统计参与链路的工具，未跟踪的调用既不计数也不重置；
4. **用户插话重置**：``before_invoke``（每条新消息）清空当前会话的重复链——
   跨用户请求的重复不是循环；
5. **会话内逐次刷新**：命中阈值 upsert 提醒、链路断裂清空，附件权威性低于系统段。

附件属于瞬态（8xx 运行时区），不占 段序表号位（同 turn2_scene_pack / 水位提醒）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.rails.base import DeepAgentRail

# 会话内重复链键：一次性注入 sid 到 ctx.extra 供 after_tool_call 解析。
_SID_KEY = "__jiuwenswarm_flash_repeat_guard_sid__"

_SECTION = "repeat_call_reminder"
_SOURCE = "jiuwenswarm.flash.repeat_call_guard"
_KIND = PromptAttachmentKind.RUNTIME
_ATTACHMENT_PRIORITY = 70

_DEFAULT_THRESHOLDS = (3, 5, 8)
_DEFAULT_ARGUMENTS_PREVIEW_CHARS = 500


def _wildcard_to_regex(pattern: str) -> re.Pattern[str]:
    """把 ``*`` 通配转成锚定正则；其余正则元字符按字面匹配（dsh wildcardToRegExp）。"""
    escaped = re.escape(pattern)
    return re.compile("^" + escaped.replace(r"\*", ".*") + "$")


def _sort_json_value(value: Any) -> Any:
    """深度键排序：仅属性顺序不同的对象规范化为同一结构（dsh sortJsonValue）。"""
    if isinstance(value, list):
        return [_sort_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sort_json_value(value[key]) for key in sorted(value)}
    return value


def _canonicalize(arguments: Any) -> str:
    return json.dumps(_sort_json_value(arguments), ensure_ascii=False, default=str)


def _preview(canonical: str, cap: int) -> str:
    if len(canonical) <= cap:
        return canonical
    return f"{canonical[:cap]}… (+{len(canonical) - cap} more chars)"


def _validate_thresholds(values: tuple[int, ...] | list[int]) -> list[int]:
    if not values:
        raise ValueError("repeat-call-guard: `thresholds` 不能为空")
    for value in values:
        if not isinstance(value, int) or value < 2:
            raise ValueError(
                f"repeat-call-guard: 非法阈值 {value} —— 每个阈值必须是 >= 2 的整数"
            )
    if len(set(values)) != len(values):
        raise ValueError("repeat-call-guard: `thresholds` 不允许重复")
    return sorted(values)


def _gentle_reminder(language: str) -> str:
    if language == "cn":
        return (
            "你正在以完全相同的参数重复调用同一个工具。再次调用前，请先仔细分析上一次的"
            "结果：如果任务尚未完成，尝试换一种方法或换一套参数，而不是原样重试。"
        )
    return (
        "This tool has just been invoked again with an unchanged argument set. "
        "Read the newest output before deciding the next step: unfinished work "
        "calls for a revised plan or revised arguments, not a verbatim resend."
    )


def _detailed_reminder(language: str, tool_name: str, count: int, canonical: str) -> str:
    if language == "cn":
        return (
            "检测到重复工具调用：\n"
            f"- 工具：{tool_name}\n"
            f"- 连续次数：{count}\n"
            f"- 参数：{canonical}\n"
            "这些重复调用没有进展。请不要再以这套参数调用该工具；检查最新结果，"
            "换一种操作、换一套参数，或证据已足够时直接收尾。"
        )
    return (
        "A duplicate invocation loop has been observed.\n"
        f"- tool name: {tool_name}\n"
        f"- occurrences in the current streak: {count}\n"
        f"- argument set: {canonical}\n"
        "These repeats yield nothing new. Examine the newest output, then revise "
        "the argument set, choose another operation, or wrap up if the evidence "
        "already gathered is sufficient."
    )


class FlashRepeatCallGuardRail(DeepAgentRail):
    """Advisory repeat-call detector; enriches post-execute context, never vetoes."""

    # 观测发生在工具执行之后，无严格排序依赖；取中等优先级。
    priority = 50
    inherit_to_subagents = False

    def __init__(
        self,
        thresholds: tuple[int, ...] = _DEFAULT_THRESHOLDS,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        arguments_preview_chars: int = _DEFAULT_ARGUMENTS_PREVIEW_CHARS,
    ) -> None:
        super().__init__()
        self._thresholds = _validate_thresholds(list(thresholds))
        self._threshold_set = set(self._thresholds)
        self._include_patterns = [_wildcard_to_regex(p) for p in include]
        self._exclude_patterns = [_wildcard_to_regex(p) for p in exclude]
        if not isinstance(arguments_preview_chars, int) or arguments_preview_chars < 1:
            raise ValueError(
                f"repeat-call-guard: 非法 arguments_preview_chars {arguments_preview_chars} —— 必须 >= 1"
            )
        self._arguments_preview_chars = arguments_preview_chars
        # session_id -> (chain_key, count)
        self._chains: dict[str, tuple[str, int]] = {}
        self._attachment_manager: Any = None
        self._system_prompt_builder: Any = None

    def init(self, agent: Any) -> None:
        self._attachment_manager = getattr(agent, "prompt_attachment_manager", None)
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._attachment_manager = None
        self._system_prompt_builder = None
        self._chains.clear()

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """每条新消息重置当前会话的重复链（用户插话 = 上下文改变，非循环）。"""
        sid = getattr(getattr(ctx, "inputs", None), "conversation_id", None) or "default"
        ctx.extra[_SID_KEY] = sid
        self._chains[sid] = None  # type: ignore[assignment]
        await self._clear_reminder(ctx, sid)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """观测一次工具调用；命中阈值则 upsert 提醒，链路断裂则清空。"""
        tool_call = getattr(getattr(ctx, "inputs", None), "tool_call", None)
        if tool_call is None:
            return

        tool_name = getattr(tool_call, "name", "") or ""
        if not tool_name:
            return
        if not self._tracked(tool_name):
            return

        arguments = getattr(tool_call, "arguments", None)
        key = json.dumps([tool_name, _canonicalize(arguments)], ensure_ascii=False)

        sid = ctx.extra.get(_SID_KEY, "default")
        chain = self._chains.get(sid)
        count = chain[1] + 1 if chain is not None and chain[0] == key else 1
        self._chains[sid] = (key, count)

        if count in self._threshold_set:
            language = self._resolve_language()
            canonical = _canonicalize(arguments)
            if count == self._thresholds[0]:
                text = _gentle_reminder(language)
            else:
                text = _detailed_reminder(
                    language, tool_name, count,
                    _preview(canonical, self._arguments_preview_chars),
                )
            await self._upsert_reminder(ctx, sid, text)
        elif count == 1:
            # 链路断裂（换了工具或参数）：清空上一条提醒。
            await self._clear_reminder(ctx, sid)

    def _tracked(self, tool_name: str) -> bool:
        if self._include_patterns and not any(p.match(tool_name) for p in self._include_patterns):
            return False
        return not any(p.match(tool_name) for p in self._exclude_patterns)

    def _resolve_language(self) -> str:
        lang = getattr(self._system_prompt_builder, "language", None) or "cn"
        return "cn" if lang in ("cn", "zh") else "en"

    async def _upsert_reminder(self, ctx: AgentCallbackContext, sid: str, text: str) -> None:
        manager = self._attachment_manager
        if manager is None:
            return
        try:
            await manager.add_section(
                session_id=sid,
                section=_SECTION,
                kind=_KIND,
                content=text,
                priority=_ATTACHMENT_PRIORITY,
                source=_SOURCE,
                content_kind="text/markdown",
            )
        except Exception as exc:
            logger.warning("[FlashRepeatCallGuard] upsert reminder failed: %s", exc)

    async def _clear_reminder(self, ctx: AgentCallbackContext, sid: str) -> None:
        manager = self._attachment_manager
        if manager is None:
            return
        try:
            await manager.clear_section(session_id=sid, section=_SECTION)
        except Exception:
            logger.debug("[FlashRepeatCallGuard] clear reminder failed")


__all__ = ["FlashRepeatCallGuardRail"]
