# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native HITL bridge for the high-level DeepResearch execution tool."""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Mapping

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRequest

from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
    _IN_FLIGHT_PHASES,
    _query_matches_stopped_claim,
    EXECUTION_SCHEMA,
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
    idx[workflow_id] = {
        "query": _normalize_topic(query),
        "file_name": str(file_name or ""),
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
    """
    topic = _normalize_topic(query)
    fname = str(file_name or "")
    for entry in _load_delivery_index(session).values():
        if (
            _normalize_topic(str(entry.get("query") or "")) == topic
            and entry.get("file_name") == fname
        ):
            return dict(entry)
    return None


def _workflow_id(session: Any, tool_call_id: str) -> str:
    return _load_aliases(session).get(tool_call_id, tool_call_id)


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

    def __init__(self, *, model_provider):
        super().__init__()
        self._model_provider = model_provider
        self._agent_id = "jiuwenswarm"

    def init(self, agent: Any) -> None:
        card = getattr(agent, "card", None)
        self._agent_id = str(
            getattr(card, "id", "")
            or getattr(card, "name", "")
            or "jiuwenswarm"
        ).strip()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if _tool_name(ctx) != _TOOL_NAME:
            return
        tool_call_id = _tool_call_id(ctx)
        workflow_id = _workflow_id(ctx.session, tool_call_id)
        states = _load_states(ctx.session)
        if workflow_id not in states:
            # state miss 兜底桥：硬中断丢失 conversation_id 后模型换新
            # tool_call_id 重试时，桥回旧 workflow state 直通续跑。
            args = _tool_args(ctx)
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
        args = _tool_args(ctx)
        hit = _lookup_delivery(
            ctx.session,
            query=str(args.get("query") or ""),
            file_name=str(args.get("file_name") or ""),
        )
        if hit is not None:
            from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
                _completion_content,
                _persist_delivered_state,
                _result,
            )

            short_state = _persist_delivered_state(
                ctx,
                workflow_id=_workflow_id(ctx.session, tool_call_id),
                conversation_id=str(hit.get("conversation_id") or ""),
            )
            ctx.inputs.tool_result = _result(
                "completed",
                short_state,
                content=_completion_content(short_state, hit.get("html_style_status")),
            )
            ctx.request_force_finish(
                {
                    "output": str(ctx.inputs.tool_result.get("content", "") or ""),
                    "result_type": "answer",
                }
            )
            logger.info(
                "[deepresearch_execution_rail] idempotent short-circuit "
                "tool_call_id=%s conversation_id=%s",
                tool_call_id,
                str(hit.get("conversation_id") or ""),
            )
            # 标记短路供 after_tool_call early-return，避免二次 force_finish 与空 delivery 记录
            ctx.extra["_deepresearch_short_circuit"] = workflow_id
            return

        model = self._model_provider() if callable(self._model_provider) else None
        request_context = get_request_context(
            session_id=extract_session_id_from_callback(ctx)
        )
        request_id = str((request_context or {}).get("request_id") or "").strip()
        requested_report_type = (request_context or {}).get("requested_report_type")
        token = bind_deepresearch_execution_context(
            tool_call_id=workflow_id,
            state=states.get(workflow_id),
            user_input=_resume_input(ctx, tool_call_id),
            model=model,
            save_state=save_state,
            agent_id=self._agent_id,
            request_id=request_id,
            requested_report_type=requested_report_type,
        )
        # B: 打断续跑时 harness 用 interaction_id 续跑，终端 chat.tool_result 会携带
        # interaction_id，原始 tool_call_id 的 chat.tool_call 永远等不到配对结果，
        # open_calls/pending 泄漏 -> 下一轮 LLM 误判「研究未完成」触发重弹问题/重跑。
        # 这里把 tool_call.id 重写回 workflow_id（经 alias 解析的原始 id），使
        # StreamEventRail(priority=80) 发出的 chat.tool_call/chat.tool_result 都对齐原始 id。
        original_workflow_id = workflow_id
        if tool_call_id != original_workflow_id:
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
            if ctx.extra.pop("_deepresearch_short_circuit", None) is not None:
                return
            payload = _result_payload(ctx.inputs.tool_result)
            if payload is None or payload.get("schema_version") != EXECUTION_SCHEMA:
                return
            tool_call_id = _tool_call_id(ctx)
            workflow_id = _workflow_id(ctx.session, tool_call_id)
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
                    content=(
                        "DeepResearch 已暂停并向用户发起结构化问答，正在等待"
                        "用户答复。用户答复后本工具将自动恢复执行并返回后续"
                        "结果；请勿代替用户作答，也无需重复调用本工具轮询进度。"
                    ),
                    tool_call_id=tool_call_id,
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
                    tool_call_id=tool_call_id,
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
            states.pop(workflow_id, None)
            _save_states(ctx.session, states)
            aliases = {
                alias: target
                for alias, target in _load_aliases(ctx.session).items()
                if target != workflow_id
            }
            _save_aliases(ctx.session, aliases)
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                ctx.inputs.tool_msg = ToolMessage(
                    content=content.strip(),
                    tool_call_id=tool_call_id,
                )
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
    "DeepResearchExecutionRail",
]
