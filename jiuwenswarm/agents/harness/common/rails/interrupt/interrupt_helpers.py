# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interrupt helpers for DeepAgent.

Provides utilities for converting interrupt payloads to frontend format
and building permission rails.
"""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    build_plan_approval_actions,
)
from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
    build_plan_approval_options_from_message,
    extract_plan_approval_content,
    is_plan_approval_message,
    strip_inline_plan_approval_choices,
)
from jiuwenswarm.common.utils import logger
from jiuwenswarm.edition import is_enterprise

SKILL_EVOLUTION_APPROVAL_SCHEMA = "openjiuwen.skill_evolution_approval.v1"
EVOLUTION_INTERRUPT_SOURCE = "evolution_interrupt"
LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE = "skill_evolution_approval"
INTERRUPT_RESUME_SOURCES = frozenset({
    "permission_interrupt",
    "confirm_interrupt",
    "ask_user_interrupt",
    EVOLUTION_INTERRUPT_SOURCE,
})
EVOLUTION_INTERRUPT_METADATA_SOURCES = frozenset({
    EVOLUTION_INTERRUPT_SOURCE,
    LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE,
})
SKILL_EVOLUTION_APPROVAL_TOOL_KINDS = {
    "evolve_skill_experiences": "evolve",
    "simplify_skill_experiences": "simplify",
}

_HITL_SEQ_SUFFIX_RE = re.compile(r"^(?P<base>.+)#\d+$")


def strip_hitl_seq_suffix(request_id: str) -> str:
    """剥离 HITL 卡片序号后缀（``{call_id}#{n}``），恢复 harness 原始 tool_call id。

    同一外层调用内第 n 次中断（TIE 重写共用外层 call id 的顺序权限门 /
    ask_user 顺序多问卡）共用同一 base id。用于作答对齐与"base 当前代卡"
    判定；注意：权限账本去重 key 不做此归一化——``#{n}`` 是顺序门的不同
    代次（不同权限内容），归一化会把下一个合法门误判为重复（CR-1）。
    未命中后缀时原样返回（保留空白差异，id 对调用方仍是不透明串）。
    """
    if not isinstance(request_id, str):
        return request_id
    match = _HITL_SEQ_SUFFIX_RE.match(request_id.strip())
    if match and match.group("base").strip():
        return match.group("base").strip()
    return request_id


def is_interrupt_resume_source(source: Any) -> bool:
    """True for permission / confirm / ask_user HITL resume sources.

    Used to keep the active skill (and thus skill_envs injection) across
    security-guard HITL. Intentionally excludes skill-evolution sources.
    """
    text = str(source or "").strip()
    return text in {
        "permission_interrupt",
        "confirm_interrupt",
        "ask_user_interrupt",
    }


def _filter_init_kwargs(cls: type, **kwargs: Any) -> dict[str, Any]:
    """Drop kwargs the installed openjiuwen constructor does not accept."""
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}


def has_interrupt_resume_payload(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if not str(params.get("request_id") or "").strip():
        return False
    answers = params.get("answers")
    return isinstance(answers, list) and bool(answers)


def _has_active_skill_overlay(
    ctx: Any = None,
    scope_resolver: Callable[[Any], tuple[str, str] | None] | None = None,
) -> bool:
    """当前 Skill 授权上下文是否存在 ACTIVE Grant（owner_scopes allow 回落判定）。

    判定失败按存在处理（返回 True，强制回落 engine），不放宽权限。
    """
    try:
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_authorization_context,
            get_skill_grant_store,
        )

        authz = get_skill_authorization_context()
        if authz is not None and authz.session_id and authz.agent_scope_id:
            scope = authz.session_id, authz.agent_scope_id
        else:
            scope = scope_resolver(ctx) if scope_resolver is not None and ctx is not None else None
        if scope is None:
            return False
        return get_skill_grant_store().get_active(*scope) is not None
    except Exception:  # noqa: BLE001 — 不确定时不允许绕过引擎
        logger.warning(
            "[InterruptHelpers] active Skill lookup failed; owner allow falls through to engine",
            exc_info=True,
        )
        return True


def is_interrupt_resume_payload(params: Any) -> bool:
    if not has_interrupt_resume_payload(params):
        return False
    source = str(params.get("source") or "").strip()
    if source in INTERRUPT_RESUME_SOURCES:
        return True
    if source != LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE:
        return False
    evolution_meta = params.get("evolution_meta")
    return (
        isinstance(evolution_meta, dict)
        and evolution_meta.get("approval_transport") == "interrupt"
    )


_SUBAGENT_PERMISSION_APPROVAL_TIMEOUT = 120.0

# Hosted cards need chat.ask_user_question rendering (web / OfficeClaw / TUI).
# ACP keeps native session/request_permission JSON-RPC — do not hijack it.
_HOSTED_PERMISSION_CHANNELS = frozenset({"web", "officeclaw", "tui"})

_HOSTED_ALLOW_ONCE_LABELS = frozenset({
    "本次允许",
    "允许",
    "Allow once",
    "allow_once",
    "allow-once",
})
_HOSTED_SESSION_REMEMBER_LABELS = frozenset({
    "本会话内允许",
    "会话内记住",
    "Session remember",
    "allow_always_session",
})
_HOSTED_REJECT_LABELS = frozenset({
    "拒绝",
    "Reject",
    "reject",
    "reject-once",
    "reject_once",
})


def _unwrap_session(session: Any) -> Any:
    return getattr(session, "_parent", session) if session is not None else None


def _session_id_of(session: Any) -> str | None:
    if session is None:
        return None
    for attr_name in ("get_session_id", "session_id"):
        attr = getattr(session, attr_name, None)
        try:
            value = attr() if callable(attr) else attr
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_subagent_permission_parent_session(ctx: Any) -> Any | None:
    """Return parent session when ``ctx`` is a nested agent under an outer tool.

    Real trigger surface (not ``task_tool`` inheriting PermissionInterruptRail):
    a child agent that still mounts its own permission rail, with
    ``ctx.session`` id different from the parent session ContextVar bound by
    ``StreamEventRail`` for the outer tool call (OfficeClaw worker / nested
    agent). Same-session bindings must not take the hosted path.

    ``PermissionInterruptRail`` on the *same* agent as the outer tool still
    sees no foreign parent, so main-agent ASK stays on interrupt / ACP.
    """
    try:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            get_subagent_parent_session,
        )

        parent = get_subagent_parent_session()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[InterruptHelpers] subagent parent session read failed",
            exc_info=True,
        )
        return None

    parent = _unwrap_session(parent)
    current = _unwrap_session(getattr(ctx, "session", None))
    if parent is None or current is None or parent is current:
        return None
    parent_sid = _session_id_of(parent)
    current_sid = _session_id_of(current)
    if parent_sid and current_sid and parent_sid == current_sid:
        return None
    if not callable(getattr(parent, "write_stream", None)):
        return None
    return parent


def _selected_option_labels(answer: Any) -> list[str]:
    labels: list[str] = []
    if isinstance(answer, list):
        for item in answer:
            labels.extend(_selected_option_labels(item))
        return labels
    if not isinstance(answer, dict):
        return labels
    selected = answer.get("selected_options")
    if isinstance(selected, list):
        for item in selected:
            text = str(item).strip()
            if text:
                labels.append(text)
    return labels


def parse_hosted_permission_answer(answer: Any) -> Any:
    """Map card answers to :class:`PermissionConfirmResponse` (or None if empty).

    Enterprise never persists permanent-remember to local config.yaml (same
    constraint as :func:`_default_interrupt_options`); those labels degrade to
    session auto_confirm only.
    """
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    labels = _selected_option_labels(answer)
    if not labels:
        return None
    if any(label in _HOSTED_REJECT_LABELS for label in labels):
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            persist_allow=False,
            feedback="[PERMISSION_REJECTED] User rejected the request.",
        )
    if any(label in _PERMANENT_REMEMBER_LABELS for label in labels):
        # 企业版：永久标签降级为本会话内允许，不写本地 config。
        persist = not is_enterprise()
        return PermissionConfirmResponse(
            approved=True,
            auto_confirm=True,
            persist_allow=persist,
            feedback="",
        )
    if any(label in _HOSTED_SESSION_REMEMBER_LABELS for label in labels):
        return PermissionConfirmResponse(
            approved=True,
            auto_confirm=True,
            persist_allow=False,
            feedback="",
        )
    if any(label in _HOSTED_ALLOW_ONCE_LABELS for label in labels):
        return PermissionConfirmResponse(
            approved=True,
            auto_confirm=False,
            persist_allow=False,
            feedback="",
        )
    return None


_HEADLINE_ACTION_MAX = 60
_HEADLINE_GOAL_MAX = 80

# 这些工具的审批卡由专用 rail 产出语义完整的定制文案（skill_turbo 的
# 工具清单统一授权卡），通用意图标题叠上去只会破坏其定制语义（下游 BFF
# 以首行文案识别品牌卡），一律豁免。
_INTENT_HEADLINE_EXEMPT_TOOLS = frozenset({"skill_acceleration_exec"})


def _permission_headline_plain(text: str, max_len: int) -> str:
    """标题块专用纯文本：单行、无 markdown 结构符、有长度上限。

    action / call_goal 是模型或参数可控文本，直接置顶会在真实声明行上方
    伪造结构（如换行 + ``**工具 …**`` 假声明、``「」`` 配对被打断）；这里
    折叠空白为单行、内容中的 ``「」`` 换成 ``【】`` 保证模板配对不被打断、
    剔除 markdown 控制符并截断（与子代理审批卡防膨胀思路一致）。
    """
    collapsed = " ".join(str(text or "").split())
    collapsed = collapsed.replace("「", "【").replace("」", "】")
    collapsed = re.sub(r"[*`_\[\]]", "", collapsed)
    if len(collapsed) > max_len:
        return collapsed[:max_len] + "…"
    return collapsed


def _permission_headline_code_span(text: str) -> str:
    """反引号代码段专用清洗：折叠为单行、剔除反引号，其余原样。

    工具名与 action/call_goal 同属模型可控文本，但它落入 `` `…` `` 代码段：
    换行会伪造行结构、反引号会提前终止代码段，须清洗；``_`` 等在代码段内
    原样渲染且是真实工具名的合法字符（如 ``mcp__server__tool``），不做
    ``_permission_headline_plain`` 式全量清洗以免改写工具名。
    """
    collapsed = " ".join(str(text or "").split()).replace("`", "")
    if len(collapsed) > _HEADLINE_ACTION_MAX:
        return collapsed[:_HEADLINE_ACTION_MAX] + "…"
    return collapsed


def build_permission_intent_headline(tool_name: str, tool_args: Any) -> str:
    """权限审批卡的意图标题：L1 规则动作短语 + L2 call_goal（模型自述目标）。

    组出形如 ``**是否允许「写入 report.md」？**\\n\\n模型说明（仅供参考）：生成周报``
    的标题块；动作短语与 call_goal 均不可得时返回空串，调用方保持原文案不变。
    ``_INTENT_HEADLINE_EXEMPT_TOOLS`` 内的工具（skill_turbo 统一审批等定制卡）
    同样返回空串。仅做展示增强：审批目标本身（工具声明行与参数区）始终保留，
    意图不可替代具体操作（模型自述可能被提示词注入包装，用户须能核对原始参数；
    故 call_goal 以「模型说明」弱措辞呈现，且 action/call_goal 均经
    ``_permission_headline_plain`` 清洗、兜底分支的工具名经
    ``_permission_headline_code_span`` 清洗，标题块内不得出现伪造的声明结构）。
    """
    name = (tool_name or "").strip()
    if not name or name in _INTENT_HEADLINE_EXEMPT_TOOLS:
        return ""
    from jiuwenswarm.common.tool_display import (
        build_tool_display_name,
        extract_call_goal,
    )

    call_goal, cleaned_args = extract_call_goal(tool_args)
    goal = _permission_headline_plain(call_goal, _HEADLINE_GOAL_MAX)
    action = _permission_headline_plain(
        build_tool_display_name(name, cleaned_args), _HEADLINE_ACTION_MAX
    )
    if action:
        title = f"**是否允许「{action}」？**"
    elif goal:
        name_plain = _permission_headline_code_span(name)
        if not name_plain:
            return ""
        title = f"**是否允许执行 `{name_plain}`？**"
    else:
        return ""
    parts = [title]
    if goal:
        parts.append(f"模型说明（仅供参考）：{goal}")
    return "\n\n".join(parts)


# 跨仓库契约：以下两常量与 agent-core ``tool_security_rail._build_message`` 的
# 输出文案逐字符耦合（「匹配规则：`{rule}`」行与「请确认是否允许该操作。」句）。
# 上游文案演化时此处静默失效——仅表现为对应行/句保留在卡片上，无功能破坏；
# 变更 agent-core 文案时需同步此处与测试夹具 ``_tool_permission_message``。
# 规则行匹配到行尾（``[^\n]*``）而非配对反引号：shell_subcommands 聚合规则
# 内嵌模型可控的命令文本、可携带反引号，配对匹配会失配或只截掉半行。
_PERMISSION_RULE_LINE_RE = re.compile(r"\n\n匹配规则：[^\n]*")
_PERMISSION_CONFIRM_SENTENCE = "请确认是否允许该操作。\n\n"


def apply_permission_intent(message: str, tool_name: str, tool_args: Any) -> str:
    """主 Agent 权限卡 question 头部加入意图标题，并移除内部「匹配规则」行。

    只做前置与删减：不改动「**工具 `X` 需要授权才能执行**」声明行与参数区
    （TUI ``PERMISSION_TOOL_RE`` / 前端 ``approvalPresentation`` 的既有解析依赖
    它们）。无意图可展示时仅移除「匹配规则」行。
    """
    headline = build_permission_intent_headline(tool_name, tool_args)
    cleaned = _PERMISSION_RULE_LINE_RE.sub("", message)
    if headline:
        cleaned = cleaned.replace(_PERMISSION_CONFIRM_SENTENCE, "", 1)
        cleaned = f"{headline}\n\n{cleaned}"
    return cleaned


def _format_hosted_permission_question(
    *,
    tool_name: str,
    tool_args: Any,
    reason: str,
    agent_scope_id: str,
) -> str:
    from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import (
        _format_tool_args_preview,
    )

    parts = []
    headline = build_permission_intent_headline(tool_name, tool_args)
    if headline:
        parts += [headline, "\n\n"]
    parts += [
        f"**工具 `{tool_name}` 需要授权才能执行**\n\n",
        f"> 发起方：子 Agent `{agent_scope_id}`",
        "\n\n**关键参数或命令：**\n\n",
        _format_tool_args_preview(tool_name, tool_args),
    ]
    if reason:
        parts.append(f"\n\n**权限原因：** {reason}")
    return "".join(parts)


async def request_subagent_hosted_permission_confirmation(
    req: Any,
    *,
    parent_session: Any,
    timeout: float = _SUBAGENT_PERMISSION_APPROVAL_TIMEOUT,
) -> Any:
    """Await parent-session card approval; never returns ``"interrupt"``."""
    from openjiuwen.harness.security.models import PermissionConfirmResponse
    from openjiuwen.harness.security.skill_authorization import (
        SubagentApprovalKind,
        get_subagent_approval_registry,
    )
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalCancelled,
        SubagentApprovalCapacityError,
        SubagentApprovalTimeout,
    )

    from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import (
        build_context_expiry_sender,
        emit_subagent_approval,
    )
    from jiuwenswarm.openjiuwen_streaming_tool_patch import (
        streaming_tool_wait_timeout_paused,
    )

    tool_call = req.tool_call
    tool_name = str(getattr(tool_call, "name", "") or "").strip()
    tool_call_id = str(
        getattr(tool_call, "id", "") or f"permission_{tool_name or 'tool'}"
    ).strip()
    agent_scope_id = _session_id_of(getattr(req.ctx, "session", None))
    parent_session_id = _session_id_of(parent_session)
    if not agent_scope_id or not parent_session_id or not tool_call_id:
        logger.warning(
            "[InterruptHelpers] subagent hosted permission missing ids "
            "parent=%s scope=%s tool_call=%s",
            parent_session_id,
            agent_scope_id,
            tool_call_id,
        )
        return None

    raw_args = getattr(tool_call, "arguments", None) if tool_call is not None else None
    if isinstance(raw_args, str):
        try:
            tool_args = json.loads(raw_args) if raw_args.strip() else {}
        except (TypeError, ValueError):
            tool_args = {"_raw": raw_args}
    elif isinstance(raw_args, dict):
        tool_args = raw_args
    else:
        tool_args = {}

    reason = str(getattr(getattr(req, "result", None), "reason", None) or "").strip()
    question = _format_hosted_permission_question(
        tool_name=tool_name,
        tool_args=tool_args,
        reason=reason or "Operation requires approval",
        agent_scope_id=agent_scope_id,
    )
    # Session remember is stored on the child rail session; copy must not
    # imply parent-session scope.
    options = []
    for opt in _default_interrupt_options():
        item = dict(opt)
        if item.get("label") in _HOSTED_SESSION_REMEMBER_LABELS:
            item["description"] = "该子 Agent 委托内自动放行同类操作"
        options.append(item)

    async def _send(request: Any) -> None:
        await emit_subagent_approval(
            parent_session,
            request,
            header=f"权限审批: {tool_name}",
            question=question,
            options=options,
        )

    try:
        async with streaming_tool_wait_timeout_paused():
            answer = await get_subagent_approval_registry().request(
                kind=SubagentApprovalKind.TOOL_PERMISSION,
                session_id=parent_session_id,
                agent_scope_id=agent_scope_id,
                tool_call_id=tool_call_id,
                payload={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "reason": reason or "Operation requires approval",
                    "auto_confirm_key": getattr(req, "auto_confirm_key", "") or "",
                },
                sender=_send,
                expiry_sender=build_context_expiry_sender(),
                timeout=timeout,
            )
    except SubagentApprovalTimeout:
        logger.warning(
            "[InterruptHelpers] subagent hosted permission timeout "
            "session=%s scope=%s tool=%s",
            parent_session_id,
            agent_scope_id,
            tool_name,
        )
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            feedback="[PERMISSION_DENIED] 子 Agent 权限审批超时",
        )
    except (SubagentApprovalCancelled, SubagentApprovalCapacityError) as exc:
        logger.warning(
            "[InterruptHelpers] subagent hosted permission cancelled/capacity "
            "session=%s scope=%s tool=%s error=%s",
            parent_session_id,
            agent_scope_id,
            tool_name,
            exc,
        )
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            feedback="[PERMISSION_DENIED] 子 Agent 权限审批已取消或请求过多",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[InterruptHelpers] subagent hosted permission failed: %s",
            exc,
            exc_info=True,
        )
        return None

    parsed = parse_hosted_permission_answer(answer)
    if parsed is not None:
        return parsed
    return PermissionConfirmResponse(
        approved=False,
        auto_confirm=False,
        feedback="[PERMISSION_REJECTED] User rejected the request.",
    )


@dataclass
class PermissionRailBuildOptions:
    """Optional inputs for ``build_permission_rail``."""

    llm: Any = None
    model_name: str | None = None
    permission_config: dict[str, Any] | None = None
    resolve_workspace_dir: Any | None = None
    persist_target_agent_id_provider: Any | None = None
    rail_factory: Callable[..., Any] | None = None


def resolve_permission_workspace_dir() -> Path:
    """file_guard 的当前任务 workspace，而不是 agent 数据根。

    ``get_workspace_dir()`` 指向 ``~/.jiuwenswarm/agent/workspace``（skills/memory
    等所在目录）。Web 默认任务目录是其下 ``projects/<session>``；写父目录应走区外
    审批，因此优先用 ``get_workspace()``（``_seed_runtime_cwd`` 写入的任务根）。
    """
    from jiuwenswarm.common.utils import get_workspace_dir

    try:
        from openjiuwen.core.sys_operation.cwd import get_workspace

        current = get_workspace()
    except Exception:
        current = None
    if current:
        try:
            return Path(current).expanduser().resolve()
        except (OSError, RuntimeError):
            return Path(current)
    return get_workspace_dir()


_LEVEL_RANK = {"deny": 0, "ask": 1, "allow": 2}


def _derive_user_overlay_delta(
    permissions: dict[str, Any],
    disk_bases: list[dict[str, Any]],
    *,
    approval_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从 rail 整段合并 cfg diff 出 user 层增量键。

    上游 rail 的 ``tools`` map 与 ``file_guard.paths`` 是生效全集（含全局
    preset），persist_user_overlay_from_effective 只认增量键且该键历史上
    无生产方（round-024 allow-always 假阳性落盘根因）。此处仅保留比磁盘
    生效值更放行的项（棘轮），避免整段复制全局层到 user 层。

    ``approval_baseline``（磁盘四层的新鲜合成结果）用于 approval_overrides
    增量：rail 在合成结果上追加本次批准条目后整表下发，基线外的条目才
    是真增量；否则 compose 已并入的 session 层 override 会随整表写回
    user 层，会话级放行被固化为永久（检视 2026-10-29）。为 None 时该节
    原样透传（调用方自行决定降级语义）。
    """
    payload = dict(permissions)

    # tools 增量：磁盘现值 = 各层 tools map + allow/ask/deny_tools 列表的棘轮合成
    disk_tools: dict[str, str] = {}

    def _fold_tool(tool: Any, level: str) -> None:
        name = str(tool).strip()
        if not name or level not in _LEVEL_RANK:
            return
        current = disk_tools.get(name)
        if current is None or _LEVEL_RANK[level] > _LEVEL_RANK.get(current, 0):
            disk_tools[name] = level

    for base in disk_bases:
        if not isinstance(base, dict):
            continue
        base_tools = base.get("tools")
        if isinstance(base_tools, dict):
            for name, level in base_tools.items():
                _fold_tool(name, str(level).strip().lower())
        for key, level in (
            ("allow_tools", "allow"),
            ("ask_tools", "ask"),
            ("deny_tools", "deny"),
        ):
            values = base.get(key)
            if isinstance(values, list):
                for name in values:
                    _fold_tool(name, level)
    cfg_tools = permissions.get("tools")
    if isinstance(cfg_tools, dict):
        added_by_level: dict[str, list[str]] = {"allow": [], "ask": [], "deny": []}
        for name, level in cfg_tools.items():
            tool = str(name).strip()
            value = str(level).strip().lower()
            if not tool or value not in _LEVEL_RANK:
                continue
            current = disk_tools.get(tool)
            # 未配置视为无基线（任何显式等级都是增量）；
            # 已配置则仅放行方向放大项入增量
            if current is not None and _LEVEL_RANK[value] <= _LEVEL_RANK[current]:
                continue
            added_by_level[value].append(tool)
        if added_by_level["allow"]:
            payload["_allow_tools_added"] = added_by_level["allow"]
        for key in ("ask_tools", "deny_tools"):
            if added_by_level[key.removesuffix("_tools")]:
                # persist 侧按整表替换，先并上 user 层现值防丢
                existing: list[str] = []
                for base in disk_bases:
                    if base is disk_bases[-1]:  # 末位为 user 层
                        values = base.get(key)
                        if isinstance(values, list):
                            existing = [str(v) for v in values]
                merged = existing + [
                    n for n in added_by_level[key.removesuffix("_tools")] if n not in existing
                ]
                payload[key] = merged

    # file_guard.paths 增量：按归一化 path 聚合磁盘生效轴值，仅轴放大项入增量
    def _norm_path(value: Any) -> str:
        return str(value or "").replace("\\", "/").rstrip("/")

    disk_axes: dict[str, dict[str, int]] = {}

    def _fold_guard_paths(paths: Any) -> None:
        if not isinstance(paths, list):
            return
        for entry in paths:
            if not isinstance(entry, dict):
                continue
            key = _norm_path(entry.get("path"))
            if not key:
                continue
            axes = disk_axes.setdefault(key, {})
            for axis in ("read", "write", "exec"):
                level = str(entry.get(axis) or "").strip().lower()
                if level in _LEVEL_RANK:
                    axes[axis] = max(axes.get(axis, 0), _LEVEL_RANK[level])

    for base in disk_bases:
        fg = base.get("file_guard") if isinstance(base, dict) else None
        _fold_guard_paths(fg.get("paths") if isinstance(fg, dict) else None)
    # 组合期注入的敏感路径底线（CR-2 floor）不落任何磁盘层，仅存在于
    # _compose_effective_local 合成结果中；不并入基线会把同值项当作
    # 增量整段复制进 user 层（round-027 实证 **/.ssh/** ask 落盘）。
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
            _SENSITIVE_PATH_FLOOR,
        )
        _fold_guard_paths(_SENSITIVE_PATH_FLOOR)
    except Exception:  # noqa: BLE001 — 底线常量缺失时按无基线处理
        logger.warning("[InterruptHelpers] sensitive path floor load failed", exc_info=True)
    cfg_fg = permissions.get("file_guard")
    cfg_paths = cfg_fg.get("paths") if isinstance(cfg_fg, dict) else None
    if isinstance(cfg_paths, list):
        added_paths = []
        for entry in cfg_paths:
            if not isinstance(entry, dict):
                continue
            key = _norm_path(entry.get("path"))
            if not key:
                continue
            axes = disk_axes.get(key, {})
            escalated = any(
                _LEVEL_RANK.get(str(entry.get(axis) or "").strip().lower(), 0) > axes.get(axis, 0)
                for axis in ("read", "write", "exec")
            )
            if escalated:
                added_paths.append(entry)
        if added_paths:
            payload["_file_guard_paths_added"] = added_paths

    # approval_overrides 增量：按身份键（tools/match_type/pattern）对齐
    # 磁盘合成基线，仅基线外或更放行条目入增量；基线内（含 session 层
    # 并入项）丢弃，防止会话级放行随整表提升为永久。
    if approval_baseline is not None:
        from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
            _approval_override_key,
        )

        base_overrides = approval_baseline.get("approval_overrides")
        base_map: dict[tuple[Any, ...], str] = {}
        for entry in base_overrides if isinstance(base_overrides, list) else []:
            if not isinstance(entry, dict):
                continue
            key = _approval_override_key(entry)
            action = str(entry.get("action") or "allow").lower()
            if key not in base_map or _LEVEL_RANK.get(action, 0) > _LEVEL_RANK.get(
                base_map[key], 0
            ):
                base_map[key] = action
        cfg_overrides = permissions.get("approval_overrides")
        kept_overrides = []
        for entry in cfg_overrides if isinstance(cfg_overrides, list) else []:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action") or "allow").lower()
            base_action = base_map.get(_approval_override_key(entry), "deny")
            # 仅基线外或更放行条目入增量（G.EXP.04：推导式不承载多条件）
            if _LEVEL_RANK.get(action, 0) > _LEVEL_RANK.get(base_action, 0):
                kept_overrides.append(entry)
        if kept_overrides:
            payload["approval_overrides"] = kept_overrides
        else:
            payload.pop("approval_overrides", None)

    return payload


def build_permission_rail(
    config: dict[str, Any],
    llm: Any = None,
    model_name: str | None = None,
    options: PermissionRailBuildOptions | None = None,
) -> Any | None:
    """Build openjiuwen PermissionInterruptRail for tool permission checks.

    产品三模式（full_access/auto/strict）下权限轨始终可挂载：旧 ``enabled:false``
    （Web 完全访问）经 compose 迁移为 ``mode=full_access`` 且 ``enabled=true``。

    Args:
        config: Agent config dict containing permissions section
        options: Optional LLM / permission-body / workspace persist inputs.

    Returns:
        PermissionInterruptRail instance or None if effective permissions disabled
    """
    opts = options or PermissionRailBuildOptions(llm=llm, model_name=model_name)
    llm = opts.llm
    model_name = opts.model_name
    permission_config = opts.permission_config
    # Agent 模板 body 由调用方显式传入（企业模板 / yaml agents[id] 段）。
    # 模板 rail 的引擎策略以该 body 为准，host snapshot 不得用全局 yaml
    # 回落覆盖（见 _get_permissions_snapshot 内守卫；上游 !8425 flash
    # 投影依赖此语义：snapshot=None → _static_config）。
    has_template_body = isinstance(permission_config, dict)
    resolve_workspace_dir = opts.resolve_workspace_dir
    from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
    from openjiuwen.harness.security.host import (
        PermissionConfirmationRequest,
        PermissionSceneHookInput,
        ToolPermissionHost,
    )
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        compose_host_effective_permissions,
        persist_session_overlay_from_effective,
        persist_user_overlay_from_effective,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        TOOL_PERMISSION_CHANNEL_ID,
        TOOL_PERMISSION_SESSION_ID,
    )
    from jiuwenswarm.common.e2a.acp.acp_tool_updates import build_acp_tool_descriptor
    from jiuwenswarm.common.utils import get_config_file

    raw_permission_config = (
        permission_config
        if isinstance(permission_config, dict)
        else (config.get("permissions", {}) if isinstance(config, dict) else {})
    )
    if not isinstance(raw_permission_config, dict):
        raw_permission_config = {}

    session_id = TOOL_PERMISSION_SESSION_ID.get() or None
    try:
        permission_config = compose_host_effective_permissions(
            session_id=session_id,
            global_permissions=raw_permission_config,
        )
    except Exception as exc:
        logger.warning(
            "[InterruptHelpers] compose_permissions_failed fallback=raw error=%s",
            exc,
        )
        permission_config = dict(raw_permission_config)

    logger.info(
        "[InterruptHelpers] build_permission_rail called: enabled=%s mode=%s sandbox_intent=%s",
        permission_config.get("enabled", False),
        permission_config.get("mode"),
        permission_config.get("sandbox_intent"),
    )

    if not permission_config.get("enabled", False):
        logger.info("[InterruptHelpers] Permission system is disabled, returning None")
        return None

    def _collect_optional_tool_tags(cfg: dict[str, Any]) -> list[str]:
        # openjiuwen PermissionInterruptRail 会拦截所有工具；
        # 这里的 tool_names 仅作为标签展示/日志辅助（尽量覆盖 tools + rules 声明）。
        names: set[str] = set()
        tools_cfg = cfg.get("tools") or {}
        if isinstance(tools_cfg, dict):
            for k in tools_cfg.keys():
                label = str(k).strip()
                if label:
                    names.add(label)
        for key in ("ask_tools", "deny_tools"):
            raw_list = cfg.get(key) or []
            if isinstance(raw_list, list):
                for item in raw_list:
                    if isinstance(item, str) and item.strip():
                        names.add(item.strip())
        rules = cfg.get("rules") or []
        if isinstance(rules, list):
            for entry in rules:
                if not isinstance(entry, dict):
                    continue
                raw_tools = entry.get("tools")
                if raw_tools is None:
                    continue
                if isinstance(raw_tools, str):
                    raw_tools = [raw_tools]
                if isinstance(raw_tools, list):
                    for item in raw_tools:
                        if isinstance(item, str) and item.strip():
                            names.add(item.strip())
        return sorted(names)

    tool_names = _collect_optional_tool_tags(permission_config)
    logger.info(
        "[InterruptHelpers] tools_config keys: %s, rail tool_names (with rules): %s",
        list((permission_config.get("tools") or {}).keys()),
        tool_names,
    )
    logger.info(
        "[InterruptHelpers] Building PermissionInterruptRail with tool_names=%s llm=%s model_name=%s",
        tool_names, llm is not None, model_name,
    )
    try:
        def _persist_allow_rule(permissions: dict[str, Any]) -> bool:
            """永久允许：写入 user_permissions.yaml（pattern / file_guard / allow_tools）。

            上游 rail 传整段合并 cfg（``tools`` map 与 ``file_guard.paths``
            为生效全集），persist_user_overlay_from_effective 只认增量键
            （``_file_guard_paths_added`` 全库无生产方，直传必落空段）。
            此处按磁盘现值 diff 出真增量，仅棘轮放大项入 user 层。
            """
            try:
                from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                    get_shipped_template_permissions_config,
                )
                from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
                    load_global_permissions,
                    load_user_permissions,
                )

                disk_bases = [
                    load_global_permissions(),
                    get_shipped_template_permissions_config(),
                    load_user_permissions(),
                ]
            except Exception:  # noqa: BLE001 — 底座读取失败退回直传
                logger.warning(
                    "[InterruptHelpers] persist_allow_rule disk base read failed",
                    exc_info=True,
                )
                return persist_user_overlay_from_effective(dict(permissions))

            sid = (TOOL_PERMISSION_SESSION_ID.get() or "").strip() or None
            approval_baseline: dict[str, Any] | None = None
            try:
                from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
                    compose_host_effective_permissions,
                )

                approval_baseline = compose_host_effective_permissions(session_id=sid)
            except Exception:  # noqa: BLE001 — 基线合成失败时丢弃 overrides 增量
                logger.warning(
                    "[InterruptHelpers] persist_allow_rule approval baseline compose failed",
                    exc_info=True,
                )

            payload = _derive_user_overlay_delta(
                permissions, disk_bases, approval_baseline=approval_baseline
            )
            if approval_baseline is None:
                # 基线不可用时无法区分 session 并入项，宁缺勿滥：
                # 不落 overrides，避免会话级放行被整表固化为永久。
                payload.pop("approval_overrides", None)
            return persist_user_overlay_from_effective(payload)

        def _persist_session_allow_rule(permissions: dict[str, Any]) -> bool:
            """会话内记住：按增量派生后写入 session_permissions.yaml。

            与 _persist_allow_rule 同理：rail 传整段合并 cfg，persist
            只认增量键（``_allow_tools_added`` / ``_file_guard_paths_added``），
            整段直传时 tools / file_guard 放行语义静默落空、假成功（检视
            2026-10-29）。基线取磁盘三层现值（global/template/session），
            仅棘轮放大项入 session 层。
            """
            sid = (
                (TOOL_PERMISSION_SESSION_ID.get() or "").strip()
                or str(permissions.get("_persist_session_id") or "").strip()
            )
            try:
                from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                    get_shipped_template_permissions_config,
                )
                from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
                    load_global_permissions,
                    load_session_permissions,
                )

                disk_bases = [
                    load_global_permissions(),
                    get_shipped_template_permissions_config(),
                    load_session_permissions(sid or ""),
                ]
            except Exception:  # noqa: BLE001 — 底座读取失败退回直传
                logger.warning(
                    "[InterruptHelpers] persist_session_allow_rule disk base read failed",
                    exc_info=True,
                )
                return persist_session_overlay_from_effective(
                    permissions,
                    session_id=sid or None,
                )
            approval_baseline: dict[str, Any] | None = None
            try:
                from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
                    compose_host_effective_permissions,
                )

                approval_baseline = compose_host_effective_permissions(
                    session_id=sid or None
                )
            except Exception:  # noqa: BLE001 — session 层写入无越层风险，退回整表
                logger.warning(
                    "[InterruptHelpers] persist_session_allow_rule approval baseline compose failed",
                    exc_info=True,
                )
            payload = _derive_user_overlay_delta(
                permissions, disk_bases, approval_baseline=approval_baseline
            )
            return persist_session_overlay_from_effective(
                payload,
                session_id=sid or None,
            )

        def _resolve_session_id(ctx: Any) -> str | None:
            session = getattr(ctx, "session", None)
            if session is None:
                return None
            for attr_name in ("get_session_id", "session_id"):
                attr = getattr(session, attr_name, None)
                try:
                    value = attr() if callable(attr) else attr
                except Exception:
                    value = None
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        async def _request_permission_confirmation(
            req: PermissionConfirmationRequest,
        ) -> PermissionConfirmResponse | str | None:
            channel = (TOOL_PERMISSION_CHANNEL_ID.get() or "web").strip() or "web"
            parent_session = resolve_subagent_permission_parent_session(req.ctx)
            # Hosted parent-card path: nested agent with own rail + different
            # session id (e.g. OfficeClaw worker). Only channels that render
            # chat.ask_user_question — never hijack ACP JSON-RPC.
            if (
                parent_session is not None
                and channel in _HOSTED_PERMISSION_CHANNELS
            ):
                return await request_subagent_hosted_permission_confirmation(
                    req,
                    parent_session=parent_session,
                )

            if channel != "acp":
                return "interrupt"

            session_id = _resolve_session_id(req.ctx)
            if not session_id:
                return None

            from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager

            tool_call = req.tool_call
            tool_name = getattr(tool_call, "name", "") if tool_call is not None else ""
            tool_args_raw = getattr(tool_call, "arguments", None) if tool_call is not None else None
            tool_call_id = str(getattr(tool_call, "id", "") or f"permission_{tool_name or 'tool'}").strip()
            descriptor = build_acp_tool_descriptor(
                tool_name,
                tool_args_raw,
                tool_call_id=tool_call_id,
                status="pending",
                kind="other",
            )
            title = str(descriptor.get("title") or f"Approve `{tool_name}`")
            if getattr(req.result, "reason", None):
                title = f"{title}: {req.result.reason}"

            request_params: dict[str, Any] = {
                "toolCall": {
                    **descriptor,
                    "title": title,
                },
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            }

            try:
                # ACP permission HITL must not consume StreamingToolExecutor.wait_all budget.
                from jiuwenswarm.openjiuwen_streaming_tool_patch import (
                    streaming_tool_wait_timeout_paused,
                )

                async with streaming_tool_wait_timeout_paused():
                    response = await get_acp_output_manager().send_jsonrpc_request(
                        "session/request_permission",
                        request_params,
                        session_id=session_id,
                    )
            except Exception as exc:
                logger.warning("[InterruptHelpers] ACP permission request failed: %s", exc)
                return None

            if not isinstance(response, dict):
                return None
            if isinstance(response.get("error"), dict):
                message = str(response["error"].get("message") or "Permission request failed")
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback=f"[PERMISSION_DENIED] {message}",
                )

            result_payload = response.get("result") if isinstance(response.get("result"), dict) else {}
            outcome = result_payload.get("outcome") if isinstance(result_payload.get("outcome"), dict) else {}
            outcome_kind = str(outcome.get("outcome") or "").strip().lower()
            option_id = str(outcome.get("optionId") or "").strip().lower()

            if outcome_kind == "selected":
                if option_id == "allow-once":
                    return PermissionConfirmResponse(approved=True, auto_confirm=False, feedback="")
                if option_id == "allow-always":
                    return PermissionConfirmResponse(approved=True, auto_confirm=True, feedback="")
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback="[PERMISSION_REJECTED] User rejected the request.",
                )

            if outcome_kind == "cancelled":
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback="[PERMISSION_REJECTED] Permission request was cancelled.",
                )
            return None

        async def _permission_scene_hook(
            inp: PermissionSceneHookInput,
        ) -> tuple[str, ...] | None:
            from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
                TOOL_PERMISSION_CONTEXT,
                check_avatar_permission,
                _resolve_owner_scope_level,
            )

            perm_ctx = TOOL_PERMISSION_CONTEXT.get()

            # issue #1976: ask_user has its own dedicated interrupt rail. The
            # permission rail intercepts *every* tool, so on resume it grabs the
            # ask_user answer (keyed by tool_call_id) as its own user_input,
            # fails to parse it as a ConfirmPayload, and re-raises a permission
            # interrupt that swallows the answer — the option card then re-pops
            # forever. Bypass the permission rail for ask_user so the ask_user
            # rail's answer reaches the model. The digital-avatar scene below
            # intentionally blocks interactive tools, so exclude it here.
            if inp.normalized_tool_name == "ask_user" and (
                perm_ctx is None
                or getattr(perm_ctx, "scene", None) != "group_digital_avatar"
            ):
                return ("approve",)

            # deepresearch_execute owns a sequence of native AskUser
            # interruptions under one outer tool call.  Once its initial tool
            # permission has been decided, structured card answers must reach
            # DeepResearchExecutionRail instead of being parsed again as a
            # ConfirmPayload by the permission rail.  Keep first-pass and
            # explicit permission responses on the normal permission path.
            workflow_input = inp.user_input
            is_deepresearch_workflow = (
                inp.normalized_tool_name == "deepresearch_execute"
                and isinstance(workflow_input, dict)
            )
            if is_deepresearch_workflow:
                status = workflow_input.get("status")
                if status in {
                    "answered",
                    "skipped",
                    "cancelled",
                    "error",
                } and isinstance(workflow_input.get("answers"), list):
                    return ("approve",)

            if perm_ctx is None:
                return None

            if getattr(perm_ctx, "scene", None) == "group_digital_avatar":
                if inp.user_input is not None:
                    return ("reject", "[PERMISSION_DENIED] 数字分身场景不支持交互审批")
                level = await check_avatar_permission(
                    inp.normalized_tool_name,
                    inp.tool_args,
                    channel_id=str(getattr(perm_ctx, "channel_id", "") or ""),
                    session_id=None,
                )
                if level == "allow":
                    return ("approve",)
                return ("reject", "[PERMISSION_DENIED] 该工具未被授权在数字分身场景下使用")

            principal_user_id = str(getattr(perm_ctx, "principal_user_id", "") or "").strip()
            channel_id = str(getattr(perm_ctx, "channel_id", "") or "").strip()
            if not principal_user_id or not channel_id:
                return None

            from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                get_effective_permissions_config,
            )

            perm_cfg = get_effective_permissions_config()
            owner_scopes = perm_cfg.get("owner_scopes") if isinstance(perm_cfg, dict) else None
            if not isinstance(owner_scopes, dict) or not owner_scopes:
                return None

            scope_cfg = (owner_scopes.get(channel_id) or {}).get(principal_user_id)
            owner_level = _resolve_owner_scope_level(
                scope_cfg, inp.normalized_tool_name, inp.tool_args
            )
            if owner_level is None:
                return None
            if owner_level == "allow":
                # 有 ACTIVE Skill overlay 时强制走 engine，避免 owner allow 绕过 overlay 收紧。
                if _has_active_skill_overlay(
                    inp.ctx, getattr(permission_rail, "_skill_scope_resolver", None),
                ):
                    return None
                return ("approve",)
            return ("reject", f"[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: {owner_level})")

        def _get_permissions_snapshot(
            session_id: str | None = None,
        ) -> dict[str, Any] | None:
            # 模板 body rail：与 base 一致返回 None → 引擎沿用 _static_config
            # （构建期已按模板 body compose），企业版 DeepAgent supervisor
            # Task 场景若回落全局 yaml 会以 enabled:false 覆盖模板配置。
            # effective 来源（标准 agent 跨进程 mode.set 链路）照旧走 compose。
            if has_template_body:
                return None
            sid = (
                (session_id or "").strip()
                or (TOOL_PERMISSION_SESSION_ID.get() or "").strip()
                or None
            )
            try:
                return compose_host_effective_permissions(session_id=sid)
            except Exception:
                # compose 失败回 None（引擎沿用构建期 _static_config，限制
                # 仍在）而非 {}——空快照会静默清空 deny/ask/rules（评审
                # 2026-10-29 fail-open）。
                logger.warning(
                    "[InterruptHelpers] permissions_snapshot_compose_failed",
                    exc_info=True,
                )
                return None

        host = ToolPermissionHost(
            **_filter_init_kwargs(
                ToolPermissionHost,
                get_permissions_snapshot=_get_permissions_snapshot,
                persist_allow_rule=_persist_allow_rule,
                persist_session_allow_rule=_persist_session_allow_rule,
                resolve_workspace_dir=(
                    resolve_workspace_dir or resolve_permission_workspace_dir
                ),
                permission_yaml_path=get_config_file(),
                request_permission_confirmation=_request_permission_confirmation,
                permission_scene_hook=_permission_scene_hook,
            )
        )

        # Skill 动态授权协调：默认权限 Rail 叠加 gate-handled 短路
        # （skill_tool/skill_complete 由 SkillAuthorizationRail 专属裁决，避免重复弹卡）。
        rail_cls = PermissionInterruptRail
        try:
            from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
                SkillAuthorizationPermissionRail,
            )

            rail_cls = SkillAuthorizationPermissionRail
        except Exception:
            logger.debug(
                "[InterruptHelpers] SkillAuthorizationPermissionRail switch failed, "
                "fallback to PermissionInterruptRail",
                exc_info=True,
            )

        # skill_turbo 启用时使用子类，定制 skill_acceleration_exec 审批消息
        # （SkillTurboPermissionRail 继承 SkillAuthorizationPermissionRail，两种定制叠加）
        try:
            from jiuwenswarm.common.config import get_config as _get_cfg
            _cfg = _get_cfg()
            _react = _cfg.get("react", {}) if isinstance(_cfg, dict) else {}
            _st = _react.get("skill_turbo", {}) if isinstance(_react, dict) else {}
            if _st.get("enabled", False):
                from jiuwenswarm.server.runtime.skill_turbo.rails.permission_rail import (
                    SkillTurboPermissionRail,
                )
                rail_cls = SkillTurboPermissionRail
                logger.info("[InterruptHelpers] Using SkillTurboPermissionRail")
        except Exception:
            logger.debug(
                "[InterruptHelpers] SkillTurboPermissionRail switch failed, "
                "fallback to PermissionInterruptRail",
                exc_info=True,
            )

        rail_factory = opts.rail_factory or rail_cls
        permission_rail = rail_factory(
            **_filter_init_kwargs(
                rail_cls,
                config=permission_config,
                tool_names=tool_names,
                llm=llm,
                model_name=model_name,
                host=host,
                sandbox_intent=permission_config.get("sandbox_intent"),
                permission_mode=permission_config.get("mode"),
            )
        )
        if getattr(permission_rail, "permission_mode", None) in (None, ""):
            permission_rail.permission_mode = permission_config.get("mode")
        if getattr(permission_rail, "sandbox_intent", None) in (None, ""):
            permission_rail.sandbox_intent = permission_config.get("sandbox_intent")
        logger.info(
            "[InterruptHelpers] PermissionInterruptRail created successfully with tool_names=%s mode=%s",
            tool_names,
            permission_config.get("mode"),
        )
    except Exception as exc:
        logger.warning("[InterruptHelpers] PermissionInterruptRail create failed: %s", exc)
        permission_rail = None
    return permission_rail



def _read_value_field(value_obj: Any, field_name: str, default: Any = "") -> Any:
    if hasattr(value_obj, field_name):
        return getattr(value_obj, field_name, default)
    if isinstance(value_obj, dict):
        return value_obj.get(field_name, default)
    return default


def _normalize_tool_args(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _is_ask_user_interrupt_value(value_obj: Any) -> bool:
    tool_name = str(_read_value_field(value_obj, "tool_name", "") or "").strip()
    if tool_name == "ask_user":
        return True
    if hasattr(value_obj, "payload_schema") and hasattr(value_obj, "questions"):
        return True
    if isinstance(value_obj, dict) and "payload_schema" in value_obj and "questions" in value_obj:
        return True
    return False


def _build_plain_ask_user_question(value_obj: Any) -> dict | None:
    """Build a free-text ask_user question when no structured options are present."""
    if not _is_ask_user_interrupt_value(value_obj):
        return None
    if _extract_questions_from_value(value_obj) is not None:
        return None

    query = ""
    tool_args = _normalize_tool_args(_read_value_field(value_obj, "tool_args", None))
    if isinstance(tool_args, dict):
        query = str(tool_args.get("query") or "").strip()
    if not query:
        query = str(_read_value_field(value_obj, "message", "") or "").strip()
    if not query:
        query = str(_read_value_field(value_obj, "question", "") or "").strip()
    if not query:
        return None

    return {
        "question": query,
        "header": "Question",
        "options": [],
        "multi_select": False,
    }


_PERMISSION_INTERRUPT_MARKERS = (
    "需要授权才能执行",
    "需要授权后才能使用",
    "检测到受保护的文件路径访问",
    "检测到需确认的网络访问",
    "检测到需确认的命令执行",
    "检测到风险命令结构",
    "操作需要授权",
    "requires permission",
    "Permission denied",
    "安全风险评估",
)
# exit_plan_mode uses PlanApprovalInterruptRail (extends ConfirmInterruptRail)
_CONFIRM_INTERRUPT_TOOLS = frozenset({"switch_mode", "exit_plan_mode"})  


def _read_interrupt_fields(value_obj: Any) -> tuple[str, str, dict | None]:
    """Return ``(tool_name, message, tool_args)`` from an interrupt value object."""
    tool_name = ""
    message = ""
    tool_args: dict | None = None

    if hasattr(value_obj, "tool_name"):
        tool_name = str(getattr(value_obj, "tool_name", "") or "").strip()
    if hasattr(value_obj, "message"):
        message = str(getattr(value_obj, "message", "") or "").strip()
    if not message and hasattr(value_obj, "question"):
        message = str(getattr(value_obj, "question", "") or "").strip()
    tool_args = _normalize_tool_args(getattr(value_obj, "tool_args", None))

    if isinstance(value_obj, dict):
        tool_name = tool_name or str(value_obj.get("tool_name", "") or "").strip()
        message = message or str(
            value_obj.get("message", "") or value_obj.get("question", "") or ""
        ).strip()
        if tool_args is None:
            tool_args = _normalize_tool_args(value_obj.get("tool_args"))

    return tool_name, message, tool_args


def _is_permission_interrupt_message(message: str, tool_name: str) -> bool:
    """Heuristic: PermissionInterruptRail copy vs ConfirmInterruptRail copy."""
    # Confirm 白名单必须最先判定（CR-6 扩展，评审 2026-10-30 Must
    # Fix）：exit_plan_mode/switch_mode 的中断来源由 rail 类型决定
    # （PlanApprovalInterruptRail 继承 ConfirmInterruptRail），文案
    # 不可信——ASK 前缀是宽松通用词（write/read/edit/list）；计划
    # 审批 message 还内嵌计划全文（任意自然文本，截断上限 3000），
    # "Permission denied"/"requires permission" 等 marker 是常见英
    # 文子串，任一先判都会把 confirm 中断误判为 permission_interrupt，
    # 计划「批准/拒绝」选项丢失，下游 OfficeAce relay 据此自动放行，
    # 绕过计划 HITL 审批门。
    if tool_name in _CONFIRM_INTERRUPT_TOOLS:
        return False
    normalized = message.strip()
    if any(marker in normalized for marker in _PERMISSION_INTERRUPT_MARKERS):
        return True
    if normalized.startswith("**工具 `") or normalized.startswith("**Tool `"):
        return True
    # 到达此处时 tool_name 必非 confirm 工具（白名单前置已 return）：
    # 非空 tool_name（含 ASK 摘要行等任意文案）一律判权限中断；
    # 空 tool_name 仅在空文案 / approve 话术时判 True。
    if tool_name:
        return True
    return normalized in {"", "Please approve or reject?"}


def _parse_plan_metadata_from_message(message: str) -> tuple[str, str]:
    plan_path = ""
    plan_slug = ""
    path_match = re.search(r"\*\*Plan file:\*\* `([^`]+)`", message)
    if path_match:
        plan_path = path_match.group(1).strip()
    slug_match = re.search(r"\*\*Plan id:\*\* `([^`]+)`", message)
    if slug_match:
        plan_slug = slug_match.group(1).strip()
    return plan_path, plan_slug


def _resolve_interrupt_source(tool_name: str, message: str) -> str:
    if _is_permission_interrupt_message(message, tool_name):
        return "permission_interrupt"
    return "confirm_interrupt"


def convert_interactions_to_ask_user_question(state_outputs: list) -> dict | None:
    """Convert __interaction__ list to frontend chat.ask_user_question format.

    AskUserRail 中断: value 有 questions 字段，或 ask_user 的 plain query
        → source="ask_user_interrupt"
    PermissionRail 中断: value 无 questions 字段 → source="permission_interrupt"
    ConfirmInterruptRail 中断: 控制类工具确认 → source="confirm_interrupt"

    state_outputs 中的元素可能是:
    - InteractionOutput 对象 (有 id, value 属性, value 是 ToolCallInterruptRequest)
    - dict (有 id, value 键)
    """
    if not state_outputs:
        return None

    interactions = list(_iter_interactions(state_outputs))
    if not interactions:
        return None

    # A controller output can contain both a permission interrupt shell and the
    # real ask_user interrupt. Prefer the structured ask_user payload; otherwise
    # the frontend may receive an empty permission prompt and have no request_id
    # to resume the waiting tool call.
    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        questions_raw = _extract_questions_from_value(value_obj)
        if questions_raw is None:
            continue

        questions = _build_multi_questions(questions_raw)
        return {
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "questions": questions,
            "source": "ask_user_interrupt",
        }

    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        plain_question = _build_plain_ask_user_question(value_obj)
        if plain_question:
            return {
                "event_type": "chat.ask_user_question",
                "request_id": request_id,
                "questions": [plain_question],
                "source": "ask_user_interrupt",
            }

    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        question_data = extract_question_from_interaction(interaction)
        if not question_data:
            continue

        tool_name, message, _tool_args = _read_interrupt_fields(value_obj)
        source = _resolve_interrupt_source(tool_name, message)

        payload = {
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "questions": [question_data],
            "source": source,
        }
        # Skill 动态授权审批卡：随事件顶层透传给前端结构化渲染（契约键冻结）。
        payload_schema = _read_value_field(value_obj, "payload_schema", None)
        if isinstance(payload_schema, dict):
            from openjiuwen.harness.security.skill_authorization import (
                SKILL_APPROVAL_CARD_EXTENSION_KEY,
            )

            card = payload_schema.get(SKILL_APPROVAL_CARD_EXTENSION_KEY)
            if isinstance(card, dict):
                payload[SKILL_APPROVAL_CARD_EXTENSION_KEY] = card
        if (
            source == "confirm_interrupt"
            and tool_name == "exit_plan_mode"
            and is_plan_approval_message(message)
        ):
            plan_content, plan_language = extract_plan_approval_content(message)
            resolved_plan_language = "en" if plan_language == "en" else "cn"
            payload["plan_content"] = plan_content
            payload["plan_language"] = resolved_plan_language
            payload["plan_approval_kind"] = "plan_approval"
            # Web 用的动作说明。TUI 忽略该字段，继续使用 questions[].options 的
            # approve / reject，因此两端行为互不影响。
            payload["plan_actions"] = build_plan_approval_actions(resolved_plan_language)
        plan_path = str(question_data.get("plan_path") or "").strip()
        plan_slug = str(question_data.get("plan_slug") or "").strip()
        if plan_path:
            payload["plan_path"] = plan_path
        if plan_slug:
            payload["plan_slug"] = plan_slug
        structured_approval = _classify_structured_approval(value_obj, question_data)
        if structured_approval:
            payload.update(structured_approval)
        return payload

    return None


def _iter_interactions(state_outputs: list) -> Any:
    """Yield interaction objects, flattening nested interaction lists."""
    for interaction in state_outputs:
        if isinstance(interaction, (list, tuple)):
            yield from _iter_interactions(list(interaction))
        else:
            yield interaction


def _extract_interaction_parts(interaction: Any) -> tuple[str, Any]:
    """Return ``(request_id, value)`` for dict or InteractionOutput-like objects."""
    if hasattr(interaction, "id"):
        request_id = getattr(interaction, "id", "")
        value_obj = interaction.value
    elif isinstance(interaction, dict):
        request_id = interaction.get("id", "")
        value_obj = interaction.get("value", {})
    else:
        return "", None

    return str(request_id or "").strip(), value_obj


def _extract_questions_from_value(value_obj: Any) -> list | None:
    """从 value 对象中提取 questions 列表.

    AskUserRail 的 value (ToolCallInterruptRequest) 有 questions 属性.
    如果 questions 存在且非空, 返回列表; 否则返回 None 表示不是 AskUserRail 中断.

    Additional source: StructuredAskUserRail puts `questions` in the tool call
    arguments, which are preserved in ToolCallInterruptRequest.tool_args.
    """
    # 1. Direct questions attribute on value_obj
    if hasattr(value_obj, 'questions'):
        qs = value_obj.questions
        if qs and len(qs) > 0:
            return qs
    elif isinstance(value_obj, dict):
        qs = value_obj.get("questions", [])
        if qs and len(qs) > 0:
            return qs

    # 2. questions embedded in tool_args (StructuredAskUserRail path)
    # ToolCallInterruptRequest.tool_args preserves the original tool call
    # arguments, including the `questions` parameter. task_interaction 载荷
    # 到达时值已是 model_dump 的 dict,tool_args 是键而非属性。
    tool_args = getattr(value_obj, 'tool_args', None)
    if tool_args is None and isinstance(value_obj, dict):
        tool_args = value_obj.get('tool_args')
    if tool_args is not None:
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except (ValueError, TypeError):
                pass
        if isinstance(tool_args, dict):
            qs = tool_args.get("questions", [])
            if qs and len(qs) > 0:
                return qs

    return None


def _build_multi_questions(questions_data: list) -> list:
    """Build frontend PendingQuestionItem list from questions data.

    有选项的问题: 保留原始选项 + 追加 __other__ (自定义输入)
    无选项的问题: 不追加 __other__, 前端应直接进入自由输入模式
    """
    questions = []
    for q in questions_data:
        raw_options = q.get("options", [])
        # Non-array options (e.g. "a,b") must not be iterated as characters (#2331).
        if not isinstance(raw_options, list):
            raw_options = []
        if raw_options:
            options = [_normalize_question_option(opt) for opt in raw_options if isinstance(opt, dict)]
            options.append({"label": "Other", "description": "Custom input"})
        else:
            options = []
        question_payload = {
            "question": q["question"],
            "header": q.get("header") or "Question",
            "options": options,
            "multi_select": q.get("multi_select", False),
        }
        preview = _normalize_question_preview(q.get("preview"))
        if preview is not None:
            question_payload["preview"] = preview
        questions.append(question_payload)
    return questions


def _extract_ui_options(value_obj: Any) -> list[dict[str, Any]]:
    options = getattr(value_obj, "ui_options", None) if hasattr(value_obj, "ui_options") else None
    if options is None and isinstance(value_obj, dict):
        options = value_obj.get("ui_options")
    return [item for item in options or [] if isinstance(item, dict)]


def _extract_tool_name(value_obj: Any) -> str:
    if hasattr(value_obj, "tool_name"):
        return str(getattr(value_obj, "tool_name", "") or "")
    if isinstance(value_obj, dict):
        return str(value_obj.get("tool_name") or "")
    return ""


def _extract_interrupt_metadata(value_obj: Any) -> dict[str, Any]:
    metadata = getattr(value_obj, "metadata", None)
    if metadata is None and isinstance(value_obj, dict):
        metadata = value_obj.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _normalize_question_option(option: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "label": str(option.get("label") or option.get("value") or "").strip(),
        "description": str(option.get("description") or "").strip(),
    }
    value = option.get("value")
    if isinstance(value, str) and value:
        normalized["value"] = value
    preview = option.get("preview")
    if isinstance(preview, str) and preview.strip():
        normalized["preview"] = preview
    return normalized


def _normalize_question_preview(preview: Any) -> dict[str, Any] | None:
    if not isinstance(preview, dict):
        return None
    text = preview.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    normalized: dict[str, Any] = {"text": text}
    for field in ("title", "outline_ref"):
        value = preview.get(field)
        if isinstance(value, str):
            normalized[field] = value
    if preview.get("format") == "markdown":
        normalized["format"] = "markdown"
    editable = preview.get("editable")
    if isinstance(editable, bool):
        normalized["editable"] = editable
    meta = preview.get("meta")
    if isinstance(meta, dict):
        normalized["meta"] = dict(meta)
    return normalized


_PERMANENT_REMEMBER_LABELS = frozenset({
    "永久记住",
    "总是允许",  # OfficeClaw PermissionBridge global scope 回传标签
    "Always Allow",
    "always_allow",
    "allow_always",
})

_PERMANENT_REMEMBER_HINT_RE = re.compile(
    r"[；;]?\s*选择「永久记住」[^。\n]*[。.]?",
)


def _default_interrupt_options() -> list[dict[str, str]]:
    """权限审批默认选项。

    企业版不提供「永久记住」：该能力依赖写回本地 config.yaml / session overlay，
    与 Agent permissions 模板权威源不一致，且当前 supervisor Task 下 session_id
    常丢失导致落盘失败。长期策略请改 Manager permissions 模板。
    """
    options = [
        {"label": "本次允许", "description": "仅本次授权执行"},
        {"label": "本会话内允许", "description": "本次会话内自动放行同类操作"},
        {"label": "拒绝", "description": "拒绝执行此工具"},
    ]
    if not is_enterprise():
        options.insert(
            2,
            {"label": "永久记住", "description": "写回磁盘，所有会话均自动放行"},
        )
    return options


def _is_permanent_remember_option(option: dict[str, Any]) -> bool:
    label = str(option.get("label") or "").strip()
    value = str(option.get("value") or "").strip()
    return label in _PERMANENT_REMEMBER_LABELS or value in _PERMANENT_REMEMBER_LABELS


def _strip_permanent_remember_hint(message: str) -> str:
    cleaned = _PERMANENT_REMEMBER_HINT_RE.sub("", message or "")
    return cleaned.strip()


def _plan_approval_interrupt_options(
    source: str,
    tool_name: str,
    message: str,
) -> list[dict[str, str]] | None:
    if not (
        source == "confirm_interrupt"
        and tool_name == "exit_plan_mode"
        and is_plan_approval_message(message)
    ):
        return None
    return build_plan_approval_options_from_message(message)


def _question_options_from_ui_options(
    value_obj: Any,
    source: str,
    tool_name: str,
    message: str,
) -> list[dict[str, Any]]:
    options = []
    for option in _extract_ui_options(value_obj):
        normalized = _normalize_question_option(option)
        if normalized["label"]:
            options.append(normalized)
    if options:
        # 仅过滤权限审批弹窗；技能演进等仍可保留「总是允许」。
        if is_enterprise() and source == "permission_interrupt":
            options = [
                option for option in options
                if not _is_permanent_remember_option(option)
            ]
        return options
    return _plan_approval_interrupt_options(source, tool_name, message) or _default_interrupt_options()


def _classify_structured_approval(
    value_obj: Any,
    question_data: dict[str, Any],
) -> dict[str, Any] | None:
    del question_data
    metadata = _extract_interrupt_metadata(value_obj)
    source = str(metadata.get("source") or "").strip()
    interrupt_kind = str(metadata.get("interrupt_kind") or "").strip()
    tool_name = _extract_tool_name(value_obj)

    is_evolution_interrupt = (
        source in EVOLUTION_INTERRUPT_METADATA_SOURCES
        or interrupt_kind == LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE
    )
    if not is_evolution_interrupt and tool_name not in SKILL_EVOLUTION_APPROVAL_TOOL_KINDS:
        return None
    approval_kind = str(metadata.get("approval_kind") or "").strip()
    if approval_kind not in {"evolve", "simplify"}:
        approval_kind = SKILL_EVOLUTION_APPROVAL_TOOL_KINDS.get(tool_name, "evolve")

    payload: dict[str, Any] = {
        "source": EVOLUTION_INTERRUPT_SOURCE,
        "approval_kind": approval_kind,
    }
    evolution_context = str(metadata.get("evolution_context") or "").strip()
    if evolution_context in {"agent", "team"}:
        payload["evolution_context"] = evolution_context
    return payload


def extract_question_from_interaction(payload: Any) -> dict | None:
    """Extract question info from a single interaction payload.

    Args:
        payload: InteractionOutput instance or dict

    Returns:
        Question format dict for frontend
    """
    if payload is None:
        return None

    if hasattr(payload, "value"):
        value_obj = payload.value
    elif isinstance(payload, dict):
        value_obj = payload.get("value", payload)
    else:
        return None

    tool_name, message, tool_args = _read_interrupt_fields(value_obj)
    source = _resolve_interrupt_source(tool_name, message)

    generic_confirm_message = message.strip() in {"", "Please approve or reject?"}
    needs_message = not message or (source == "confirm_interrupt" and generic_confirm_message)
    if tool_name and needs_message:
        if source == "confirm_interrupt":
            from jiuwenswarm.agents.harness.code.rails.code_confirm_interrupt_rail import (
                build_confirm_interrupt_message,
            )

            message = build_confirm_interrupt_message(tool_name, tool_args or {})
        elif not message:
            message = f"工具 `{tool_name}` 需要授权才能执行"

    plan_approval_options = _plan_approval_interrupt_options(source, tool_name, message)
    if plan_approval_options:
        header = "Exit Plan and Execute"
        question = strip_inline_plan_approval_choices(message)
    elif source == "confirm_interrupt":
        header = f"操作确认: {tool_name}" if tool_name else "操作确认"
        question = message
    else:
        metadata = _extract_interrupt_metadata(value_obj)
        ask_title = str(metadata.get("ask_title") or "").strip()
        header = ask_title or (f"权限审批: {tool_name}" if tool_name else "权限审批")
        question = message
        if source == "permission_interrupt":
            question = apply_permission_intent(question, tool_name, tool_args)
        if is_enterprise() and source == "permission_interrupt":
            question = _strip_permanent_remember_hint(question)

    return {
        "question": question,
        "header": header,
        "options": _question_options_from_ui_options(value_obj, source, tool_name, message),
        "multi_select": False,
    }
