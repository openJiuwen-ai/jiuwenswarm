# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo resume 上下文的单一 owner。

收口 save / load / clear / mark_in_flight / clear_in_flight，clear() 内置
isolated session 通道强制落盘——修复 2 处键空间错误（清不到 resume_ctx）与
落盘语义不一致（R1/R2 红线根源）。

职责边界：只收口 ``SKILL_TURBO_RESUME_CTX_KEY``，严禁吸收 ``INTERRUPTION_KEY``
（与 §2.6 的 R3 规避线耦合）。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

SKILL_TURBO_RESUME_CTX_KEY = "__skill_turbo_resume_ctx__"
SKILL_TURBO_ID_SUFFIX = "__skill_turbo"


def _get_sid(session: Any) -> str:
    """获取 session ID，兼容 session_id 属性和 get_session_id() 方法。"""
    sid = getattr(session, "session_id", None)
    if sid is None:
        getter = getattr(session, "get_session_id", None)
        if callable(getter):
            try:
                sid = getter()
            except Exception:
                sid = "?"
                logger.debug("[ResumeContextManager] get_session_id failed", exc_info=True)
        else:
            sid = "?"
    return str(sid) if sid else "?"


def _resolve_card(session: Any, card: Any = None) -> Any:
    """从参数或 session 上提取 card。"""
    if card is not None:
        return card
    return getattr(session, "card", None)


def set_skill_turbo_id(session: Any, card: Any) -> None:
    """将 session 的 agent_id 设为 '{card.id}__skill_turbo'，使 SkillTurbo 的
    checkpointer key 与 DeepAgent 隔离。

    必须在 session.pre_run() 之前调用。对无 _inner 的 stub 是 no-op。
    """
    if session is None or card is None:
        return
    card_id = getattr(card, "id", None)
    if not card_id:
        return
    inner = getattr(session, "_inner", None)
    if inner is None:
        return
    try:
        config = inner.config()
        skill_turbo_id = f"{card_id}{SKILL_TURBO_ID_SUFFIX}"
        config.set_agent_config(type("SkillTurboAgentConfig", (), {"id": skill_turbo_id})())
        logger.debug("[ResumeContextManager] set_skill_turbo_id: %s", skill_turbo_id)
    except Exception as exc:
        logger.warning("[ResumeContextManager] set_skill_turbo_id failed: %s", exc)


class ResumeContextManager:
    """resume_ctx 生命周期的单一 owner。

    用法：在需要 save/load/clear 的调用位构造实例（session + card），调对应方法。
    clear() / clear_in_flight() 内置 isolated session 强制落盘（自动带隔离键后缀），
    修复 ``interface_deep.py`` supplement / _clear_session_persisted 两处键空间错误。
    """

    def __init__(self, session: Any, *, card: Any = None) -> None:
        self._session = session
        self._card = _resolve_card(session, card)
        self._sid = _get_sid(session)

    @classmethod
    def for_isolated_clear(cls, session_id: str, card: Any) -> "ResumeContextManager":
        """构造仅用于 isolated clear 的实例（无 session 句柄，按 session_id 定位）。

        供 adapter 侧按目标 session_id 清除隔离键 resume_ctx 使用；
        card 为空时 clear() 自动降级为 no-op。
        """
        mgr = cls.__new__(cls)
        mgr._session = None
        mgr._card = card
        mgr._sid = str(session_id) if session_id else "?"
        return mgr

    async def save(
        self,
        *,
        plan_code: str,
        inputs: dict[str, Any],
        pending_tool_call_id: str,
        task_states: list[dict[str, Any]] | None = None,
    ) -> None:
        """中断时保存断点上下文（checkpointer 持久化）。

        entry 显式含 resume_in_flight=None（replace 语义，防 R2 merge 陷阱）。
        """
        if self._session is None:
            logger.warning("[ResumeContextManager] save: session is None, skipping")
            return
        entry: dict[str, Any] = {
            "plan_code": plan_code,
            "inputs": dict(inputs),
            "pending_tool_call_id": pending_tool_call_id,
            "resume_in_flight": None,
        }
        if task_states:
            entry["task_states"] = copy.deepcopy(task_states)
        try:
            await self._session.pre_run(inputs=None)
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] save pre_run failed: sid=%s err=%s", self._sid, e
            )
        try:
            self._session.update_state({SKILL_TURBO_RESUME_CTX_KEY: entry})
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] save update_state failed: sid=%s err=%s", self._sid, e
            )
            raise
        logger.info(
            "[ResumeContextManager] save: sid=%s tcid=%s plan_code_len=%d task_states=%d",
            self._sid,
            pending_tool_call_id,
            len(plan_code or ""),
            len(entry.get("task_states") or []),
        )
        try:
            await self._session.post_run()
            logger.info("[ResumeContextManager] save: persisted OK sid=%s", self._sid)
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] save post_run failed: sid=%s err=%s", self._sid, e
            )

    async def load(self) -> dict[str, Any] | None:
        """从 checkpointer 读取断点上下文。None 表示无可恢复的中断。

        返回语义与原 load_resume_ctx 逐位一致（影响 _try_skill_turbo_resume 分流）。
        """
        if self._session is None:
            logger.warning("[ResumeContextManager] load: session is None")
            return None
        try:
            await self._session.pre_run(inputs=None)
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] load pre_run failed: sid=%s err=%s", self._sid, e
            )
            return None
        try:
            state = self._session.get_state(SKILL_TURBO_RESUME_CTX_KEY)
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] load get_state failed: sid=%s err=%s", self._sid, e
            )
            return None
        if isinstance(state, dict) and state.get("plan_code"):
            logger.info(
                "[ResumeContextManager] load: found ctx sid=%s tcid=%s",
                self._sid,
                state.get("pending_tool_call_id"),
            )
            return copy.deepcopy(state)
        logger.info("[ResumeContextManager] load: no ctx found sid=%s", self._sid)
        return None

    async def clear(self) -> None:
        """清除断点上下文。

        主方案：isolated session 通道强制落盘（吸收 _clear_skill_turbo_resume_ctx_via_isolated_session）。
        自动带 {card.id}__skill_turbo 后缀，与写入同 key——修复 supplement /
        _clear_session_persisted 两处键空间错误。

        card 存在时走 isolated 通道（不依赖 _session）；card 缺失时降级为原 session 清理。
        """
        if self._card is None:
            # card 缺失时无法打开 checkpointer 通道，降级为原 session 清理
            # （键空间可能不命中，但保留原行为兜底）
            if self._session is None:
                return
            try:
                self._session.update_state({SKILL_TURBO_RESUME_CTX_KEY: None})
            except Exception:
                logger.debug(
                    "[ResumeContextManager] clear (no card) update_state failed",
                    exc_info=True,
                )
            logger.info("[ResumeContextManager] clear (no card): sid=%s", self._sid)
            return
        from openjiuwen.core.session.agent import create_agent_session

        isolated = create_agent_session(session_id=self._sid, card=self._card)
        set_skill_turbo_id(isolated, self._card)
        try:
            await isolated.pre_run(inputs=None)
            isolated.update_state({SKILL_TURBO_RESUME_CTX_KEY: None})
        finally:
            try:
                await isolated.post_run()
            except Exception:
                logger.warning(
                    "[ResumeContextManager] clear post_run failed sid=%s "
                    "(stale resume_ctx may trigger task rerun)",
                    self._sid,
                    exc_info=True,
                )
        logger.info("[ResumeContextManager] clear: cleared sid=%s", self._sid)

    async def mark_in_flight(self, resume_ctx: dict[str, Any]) -> None:
        """标记 resume_ctx 为 in-flight，防重复答案提交触发两次续跑。"""
        if self._session is None or not isinstance(resume_ctx, dict):
            return
        marked = dict(resume_ctx)
        marked["resume_in_flight"] = True
        try:
            await self._session.pre_run(inputs=None)
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] mark_in_flight pre_run failed: sid=%s err=%s",
                self._sid, e,
            )
        try:
            self._session.update_state({SKILL_TURBO_RESUME_CTX_KEY: marked})
        except Exception:
            logger.debug(
                "[ResumeContextManager] mark_in_flight update_state failed",
                exc_info=True,
            )
            return
        try:
            await self._session.post_run()
        except Exception as e:
            logger.warning(
                "[ResumeContextManager] mark_in_flight post_run failed: sid=%s err=%s",
                self._sid, e,
            )

    async def clear_in_flight(self) -> None:
        """清除 in-flight 标志（resume_ctx 仍在时）。

        使用 isolated 通道强制落盘：in_flight 残留会把重试作答判为 duplicate（S8）。
        """
        if self._session is None:
            return
        if self._card is None:
            # 无 card 时降级原 session 清理
            try:
                state = self._session.get_state(SKILL_TURBO_RESUME_CTX_KEY)
            except Exception as e:
                logger.warning(
                    "[ResumeContextManager] clear_in_flight (no card) get_state failed: err=%s", e
                )
                return
            if not isinstance(state, dict) or not state.get("resume_in_flight"):
                return
            cleaned = dict(state)
            cleaned.pop("resume_in_flight", None)
            try:
                self._session.update_state({SKILL_TURBO_RESUME_CTX_KEY: cleaned})
            except Exception:
                logger.debug(
                    "[ResumeContextManager] clear_in_flight (no card) update_state failed",
                    exc_info=True,
                )
            return
        from openjiuwen.core.session.agent import create_agent_session

        isolated = create_agent_session(session_id=self._sid, card=self._card)
        set_skill_turbo_id(isolated, self._card)
        try:
            await isolated.pre_run(inputs=None)
            state = isolated.get_state(SKILL_TURBO_RESUME_CTX_KEY)
            if isinstance(state, dict) and state.get("resume_in_flight"):
                cleaned = dict(state)
                cleaned.pop("resume_in_flight", None)
                isolated.update_state({SKILL_TURBO_RESUME_CTX_KEY: cleaned})
        finally:
            try:
                await isolated.post_run()
            except Exception:
                logger.warning(
                    "[ResumeContextManager] clear_in_flight post_run failed sid=%s",
                    self._sid,
                    exc_info=True,
                )


# ── 薄委托：保留原函数签名供逐步迁移调用方 ──
# permission_bridge 的旧函数改为调 ResumeContextManager，守护测试 mock 的
# adapter 方法名（_clear_skill_turbo_resume_ctx_via_isolated_session 等）不受影响。

__all__ = [
    "ResumeContextManager",
    "SKILL_TURBO_RESUME_CTX_KEY",
    "set_skill_turbo_id",
]
