# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillComplianceRail — event-driven SKILL execution state machine.

Replaces the previous polling design. State changes happen on five observable
tool-call events; non-skill_step tools touch nothing.

Phases (per session):
    IDLE          — no SKILL active
    WAITING_PLAN  — SKILL.md loaded, no skill_step plan yet
    IN_PROGRESS   — plan exists, executing
    DONE          — all skill_step items complete; awaiting skill_complete

Transitions are driven by:
    skill_tool body load       -> IDLE -> WAITING_PLAN | IN_PROGRESS | DONE
                                  (one disk read here to detect existing plan)
    TodoOpResult.kind=CREATE   -> WAITING_PLAN -> IN_PROGRESS
    TodoOpResult.kind=COMPLETE -> IN_PROGRESS -> DONE  (when all_completed)
    TodoOpResult.kind=REMOVE   -> IN_PROGRESS -> WAITING_PLAN  (when total_count==0)
    skill_complete             -> any -> IDLE  (also clears skill_step.md)

Phase observation is published via ``get_session_phase`` /
``get_session_active_skill`` (consumed by SkillProtocolPromptRail).
Step-skip detection (model-text scan) and mcp_exec_command failure recovery
are orthogonal and remain in this rail.

after_tool_call: 当 ``skill_tool`` 成功返回后激活。
上游 ``openjiuwen.SkillUseRail`` 会先 (a) 把全文写入 session
``active_skill_bodies`` state，(b) 把 ``tool_msg.content`` 替换成
``[SKILL LOADED]`` stub 并把 ``metadata['is_skill_body']`` 翻为 False、
设置 ``original_is_skill_body=True``。本 rail 通过这两个标记位识别
「上游已 stub 化」的场景，并从 session active state 反查真正的 SKILL.md
全文用于 bash 命令解析与 active_skill_content；当上游因
``max_active_skill_bodies<=0`` 等原因未 stub 化时，回退用 tool_msg.content。
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.tools.todo_toolkits import (
    TaskStatus,
    TodoOpKind,
    TodoOpResult,
    consume_last_op_result,
)
from jiuwenclaw.config import get_config
from jiuwenclaw.utils import logger


_BASH_BLOCK_RE = re.compile(r'```bash\s*\n(.*?)```', re.DOTALL)
_STEP_DECL_RE = re.compile(r'\[(?:当前步骤|[Cc]urrent\s*[Ss]tep)[：:]\s*(.+?)\]')
_STAGE_NUM_RE = re.compile(r'[Ss]tage\s*(\d+)|阶段\s*(\d+)|[Ss]tep\s*(\d+)')

_FAILURE_INDICATORS_RE = re.compile(
    r'"exit_code"\s*:\s*[1-9]'
    r'|ModuleNotFoundError|No module named|ImportError'
    r'|not (?:found|installed)|library is missing'
    r'|\[ERROR\]',
    re.IGNORECASE,
)
_PY_SCRIPT_RE = re.compile(r'([\w][\w.-]*\.py)\b')

_NO_TOOL_DEACTIVATE_THRESHOLD = 2
_DEFAULT_SESSION_ID = "default"

_current_session_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "skill_compliance_session_id", default=None,
)


class SkillPhase(str, Enum):
    IDLE = "idle"
    WAITING_PLAN = "waiting_plan"
    IN_PROGRESS = "in_progress"
    DONE = "done"


@dataclass
class _SkillSessionState:
    phase: SkillPhase = SkillPhase.IDLE
    active_skill: Optional[str] = None
    active_skill_content: Optional[str] = None
    skill_bash_commands: List[str] = field(default_factory=list)
    last_declared_step: Optional[int] = None
    skip_warned: bool = False
    pending_skip_warning: Optional[str] = None
    no_tool_invoke_count: int = 0

    def reset(self) -> None:
        self.__init__()


# Process-local in-memory state. Intentionally NOT persisted:
#
# - On process restart, ``_sessions`` is empty. If a session was mid-skill when
#   the process died, the rail has no record. Behavior:
#   * If the LLM next calls ``skill_tool`` to (re)load SKILL.md, ``_activate_skill``
#     reads ``skill_step.md`` from disk and rebuilds the correct phase.
#   * If the LLM does NOT reload and goes straight to other tools, the rail is
#     fully no-op for this session and the system prompt carries no SKILL
#     constraint section. The skill execution context is silently lost. This is
#     accepted as the cost of avoiding cross-process persistence; rare in
#     practice (LLMs reload when context is missing).
#
# - Cross-process: if two processes serve the same ``session_id`` (not the
#   normal deployment), each maintains its own ``_sessions`` entry. The disk
#   file (``skill_step.md``) is the only shared truth; phase divergence is
#   bounded — the next ``skill_tool`` load resyncs from disk.
_sessions: dict[str, _SkillSessionState] = {}


def _get_or_create_state(session_id: str) -> _SkillSessionState:
    state = _sessions.get(session_id)
    if state is None:
        state = _SkillSessionState()
        _sessions[session_id] = state
    return state


def get_session_phase(session_id: str) -> SkillPhase:
    """Public read-side: SkillProtocolPromptRail consumes this."""
    if not session_id:
        return SkillPhase.IDLE
    state = _sessions.get(session_id)
    return state.phase if state is not None else SkillPhase.IDLE


def get_session_active_skill(session_id: str) -> Optional[str]:
    """Public read-side: SkillProtocolPromptRail consumes this."""
    if not session_id:
        return None
    state = _sessions.get(session_id)
    return state.active_skill if state is not None else None


def _parse_skill_bash_commands(skill_md_text: str) -> List[str]:
    commands: List[str] = []
    for block in _BASH_BLOCK_RE.findall(skill_md_text):
        for raw in block.strip().splitlines():
            line = raw.strip()
            if line and not line.startswith('#'):
                commands.append(line)
    return commands


def _resolve_lang() -> str:
    cfg = get_config() if callable(get_config) else {}
    lang = cfg.get("preferred_language", "zh") if isinstance(cfg, dict) else "zh"
    return "zh" if lang in ("zh", "cn") else "en"


def _str_content(msg: Any) -> str:
    c = getattr(msg, "content", "")
    return c if isinstance(c, str) else str(c)


def _extract_session_id(ctx: AgentCallbackContext) -> Optional[str]:
    inputs = getattr(ctx, "inputs", None)
    if inputs is None:
        return None
    conv_id = getattr(inputs, "conversation_id", None)
    return str(conv_id) if conv_id else None


def _read_initial_phase_from_disk(session_id: str) -> SkillPhase:
    """Detect existing skill_step.md state at SKILL.md load time. The ONLY
    disk read in this rail outside script-failure detection."""
    if not session_id:
        return SkillPhase.WAITING_PLAN
    try:
        from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
        tasks = SkillStepToolkit(session_id=session_id).load_tasks()
    except Exception as exc:
        logger.debug("[SkillComplianceRail] initial phase read failed: %s", exc)
        return SkillPhase.WAITING_PLAN
    if not tasks:
        return SkillPhase.WAITING_PLAN
    if all(t.status == TaskStatus.COMPLETED for t in tasks):
        return SkillPhase.DONE
    return SkillPhase.IN_PROGRESS


def _resolve_todo_file_path(session_id: str) -> Optional[str]:
    try:
        from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
        _, path = SkillStepToolkit(session_id=session_id).resolve_todo_path()
        return str(path)
    except Exception as exc:
        logger.debug("[SkillComplianceRail] resolve_todo_file_path failed: %s", exc)
        return None


def _clear_skill_step_file(session_id: str, skill_name: str) -> bool:
    """Remove skill_step.md after skill_complete to prevent stale-plan misread
    when the next SKILL.md is loaded on this session."""
    if not session_id:
        return False
    try:
        from jiuwenclaw.agentserver.tools.skill_step_toolkit import SkillStepToolkit
        removed = SkillStepToolkit(session_id=session_id).clear_tasks()
    except Exception as exc:
        logger.warning(
            "[SkillComplianceRail] clear skill_step.md failed (session=%s skill=%s): %s",
            session_id, skill_name, exc,
        )
        return False
    if removed:
        logger.info(
            "[SkillComplianceRail] cleared skill_step.md after skill_complete(%s) session=%s",
            skill_name, session_id,
        )
    return removed


def _read_skill_body_from_session(
    ctx: AgentCallbackContext, skill_name: str, relative_file_path: str,
) -> Optional[str]:
    """Read the full SKILL.md body recorded by openjiuwen SkillUseRail.

    SkillUseRail runs before this rail and (a) writes the full body into
    ``session.get_state("active_skill_bodies")[key]``, then (b) replaces
    ``tool_msg.content`` with a short ``[SKILL LOADED] ...`` stub and flips
    ``metadata['is_skill_body']`` to False. To get the real body for bash-block
    parsing and active_skill_content, we must read it from session state, not
    from ``tool_msg.content``.
    """
    try:
        from openjiuwen.core.context_engine.active_skill_bodies import (
            ACTIVE_SKILL_BODIES_STATE_KEY,
            _state_key,
            normalize_skill_relative_file_path,
        )
        ctx_model = getattr(ctx, "context", None)
        session = getattr(ctx_model, "_session_ref", None) if ctx_model is not None else None
        if session is None:
            return None
        active = session.get_state(ACTIVE_SKILL_BODIES_STATE_KEY) or {}
        if not isinstance(active, dict):
            return None
        key = _state_key(skill_name, normalize_skill_relative_file_path(relative_file_path))
        entry = active.get(key)
        if not isinstance(entry, dict):
            return None
        body = entry.get("body")
        return body if isinstance(body, str) and body else None
    except Exception as exc:
        logger.debug("[SkillComplianceRail] read body from session failed: %s", exc)
        return None


# ---------------- directive builders (one per transition kind) ----------------

def _build_load_directive(
    lang: str, phase: SkillPhase, skill_name: str, session_id: str,
) -> str:
    """Directive to append on SKILL.md load (covers all three landing phases)."""
    todo_path = _resolve_todo_file_path(session_id)
    if lang == "zh":
        path_line = f"\nSkill 步骤追踪文件：{todo_path}\n" if todo_path else ""
        if phase == SkillPhase.WAITING_PLAN:
            head = (
                "\n\n[技能文档已加载 · 强制规约] 当前 session 不存在 skill_step 计划。"
                "在执行 SKILL.md 中任何步骤之前，**必须**先调用 "
                "`skill_step(action=\"create\")` 为文档中定义的每个步骤创建一个 skill_step 项。"
                "未创建计划前，禁止执行 SKILL 中的任何动作（包括只是『先看一下』）。\n"
            )
        elif phase == SkillPhase.DONE:
            head = (
                "\n\n[技能文档已加载] 当前 session 的 skill_step 计划全部已完成（来自先前执行）。"
                "如果是延续先前任务，请直接调用 `skill_complete` 收尾；"
                "如果是新一轮执行，请先 `skill_step(action=\"remove\")` 清空旧 plan "
                "再 `skill_step(action=\"create\")` 重建。\n"
            )
        else:  # IN_PROGRESS
            head = (
                "\n\n[技能文档已加载] 当前 session 已存在 skill_step 计划。"
                "请用 `skill_step(action=\"list\")` 核对其是否对应当前 SKILL.md；"
                "若不对应，必须先 `skill_step(action=\"remove\")` 清空"
                "再 `skill_step(action=\"create\")` 重建。\n"
            )
        tail = (
            "⚠️ 完成步骤/子任务后，必须立即标记进度：单项完成用 "
            "`skill_step(action=\"complete\")`；若多个连续步骤都已实际完成，"
            "优先用 `skill_step(action=\"complete_batch\")` 一次性收尾。"
            "禁止预先完成、跳过未完成项或不标记就推进。"
            "可随时用 `skill_step(action=\"list\")` 查看进度。"
            f"{path_line}"
            "⚠️ Skill 脚本执行原则：SKILL.md 中定义的脚本必须按原样执行，"
            "禁止自行编写代码替代其功能。脚本失败时应修复执行环境（如安装依赖）后重试原脚本。"
        )
        return head + tail
    path_line = f"\nSkill step tracking file: {todo_path}\n" if todo_path else ""
    if phase == SkillPhase.WAITING_PLAN:
        head = (
            "\n\n[Skill document loaded · MANDATORY] No skill_step plan exists "
            "for this session. Before executing ANY step from SKILL.md, you MUST "
            "first call `skill_step(action=\"create\")` with one skill_step item per step "
            "defined in the document. No SKILL action is permitted (including "
            "'just taking a look') until the plan is created.\n"
        )
    elif phase == SkillPhase.DONE:
        head = (
            "\n\n[Skill document loaded] All skill_step items from a previous run "
            "are already completed. If you are continuing that work, call "
            "`skill_complete` to finalize. If this is a new run, "
            "`skill_step(action=\"remove\")` the old plan and "
            "`skill_step(action=\"create\")` a fresh one.\n"
        )
    else:  # IN_PROGRESS
        head = (
            "\n\n[Skill document loaded] A skill_step plan already exists for "
            "this session. Use `skill_step(action=\"list\")` to verify it matches "
            "the current SKILL.md; if not, you MUST `skill_step(action=\"remove\")` "
            "to clear it and then `skill_step(action=\"create\")` to rebuild before proceeding.\n"
        )
    tail = (
        "After completing step/sub-task progress, you MUST mark it promptly: use "
        "`skill_step(action=\"complete\")` for a single finished item; when several "
        "contiguous items are already finished, prefer "
        "`skill_step(action=\"complete_batch\")`. Never "
        "pre-complete, skip unfinished items, or advance without marking progress. "
        "Use `skill_step(action=\"list\")` anytime to inspect progress."
        f"{path_line}"
        "Script execution principle: Scripts defined in SKILL.md must be executed as specified. "
        "Do NOT write your own code to replace their functionality. "
        "On script failure, fix the environment (e.g., install dependencies) and retry the original script."
    )
    return head + tail


def _build_plan_created_directive(lang: str, skill_name: str, total: int) -> str:
    if lang == "zh":
        return (
            f"\n\n[skill_step 计划已建立 · {total} 项] 现在请按顺序从第 1 项开始执行。"
            f"开始第 1 项前，先用 `skill_step(action=\"insert\")` 将其拆解为原子级子步骤；"
            f"完成单项用 `skill_step(action=\"complete\")` 并写 result；"
            f"若多个连续项都已实际完成，优先用 `skill_step(action=\"complete_batch\")` 一次性收尾。"
        )
    return (
        f"\n\n[skill_step plan created · {total} items] Begin with item 1. "
        f"Before starting item 1, use `skill_step(action=\"insert\")` to break it into atomic "
        f"sub-steps; use `skill_step(action=\"complete\")` for a single finished item, and "
        f"prefer `skill_step(action=\"complete_batch\")` when several contiguous items are "
        f"already finished."
    )


def _build_all_done_directive(lang: str, skill_name: str) -> str:
    if lang == "zh":
        return (
            f"\n\n[技能 {skill_name} · 全部完成] 所有 skill_step 已完成。"
            f"**必须**立即调用 `skill_complete(skill_name=\"{skill_name}\")` 收尾，"
            f"释放技能上下文后才能进入下一任务。"
        )
    return (
        f"\n\n[Skill {skill_name} · All steps complete] Every skill_step item is done. "
        f"You MUST now call `skill_complete(skill_name=\"{skill_name}\")` to finalize "
        f"and release the skill context before moving on."
    )


def _build_plan_emptied_directive(lang: str, skill_name: str) -> str:
    if lang == "zh":
        return (
            f"\n\n[skill_step 计划已清空] 当前 session 的 skill_step 列表已空。"
            f"如果还要继续执行 SKILL.md，请重新调用 `skill_step(action=\"create\")` 重建计划；"
            f"如果不再执行，请调用 `skill_complete(skill_name=\"{skill_name}\")` 收尾。"
        )
    return (
        f"\n\n[skill_step plan emptied] The skill_step list is now empty. "
        f"To continue executing SKILL.md, call `skill_step(action=\"create\")` to rebuild it. "
        f"To abandon, call `skill_complete(skill_name=\"{skill_name}\")` to finalize."
    )


# ---------------- the rail ---------------------------------------------------

class SkillComplianceRail(DeepAgentRail):
    """Event-driven SKILL execution state machine (per-session)."""

    priority = 30

    def __init__(self, session_id: Optional[str] = None) -> None:
        super().__init__()
        # team 场景由 build_member_rails 预绑定；主 agent 从 ctx 解析。
        self._preset_session_id: Optional[str] = session_id

    def _resolve_session_id(self, ctx: AgentCallbackContext) -> str:
        return (
            self._preset_session_id
            or _extract_session_id(ctx)
            or _current_session_var.get()
            or _DEFAULT_SESSION_ID
        )

    @staticmethod
    def _get_arg(tc: Any, key: str, default: Any = None) -> Any:
        args = getattr(tc, "arguments", None)
        if isinstance(args, dict):
            return args.get(key, default)
        if isinstance(args, str):
            try:
                import json as _json
                parsed = _json.loads(args)
                if isinstance(parsed, dict):
                    return parsed.get(key, default)
            except Exception as exc:
                logger.debug("[SkillComplianceRail] parse tc.arguments failed: %s", exc)
        return default

    # -- step-skip detection (model-text, no IO) --
    def _check_step_skip(self, state: _SkillSessionState, ai_content: str) -> None:
        if state.phase == SkillPhase.IDLE or not ai_content:
            return
        m = _STEP_DECL_RE.search(ai_content)
        if not m:
            return
        nm = _STAGE_NUM_RE.search(m.group(1))
        if not nm:
            return

        current = int(next(g for g in nm.groups() if g is not None))
        lang = _resolve_lang()

        if state.last_declared_step is not None:
            gap = current - state.last_declared_step
            if gap > 1:
                skipped = ", ".join(
                    f"Stage {state.last_declared_step + i}" for i in range(1, gap)
                )
                if not state.skip_warned:
                    state.skip_warned = True
                    if lang == "zh":
                        state.pending_skip_warning = (
                            f"⚠️ 你从 Stage {state.last_declared_step} "
                            f"跳到了 Stage {current}，跳过了 {skipped}。"
                            f"请确认 SKILL.md 是否允许跳过这些步骤。"
                            f"如果不允许，请立即回退执行被跳过的步骤。"
                        )
                    else:
                        state.pending_skip_warning = (
                            f"⚠️ You jumped from Stage {state.last_declared_step} "
                            f"to Stage {current}, skipping {skipped}. "
                            f"Verify SKILL.md allows skipping these. If not, go back and execute them now."
                        )
                    logger.warning(
                        "[SkillComplianceRail] step skip warned %s -> %s (skipped %s)",
                        state.last_declared_step, current, skipped,
                    )
                    return
                if lang == "zh":
                    state.pending_skip_warning = (
                        f"[步骤跳跃拦截] 已警告过但你仍然跳过了 {skipped}。\n"
                        f"请重新阅读 SKILL.md，从被跳过的步骤开始执行。"
                    )
                else:
                    state.pending_skip_warning = (
                        f"[Step skip blocked] You were warned but still skipped {skipped}.\n"
                        f"Re-read SKILL.md and execute the skipped stages."
                    )
                logger.warning(
                    "[SkillComplianceRail] step skip blocked (post-warning) %s -> %s",
                    state.last_declared_step, current,
                )
                return

        state.skip_warned = False
        state.pending_skip_warning = None
        state.last_declared_step = current

    # -- transition handlers --
    def _activate_skill(
        self, state: _SkillSessionState, skill_name: str,
        skill_content: str, tool_msg: Any, session_id: str,
    ) -> None:
        state.phase = _read_initial_phase_from_disk(session_id)
        state.active_skill = skill_name
        state.active_skill_content = skill_content
        state.skill_bash_commands = _parse_skill_bash_commands(skill_content)
        state.last_declared_step = None
        state.skip_warned = False
        state.pending_skip_warning = None
        state.no_tool_invoke_count = 0
        logger.info(
            "[SkillComplianceRail] activated '%s' phase=%s bash=%d session=%s",
            skill_name, state.phase.value, len(state.skill_bash_commands), session_id,
        )
        directive = _build_load_directive(_resolve_lang(), state.phase, skill_name, session_id)
        tool_msg.content = _str_content(tool_msg) + directive

    def _handle_skill_complete(
        self, state: _SkillSessionState, skill_name: str, session_id: str,
    ) -> None:
        if not skill_name or state.active_skill != skill_name:
            return
        logger.info(
            "[SkillComplianceRail] skill_complete(%s) session=%s -> IDLE",
            skill_name, session_id,
        )
        state.reset()
        _clear_skill_step_file(session_id, skill_name)

    def _apply_op_result(
        self, state: _SkillSessionState, op: TodoOpResult, tool_msg: Any,
    ) -> None:
        """Phase transitions driven by TodoOpResult (only on op.success)."""
        if not op.success or state.phase == SkillPhase.IDLE:
            return
        lang = _resolve_lang()
        skill_name = state.active_skill or ""
        prev = state.phase

        if op.kind == TodoOpKind.CREATE:
            if prev == SkillPhase.WAITING_PLAN and op.total_count > 0:
                state.phase = SkillPhase.IN_PROGRESS
                tool_msg.content = _str_content(tool_msg) + _build_plan_created_directive(
                    lang, skill_name, op.total_count,
                )
        elif op.kind == TodoOpKind.COMPLETE:
            if prev == SkillPhase.IN_PROGRESS and op.all_completed:
                state.phase = SkillPhase.DONE
                tool_msg.content = _str_content(tool_msg) + _build_all_done_directive(
                    lang, skill_name,
                )
        elif op.kind == TodoOpKind.REMOVE:
            if prev == SkillPhase.IN_PROGRESS and op.total_count == 0:
                state.phase = SkillPhase.WAITING_PLAN
                tool_msg.content = _str_content(tool_msg) + _build_plan_emptied_directive(
                    lang, skill_name,
                )
        # INSERT / START never change phase; no directive.

        if prev != state.phase:
            logger.info(
                "[SkillComplianceRail] phase %s -> %s (op=%s skill=%s)",
                prev.value, state.phase.value, op.kind.value, skill_name,
            )

    def _handle_tool_event(
        self, state: _SkillSessionState, tc: Any, tool_msg: Any,
        tool_name: str, session_id: str,
        *, ctx: Optional[AgentCallbackContext] = None,
    ) -> None:
        if not tool_name:
            return

        if tool_name == "skill_complete":
            skill_name = (self._get_arg(tc, "skill_name", "") or "").strip()
            self._handle_skill_complete(state, skill_name, session_id)
            return

        if tool_name == "skill_tool":
            meta = getattr(tool_msg, "metadata", None) or {}
            # 同时认 is_skill_body（上游未 stub 化的场景，如 max_active_skill_bodies<=0）
            # 与 original_is_skill_body（上游 SkillUseRail 已 stub 化并翻字段后留下的痕迹）
            if not (meta.get("is_skill_body") or meta.get("original_is_skill_body")):
                return
            skill_name = (
                meta.get("skill_name") or self._get_arg(tc, "skill_name", "") or ""
            ).strip()
            if not skill_name:
                return
            rel_path = str(meta.get("relative_file_path") or "SKILL.md")
            # 优先从 session active state 反查真正的 SKILL.md 全文；
            # 上游 SkillUseRail.record_active_skill_body 会在 stub 化前写入。
            body = _read_skill_body_from_session(ctx, skill_name, rel_path) \
                   or _str_content(tool_msg)
            if not body.strip():
                return
            self._activate_skill(state, skill_name, body, tool_msg, session_id)
            return

        if tool_name == "skill_step":
            op = consume_last_op_result(session_id)
            if op is None:
                # skill_step(action="list") is read-only (no publish), or the toolkit
                # somehow didn't publish. No state change.
                return
            self._apply_op_result(state, op, tool_msg)
            return

        # Any other tool: rail does nothing structural here. Step-skip warnings
        # and script-failure recovery are appended downstream in after_tool_call.

    # -- script failure recovery (orthogonal) --
    def _detect_script_failure(
        self, state: _SkillSessionState, tc: Any, tool_msg: Any,
    ) -> None:
        if state.phase == SkillPhase.IDLE or not state.skill_bash_commands:
            return
        if getattr(tc, "name", "") != "mcp_exec_command":
            return
        content = _str_content(tool_msg)
        if not _FAILURE_INDICATORS_RE.search(content):
            return

        command = self._get_arg(tc, "command", default="")
        if not isinstance(command, str):
            command = str(command)
        if not command:
            return

        matching_cmd: Optional[str] = None
        for cmd in state.skill_bash_commands:
            if any(s in command for s in _PY_SCRIPT_RE.findall(cmd)):
                matching_cmd = cmd
                break
        if not matching_cmd:
            return

        lang = _resolve_lang()
        if lang == "zh":
            recovery = (
                f"\n\n[脚本执行失败 · 恢复指引]\n"
                f"SKILL.md 指定的脚本执行失败。请严格按以下步骤恢复：\n"
                f"1. 分析上方错误信息，判断失败原因（缺少依赖/路径错误/其他）\n"
                f"2. 使用 mcp_exec_command 修复问题（如 pip install 缺失的库）\n"
                f"3. 使用 mcp_exec_command 重新执行原始命令：\n"
                f"   {matching_cmd}\n"
                f"⚠️ 禁止使用 execute_python_code 自行编写代码替代该脚本。"
            )
        else:
            recovery = (
                f"\n\n[Script Failure · Recovery Guide]\n"
                f"A SKILL.md-designated script failed. Follow these steps:\n"
                f"1. Analyze the error above to determine the cause\n"
                f"2. Fix the issue via mcp_exec_command (e.g., pip install missing library)\n"
                f"3. Re-execute the original command via mcp_exec_command:\n"
                f"   {matching_cmd}\n"
                f"Do NOT use execute_python_code to rewrite the script's logic."
            )
        tool_msg.content = content + recovery
        logger.info(
            "[SkillComplianceRail] script failure detected, injected recovery for '%s'",
            matching_cmd,
        )

    # -- lifecycle hooks --
    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        session_id = self._resolve_session_id(ctx)
        _current_session_var.set(session_id)
        state = _get_or_create_state(session_id)
        if state.phase != SkillPhase.IDLE and state.no_tool_invoke_count >= _NO_TOOL_DEACTIVATE_THRESHOLD:
            logger.info(
                "[SkillComplianceRail] deactivating '%s' (session=%s) after %d no-tool invokes",
                state.active_skill, session_id, state.no_tool_invoke_count,
            )
            state.reset()

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        response = getattr(inputs, "response", None) if inputs else None
        if response is None:
            return

        state = _get_or_create_state(self._resolve_session_id(ctx))
        content = getattr(response, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)

        try:
            self._check_step_skip(state, content)
        except Exception as exc:
            logger.warning("[SkillComplianceRail] step skip check failed: %s", exc)

        if state.phase != SkillPhase.IDLE:
            if getattr(response, "tool_calls", None):
                state.no_tool_invoke_count = 0
            else:
                state.no_tool_invoke_count += 1

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return
        tool_msg = getattr(inputs, "tool_msg", None)
        tool_call = getattr(inputs, "tool_call", None)
        if tool_msg is None or tool_call is None:
            return
        tool_name = getattr(inputs, "tool_name", "") or ""

        session_id = self._resolve_session_id(ctx)
        state = _get_or_create_state(session_id)

        try:
            self._handle_tool_event(
                state, tool_call, tool_msg, tool_name, session_id, ctx=ctx,
            )
        except Exception as exc:
            logger.warning("[SkillComplianceRail] handle tool event failed: %s", exc)

        # Consume any pending step-skip warning from the prior model_call.
        if state.pending_skip_warning:
            tool_msg.content = _str_content(tool_msg) + f"\n{state.pending_skip_warning}"
            state.pending_skip_warning = None

        try:
            self._detect_script_failure(state, tool_call, tool_msg)
        except Exception as exc:
            logger.warning("[SkillComplianceRail] script failure detect failed: %s", exc)


__all__ = [
    "SkillComplianceRail",
    "SkillPhase",
    "get_session_phase",
    "get_session_active_skill",
]
