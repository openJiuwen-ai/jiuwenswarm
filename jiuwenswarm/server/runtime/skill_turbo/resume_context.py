# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo resume 上下文的单一 owner。

收口 save / load / clear / mark_in_flight / clear_in_flight，clear() 内置
isolated session 通道强制落盘——修复 2 处键空间错误（清不到 resume_ctx）与
落盘语义不一致（R1/R2 红线根源）。

职责边界：只收口 ``SKILL_TURBO_RESUME_CTX_KEY``，严禁吸收 ``INTERRUPTION_KEY``
（与 §2.6 的 R3 规避线耦合）。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import weakref
from contextlib import asynccontextmanager
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

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
    """从参数或 session 上提取 card。

    openjiuwen Session 只把 card 存在私有 ``_card`` 上（无公开属性），
    需同时回退读 ``_card``，否则仅传 session 的调用 card 恒为 None、
    isolated 通道无法生效（键空间修复失效）。
    """
    if card is not None:
        return card
    if session is None:
        return None
    return getattr(session, "card", None) or getattr(session, "_card", None)


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


class SkillStateError(RuntimeError):
    """隔离状态的身份、读写或提交确认失败。"""


_state_write_locks: dict[tuple[str, str], asyncio.Lock] = {}
_state_callers: dict[tuple[str, str], weakref.WeakSet] = {}
_state_keys: dict[tuple[str, str], set[str]] = {}


def skill_state_identity(session: Any) -> tuple[str, str]:
    card_id = getattr(_resolve_card(session), "id", None)
    sid = _get_sid(session)
    if not card_id or sid == "?":
        raise SkillStateError("skill state requires card and session identity")
    return str(card_id), sid


def _new_isolated_session(session: Any) -> Any:
    from openjiuwen.core.session.agent import create_agent_session

    card_id, sid = skill_state_identity(session)
    card = _resolve_card(session)
    isolated = create_agent_session(session_id=sid, card=card)
    set_skill_turbo_id(isolated, card)
    if isolated._inner.agent_id() != f"{card_id}{SKILL_TURBO_ID_SUFFIX}":
        raise SkillStateError("skill state isolation could not be established")
    return isolated


class IsolatedSkillState:
    """一次隔离读改写；每个替换先删除旧键，提交后独立重读确认。"""

    def __init__(self, caller: Any, session: Any, *, write: bool):
        self.caller = caller
        self.session = session
        self.write = write
        self.identity = skill_state_identity(caller)
        self.replacements: dict[str, Any] = {}
        self.committed = False

    def get(self, key: str) -> Any:
        _state_keys.setdefault(self.identity, set()).add(key)
        return copy.deepcopy(self.session.get_state(key))

    def replace(self, key: str, value: Any) -> None:
        if not self.write or self.committed:
            raise SkillStateError("state is read-only or already committed")
        _state_keys.setdefault(self.identity, set()).add(key)
        self.replacements[key] = copy.deepcopy(value)
        self.session.update_state({key: None})
        if value is not None:
            self.session.update_state({key: copy.deepcopy(value)})

    async def commit(self) -> None:
        if not self.replacements or self.committed:
            return
        try:
            await self.session.post_run()
            current = await self._read_current()
            if any(current[key] != value for key, value in self.replacements.items()):
                raise SkillStateError("skill state commit was not confirmed")
        except AbortError:
            raise
        except Exception:
            # ACK丢失也不能让调用方finally把实际已写入的身份/失效状态覆盖回旧值。
            try:
                self._sync_callers(await self._read_current())
            except AbortError:
                raise
            except Exception:
                logger.warning("[SkillState] failed commit could not refresh caller snapshots", exc_info=True)
            raise
        self.committed = True
        self._sync_callers(current)

    async def _read_current(self) -> dict:
        confirmed = _new_isolated_session(self.caller)
        try:
            await confirmed.pre_run(inputs=None)
            return {key: copy.deepcopy(confirmed.get_state(key)) for key in _state_keys[self.identity]}
        finally:
            await confirmed.close_stream()

    def _sync_callers(self, current: dict) -> None:
        # 只同步已参与本通道且本身位于turbo键空间的Session，不污染主ReAct。
        for caller in _state_callers.get(self.identity, ()):
            inner = getattr(caller, "_inner", None)
            if inner is None or inner.agent_id() != self.session._inner.agent_id():
                continue
            for key, value in current.items():
                caller.update_state({key: None})
                if value is not None:
                    caller.update_state({key: copy.deepcopy(value)})


@asynccontextmanager
async def isolated_skill_state(session: Any, *, write: bool = False):
    """台账/新产物桶专用通道；读只关闭流，写单次提交，异常不提交。"""
    identity = skill_state_identity(session)
    _state_callers.setdefault(identity, weakref.WeakSet()).add(session)
    lock = _state_write_locks.setdefault(identity, asyncio.Lock())
    if write:
        await lock.acquire()
    isolated = None
    try:
        isolated = _new_isolated_session(session)
        await isolated.pre_run(inputs=None)
        state = IsolatedSkillState(session, isolated, write=write)
        yield state
        if write:
            await state.commit()
    finally:
        try:
            if isolated is not None:
                await isolated.close_stream()
        finally:
            if write:
                lock.release()


class ResumeContextManager:
    """resume_ctx 生命周期的单一 owner。

    用法：在需要 save/load/clear 的调用位构造实例（session + card），调对应方法。
    clear() / clear_in_flight() 内置 isolated session 强制落盘（自动带隔离键后缀），
    修复 ``interface_deep.py`` supplement / _clear_session_persisted 两处键空间错误。
    """

    def __init__(
        self,
        session: Any = None,
        *,
        card: Any = None,
        session_id: str = "",
    ) -> None:
        self._session = session
        self._card = _resolve_card(session, card) if card is None else card
        self._sid = (session_id or _get_sid(session)) if (session_id or session) else "?"

    @classmethod
    def for_isolated_clear(cls, session_id: str, card: Any) -> "ResumeContextManager":
        """构造仅用于 isolated clear 的实例（无 session 句柄，按 session_id 定位）。

        供 adapter 侧按目标 session_id 清除隔离键 resume_ctx 使用；
        card 为空时 clear() 自动降级为 no-op。
        """
        return cls(session=None, card=card, session_id=session_id)

    async def save(
        self,
        *,
        plan_code: str,
        inputs: dict[str, Any],
        pending_tool_call_id: str,
        task_states: list[dict[str, Any]] | None = None,
        outer_task_id: str | None = None,
        segment: dict[str, Any] | None = None,
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
            "segment": copy.deepcopy(segment),
        }
        if task_states or segment is not None:
            entry["task_states"] = copy.deepcopy(task_states or [])
        if outer_task_id:
            entry["outer_task_id"] = str(outer_task_id).removeprefix("todo:")
        if segment is not None:
            async with isolated_skill_state(self._session, write=True) as state:
                state.replace(SKILL_TURBO_RESUME_CTX_KEY, entry)
            return
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

        返回语义与原 load_resume_ctx 逐位一致（None 时工具体 resume 分支
        退化 fresh run_stream——消费方见 skill_turbo_tools.py）。
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
            if isinstance(state, dict) and isinstance(state.get('segment'), dict):
                # SDK Session 的重复 pre_run 不保证刷新；段控制状态必须读独立快照。
                async with isolated_skill_state(self._session) as current:
                    state = current.get(SKILL_TURBO_RESUME_CTX_KEY)
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

        card 缺失时无法打开 checkpointer 隔离通道：降级为原 session 写 None
        并 warning 告警——真实 Session 双键空间下该写入清不到隔离键（残留
        会触发重跑），但单键空间测试桩/降级环境仍可清到，且优于完全跳过。
        调用方应显式传 card（adapter 侧取 self._instance.card）。
        """
        if self._card is None:
            logger.warning(
                "[ResumeContextManager] clear degraded (no card): writing to "
                "session-default key sid=%s; isolated channel unavailable, "
                "stale resume_ctx may trigger task rerun in dual-key setups",
                self._sid,
            )
            if self._session is None:
                return
            try:
                self._session.update_state({SKILL_TURBO_RESUME_CTX_KEY: None})
            except Exception:
                logger.debug(
                    "[ResumeContextManager] clear (no card) update_state failed",
                    exc_info=True,
                )
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
            # 同步清调用方 session 的内存快照：checkpointer 的 post_run 是
            # 全量状态覆盖（非增量合并），若调用方随后 post_run 本 session，
            # 含旧条目的内存快照会把磁盘上刚清掉的 resume_ctx 写回复活
            # （R1「resume 跑通后重放已完成任务」回归）。
            if self._session is not None:
                try:
                    self._session.update_state({SKILL_TURBO_RESUME_CTX_KEY: None})
                except Exception:
                    logger.debug(
                        "[ResumeContextManager] clear in-memory sync failed",
                        exc_info=True,
                    )
        logger.info("[ResumeContextManager] clear: cleared sid=%s", self._sid)

    async def mark_in_flight(self, resume_ctx: dict[str, Any]) -> None:
        """标记 resume_ctx 为 in-flight，防重复答案提交触发两次续跑。

        使用 isolated 通道强制落盘（与 clear_in_flight 对齐）：避免 post_run
        主 session 把中断过程中的整份状态写盘。
        """
        if self._session is None or not isinstance(resume_ctx, dict):
            return
        marked = dict(resume_ctx)
        marked["resume_in_flight"] = True
        if isinstance(resume_ctx.get('segment'), dict):
            async with isolated_skill_state(self._session, write=True) as state:
                if state.get(SKILL_TURBO_RESUME_CTX_KEY) != resume_ctx:
                    raise SkillStateError('checkpoint_changed_before_resume')
                state.replace(SKILL_TURBO_RESUME_CTX_KEY, marked)
            return
        if self._card is None:
            # 无 card 时降级原 session 落盘（无 isolated 键空间，仍须 post_run 持久化）
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
                    "[ResumeContextManager] mark_in_flight (no card) update_state failed",
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
            return
        from openjiuwen.core.session.agent import create_agent_session

        isolated = create_agent_session(session_id=self._sid, card=self._card)
        set_skill_turbo_id(isolated, self._card)
        try:
            await isolated.pre_run(inputs=None)
            isolated.update_state({SKILL_TURBO_RESUME_CTX_KEY: marked})
        finally:
            try:
                await isolated.post_run()
            except Exception as e:
                logger.warning(
                    "[ResumeContextManager] mark_in_flight post_run failed sid=%s err=%s",
                    self._sid, e,
                )

    async def clear_in_flight(self) -> None:
        """清除 in-flight 标志（resume_ctx 仍在时）。

        使用 isolated 通道强制落盘：in_flight 残留会把重试作答判为 duplicate（S8）。
        """
        if self._session is None:
            return
        if self._card is not None:
            async with isolated_skill_state(self._session) as state:
                current = state.get(SKILL_TURBO_RESUME_CTX_KEY)
            if isinstance(current, dict) and isinstance(current.get('segment'), dict):
                async with isolated_skill_state(self._session, write=True) as state:
                    current = state.get(SKILL_TURBO_RESUME_CTX_KEY)
                    if isinstance(current, dict) and current.get('resume_in_flight'):
                        current.pop('resume_in_flight', None)
                        state.replace(SKILL_TURBO_RESUME_CTX_KEY, current)
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
            cleaned["resume_in_flight"] = None
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
                cleaned["resume_in_flight"] = None
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


    async def clear_matching(self, expected: dict) -> None:
        """只清理已核实归属的原断点，避免并发拒绝误清正在恢复的新状态。"""
        async with isolated_skill_state(self._session, write=True) as state:
            if state.get(SKILL_TURBO_RESUME_CTX_KEY) == expected:
                state.replace(SKILL_TURBO_RESUME_CTX_KEY, None)


# ── 薄委托：保留原函数签名供逐步迁移调用方 ──
# permission_bridge 的旧函数改为调 ResumeContextManager，守护测试 mock 的
# adapter 方法名（_clear_skill_turbo_resume_ctx_via_isolated_session 等）不受影响。

__all__ = [
    "ResumeContextManager",
    "SKILL_TURBO_RESUME_CTX_KEY",
    "set_skill_turbo_id",
]
