# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工具结果出口归一化 rail（T1 v3.0 §4）。

AFTER_TOOL_CALL 挂载，priority=82：位于 TaskExecutionRail(85) 之后（领域 rail
读到原始形状，行为不变）、StreamEventRail(80) 之前（前端事件读到归一形状，
``chat.tool_result`` 的 status/success/is_error 标记免费正确）、multimodal(75)
之前（图片载荷平铺顶层，提取不受影响）。

设计文档：GitCode openJiuwen/jiuwenswarm#5243（v3.0）。
归一逻辑在 :mod:`jiuwenswarm.agents.harness.common.tool_result_schema`（纯函数，
独立可测）。rail 自身只做：config 开关判定 + ctx 改写 + fail-open。

fail-open 契约：归一/渲染任何异常时保留原始 tool_result 不动、只打 warning，
绝不中断工具流（观测类改动不得弄坏请求路径）。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.tool_result_schema import (
    normalize_tool_result,
    render_tool_result_for_llm,
)

logger = logging.getLogger(__name__)

_FLAG_NORMALIZE = "tool_result_normalize_enabled"
_FLAG_LLM_RENDER = "tool_result_llm_render_enabled"

# react_agent._iter_multimodal_image_items 以 **属性访问** 读取
# ``getattr(tool_result, "data")`` 提取多模态图片（openjiuwen 不可改）；归一产物
# 必须保持属性可读，否则图片提取断链（S4 的访问形态约束，比"键存在"更强）。


class _ResultDict(dict):
    """dict + 属性访问兼容层：缺失键按 AttributeError 处理（保持 getattr 语义）。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _is_internal_protocol_result(result: Any) -> bool:
    """内部协议信封不归一：保持原对象交由各自协议消费方处理。"""
    # dict 子类（如 symphony AgenticToolResult：dict + detailed_output 属性）
    # 自带协议语义，归一成普通 dict 会丢属性——保持原样（阶段 D 单独对齐）。
    # 需要"精确普通 dict"而非 isinstance 的包含语义，故比较 __class__。
    if isinstance(result, dict) and result.__class__ is not dict:
        return True
    if isinstance(result, dict):
        # 双下划线开头键 = 内部协议标记（如 skill_turbo HITL 的
        # __skill_turbo_hitl__），由 stream_event_rail HITL 路径改写消费
        return any(isinstance(key, str) and key.startswith("__") for key in result)
    if isinstance(result, list):
        # __interaction__ 交互列表由 react_agent._is_interrupted 消费
        return any(
            getattr(item, "type", None) == "__interaction__" for item in result
        )
    try:
        from openjiuwen.core.workflow import WorkflowOutput
    except Exception:
        # 依赖版本无 WorkflowOutput 时按非协议对象处理（不静默吞异常）
        logger.debug(
            "[ToolResultNormalizeRail] WorkflowOutput unavailable; skip protocol check",
            exc_info=True,
        )
        return False
    # 工作流中断协议对象（_is_interrupted 按类型判定）
    return isinstance(result, WorkflowOutput)


def _read_config_flag(name: str, default: bool) -> bool:
    """读顶层 config 开关；任何异常回退默认值（fail-open）。"""
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config() or {}
        value = config.get(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


class ToolResultNormalizeRail(DeepAgentRail):
    """把每次工具调用结果归一为统一 schema，并把 LLM 视图渲染为 JSON/display。"""

    priority = 82

    def __init__(
        self,
        *,
        normalize_enabled: bool | None = None,
        llm_render_enabled: bool | None = None,
    ) -> None:
        super().__init__()
        # None = 启动后从 config 懒加载一次（构造参数主要供测试注入）
        self._normalize_enabled = normalize_enabled
        self._llm_render_enabled = llm_render_enabled
        self._flags_resolved = False

    def _resolve_flags(self) -> None:
        if self._flags_resolved:
            return
        if self._normalize_enabled is None:
            self._normalize_enabled = _read_config_flag(_FLAG_NORMALIZE, True)
        if self._llm_render_enabled is None:
            self._llm_render_enabled = _read_config_flag(_FLAG_LLM_RENDER, True)
        self._flags_resolved = True

    async def after_tool_call(self, ctx: Any) -> None:
        try:
            self._resolve_flags()
            if not self._normalize_enabled:
                return
            inputs = getattr(ctx, "inputs", None)
            if inputs is None or not hasattr(inputs, "tool_result"):
                return
            original = inputs.tool_result
            # None 表示"无结果可归一"（HITL 占位等路径依赖该语义），保持原样
            if original is None:
                return
            # 内部协议信封（dict 子类协议 / HITL 标记 / 交互列表 / WorkflowOutput）
            # 不归一：保持原对象，交由各自协议消费方处理
            if _is_internal_protocol_result(original):
                return
            # 未知对象（非 dict/str/list/ToolOutput/异常形态）不强行字符串化——
            # 保底跳过，避免破坏依赖对象形态的下游消费方
            if not isinstance(original, (dict, str, list)) and not hasattr(
                original, "success"
            ) and not isinstance(original, BaseException):
                return
            # 先完整计算，再一次性改写：归一抛错时不落任何部分状态（fail-open）
            normalized = _ResultDict(normalize_tool_result(original))
            content: str | None = None
            if self._llm_render_enabled:
                content = render_tool_result_for_llm(normalized)
            inputs.tool_result = normalized
            if content is not None:
                tool_msg = getattr(inputs, "tool_msg", None)
                if tool_msg is not None:
                    tool_msg.content = content
                else:
                    from openjiuwen.core.foundation.llm import ToolMessage

                    tool_call_id = str(getattr(getattr(inputs, "tool_call", None), "id", "") or "")
                    inputs.tool_msg = ToolMessage(content=content, tool_call_id=tool_call_id)
        except Exception:
            # fail-open：保留原始 tool_result，绝不中断工具流
            logger.warning(
                "[ToolResultNormalizeRail] normalize failed; keep original tool_result",
                exc_info=True,
            )


__all__ = ["ToolResultNormalizeRail"]
