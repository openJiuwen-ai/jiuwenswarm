# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IDManager - 生成和管理 interaction_seq、llm_call_seq、tool_call_seq 等标识字段。"""

from __future__ import annotations

import logging
from collections import OrderedDict
from threading import Lock

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)

logger = logging.getLogger(__name__)


class IDManager:
    """ID 管理模块。

    生成和管理交互标识(int 类型自增序号)及各类会话/Agent/Trace 标识。

    interaction_seq:使用 session 级 LRU 池存储(跨层共享),因为
    agent-core 中 DeepAgent 外层与 ReActAgent 内层各自创建独立的
    AgentCallbackContext 实例,ctx.extra 不跨层共享。

    llm_call_seq、tool_call_seq:继续使用 ctx.extra(内层共享),因为
    内层 ReActAgent 的 ctx.extra 在内层事件间是共享的(同一 ctx 贯穿
    ReAct 循环,tool_ctx 显式 extra=ctx.extra)。

    LRU 池防止 session 无限增长导致内存泄漏。
    后续版本将基于 session_end 事件实现精确生命周期管理。
    """

    # LRU 池最大 session 数
    _MAX_SESSIONS = 100

    def __init__(self):
        # session_id → interaction_seq 的 LRU 池
        # OrderedDict 实现 LRU:最近访问的在末尾,满了从头部淘汰
        self._session_interaction_seqs: OrderedDict[str, int] = OrderedDict()
        self._lock = Lock()

    def _get_interaction_seq_from_pool(self, session_id: str) -> int:
        """从 LRU 池获取或创建 interaction_seq(只读,更新 LRU 顺序)。"""
        key = session_id if session_id else "__no_session__"
        with self._lock:
            if key in self._session_interaction_seqs:
                # 已存在,移到末尾(LRU 最近使用)
                seq = self._session_interaction_seqs.pop(key)
                self._session_interaction_seqs[key] = seq
                return seq
            # 新 session,检查池是否已满
            if len(self._session_interaction_seqs) >= self._MAX_SESSIONS:
                # LRU 淘汰:弹出最久未使用的(头部)
                evicted_key, _ = self._session_interaction_seqs.popitem(last=False)
                logger.info("LRU 淘汰 session: %s", evicted_key)
            # 创建新 session 的 interaction_seq,初始值 -1
            self._session_interaction_seqs[key] = -1
            return -1

    def _increment_interaction_seq_in_pool(self, session_id: str) -> int:
        """在 LRU 池中自增 interaction_seq 并返回新值(原子操作)。

        单次加锁完成读-改-写,避免并发竞态。
        """
        key = session_id if session_id else "__no_session__"
        with self._lock:
            if key in self._session_interaction_seqs:
                # 已存在,移到末尾(LRU 最近使用)并自增
                seq = self._session_interaction_seqs.pop(key)
                seq += 1
                self._session_interaction_seqs[key] = seq
                return seq
            # 新 session,检查池是否已满
            if len(self._session_interaction_seqs) >= self._MAX_SESSIONS:
                evicted_key, _ = self._session_interaction_seqs.popitem(last=False)
                logger.info("LRU 淘汰 session: %s", evicted_key)
            # 创建新 session,初始值 -1,自增后为 0
            self._session_interaction_seqs[key] = 0
            return 0

    def _ensure_interaction_seq(self, ctx: AgentCallbackContext, event: AgentCallbackEvent = None) -> int:
        """获取或生成 interaction_seq(session 级,BEFORE_INVOKE 时自增)。

        使用 session 级 LRU 池存储,跨 DeepAgent/ReActAgent 层共享。
        初始值为 -1,BEFORE_INVOKE 时先自增再使用。
        后续事件复用同一序号(从 LRU 池读取当前值)。
        """
        session_id = self._resolve_session_id(ctx)
        if event is not None and event != AgentCallbackEvent.BEFORE_INVOKE:
            # 非 BEFORE_INVOKE:从 LRU 池复用当前值
            return self._get_interaction_seq_from_pool(session_id)
        # BEFORE_INVOKE 或未指定事件:原子自增
        return self._increment_interaction_seq_in_pool(session_id)

    def _ensure_llm_call_seq(self, ctx: AgentCallbackContext, event: AgentCallbackEvent) -> int:
        """获取或生成 llm_call_seq(整数自增序号)。

        初始值为 -1,BEFORE_MODEL_CALL 时先自增再使用,AFTER_MODEL_CALL 复用。
        tool_call 事件直接读取当前值。
        """
        if event == AgentCallbackEvent.BEFORE_MODEL_CALL:
            seq = ctx.extra.get("llm_call_seq", -1)
            seq += 1
            ctx.extra["llm_call_seq"] = seq
            return seq
        # AFTER_MODEL_CALL 及 tool_call 事件复用当前值
        return ctx.extra.get("llm_call_seq", -1)

    def _ensure_tool_call_seq(self, ctx: AgentCallbackContext, event: AgentCallbackEvent) -> int:
        """获取或生成 tool_call_seq(整数自增序号)。

        初始值为 -1,BEFORE_TOOL_CALL 时先自增再使用,AFTER_TOOL_CALL 复用。
        """
        if event == AgentCallbackEvent.BEFORE_TOOL_CALL:
            seq = ctx.extra.get("tool_call_seq", -1)
            seq += 1
            ctx.extra["tool_call_seq"] = seq
            return seq
        # AFTER_TOOL_CALL 复用当前值
        return ctx.extra.get("tool_call_seq", -1)

    def _resolve_subsession_id(self, ctx: AgentCallbackContext) -> str:
        """获取 subsession_id(子 Agent 场景的父会话 ID)。

        子 Agent 场景下,子 Agent 的 parent_session_id 写入 subsession_id,
        session_id 用子 Agent 自己的 session_id。非子 Agent 场景为空字符串。
        """
        try:
            return getattr(ctx.inputs, "parent_session_id", "") or ""
        except Exception:
            logger.debug("解析 subsession_id 失败, 返回空字符串", exc_info=True)
            return ""

    def _resolve_conversation_id(self, ctx: AgentCallbackContext, event: AgentCallbackEvent) -> str:
        """获取 conversation_id,优先从 inputs 取,回退到 extra 缓存。"""
        cid = getattr(ctx.inputs, "conversation_id", None)
        if cid:
            ctx.extra["conversation_id"] = cid
            return cid
        return ctx.extra.get("conversation_id", "")

    def _resolve_session_id(self, ctx: AgentCallbackContext) -> str:
        """获取 session_id,无 session 时返回空字符串。"""
        try:
            return ctx.session.get_session_id() if ctx.session else ""
        except Exception:
            logger.debug("解析 session_id 失败, 返回空字符串", exc_info=True)
            return ""

    def _resolve_agent_id(self, ctx: AgentCallbackContext) -> str:
        """获取 agent_id,无 agent 时返回空字符串。"""
        try:
            return ctx.agent.card.id if ctx.agent and ctx.agent.card else ""
        except Exception:
            logger.debug("解析 agent_id 失败, 返回空字符串", exc_info=True)
            return ""

    def _resolve_trace_id(self, ctx: AgentCallbackContext) -> str:
        """获取 trace_id,无 session 时返回空字符串。"""
        try:
            return ctx.session._inner._tracer._trace_id if ctx.session else ""
        except Exception:
            logger.debug("解析 trace_id 失败, 返回空字符串", exc_info=True)
            return ""

    def _resolve_context_id(self, ctx: AgentCallbackContext) -> str:
        """获取 context_id,无 context 时返回空字符串。"""
        try:
            return ctx.context.context_id() if ctx.context else ""
        except Exception:
            logger.debug("解析 context_id 失败, 返回空字符串", exc_info=True)
            return ""

    def _resolve_tool_name(self, ctx: AgentCallbackContext, event: AgentCallbackEvent) -> str:
        """获取 tool_name,仅 TOOL 事件有值。"""
        try:
            if event in (AgentCallbackEvent.BEFORE_TOOL_CALL,
                         AgentCallbackEvent.AFTER_TOOL_CALL):
                return getattr(ctx.inputs, "tool_name", "")
            return ""
        except Exception:
            logger.debug("解析 tool_name 失败, 返回空字符串", exc_info=True)
            return ""

    def _resolve_tool_call_id(self, tool_call) -> str:
        """获取 tool_call_id。

        基类 BaseSecurityRail 中已存在此方法(用于 _resolve_subject_id),
        此处保持一致签名与行为,供 _run_and_apply 显式调用填充 security_ctx。
        """
        try:
            return tool_call.id if tool_call else ""
        except Exception:
            logger.debug("解析 tool_call_id 失败, 返回空字符串", exc_info=True)
            return ""

    def _event_class_for(self, event: AgentCallbackEvent, event_type: str) -> str:
        """根据 event_type 判断事件类别。

        安全检测事件(如 permission_interrupt_tool)返回 "security",
        生命周期事件返回 "lifecycle"。
        """
        if event_type == "permission_interrupt_tool":
            return "security"
        return "lifecycle"
