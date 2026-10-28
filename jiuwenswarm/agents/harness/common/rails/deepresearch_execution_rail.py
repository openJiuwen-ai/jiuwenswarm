# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native HITL bridge for the high-level DeepResearch execution tool."""

from __future__ import annotations

import copy
import inspect
import json
import logging
import time
from typing import Any, Mapping

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRequest

from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
    _IN_FLIGHT_PHASES,
    _query_matches_stopped_claim,
    EXECUTION_SCHEMA,
    MODEL_RESEARCH_HANDOFF_ERRORS,
    bind_deepresearch_execution_context,
    reset_deepresearch_execution_context,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import (
    _probe_resumable_checkpoint,
)
from jiuwenswarm.common.schema.ask_user import ask_user_response_schema
from jiuwenswarm.perf.context import (
    extract_session_id_from_callback,
    get_request_context,
)

DEEPRESEARCH_EXECUTION_STATE_KEY = "deepresearch_execution_states"
DEEPRESEARCH_EXECUTION_ALIAS_KEY = "deepresearch_execution_aliases"
DEEPRESEARCH_DELIVERY_INDEX_KEY = "deepresearch_delivery_index"
_TOKENS_KEY = "__deepresearch_execution_context_tokens__"
_TOOL_NAME = "deepresearch_execute"
_BRIEF_REFRESH_SECTION = "deepresearch_brief_refresh"
_BRIEF_REFRESH_INSTRUCTION = (
    "本轮已由服务端校验为 refresh_latest_brief：补充证据后重新交付精简报告。"
    "加载 deepresearch 技能，然后调用一次 deepresearch_execute，query 使用本轮用户的补充要求，"
    "report_type='brief'、upgrade_from_brief=False；执行工具会核对来源报告并读取原报告。"
    "必须生成并交付包含补充数据、事实依据和来源的新版精简报告文件，不要只在聊天里回答。"
    "用户所说的‘不需要重新生成完整报告’指不生成专业版，不禁止生成新版精简报告。"
    "不要升级专业版、不要另建 todo，也不要重复发送工具已交付的报告。"
)

# HITL 暂停占位文案（interaction 分支写入 tool_msg）。必须注册进
# StreamEventRail 的占位模板白名单（_is_legacy_tool_interrupt_placeholder_text）：
# resume 完成后占位与 completed tool_msg 挂同一原始 tool_call_id，
# 白名单识别使孤儿清理按"占位被真实结果替换"收敛，而非 duplicate
# 规则保留占位删掉完成态（后者会让历史停留在"等待答复"）。
DEEPRESEARCH_INTERACTION_PLACEHOLDER = (
    "DeepResearch 已暂停并向用户发起结构化问答，正在等待用户答复。"
    "用户答复后本工具将自动恢复执行并返回后续结果；请勿代替用户"
    "作答，也无需重复调用本工具轮询进度。"
)

logger = logging.getLogger(__name__)


def _tool_name(ctx: AgentCallbackContext) -> str:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return ""
    return str(
        ctx.inputs.tool_name
        or getattr(ctx.inputs.tool_call, "name", "")
        or ""
    ).strip()


def _tool_call_id(ctx: AgentCallbackContext) -> str:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return ""
    return str(getattr(ctx.inputs.tool_call, "id", "") or "").strip()


def _load_states(session: Any) -> dict[str, dict[str, Any]]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_EXECUTION_STATE_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): dict(state)
        for key, state in value.items()
        if isinstance(state, Mapping)
    }


def _save_states(session: Any, states: dict[str, dict[str, Any]]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_EXECUTION_STATE_KEY: states})


def _load_aliases(session: Any) -> dict[str, str]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_EXECUTION_ALIAS_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(workflow_id)
        for key, workflow_id in value.items()
        if str(key).strip() and str(workflow_id).strip()
    }


def _save_aliases(session: Any, aliases: dict[str, str]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_EXECUTION_ALIAS_KEY: aliases})


def _normalize_topic(query: str) -> str:
    return " ".join(str(query or "").strip().split()).lower()


def _load_delivery_index(session: Any) -> dict[str, dict[str, Any]]:
    if session is None:
        return {}
    value = session.get_state(DEEPRESEARCH_DELIVERY_INDEX_KEY)
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): dict(entry)
        for key, entry in value.items()
        if isinstance(entry, Mapping)
    }


def _save_delivery_index(session: Any, idx: dict[str, dict[str, Any]]) -> None:
    if session is not None:
        session.update_state({DEEPRESEARCH_DELIVERY_INDEX_KEY: idx})


def _record_delivery(
    session: Any,
    workflow_id: str,
    *,
    query: str,
    file_name: str,
    conversation_id: str,
    html_style_status: str | None,
) -> None:
    idx = _load_delivery_index(session)
    normalized_query = _normalize_topic(query)
    fname = str(file_name or "").strip()
    # 空键不入索引：空 query/file_name 的条目会误拦后续合法空参调用（issue #5131 审查）
    if not normalized_query or not fname:
        return
    idx[workflow_id] = {
        "query": normalized_query,
        "file_name": fname,
        "conversation_id": str(conversation_id or ""),
        "html_style_status": html_style_status,
        "delivered_at": time.time(),
    }
    _save_delivery_index(session, idx)
    logger.info(
        "[deepresearch_execution_rail] recorded delivery workflow_id=%s "
        "conversation_id=%s",
        workflow_id,
        str(conversation_id or ""),
    )


def _lookup_delivery(
    session: Any, *, query: str, file_name: str
) -> dict[str, Any] | None:
    """同会话×同主题×同 file_name 已交付则返回交付条目，否则 None。

    主题按 _normalize_topic 归一化后比较（两侧均归一化），消除大小写/空白差异。
    空 query 或空 file_name 不参与匹配——空键不应拦截合法研究（issue #5131 审查）。
    """
    topic = _normalize_topic(query)
    fname = str(file_name or "").strip()
    if not topic or not fname:
        return None
    for entry in _load_delivery_index(session).values():
        if (
            _normalize_topic(str(entry.get("query") or "")) == topic
            and str(entry.get("file_name") or "").strip() == fname
        ):
            return dict(entry)
    return None


def _workflow_id(session: Any, tool_call_id: str) -> str:
    return _load_aliases(session).get(tool_call_id, tool_call_id)


def _history_tool_call_id(tool_call_id: str, workflow_id: str) -> str:
    """tool_msg 应挂的历史 tool_call_id：须与 assistant 消息 tool_calls
    的原始 id 对齐，否则被 StreamEventRail 孤儿清理删除。

    resume 执行时 ctx.inputs.tool_call 是 TIE 克隆件，id 已被改写为
    interaction_id（{workflow_id}_interaction_N），须映射回原始 id。
    bridge 场景（模型换新 id 重试被桥接旧 state）例外：本次调用 id
    本身就是历史 id，原样保留——故按 interaction 后缀模式判断，而非
    "是否在 aliases 中"（bridge 的 alias 不带该后缀）。
    """
    if workflow_id and tool_call_id.startswith(f"{workflow_id}_interaction_"):
        return workflow_id
    return tool_call_id


# 修复点3.2：error 相位入桥候选。取消转译的 stopped 写入（phase=new）
# 被同 revision 的 error 覆盖写吞掉时，旧 state 实际存有可恢复停止快照，
# error 候选仍可桥回复用。cancelled 是用户显式放弃的死终态，不入候选。
_BRIDGE_CANDIDATE_PHASES = _IN_FLIGHT_PHASES | {"new", "error"}


def _tool_args(ctx: AgentCallbackContext) -> dict[str, Any]:
    if not isinstance(ctx.inputs, ToolCallInputs):
        return {}
    args = getattr(ctx.inputs.tool_call, "arguments", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return {}
    return dict(args) if isinstance(args, Mapping) else {}


def _bridge_stale_workflow(
    session: Any,
    tool_call_id: str,
    states: Mapping[str, Mapping[str, Any]],
    query: str,
    conversation_id: str,
) -> str:
    """state miss 兜底桥：新 tool_call_id 桥回旧 workflow state。

    硬中断（用户停止/进程被杀）会把 in-flight state 遗留在旧
    tool_call_id 下；模型重试换了新 tool_call_id 时 state 读不到，
    昂贵研究会被全新重跑。这里过五守卫 + 探针门后写 alias 桥接
    新旧 id，使本次调用复用旧 state 直通续跑。返回桥接到的
    workflow_id，未桥接返回空串。

    守卫：① state miss（调用方保证）；② 候选 phase ∈ in-flight ∪
    new ∪ error（wait_feedback/wait_outline 卡由交互卡 id 自带 alias，
    不入候选；cancelled 是显式放弃的死终态，不入候选）；③ query 与
    停止认领同口径；④ 显式 conversation_id 不抢别的会话；⑤ 过滤后
    唯一。探针门：in-flight 与 error 候选须存在优雅停止快照（in-flight
    防活子进程交叉写坏；error 可能是子进程失败退出无快照可恢复）才桥；
    new 相位候选免门（停止终态已返回，子进程必已退出）。
    """
    if tool_call_id in states:
        return ""
    incoming_cid = str(conversation_id or "").strip()
    candidates: list[tuple[str, Mapping[str, Any], str]] = []
    for workflow_id, state in states.items():
        phase = str(state.get("phase") or "")
        if phase not in _BRIDGE_CANDIDATE_PHASES:
            continue
        if not _query_matches_stopped_claim(query, state):
            continue
        state_cid = str(state.get("conversation_id") or "").strip()
        if incoming_cid and incoming_cid != state_cid:
            continue
        candidates.append((workflow_id, state, phase))
    verified: list[tuple[str, Mapping[str, Any], str]] = []
    for workflow_id, state, phase in candidates:
        if phase != "new":
            state_cid = str(state.get("conversation_id") or "").strip()
            probe = _probe_resumable_checkpoint(state_cid) if state_cid else None
            if not (isinstance(probe, Mapping) and probe.get("stopped")):
                logger.warning(
                    "[deepresearch_execution_rail] bridge rejected: "
                    "candidate state has no stopped snapshot; "
                    "tool_call_id=%s workflow_id=%s phase=%s",
                    tool_call_id,
                    workflow_id,
                    phase,
                )
                continue
        verified.append((workflow_id, state, phase))
    if not verified:
        return ""
    if len(verified) > 1:
        logger.warning(
            "[deepresearch_execution_rail] bridge rejected: multiple "
            "candidate states; tool_call_id=%s candidates=%s",
            tool_call_id,
            sorted(workflow_id for workflow_id, _, _ in verified),
        )
        return ""
    workflow_id, state, phase = verified[0]
    aliases = _load_aliases(session)
    aliases[tool_call_id] = workflow_id
    _save_aliases(session, aliases)
    logger.info(
        "[deepresearch_execution_rail] bridged tool_call_id=%s to stale "
        "workflow_id=%s phase=%s conversation_id=%s",
        tool_call_id,
        workflow_id,
        phase,
        str(state.get("conversation_id") or ""),
    )
    return workflow_id


def _interaction_tool_call(tool_call: Any, interaction_id: str) -> Any:
    model_copy = getattr(tool_call, "model_copy", None)
    cloned = model_copy(deep=True) if callable(model_copy) else copy.deepcopy(tool_call)
    cloned.id = interaction_id
    return cloned


def _clone_tool_call_with_id(tool_call: Any, new_id: str) -> Any:
    model_copy = getattr(tool_call, "model_copy", None)
    cloned = model_copy(deep=True) if callable(model_copy) else copy.deepcopy(tool_call)
    cloned.id = new_id
    return cloned


def _resume_input(ctx: AgentCallbackContext, tool_call_id: str) -> Any:
    value = getattr(ctx, "extra", {}).get(RESUME_USER_INPUT_KEY)
    if isinstance(value, InteractiveInput):
        user_inputs = value.user_inputs
        if tool_call_id in user_inputs:
            return user_inputs[tool_call_id]
        # 键断裂兜底：作答可能挂在历史卡片 id 下（如优雅停止前子进程
        # 下发的 ask_user 卡）。恢复会话时 pending 中断调用通常只有本
        # 工具一个；若作答恰好唯一则取之，避免确认丢失被误判为无效
        # 结果（"AskUser response must be an object"）。
        if len(user_inputs) == 1:
            only_key = next(iter(user_inputs))
            logger.warning(
                "[deepresearch_execution_rail] resume key mismatch: "
                "expected=%s got=%s; using the only user input",
                tool_call_id,
                only_key,
            )
            return user_inputs[only_key]
        return None
    if isinstance(value, Mapping) and tool_call_id in value:
        return value[tool_call_id]
    return value


def _result_payload(value: Any) -> dict[str, Any] | None:
    detailed_output = getattr(value, "detailed_output", None)
    if isinstance(detailed_output, Mapping):
        value = detailed_output
    return dict(value) if isinstance(value, Mapping) else None


class DeepResearchExecutionRail(DeepAgentRail):
    """Persist the workflow state and turn tool envelopes into native HITL."""

    # Higher priorities run first. Convert the envelope before
    # JiuSwarmStreamEventRail(priority=80), so the stream rail sees the native
    # interrupt and does not leak the internal envelope as a tool result.
    priority = 81

    def __init__(self, *, model_provider, materials_context_provider=None, mcp_context_provider=None):
        super().__init__()
        self._model_provider = model_provider
        self._materials_context_provider = materials_context_provider
        self._mcp_context_provider = mcp_context_provider
        self._agent_id = "jiuwenswarm"
        self.system_prompt_builder: Any = None

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        card = getattr(agent, "card", None)
        self._agent_id = str(
            getattr(card, "id", "")
            or getattr(card, "name", "")
            or "jiuwenswarm"
        ).strip()

    def uninit(self, agent: Any) -> None:
        builder = getattr(agent, "system_prompt_builder", None) or self.system_prompt_builder
        if builder is not None:
            builder.remove_section(_BRIEF_REFRESH_SECTION)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = getattr(getattr(ctx, "agent", None), "system_prompt_builder", None)
        if builder is not None:
            self.system_prompt_builder = builder
        builder = self.system_prompt_builder
        if builder is None:
            return
        # Agent instances can be reused: refresh instructions belong to one request.
        builder.remove_section(_BRIEF_REFRESH_SECTION)
        request_context = get_request_context(
            session_id=extract_session_id_from_callback(ctx)
        )
        followup = (request_context or {}).get("deepresearch_followup")
        if not isinstance(followup, Mapping) or followup.get("action") != "refresh_latest_brief":
            return
        language = getattr(builder, "language", "cn") or "cn"
        builder.add_section(PromptSection(
            name=_BRIEF_REFRESH_SECTION,
            content={language: _BRIEF_REFRESH_INSTRUCTION},
            priority=121,
        ))

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        tool_call_id = _tool_call_id(ctx)
        workflow_id = _workflow_id(ctx.session, tool_call_id)
        states = _load_states(ctx.session)
        request_context = get_request_context(
            session_id=extract_session_id_from_callback(ctx)
        )
        deepresearch_followup = (request_context or {}).get("deepresearch_followup")
        args = _tool_args(ctx)
        is_upgrade = deepresearch_followup is not None or args.get("upgrade_from_brief") is True
        if workflow_id not in states and not is_upgrade:
            # state miss 兜底桥：硬中断丢失 conversation_id 后模型换新
            # tool_call_id 重试时，桥回旧 workflow state 直通续跑。
            workflow_id = _bridge_stale_workflow(
                ctx.session,
                tool_call_id,
                states,
                query=str(args.get("query") or ""),
                conversation_id=str(args.get("conversation_id") or ""),
            ) or workflow_id

        def save_state(state: dict[str, Any]) -> None:
            current = _load_states(ctx.session)
            current[workflow_id] = dict(state)
            _save_states(ctx.session, current)

        # 幂等守卫：同会话×同主题×同 file_name 已交付则短路返回「已完成」，
        # 不进入 bind_deepresearch_execution_context，阻止报告交付后重复执行（issue #5131）。
        # workflow_id not in states：进行中 workflow 的续跑不走短路，避免吞掉用户答复
        # 与残留 state（atomgit-bot 审查 Issue 2）。
        # not is_upgrade：升级/追问会用 original_query 覆盖本次 query，与已交付 brief
        # 条目同键，命中短路会把「升级专业版」当成已完成吞掉（renquanjia 审查 CR-005）。
        hit = _lookup_delivery(
            ctx.session,
            query=str(args.get("query") or ""),
            file_name=str(args.get("file_name") or ""),
        )
        if hit is not None and not is_upgrade and workflow_id not in states:
            from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
                _completion_content,
                _persist_delivered_state,
                _result,
            )

            completion_text = _completion_content(
                {"file_name": str(hit.get("file_name") or "")},
                hit.get("html_style_status"),
            )
            short_state = _persist_delivered_state(
                ctx,
                workflow_id=workflow_id,
                conversation_id=str(hit.get("conversation_id") or ""),
                query=str(args.get("query") or ""),
                file_name=str(hit.get("file_name") or ""),
                requested_report_type=str(args.get("report_type") or "brief"),
            )
            ctx.inputs.tool_result = _result(
                "completed",
                short_state,
                content=completion_text,
            )
            ctx.inputs.tool_msg = ToolMessage(
                content=completion_text,
                tool_call_id=tool_call_id,
            )
            ctx.request_force_finish(
                {"output": completion_text, "result_type": "answer"}
            )
            logger.info(
                "[deepresearch_execution_rail] idempotent short-circuit "
                "tool_call_id=%s conversation_id=%s",
                tool_call_id,
                str(hit.get("conversation_id") or ""),
            )
            # 跳过工具方法体靠上面的 request_force_finish：@rail wrapper 在 before
            # 钩子之后检查 has_force_finish_request 并直接 return None
            # （openjiuwen/core/single_agent/rail/base.py:613-615）。因此不能写共享
            # ctx.extra["_skip_tool"]——该标记由 AbilityManager 按名 pop
            # （ability_manager.py:653），本调用根本不走到 pop，标记会残留并被同轮
            # 并行 tool call 误消费（工具静默返回空结果）。
            # 短路标记挂在 per-tool-call 的 ctx 上（同 flash 选择器不用共享标记的
            # 做法），供 after_tool_call early-return（openJiuwen-bot 审查 Issue 9）。
            ctx._deepresearch_short_circuit = workflow_id
            return

        model = self._model_provider() if callable(self._model_provider) else None
        request_id = str((request_context or {}).get("request_id") or "").strip()
        requested_report_type = (request_context or {}).get("requested_report_type")
        mcp_servers = self._mcp_context_provider() if callable(self._mcp_context_provider) else None
        if inspect.isawaitable(mcp_servers):
            mcp_servers = await mcp_servers
        token = bind_deepresearch_execution_context(
            tool_call_id=workflow_id,
            state=states.get(workflow_id),
            user_input=_resume_input(ctx, tool_call_id),
            model=model,
            save_state=save_state,
            agent_id=self._agent_id,
            request_id=request_id,
            requested_report_type=requested_report_type,
            deepresearch_followup=deepresearch_followup,
            materials_context=(
                self._materials_context_provider() if callable(self._materials_context_provider) else None
            ),
            mcp_servers=mcp_servers,
            mcp_context_provider=self._mcp_context_provider,
            load_state=lambda: _load_states(ctx.session).get(workflow_id),
        )
        # B: 打断续跑时 harness 用 interaction_id 续跑，终端 chat.tool_result 会携带
        # interaction_id，原始 tool_call_id 的 chat.tool_call 永远等不到配对结果，
        # open_calls/pending 泄漏 -> 下一轮 LLM 误判「研究未完成」触发重弹问题/重跑。
        # 这里把 tool_call.id 重写回 workflow_id（经 alias 解析的原始 id），使
        # StreamEventRail(priority=80) 发出的 chat.tool_call/chat.tool_result 都对齐原始 id。
        # bridge 场景（模型换新 id 重试被桥接旧 state）例外：本次调用 id 本身就是
        # 历史 id，原样保留——按 interaction 后缀模式判断，而非"是否在 aliases 中"
        # （bridge 的 alias 不带该后缀），否则 tool_msg.tool_call_id 会被改写成旧
        # workflow_id 而与 assistant 消息 tool_calls 的新 id 不匹配（issue #5131 回归）。
        original_workflow_id = workflow_id
        if (
            tool_call_id != original_workflow_id
            and tool_call_id.startswith(f"{original_workflow_id}_interaction_")
        ):
            try:
                ctx.inputs.tool_call.id = original_workflow_id
            except (AttributeError, TypeError):
                # 不可变 tool_call 兜底：clone 一份再赋 id（参考 _interaction_tool_call 的 model_copy/deepcopy）
                ctx.inputs.tool_call = _clone_tool_call_with_id(
                    ctx.inputs.tool_call, original_workflow_id
                )
            tool_call_id = original_workflow_id  # 后续 tokens/cleanup 都用原始 id
        tokens = ctx.extra.setdefault(_TOKENS_KEY, {})
        tokens[tool_call_id] = token

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        try:
            if getattr(ctx, "_deepresearch_short_circuit", None) is not None:
                return
            payload = _result_payload(ctx.inputs.tool_result)
            if payload is None or payload.get("schema_version") != EXECUTION_SCHEMA:
                return
            if payload.get("kind") == "in_progress":
                return  # A concurrent caller must not overwrite or finish the active workflow.
            tool_call_id = _tool_call_id(ctx)
            workflow_id = _workflow_id(ctx.session, tool_call_id)
            # tool_msg 必须挂历史原始 id（resume 时 ctx 携带的是 TIE
            # 克隆件的 interaction_id），否则被孤儿清理删除
            history_id = _history_tool_call_id(tool_call_id, workflow_id)
            state = payload.get("state")
            states = _load_states(ctx.session)
            if isinstance(state, Mapping):
                states[workflow_id] = dict(state)
                _save_states(ctx.session, states)

            if payload.get("kind") == "interaction":
                interaction = payload.get("interaction")
                if not isinstance(interaction, Mapping):
                    return
                questions = interaction.get("questions")
                if not isinstance(questions, list):
                    return
                request = AskUserRequest(
                    message=str(interaction.get("query") or ""),
                    payload_schema=ask_user_response_schema(),
                    questions=questions,
                )
                revision = int(state.get("revision") or 0) if isinstance(state, Mapping) else 0
                interaction_id = f"{workflow_id}_interaction_{revision}"
                aliases = _load_aliases(ctx.session)
                aliases[interaction_id] = workflow_id
                _save_aliases(ctx.session, aliases)
                ctx.inputs.tool_result = ToolInterruptException(
                    request=request,
                    tool_call=_interaction_tool_call(
                        ctx.inputs.tool_call,
                        interaction_id,
                    ),
                )
                # 修复点1a：interaction 占位 JSON 不再裸入 LLM 历史。tool_msg
                # 置 None 会回落到原始 result[1]（完整占位 JSON，泄露内部
                # state/revision 等实现细节），改为面向模型的口径；TIE 改写
                # 保持不变，打断/恢复流程不受影响。tool_call_id 必须用原始
                # 调用 id，与 assistant 消息中的 tool_calls 对齐。
                ctx.inputs.tool_msg = ToolMessage(
                    content=DEEPRESEARCH_INTERACTION_PLACEHOLDER,
                    tool_call_id=history_id,
                )
                return

            if payload.get("kind") == "stopped":
                # 优雅停止：保留执行状态（execution 已把 phase 回落为 new 并
                # 保留 conversation_id），不弹终态清理；下一次调用凭 probe
                # 认领停止快照直通续跑。也不 force finish——外层请求正被
                # 用户停止，无需再产出回答。
                # 修复11：stopped 结果完整 JSON（含内部 state/revision 与
                # "请再次调用本工具恢复"邀请文案）不再裸入 LLM 历史——
                # 生产事故中模型读到该邀请后自主重启了已被用户停止的
                # 研究。改写为禁止自动重启的面向模型口径，且不含
                # conversation_id（切断模型拿 cid 直通重启的最短路径）；
                # 用户明确要求继续时模型凭原主题 query 重调，修复9' 的
                # 扫库认领会自动认领停止快照。恢复入口双指引（2026-10-15
                # 19:52 回归实证：停止后新请求中 execute 被 ProgressiveToolRail
                # 过滤，单 execute 指引是死信，模型绕行 stream start 全新
                # 起跑丢快照——stream 侧已同口径扫库认领）。
                ctx.inputs.tool_msg = ToolMessage(
                    content=(
                        "DeepResearch 任务已被用户停止，执行进度已保存。"
                        "请勿代替用户决定继续，也不要自动重新调用研究工具；"
                        "仅当用户明确要求继续该研究时才恢复执行：优先再次"
                        "调用 deepresearch_execute（传入原研究主题即可自动"
                        "认领停止快照）；若该工具当前不可见，可调用"
                        " deepresearch_stream（action='start'，传入原研究"
                        "主题，同样会自动认领）。恢复将从停止处继续，已完成"
                        "阶段不会重跑。"
                    ),
                    tool_call_id=history_id,
                )
                return

            # 幂等守卫：completed 终态在清理 state 前记录交付索引，供下一轮
            # 同主题×同 file_name 调用 before_tool_call 短路返回「已交付」，
            # 阻止深度研究报告交付后再次对话重复执行（issue #5131）。
            if payload.get("kind") == "completed" and isinstance(state, Mapping):
                _record_delivery(
                    ctx.session,
                    workflow_id,
                    query=str(state.get("query") or ""),
                    file_name=str(state.get("file_name") or ""),
                    conversation_id=str(state.get("conversation_id") or ""),
                    html_style_status=payload.get("html_style_status"),
                )
            fallback = payload.get("fallback")
            model_handoff = (
                payload.get("kind") == "error"
                and payload.get("error_code") in MODEL_RESEARCH_HANDOFF_ERRORS
                and isinstance(state, Mapping)
                and state.get("model_research_fallback") is True
                and isinstance(fallback, Mapping)
                and fallback.get("mode") == "model_research"
            )
            if not (model_handoff or (isinstance(state, Mapping) and state.get("materials_prepared"))):
                states.pop(workflow_id, None)
            _save_states(ctx.session, states)
            aliases = {
                alias: target
                for alias, target in _load_aliases(ctx.session).items()
                if target != workflow_id
            }
            _save_aliases(ctx.session, aliases)
            if model_handoff:
                # Leave the tool envelope in the normal Core tool-result path.
                # The next main-model turn follows the Skill, using existing tools.
                return
            content = payload.get("content")

            if payload.get("kind") == "completed":
                # 完成态回写：completed 结果不再以原始 JSON payload（含
                # state/revision/timing/token usage 等内部字段，由框架默认
                # 序列化成 tool_msg）裸入 LLM 历史，改为与 interaction/
                # stopped 同口径的短文案。旁路自恢复（用户答复交互卡后的
                # interrupt continuation）不重入 LLM 主循环，本次执行的
                # tool_msg 是完成态进入历史的唯一通道——若不显式改写，
                # 历史中该 tool_call_id 下最后一条 tool 消息会停留在暂停
                # 占位文案，后续轮模型误判任务未完成（实测事故：任务已
                # 完成，模型答"还在第一阶段暂停等待用户答复"）。payload
                # content 即完成摘要（报告名/章节结构/交付说明），短载荷
                # 同时避开部署版按体积拒收 llm_context 的守卫；前端交付
                # 不受影响——chat.tool_result 帧使用 tool_result 原始
                # payload（StreamEventRail），与 tool_msg 解耦。
                ctx.inputs.tool_msg = ToolMessage(
                    content=(
                        content.strip()
                        if isinstance(content, str) and content.strip()
                        else "DeepResearch 任务已完成，报告已交付。"
                    ),
                    tool_call_id=history_id,
                )

            if isinstance(content, str) and content.strip():
                # 不改写 tool_result：保留结构化信封（kind=completed +
                # workflow_llm_token_usage），使 StreamEventRail raw_output 与
                # interface_deep._extract_deepresearch_sdk_usage 能提取 SDK token
                # 用量（atomgit-bot 审查 Issue 4）。面向模型的精简文案落到 tool_msg。
                ctx.request_force_finish(
                    {"output": content.strip(), "result_type": "answer"}
                )
        finally:
            tool_call_id = _tool_call_id(ctx)
            tokens = ctx.extra.get(_TOKENS_KEY, {})
            token = tokens.pop(tool_call_id, None) if isinstance(tokens, dict) else None
            if not tokens:
                ctx.extra.pop(_TOKENS_KEY, None)
            reset_deepresearch_execution_context(token)


__all__ = [
    "DEEPRESEARCH_DELIVERY_INDEX_KEY",
    "DEEPRESEARCH_EXECUTION_ALIAS_KEY",
    "DEEPRESEARCH_EXECUTION_STATE_KEY",
    "DEEPRESEARCH_INTERACTION_PLACEHOLDER",
    "DeepResearchExecutionRail",
]
