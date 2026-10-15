# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Track the active skill per session for credential injection."""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import portalocker
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_source,
)

logger = logging.getLogger(__name__)

_DEFAULT_SESSION_ID = "default"
# Shared with StreamEventRail so tool callbacks can recover the real session id
# when ToolCallInputs has no conversation_id (contextvars alone are not enough
# across gather / nested callbacks).
_SESSION_ID_EXTRA_KEY = "__jiuwenswarm_session_id__"
# chat.send 的 source（permission/confirm/ask_user 等 HITL 恢复来源）。
# 由 interface._build_inputs 写入 run_context.extra，before_invoke 读取，
# 用于 CR-3a：HITL 恢复轮不计入过期计数。
_CHAT_SEND_SOURCE_EXTRA_KEY = "__jiuwenswarm_chat_send_source__"

_current_session_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "skill_active_session_id",
    default=None,
)


@dataclass
class _SkillSessionState:
    active_skill: Optional[str] = None
    # Original session identity retained in the serialized lifecycle record.
    source_session: Optional[str] = None
    # 连续「无该技能 skill_tool 调用」的 invoke 计数（过期兜底，改动 3）：
    # 每次 before_invoke 递增，skill_tool 激活时清零；达到上限自动清空。
    invokes_since_skill_tool: int = 0

    def reset(self) -> None:
        self.active_skill = None
        self.source_session = None
        self.invokes_since_skill_tool = 0


def _shared_store(session_id: str):
    if not session_id or session_id == _DEFAULT_SESSION_ID:
        return None
    from openjiuwen.harness.security.skill_authorization.grant_store import (
        peek_skill_grant_store,
    )

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
        FileSkillGrantStore,
    )

    store = peek_skill_grant_store()
    # Standalone rail use also persists files; never keep an active-skill cache.
    return store if isinstance(store, FileSkillGrantStore) else FileSkillGrantStore()


def _update_state(
    session_id: str, update: Callable[[_SkillSessionState], None]
) -> None:
    store = _shared_store(session_id)
    if store is None:
        logger.warning(
            "[SkillActiveStateRail] refusing state without a real session id"
        )
        return

    def mutate(raw: dict) -> dict:
        state = _SkillSessionState(**raw)
        update(state)
        return asdict(state)

    try:
        # Missing sessions contain no active state. Never create their parent
        # directory: a late callback must not resurrect a deleted session.
        if not store.session_directory(session_id).is_dir():
            logger.debug("[SkillActiveStateRail] session unavailable: %s", session_id)
            return
        store.update_active_skill_state(session_id, mutate)
    except (
        OSError, ValueError, TypeError, RuntimeError,
        portalocker.exceptions.LockException,
    ) as exc:
        logger.warning(
            "[SkillActiveStateRail] state update failed session=%s: %s",
            session_id, exc,
        )
        # An ordinary exception is swallowed by the callback framework. Abort
        # this turn so failed expiry/switch/completion cannot use the old state.
        raise AbortError("Skill state storage unavailable; retry the request") from exc


def clear_session_skill_state(session_id: str, *, local_only: bool = False) -> None:
    """Clear session files on explicit deactivation; adapter eviction has no state."""
    if local_only:
        return
    store = _shared_store(session_id)
    if store is not None and store.session_directory(session_id).is_dir():
        store.update_active_skill_state(session_id, lambda _state: None)


def get_session_active_skill(session_id: str) -> str | None:
    store = _shared_store(session_id)
    if store is None:
        return None
    state = store.get_active_skill_state(session_id)
    return state.get("active_skill") if state else None


def adopt_default_active_skill(session_id: str) -> str | None:
    """Compatibility lookup; credentials without a real session are never adopted."""
    return get_session_active_skill(session_id)


def _nonempty_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_interrupt_resume_invoke(ctx: AgentCallbackContext) -> bool:
    """本轮 invoke 是否为 permission/confirm/ask_user 等 HITL 恢复轮。

    source 由 ``interface._build_inputs`` 从 chat.send 参数透传到
    ``run_context.extra``（CR-3a）；恢复轮是同一任务的继续而非技能闲置，
    不计入过期计数。evolution 等其他 source 不在此列。
    """
    inputs = getattr(ctx, "inputs", None)
    run_context = getattr(inputs, "run_context", None)
    extra = getattr(run_context, "extra", None)
    if not isinstance(extra, dict):
        return False
    source = _nonempty_str(
        extra.get(_CHAT_SEND_SOURCE_EXTRA_KEY) or extra.get("chat_send_source")
    )
    return is_interrupt_resume_source(source)


def resolve_stale_invoke_limit(config: Any) -> Optional[int]:
    """从 agent 配置读取过期兜底轮数（CR-3b）。

    读取 ``skill_stale_invoke_limit``，兼容两种 config 形态（N2）：
    完整 agent 配置（``config["react"][key]``）与 react 子 dict
    （``config[key]``，reload 路径传入的形态）。缺失/类型无效返回
    None（用 rail 默认值 5）；``<= 0`` 原样透传（显式禁用兜底）。
    装配点（interface_deep._build_skill_active_state_rail）消费。
    """
    if not isinstance(config, dict):
        return None
    sections = [config]
    react = config.get("react")
    if isinstance(react, dict):
        sections.insert(0, react)
    for section in sections:
        if "skill_stale_invoke_limit" not in section:
            continue
        raw = section.get("skill_stale_invoke_limit")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[SkillActiveStateRail] invalid skill_stale_invoke_limit=%r, "
                "fallback to default",
                raw,
            )
            return None
    return None


def _extract_session_id(ctx: AgentCallbackContext) -> Optional[str]:
    inputs = getattr(ctx, "inputs", None)
    if inputs is not None:
        conv_id = _nonempty_str(getattr(inputs, "conversation_id", None))
        if conv_id:
            return conv_id
        if isinstance(inputs, dict):
            conv_id = _nonempty_str(inputs.get("conversation_id"))
            if conv_id:
                return conv_id

    session = getattr(ctx, "session", None)
    if session is not None:
        getter = getattr(session, "get_session_id", None)
        if callable(getter):
            conv_id = _nonempty_str(getter())
            if conv_id:
                return conv_id
        conv_id = _nonempty_str(getattr(session, "session_id", None))
        if conv_id:
            return conv_id

    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        conv_id = _nonempty_str(extra.get(_SESSION_ID_EXTRA_KEY))
        if conv_id and conv_id != _DEFAULT_SESSION_ID:
            return conv_id

    return None


def resolve_skill_session_id(
    ctx: AgentCallbackContext,
    preset: Optional[str] = None,
) -> str:
    return (
        _nonempty_str(preset)
        or _extract_session_id(ctx)
        or _nonempty_str(_current_session_var.get())
        or _DEFAULT_SESSION_ID
    )


def _str_content(msg: Any) -> str:
    content = getattr(msg, "content", "")
    return content if isinstance(content, str) else str(content)


def _get_arg(tool_call: Any, name: str, default: str = "") -> str:
    arguments = getattr(tool_call, "arguments", None)
    if isinstance(arguments, dict):
        value = arguments.get(name, default)
        return value if isinstance(value, str) else str(value or default)
    if isinstance(arguments, str):
        try:
            import json

            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return default
        if isinstance(parsed, dict):
            value = parsed.get(name, default)
            return value if isinstance(value, str) else str(value or default)
    return default


class SkillActiveStateRail(DeepAgentRail):
    """Track which skill is active after skill_tool loads SKILL.md.

    Active skill is per-session lifecycle state — it is NOT cleared per
    invoke: it survives new user tasks and HITL interrupt continuations
    (permission / confirm / ask_user / …). It is cleared only when the skill
    completes (``skill_complete``), when another skill is activated (switch),
    when the session is permanently deleted, or when it expires: after
    ``stale_invoke_limit`` consecutive invokes without any ``skill_tool``
    call for it (skill_complete compliance is unreliable, so the expiry is
    the safety net that keeps a forgotten skill from holding credentials
    for a whole session).
    Adapter eviction and process restart leave the session files intact.
    """

    priority = 25
    # 过期兜底默认值：连续 5 轮 invoke 无 skill_tool 调用 → 自动清空。
    DEFAULT_STALE_INVOKE_LIMIT = 5

    def __init__(
        self,
        session_id: Optional[str] = None,
        stale_invoke_limit: Optional[int] = None,
        sessions_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._preset_session_id = _nonempty_str(session_id)
        from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
            FileSkillGrantStore,
        )

        self._sessions_root = sessions_root or FileSkillGrantStore().sessions_root()
        # None → 默认值；<= 0 → 显式禁用过期兜底。
        if stale_invoke_limit is None:
            stale_invoke_limit = self.DEFAULT_STALE_INVOKE_LIMIT
        self._stale_invoke_limit = int(stale_invoke_limit)

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

    def set_sessions_root(self, sessions_root: Path) -> None:
        """Capture the trusted request path before supervisor callbacks run."""
        self._sessions_root = sessions_root

    @property
    def stale_invoke_limit(self) -> int:
        """当前过期兜底轮数（公开只读，供装配点检测配置变化，N2）。"""
        return self._stale_invoke_limit

    def update_stale_invoke_limit(self, stale_invoke_limit: Optional[int]) -> None:
        """原位更新过期兜底轮数（reload 路径，N2）。

        遵循 configure() 的原位更新约定（避免重建实例脱离 agent rail 链）：
        None → 默认值；<= 0 → 禁用。计数存于会话文件，更新无副作用。
        """
        if stale_invoke_limit is None:
            stale_invoke_limit = self.DEFAULT_STALE_INVOKE_LIMIT
        self._stale_invoke_limit = int(stale_invoke_limit)

    def _resolve_session_id(self, ctx: AgentCallbackContext) -> str:
        return resolve_skill_session_id(ctx, self._preset_session_id)

    def _bind_session_id(self, ctx: AgentCallbackContext, session_id: str) -> None:
        _current_session_var.set(session_id)
        from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
            bind_session_file_context,
        )

        bind_session_file_context(session_id, self._sessions_root)
        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, dict):
            return
        # Prefer a real id already written by StreamEventRail; never overwrite
        # it with the "default" sentinel.
        existing = _nonempty_str(extra.get(_SESSION_ID_EXTRA_KEY))
        if (
            session_id == _DEFAULT_SESSION_ID
            and existing
            and existing != _DEFAULT_SESSION_ID
        ):
            return
        if (
            not existing
            or existing == _DEFAULT_SESSION_ID
            or session_id != _DEFAULT_SESSION_ID
        ):
            extra[_SESSION_ID_EXTRA_KEY] = session_id

    def _expire_stale_active_skill(
        self, session_id: str, *, is_resume_invoke: bool = False
    ) -> None:
        """过期兜底（改动 3）：连续 N 轮 invoke 无 skill_tool 调用 → 清空。

        skill_complete 遵从性不可靠（实测用户显式要求结束后模型仍不调
        用），该兜底防技能状态挂满整场会话、凭据注入窗口无限延长。
        ``stale_invoke_limit <= 0`` 时禁用。计数语义：每次 before_invoke
        递增（本轮"尚未"调用 skill_tool），skill_tool 激活清零——连续
        N 轮无调用后，第 N+1 轮开始时清空。

        HITL 恢复轮（permission/confirm/ask_user resume，CR-3a）不递增：
        连续审批/ask_user 交互是同一任务的继续而非技能闲置，若计入会
        在密集 HITL 场景误清空 active_skill。
        """
        if self._stale_invoke_limit is None or self._stale_invoke_limit <= 0:
            return
        if is_resume_invoke:
            return

        def expire(state: _SkillSessionState) -> None:
            if not state.active_skill:
                return
            if state.invokes_since_skill_tool >= self._stale_invoke_limit:
                logger.info(
                    "[SkillActiveStateRail] expired active '%s' after %d invokes session=%s",
                    state.active_skill,
                    state.invokes_since_skill_tool,
                    session_id,
                )
                state.reset()
                return
            state.invokes_since_skill_tool += 1

        _update_state(session_id, expire)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        session_id = self._resolve_session_id(ctx)
        # 生命周期修正：before_invoke 不清空 active_skill（新用户任务不清）。
        # 清空只发生在 skill_complete、切换到另一个 skill（after_tool_call
        # 覆盖替换）、会话结束（adapter 淘汰 / teardown 调
        # clear_session_skill_state）、或连续 N 轮无 skill_tool 调用的过期
        # 兜底（HITL 恢复轮不计入，CR-3a）。
        # 授权和活跃技能仅存于真实会话文件，不收养 default 孤儿状态。
        self._bind_session_id(ctx, session_id)
        self._expire_stale_active_skill(
            session_id,
            is_resume_invoke=_is_interrupt_resume_invoke(ctx),
        )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        # Do not drop here: active skill is per-session lifecycle state, not
        # per-invoke state. Cleanup happens on skill_complete, on switching to
        # another skill, or on session teardown (clear_session_skill_state).
        from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
            clear_session_file_context,
        )

        clear_session_file_context()

    def _owner_hint_for_default(self, ctx: AgentCallbackContext) -> Optional[str]:
        """Best-effort real session id when the storage key falls back to default."""
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            better = _nonempty_str(extra.get(_SESSION_ID_EXTRA_KEY))
            if better and better != _DEFAULT_SESSION_ID:
                return better
        if self._preset_session_id and self._preset_session_id != _DEFAULT_SESSION_ID:
            return self._preset_session_id
        cv = _nonempty_str(_current_session_var.get())
        if cv and cv != _DEFAULT_SESSION_ID:
            return cv
        return None

    def _prefer_real_session_id(
        self, ctx: AgentCallbackContext, session_id: str
    ) -> str:
        """Avoid recording active skill under the ``default`` sentinel when possible."""
        if session_id != _DEFAULT_SESSION_ID:
            return session_id
        return self._owner_hint_for_default(ctx) or session_id

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return
        tool_msg = getattr(inputs, "tool_msg", None)
        tool_call = getattr(inputs, "tool_call", None)
        if tool_msg is None or tool_call is None:
            return
        tool_name = getattr(inputs, "tool_name", "") or ""
        session_id = self._prefer_real_session_id(ctx, self._resolve_session_id(ctx))
        self._bind_session_id(ctx, session_id)
        if tool_name not in {"skill_tool", "skill_complete"}:
            return

        def apply(state: _SkillSessionState) -> None:

            if tool_name == "skill_complete":
                skill_name = (_get_arg(tool_call, "skill_name", "") or "").strip()
                if skill_name and state.active_skill == skill_name:
                    logger.info(
                        "[SkillActiveStateRail] skill_complete(%s) session=%s -> idle",
                        skill_name,
                        session_id,
                    )
                    state.reset()
                elif (
                    skill_name
                    and state.active_skill
                    and state.active_skill != skill_name
                ):
                    # 激活名 ≠ 完成名：不清空（保持当前激活态），但留痕，
                    # 防"状态挂死且无从排查"。
                    logger.warning(
                        "[SkillActiveStateRail] skill_complete(%s) ignored: active is "
                        "'%s' session=%s (complete only clears on name match)",
                        skill_name,
                        state.active_skill,
                        session_id,
                    )
                return

            if tool_name != "skill_tool":
                return

            meta = getattr(tool_msg, "metadata", None) or {}
            if meta.get("is_directory_listing"):
                return
            skill_name = (
                meta.get("skill_name") or _get_arg(tool_call, "skill_name", "") or ""
            ).strip()
            if not skill_name:
                return
            prior_skill = state.active_skill
            if prior_skill and prior_skill != skill_name:
                # 切换到另一个 skill：旧 active_skill 在此清空（被新技能替换）。
                logger.info(
                    "[SkillActiveStateRail] switch active '%s' -> '%s' session=%s",
                    prior_skill,
                    skill_name,
                    session_id,
                )
            state.active_skill = skill_name
            # 本轮有 skill_tool 调用，过期计数清零。
            state.invokes_since_skill_tool = 0
            if session_id == _DEFAULT_SESSION_ID:
                # Attribution for later adopt; never claim a foreign session.
                state.source_session = self._owner_hint_for_default(ctx)
            else:
                state.source_session = None
            logger.info(
                "[SkillActiveStateRail] activated '%s' session=%s source_session=%s",
                skill_name,
                session_id,
                state.source_session or "-",
            )

        _update_state(session_id, apply)


__all__ = [
    "SkillActiveStateRail",
    "_CHAT_SEND_SOURCE_EXTRA_KEY",
    "_DEFAULT_SESSION_ID",
    "_SESSION_ID_EXTRA_KEY",
    "_current_session_var",
    "adopt_default_active_skill",
    "clear_session_skill_state",
    "get_session_active_skill",
    "resolve_skill_session_id",
    "resolve_stale_invoke_limit",
]
