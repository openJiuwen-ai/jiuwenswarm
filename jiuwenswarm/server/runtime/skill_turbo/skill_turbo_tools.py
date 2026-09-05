# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 工具化 -- 将 SkillTurbo 封装为 DeepAgent 的 @tool。

参照 subagent（fork_agent / spawn_subagent）模式：
- LLM 在工具选择阶段顺带完成意图判定，消除独立 match_skill LLM 调用
- tool.invoke() 内部启动 SkillTurbo.run_stream，chunks 转发到父会话 stream
- AbortError 透传（HITL 中断/恢复机制不变）
- 返回值追加停止提示，软引导 LLM 结束
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import get_current_task_id

if TYPE_CHECKING:
    from openjiuwen.core.session.agent import Session

logger = logging.getLogger(__name__)

# ── 停止提示：追加到工具返回值，引导 LLM 总结并结束 ──
_SKILL_TURBO_STOP_HINT = (
    "\n\n[SYSTEM] The skill_acceleration_exec task is complete and the artifact has already been "
    "generated. The file(s) have ALREADY been sent to the user by the internal "
    "delivery pipeline - do NOT call send_file_to_user again. You should now "
    "summarize this result to the user and finish your turn. Do NOT call "
    "skill_acceleration_exec, skill_tool, or send_file_to_user again for this task - the "
    "work is already done; calling any of them again would duplicate the work."
)

_PPT_DELIVERY_SUMMARY_POST_TOOL_HINT = (
    "\n\n[SYSTEM] PPT 交付总结骨架将由系统在本工具结果之后通过流式通道发送给用户，"
    "无需在本回合重复输出交付总结。禁止 tool_call，禁止再调 "
    "send_file_to_user / skill_tool / skill_acceleration_exec。"
)

# 工具返回值会先被 AbilityManager 收成 ToolMessage。after_tool_call 再用
# StreamEventRail._tool_interrupted_message（随 prompt 语言中/英）覆写 tool_msg。
# _fix_incomplete_tool_context 认 rail 文案 + 中英文 legacy 模板，故本常量取中文
# 默认句即可。禁止返回 {'success': False, 'error': '任务已暂停等待审批'}：
# str(dict) 进 ToolMessage 后模型会当成加速失败并回退 skill_tool。
_SKILL_TURBO_HITL_PLACEHOLDER = (
    "[工具执行被中断] 工具 skill_acceleration_exec 执行过程中被用户打断，没有执行结果。"
)

# ── 待在外层 tool_result 之后发出的 PPT 交付总结 ──
# 不可在 ppt_gen_root / 工具执行中途以 chat.delta 发出：那时无 task_id，会与过程尾
# 同桶；且早于 skill_acceleration_exec 的 tool_result，RelayClaw 收集窗口会清空。
# 由 SkillTurboDeliverySummaryRail.after_tool_call（晚于 StreamEventRail 发
# tool_result）读取并发出。
_pending_ppt_delivery_summary: ContextVar[str | None] = ContextVar(
    "pending_ppt_delivery_summary", default=None
)

# ── SkillTurbo event_type -> DeepAgent OutputSchema.type 反向映射 ──
# SkillTurbo executor 产出的 payload.event_type 带 "chat." 前缀，而 DeepAgent 主循环 /
# _parse_stream_chunk 期望原始 OutputSchema.type（不带前缀）。直接用 event_type 作 type
# 会导致 tool_call / tool_result / tool_update 等事件被静默丢弃。
_SKILL_TURBO_EVENT_TYPE_TO_OUTPUT_TYPE: dict[str, str] = {
    "chat.delta": "llm_output",
    "chat.reasoning": "llm_reasoning",
    "chat.llm_usage": "llm_usage",
    "chat.usage_metadata": "llm_usage",
    "chat.tool_call": "tool_call",
    "chat.tool_result": "tool_result",
    "chat.tool_update": "tool_update",
    "chat.tool_calls.delta": "tool_calls.delta",
    "chat.error": "error",
}
_BUBBLE_PROGRESS_FIELD = "_bubble_progress"

# ── 不转发给父会话的事件类型 ──
# plan/node 生命周期事件：前端无对应 handler，DeepAgent 也无显式处理。
# plan.started / node.started 的 content 字段会被 _parse_stream_chunk fallback
# 误改写为 chat.delta 泄露给用户；plan.finished / node.finished 则被静默丢弃。
# 统一在此过滤，避免噪音。
_SKILL_TURBO_SKIP_EVENT_TYPES: frozenset[str] = frozenset({
    "plan.started",
    "plan.finished",
    "node.started",
    "node.finished",
})

# ── SkillTurbo 内部任务事件类型 ──
# task.update 会覆盖前端唯一的 taskProgress 槽位，导致外层 DeepAgent 的 todo 列表
# 被替换为 PPT 内部步骤；task.start/task.complete 驱动前端的 taskStack 决定
# chat.* 事件的 segment 归属。当外层有活跃 todo 时需要特殊处理这三类事件。
_SKILL_TURBO_TASK_EVENT_TYPES: frozenset[str] = frozenset({
    "task.start",
    "task.complete",
    "task.update",
})


def _without_inner_task_routing(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a parent-bound event without its SkillTurbo-only task id."""
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("task_"):
        return payload
    cleaned = dict(payload)
    cleaned.pop("task_id", None)
    return cleaned


def _prepare_parent_stream_output(
    event_type: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Map a SkillTurbo event to the parent stream without delaying bubble progress."""
    if event_type == "chat.delta" and payload.get(_BUBBLE_PROGRESS_FIELD):
        cleaned = dict(payload)
        cleaned.pop(_BUBBLE_PROGRESS_FIELD, None)
        cleaned.pop("task_id", None)
        return "content_chunk", cleaned
    return _SKILL_TURBO_EVENT_TYPE_TO_OUTPUT_TYPE.get(event_type, event_type), payload


# ── ContextVar：在 before_tool_call 中注入，供工具函数读取 ──
_current_skill_turbo_adapter: ContextVar[Any] = ContextVar(
    "current_skill_turbo_adapter", default=None
)
_skill_turbo_outer_todo_active: ContextVar[bool | None] = ContextVar(
    "skill_turbo_outer_todo_active", default=None
)


def set_skill_turbo_outer_todo_active(active: bool) -> Token:
    """Bind whether the parent task list owns this tool call's display."""
    return _skill_turbo_outer_todo_active.set(active)


def get_skill_turbo_outer_todo_active() -> bool | None:
    return _skill_turbo_outer_todo_active.get()


def reset_skill_turbo_outer_todo_active(token: Token) -> None:
    _skill_turbo_outer_todo_active.reset(token)

# ── ContextVar：SkillTurbo HITL 中断信号 ──
# skill_turbo_tools catch AbortError 后提取 ToolInterruptException 存入此 ContextVar，
# StreamEventRail.after_tool_call 读取后改写 ctx.inputs.tool_result 为 TIE，
# 使 harness 原生 HITL 机制（build_interrupt_state）检测并触发暂停。
_skill_turbo_hitl_tic: ContextVar[Any] = ContextVar("skill_turbo_hitl_tic", default=None)


def set_skill_turbo_hitl_tic(tic: Any) -> Token:
    return _skill_turbo_hitl_tic.set(tic)


def get_skill_turbo_hitl_tic() -> Any:
    return _skill_turbo_hitl_tic.get()


def set_pending_ppt_delivery_summary(summary: str) -> None:
    text = str(summary or "").strip()
    _pending_ppt_delivery_summary.set(text or None)


def clear_pending_ppt_delivery_summary() -> None:
    _pending_ppt_delivery_summary.set(None)


def take_pending_ppt_delivery_summary() -> str:
    text = str(_pending_ppt_delivery_summary.get() or "").strip()
    _pending_ppt_delivery_summary.set(None)
    return text


async def emit_pending_ppt_delivery_summary(session: "Session") -> bool:
    """在外层 tool_result 之后发出无 task_id 的交付总结 chat.delta。

    返回是否实际发出。session 为 None 或骨架非法时静默跳过。
    """
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.delivery_summary import (
        DELIVERY_SUMMARY_START,
    )
    from openjiuwen.core.session.stream.base import OutputSchema

    summary = take_pending_ppt_delivery_summary()
    if not summary.startswith(DELIVERY_SUMMARY_START):
        return False
    if session is None:
        logger.warning(
            "[SkillTurboTool] pending PPT delivery summary dropped: parent session is None"
        )
        return False
    try:
        await session.write_stream(
            OutputSchema(
                type="llm_output",
                index=0,
                payload={"content": summary},
            )
        )
        logger.info(
            "[SkillTurboTool] emitted PPT delivery summary after tool_result chars=%d",
            len(summary),
        )
        return True
    except Exception:
        logger.warning(
            "[SkillTurboTool] emit PPT delivery summary after tool_result failed",
            exc_info=True,
        )
        return False


def set_current_skill_turbo_adapter(adapter: Any) -> Token:
    """绑定当前 async 上下文的 DeepAdapter 实例，返回 Token 用于 reset。"""
    return _current_skill_turbo_adapter.set(adapter)


def get_current_skill_turbo_adapter() -> Any:
    """获取当前上下文的 DeepAdapter 实例。"""
    return _current_skill_turbo_adapter.get()


def reset_current_skill_turbo_adapter(token: Token) -> None:
    """恢复之前的 adapter 绑定。"""
    _current_skill_turbo_adapter.reset(token)


def clear_current_skill_turbo_adapter() -> None:
    """强制清空当前上下文的 adapter 绑定（用 None 覆盖，不依赖 Token）。

    供 rail 的兜底清理场景使用：当 token 已丢失或需无条件清空时调用，
    避免误用 reset(None) 抛 ValueError 被吞掉导致 adapter 泄漏。
    """
    _current_skill_turbo_adapter.set(None)


_skill_turbo_resume_answers: ContextVar[Any] = ContextVar(
    "skill_turbo_resume_answers", default=None
)


def set_skill_turbo_resume_answers(answers: Any) -> Token:
    return _skill_turbo_resume_answers.set(answers)


def get_skill_turbo_resume_answers() -> Any:
    return _skill_turbo_resume_answers.get()


def reset_skill_turbo_resume_answers(token: Token) -> None:
    _skill_turbo_resume_answers.reset(token)


def _resume_user_input_from_raw(
    raw: Any,
    resume_ctx: dict[str, Any],
    adapter: Any,
) -> Any:
    """把 handle_resume 的 InteractiveInput / 原始 answers 转成内层 rail 的 user_input。

    InteractiveInput.user_inputs 的 key 是 interaction / 外层 tool_call_id，
    value 是该次中断的完整 payload。此处取任意一个 value，因为外层 HITL 当前只有一个 skill_acceleration_exec interrupt。
    resume_stream 的形态是单次中断的 payload。
    """
    user_inputs = getattr(raw, "user_inputs", None)
    if isinstance(user_inputs, dict) and user_inputs:
        return next(iter(user_inputs.values()))
    if isinstance(raw, list) and adapter is not None:
        convert = getattr(adapter, "_skill_turbo_answers_to_confirm_payload", None)
        if callable(convert):
            return convert(raw, resume_ctx)
    return raw


# ────────────────── 中断恢复 hint（一次性 fresh 调用守卫） ──────────────────
# prepare_interrupt_artifacts_for_request 注入产物摘要时，会在 request.metadata 上
# 挂一份结构化 hint。本请求内 skill_acceleration_exec 若被全新调用（非 HITL resume），
# 工具层守卫据此先拒绝一次并附上产物摘要，引导 LLM 走非 skillTurbo 流程基于已有
# 产物继续；LLM 明确重试（视为全新任务）时 hint 已被消费，放行。
# request.metadata 经 _update_runtime_config 浅拷贝进 rail metadata（内部 dict 共享
# 引用），故 consumed 标记在原地标记即可对所有副本生效。

SKILL_TURBO_INTERRUPT_RECOVERY_KEY = "skill_turbo_interrupt_recovery"


def set_interrupt_recovery_hint(request: Any, *, summary: str, skill: str = "") -> None:
    """把一次性中断恢复 hint 挂到 request.metadata（仅注入产物摘要的请求调用）。"""
    metadata = getattr(request, "metadata", None)
    if not isinstance(metadata, dict):
        # metadata 缺失时无处挂载：放弃 hint，守卫对本请求不生效（降级，不影响主流程）
        return
    metadata[SKILL_TURBO_INTERRUPT_RECOVERY_KEY] = {
        "summary": summary,
        "skill": skill,
        "request_id": str(getattr(request, "request_id", "") or ""),
        "consumed": False,
    }


def _pending_interrupt_recovery_hint(request_metadata: Any) -> dict[str, Any] | None:
    """读取本请求未消费的中断恢复 hint；无 hint 或已消费返回 None。"""
    if not isinstance(request_metadata, dict):
        return None
    hint = request_metadata.get(SKILL_TURBO_INTERRUPT_RECOVERY_KEY)
    if isinstance(hint, dict) and hint.get("summary") and not hint.get("consumed"):
        return hint
    return None


def _consume_interrupt_recovery_hint(hint: dict[str, Any]) -> None:
    """标记 hint 已消费：同请求内的下一次 fresh 调用放行（视为明确的全新任务）。"""
    hint["consumed"] = True


def _build_interrupt_recovery_reject(hint: dict[str, Any]) -> dict[str, Any]:
    """构造 fresh 调用守卫的一次性拒绝结果（success=False，引导非 skillTurbo 继续）。"""
    summary = str(hint.get("summary") or "").strip()
    error = (
        "检测到上一轮被中断的 SkillAccelerationExec 任务仍有可复用的已完成产物：\n\n"
        f"{summary}\n\n"
        "全新调用 skill_acceleration_exec 会丢弃以上产物并从 p0 重新规划执行，"
        "导致已完成的工作被重复执行。\n"
        "- 若用户想继续或完成被中断的任务：请勿调用 skill_acceleration_exec，"
        "改用 skill_tool 走标准流程（可基于以上产物文件继续），"
        "或用 read_file / edit_file 等工具直接处理产物文件；\n"
        "- 若用户明确要求与已有产物无关的全新任务：请直接再次调用 "
        "skill_acceleration_exec，本次将被放行（该提示仅生效一次）。"
    )
    return {"success": False, "error": error}


def _resolve_skill_turbo_resume_session_id(
    external_session_id: Any,
    parent_session: Any,
) -> str:
    """Align resume checkpointer key with executor: metadata sid, else parent session."""
    sid = str(external_session_id or "").strip()
    if sid:
        return sid
    if parent_session is None:
        return ""
    getter = getattr(parent_session, "get_session_id", None)
    if callable(getter):
        try:
            sid = str(getter() or "").strip()
        except Exception:
            sid = ""
    if sid:
        return sid
    return str(getattr(parent_session, "session_id", "") or "").strip()


# ── ContextVar：当前请求的 metadata ──
# 在 _update_runtime_config 中设置（md 是局部变量，无竞态），
# skill_turbo 通过 get_current_request_metadata() 读取，
# 替代 adapter._current_request_metadata 实例属性（并发覆盖风险）。
_current_request_metadata: ContextVar[Any] = ContextVar(
    "current_request_metadata", default=None
)


def set_current_request_metadata(metadata: Any) -> Token:
    """绑定当前 async 上下文的请求 metadata，返回 Token 用于 reset。"""
    return _current_request_metadata.set(metadata)


def get_current_request_metadata() -> Any:
    """获取当前上下文的请求 metadata。"""
    return _current_request_metadata.get()


def reset_current_request_metadata(token: Token) -> None:
    """恢复之前的 metadata 绑定。"""
    _current_request_metadata.reset(token)


def _build_artifact_summary(holder: dict[str, Any]) -> str:
    """从 executor 的 _node_artifacts_holder 构建产物摘要文本。

    格式: ``- {plan_name}: {info 摘要} | 文件: {路径列表}``
    """
    if not holder:
        return ""
    lines = ["[SkillAccelerationExec 产物摘要]"]
    for plan_name, node_info in holder.items():
        if not isinstance(node_info, dict):
            continue
        parts: list[str] = []
        info = node_info.get("info")
        if isinstance(info, dict) and info:
            parts.append(", ".join(
                f"{k}={v}" for k, v in info.items() if v is not None
            ))
        files = node_info.get("files")
        if isinstance(files, list) and files:
            file_paths = [
                f.get("path", "") for f in files
                if isinstance(f, dict) and f.get("path")
            ]
            if file_paths:
                parts.append("文件: " + ", ".join(file_paths))
        if parts:
            lines.append(f"- {plan_name}: {' | '.join(parts)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _ppt_delivery_summary(artifact_holder: dict[str, Any] | None) -> str:
    node = (artifact_holder or {}).get("p10_delivery")
    if not isinstance(node, dict):
        return ""
    text = node.get("delivery_summary")
    return text.strip() if isinstance(text, str) else ""


def _wrap_skill_turbo_result(
    result_dict: dict[str, Any],
    artifact_holder: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在结果末尾追加产物摘要；成功时追加停止提示引导 LLM 结束当前轮次。

    失败时不追加停止提示--系统提示要求失败时回退到 skill_tool 走标准流程，
    若此处追加 "finish your turn" 会与之矛盾。
    """
    artifact_text = _build_artifact_summary(artifact_holder or {})
    ppt_summary = _ppt_delivery_summary(artifact_holder)
    if result_dict.get("success"):
        parts = [result_dict.get("result") or ""]
        if artifact_text:
            parts.append(artifact_text)
        if ppt_summary:
            # 骨架不进 tool_result 正文、也不在流水线内提前 chat.delta；
            # 挂到 ContextVar，由 after_tool_call 在外层 tool_result 之后流式发出。
            set_pending_ppt_delivery_summary(ppt_summary)
            parts.append(_PPT_DELIVERY_SUMMARY_POST_TOOL_HINT)
        else:
            clear_pending_ppt_delivery_summary()
            parts.append(_SKILL_TURBO_STOP_HINT)
        result_dict["result"] = "\n\n".join(p for p in parts if p)
    else:
        clear_pending_ppt_delivery_summary()
        parts = [result_dict.get("error") or ""]
        if artifact_text:
            parts.append(artifact_text)
        result_dict["error"] = "\n\n".join(p for p in parts if p)
    return result_dict


@tool(
    name="skill_acceleration_exec",
    description=(
        "技能加速模块。当用户意图涉及技能类任务（如生成 PPT、文档转换等结构化产出）时，"
        "可优先尝试调用此工具以获得更快的生成流程。工具内部会二次判断是否真正匹配已支持的技能，"
        "不匹配时自动降级为普通对话。当前内部支持 ppt-craft 技能（PPT 演示文稿制作）。"
        "【重要】每次调用仅处理一个独立任务。若用户要求生成多个同类产物（如多份不同主题的 PPT），"
        "必须为每个产物分别发起独立调用，且严格串行：等待前一次调用完全结束并收到返回结果后，"
        "才能发起下一次调用。严禁在同一轮对话中并行发起多次调用。"
        "【临时排除】当任务需要使用自定义/指定模板时，不要调用此工具--"
        "无论该模板路径是出现在当前用户消息里、引用历史会话中生成或上传的模板"
        "（如“使用刚才生成的风格模板”“用之前那个模板”等自然语言引用）、"
        "还是消息中含 模板目录路径、模板主文件路径、pack_dir 等字段名。"
        "判断“是否涉及自定义模板”应基于语义。"
        "当前 skill 加速器暂未适配模板包功能，此类请求应改用 skill_tool 走 pptx-craft 标准流程。"
        "此排除规则为临时措施，待 skill 加速器适配模板功能后移除。"
    ),
)
async def skill_turbo(query: str) -> dict[str, Any] | str:
    """执行 SkillAccelerationExec 任务。

    Args:
        query: 对单个任务的忠实总结，须严格基于用户原话与历史上下文中已有的信息，不得自行扩写、脑补或补充用户未提及的内容细节（如擅自罗列章节大纲、技术要点、子主题等）。仅在用户表达零散时做必要的凝练与指代消解，确保任务目标、产物与约束完整可执行，但不新增任何信息。每次调用只处理一个任务；若用户要求多个任务，必须串行调用：等待前一次调用完成并收到返回结果后，再发起下一次调用。
    """
    # ── [TEMP-TEMPLATE-BYPASS] 模板请求拦截（临时措施，待 skill 加速器适配模板功能后删除整块）──
    # 拦截任何携带自定义模板路径的请求：前端选择自定义模板时会注入"模板目录路径/模板主文件路径"
    # 字段；LLM 引用历史中生成/上传的模板时会以自然语言包装路径（如"模板目录：D:\..."）。
    # 早期仅靠固定字段名做子串匹配，LLM 自由措辞即击穿（见 case officeclaw_885400a37...），
    # 故改为正则匹配"模板(目录|主文件|路径)？后跟绝对路径"，覆盖两类来源与措辞变体。
    # skill 加速器暂未适配此能力，拦截后引导 LLM 改用 skill_tool 走 pptx-craft 标准流程。
    # 删除方式：搜索 [TEMP-TEMPLATE-BYPASS] 删除本标记块即可。
    import re

    template_path_pattern = re.compile(
        r"模板(?:目录|主文件|路径|目录路径|主文件路径)[：:\s]*"
        r"(?:[A-Za-z]:[\\/]|/|~/|\.{0,2}/)"
    )
    template_keywords = ("模板目录路径", "模板主文件路径", "pack_dir")
    if template_path_pattern.search(query) or any(
        kw in query for kw in template_keywords
    ):
        logger.info(
            "[SkillTurboTool] 检测到自定义模板路径，跳过 skill 加速器，建议改用 skill_tool"
        )
        return _wrap_skill_turbo_result({
            "success": False,
            "error": (
                "检测到用户提供了自定义模板路径，当前 skill 加速器暂未适配模板包功能。"
                "请改用 skill_tool 走 pptx-craft 标准流程处理此请求"
                "（直接执行，无需再调用 skill_acceleration_exec）。"
            ),
        })
    # ── [/TEMP-TEMPLATE-BYPASS] ──

    # ── [REGION-EDIT-BYPASS] 选区/编辑已有 PPT 请求拦截 ──
    # office-claw 前端在用户选中 PPT 某区域做修改（改字体/文案/样式/局部替换）时，
    # 注入的 user message 带固定选区字段（PPT选区/选区原文/选区类型/选区位置/选区容器/
    # 选区 class/修改要求）。这类请求本质是"编辑已有 PPT 局部"，而非从零生成整套演示文稿。
    # skill 加速器（pptx-craft 流水线）只会从 Stage 1 重新生成全新 PPT，无法复用已有文件
    # 做局部修改（第二次选区修复被 LLM 调进 skill_acceleration_exec，从 stage1 全量重跑，丢失原 PPT）。
    # 信号源说明：理想信号在"外层 user message"里，而本工具拿到的 query 是 LLM 重写的
    # 忠实总结——多数选区请求会保留"选区"字样，但 LLM 偶尔会脑补成"生成 N 页 PPT"
    # （上述 case 即如此，query 变成"8页左右"）。因此本块是对"query 保留选区语义"的兜底；
    # 真正的主防线在 SkillTurboPromptRail 注入的排除提示词（LLM 调工具前能看到完整
    # user message）。两层叠加降低误进概率。
    # 删除方式：待 pptx-craft 流水线支持"编辑已有 PPT"短路分支后，搜索 [REGION-EDIT-BYPASS] 删除本块。
    region_keywords = (
        "PPT选区", "选区原文", "选区类型", "选区位置", "选区容器",
        "选区 class", "选区class", "修改要求", "选区字段", "布局优化", "选区优化", "内容优化"
    )
    if any(kw in query for kw in region_keywords):
        logger.info(
            "[SkillTurboTool] 检测到 PPT 选区/编辑已有 PPT 请求，跳过 skill 加速器，"
            "建议改用 skill_tool 走 pptx-craft 标准流程或直接编辑已有 PPT 文件"
        )
        return _wrap_skill_turbo_result({
            "success": False,
            "error": (
                "检测到该请求是针对已有 PPT 某区域的局部修改（选区/编辑已有 PPT），"
                "skill 加速器仅支持从零生成全新 PPT（会从 Stage 1 全量重跑，无法复用原文件）。"
                "请改用 skill_tool 加载 pptx-craft 标准流程（支持编辑已有 PPT），"
                "或直接用 edit_file / 读写 pptx 的工具完成局部修改"
                "（直接执行，无需再调用 skill_acceleration_exec）。"
            ),
        })
    # ── [/REGION-EDIT-BYPASS] ──

    from jiuwenswarm.server.runtime.skill_turbo.agent import SkillTurbo, SkillTurboNotHandled
    from jiuwenswarm.agents.harness.common.tools.subagent_executor import (
        get_subagent_parent_session,
    )
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
        get_effective_request_workspace_dir,
        get_effective_request_output_dir,
    )
    from openjiuwen.core.session.stream.base import OutputSchema
    from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

    adapter = get_current_skill_turbo_adapter()
    if adapter is None:
        return _wrap_skill_turbo_result(
            {"success": False, "error": "SkillAccelerationExec 未初始化"}
        )

    parent_session: Session | None = get_subagent_parent_session()

    # 检查外层 DeepAgent 是否有活跃的 todo 步骤（task_execution_rail 在 task.start 时
    # 设置 _ACTIVE_TASK_ID，task.complete 时清除）。用运行时 ContextVar 而非读取
    # todo.json，避免上一轮异常中止残留旧 todo 导致误判。
    # 有活跃 todo 时：跳过 PPT 内部的 task.* 事件（task.update 覆盖外层 todo 槽位，
    # task.start/task.complete 导致前端 taskStack 嵌套、segment 分裂），让 PPT 的
    # chat.* 事件自然归到外层 todo 步骤的 segment 下渲染。
    # 无活跃 todo 时：PPT 的 task 事件正常转发，独立展示步骤列表。
    outer_task_id = get_current_task_id()
    rebound_outer_todo = get_skill_turbo_outer_todo_active()
    has_outer_todo = (
        rebound_outer_todo
        if rebound_outer_todo is not None
        else outer_task_id is not None
    )
    logger.info(
        "[SkillTurboTool] outer todo active=%s rebound=%s outer_task_id=%s "
        "parent_session=%s, "
        "task events will be %s",
        has_outer_todo,
        rebound_outer_todo,
        outer_task_id,
        type(parent_session).__name__ if parent_session is not None else None,
        "skipped" if has_outer_todo else "forwarded as-is",
    )

    # 构造 config 和 SkillTurbo 实例
    config = adapter.build_skill_turbo_config()
    skill_turbo_inst = SkillTurbo(config)

    # metadata：通过 ContextVar 读取（_update_runtime_config 中设置，无并发覆盖风险）
    request_metadata = get_current_request_metadata()
    request_id = request_metadata.get("request_id", "") if isinstance(request_metadata, dict) else ""
    channel_id = request_metadata.get("channel_id", "") if isinstance(request_metadata, dict) else ""
    external_session_id = request_metadata.get("session_id", "") if isinstance(request_metadata, dict) else ""

    # 构建 inputs：从 ContextVar 和 adapter 补全 executor 所需的上下文字段
    inputs: dict[str, Any] = {"query": query}
    if parent_session is not None:
        inputs["conversation_id"] = parent_session.get_session_id()
    if external_session_id:
        inputs["session_id"] = external_session_id
    if request_id:
        inputs["request_id"] = request_id
    if channel_id:
        inputs["channel_id"] = channel_id

    # user_id 顶层；chat_id ← routing.group_id，供 pipeline 重建路径。
    if isinstance(request_metadata, dict):
        from jiuwenswarm.common.request_identity import web_routing_identity

        user_id = str(request_metadata.get("user_id") or "").strip()
        if user_id:
            inputs["user_id"] = user_id
        identity = web_routing_identity(request_metadata)
        if identity.get("group_id"):
            inputs["chat_id"] = identity["group_id"]

    # effective_project_dir：adapter 在 _update_runtime_config 中已写入 ContextVar
    effective_project_dir = get_effective_request_workspace_dir()
    if effective_project_dir:
        inputs["effective_project_dir"] = effective_project_dir

    # 不再直接传入 output_dir，让 pipeline 自行调用 generate-timestamp-dir
    # 为每次任务创建独立的时间戳子目录，避免同一 session 连续任务共用 output 目录导致产物串扰
    # 但需要确保 pipeline 能正确获取 output 的父目录（用于生成时间戳子目录）
    # 方案：从 metadata 的 output_dir 提取父目录，作为 workspace_base 传入
    if isinstance(request_metadata, dict):
        output_dir = request_metadata.get("output_dir")
        if output_dir and isinstance(output_dir, str) and output_dir.strip():
            # output_dir 格式：.../files/user_id/chat_id/output
            # 需要将其作为 workspace_base，让 pipeline 在此目录下生成时间戳子目录
            inputs["workspace_base"] = output_dir.strip()
            logger.debug(
                "[SkillTurboTool] workspace_base 设置为 output_dir: %s",
                output_dir.strip()
            )

    if isinstance(request_metadata, dict):
        inputs["metadata"] = request_metadata

    turbo_session = None
    resume_ctx = None
    resume_answers = get_skill_turbo_resume_answers()
    if resume_answers is not None:
        try:
            from openjiuwen.core.session.agent import create_agent_session
            from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
                load_resume_ctx,
                set_skill_turbo_id,
            )

            card = getattr(getattr(adapter, "_instance", None), "card", None)
            sid = _resolve_skill_turbo_resume_session_id(
                external_session_id, parent_session
            )
            if not sid:
                logger.warning(
                    "[SkillTurboTool] resume load_resume_ctx missing session_id; "
                    "checkpointer key may miss the saved resume_ctx"
                )
            turbo_session = (
                create_agent_session(session_id=sid, card=card)
                if sid
                else create_agent_session(card=card)
            )
            set_skill_turbo_id(turbo_session, card)
            resume_ctx = await load_resume_ctx(turbo_session)
        except Exception:
            logger.warning(
                "[SkillTurboTool] load resume_ctx failed, falling back to run_stream",
                exc_info=True,
            )
            if turbo_session is not None:
                try:
                    await turbo_session.post_run()
                except Exception:
                    logger.debug(
                        "[SkillTurboTool] fallback turbo_session post_run failed",
                        exc_info=True,
                    )
                turbo_session = None
            resume_ctx = None

    hitl_interrupt = False

    release_checkpoint = False    # Only persist-clear the checkpoint when this invocation consumed it
    # (resume finished) or it cannot be reused (SkillTurboNotHandled).
    # Transient failures must keep the last HITL checkpoint; do not post_run
    # either — turbo_session shares the executor checkpointer key, and stale
    # in-memory state would clobber a still-valid resume_ctx on disk.
    try:
        if resume_ctx is not None:
            from jiuwenswarm.server.runtime.skill_turbo.interactive_ask import (
                apply_interactive_ask_to_inputs,
                resolve_resume_interactive_ask,
            )

            raw_ia = None
            if isinstance(request_metadata, dict) and (
                "interactive_ask" in request_metadata
                or "interactiveAsk" in request_metadata
            ):
                raw_ia = request_metadata.get(
                    "interactive_ask", request_metadata.get("interactiveAsk")
                )
            resume_inputs = apply_interactive_ask_to_inputs(
                resume_ctx.get("inputs") or inputs,
                resolve_resume_interactive_ask(raw_ia, resume_ctx.get("inputs")),
            )
            logger.info(
                "[SkillTurboTool] HITL resume via resume_stream tcid=%s",
                resume_ctx.get("pending_tool_call_id"),
            )
            stream = skill_turbo_inst.resume_stream(
                plan_code=resume_ctx["plan_code"],
                inputs=resume_inputs,
                request_id=request_id,
                channel_id=channel_id,
                pending_tool_call_id=resume_ctx["pending_tool_call_id"],
                user_input=_resume_user_input_from_raw(
                    resume_answers, resume_ctx, adapter
                ),
                task_states=resume_ctx.get("task_states"),
            )
        else:
            # fresh 调用守卫：本请求注入过"中断恢复 hint"（上一轮中断任务有未消费产物）
            # 时，先一次性拒绝并附产物摘要，避免新 executor 从 p0 清盘重跑（产物已在
            # prepare_interrupt_artifacts_for_request 注入时落盘清空，此处只拦 LLM 的
            # 盲目重启）。LLM 重试（明确全新任务）时 hint 已消费，直接放行。
            recovery_hint = _pending_interrupt_recovery_hint(request_metadata)
            if recovery_hint is not None:
                _consume_interrupt_recovery_hint(recovery_hint)
                logger.info(
                    "[SkillTurboTool] interrupt recovery guard: reject fresh "
                    "run_stream once (unconsumed artifacts from interrupted task)"
                )
                return _wrap_skill_turbo_result(
                    _build_interrupt_recovery_reject(recovery_hint)
                )
            stream = skill_turbo_inst.run_stream(
                query, inputs, request_id, channel_id
            )

        async for chunk in stream:
            if not chunk.payload:
                continue

            event_type = chunk.payload.get("event_type", "unknown")

            # plan/node 生命周期事件：前端无 handler，跳过转发；
            # 其 content 若进入 _parse_stream_chunk 会被误改写为 chat.delta 泄露给用户
            if event_type in _SKILL_TURBO_SKIP_EVENT_TYPES:
                continue

            # 外层有活跃 todo 时，跳过 PPT 内部的全部 task 事件：
            # - task.update：会整体替换前端唯一的 taskProgress 槽位，覆盖外层 todo
            # - task.start/task.complete：外层 task_execution_rail 已为当前 todo 步骤
            #   发了 task.start（task_id="todo:uuid"），PPT 再发 task.start（task_id="task_xxx"）
            #   会导致前端 taskStack 嵌套，chat.* 事件被盖戳 PPT 的 task_xxx，归到独立 segment。
            #   而该 segment 的 taskId 与外层 todo 不匹配，被 resolveSegmentForRow 丢弃，
            #   PPT 的思考/工具调用全部不显示。跳过后 chat.* 归到外层 todo 的 segment，正常渲染。
            if has_outer_todo and event_type in _SKILL_TURBO_TASK_EVENT_TYPES:
                continue

            payload = chunk.payload
            if has_outer_todo and isinstance(payload, dict):
                # Internal task ids are meaningful to SkillTurbo itself, but
                # become untitled task rows in the parent UI.  The original
                # chunk remains untouched for execution/resume diagnostics.
                payload = _without_inner_task_routing(payload)

            # 转发 chunk 到父会话 stream（前端实时可见）
            if parent_session is not None:
                # SkillTurbo executor 产出的 event_type 带 "chat." 前缀（如 "chat.tool_call"），
                # 但 DeepAgent 的 _parse_stream_chunk 期望原始 OutputSchema.type（如 "tool_call"）。
                # 若直接用 event_type 作 type，会因类型不匹配被静默丢弃，需反向映射回原始 type。
                output_type, payload = _prepare_parent_stream_output(
                    event_type, payload
                )
                output = OutputSchema(
                    type=output_type,
                    index=0,
                    payload=payload,
                )
                try:
                    await parent_session.write_stream(output)
                    if event_type in _SKILL_TURBO_TASK_EVENT_TYPES:
                        tasks = chunk.payload.get("tasks") if isinstance(chunk.payload, dict) else None
                        n_tasks = len(tasks) if isinstance(tasks, list) else 0
                        logger.info(
                            "[SkillTurboTool] forwarded %s via write_stream "
                            "output_type=%s tasks=%s parent_session=%s",
                            event_type,
                            output_type,
                            n_tasks,
                            type(parent_session).__name__,
                        )
                except Exception:
                    logger.warning(
                        "[SkillTurboTool] write_stream failed for event_type=%s",
                        event_type,
                        exc_info=True,
                    )
            elif event_type in _SKILL_TURBO_TASK_EVENT_TYPES:
                logger.warning(
                    "[SkillTurboTool] drop %s: parent_session is None "
                    "(task list will not reach frontend)",
                    event_type,
                )

        # 过程输出已通过 write_stream 实时推给前端，tool result 仅返回精简完成信号 + 产物摘要
        release_checkpoint = True
        return _wrap_skill_turbo_result(
            {"success": True, "result": "任务已完成"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )

    except AbortError as e:
        # HITL 中断：提取 ToolInterruptException 存入 ContextVar，
        # after_tool_call 会改写 ctx.inputs.tool_result 为 TIE 触发 harness 原生 HITL。
        # 不能直接 raise TIE（被 _execute_single_tool_call 包装为 AbilityExecutionError）。
        # resume_ctx 已由 executor.save_resume_ctx 保存；外层 HITL 恢复后会再 invoke 本工具。
        from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
            extract_tool_interrupt,
        )
        tic = extract_tool_interrupt(e)
        if tic is not None:
            logger.info(
                "[SkillTurboTool] HITL interrupt, storing TIC. tcid=%s",
                tic.tool_call.id if tic.tool_call else "?",
            )
            set_skill_turbo_hitl_tic(tic)
            hitl_interrupt = True
            return _SKILL_TURBO_HITL_PLACEHOLDER
        # Fallback: AbortError 无 ToolInterruptException cause，返回错误
        logger.warning("[SkillTurboTool] AbortError without ToolInterruptException cause")
        return _wrap_skill_turbo_result(
            {"success": False, "error": f"任务中断: {e}"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )
    except SkillTurboNotHandled as exc:
        logger.info("[SkillTurboTool] SkillTurbo 未处理: %s", exc)
        release_checkpoint = True
        return _wrap_skill_turbo_result(
            {"success": False, "error": f"SkillAccelerationExec 未处理: {exc}"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )
    except Exception as exc:
        logger.warning("[SkillTurboTool] 执行失败: %s", exc, exc_info=True)
        return _wrap_skill_turbo_result(
            {"success": False, "error": f"执行失败: {exc}"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )
    finally:
        if turbo_session is not None and not hitl_interrupt and release_checkpoint:
            if resume_ctx is not None:
                from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
                    clear_resume_ctx,
                )

                try:
                    await clear_resume_ctx(turbo_session)
                except Exception:
                    logger.debug(
                        "[SkillTurboTool] clear_resume_ctx failed",
                        exc_info=True,
                    )
            try:
                await turbo_session.post_run()
            except Exception:
                logger.debug(
                    "[SkillTurboTool] turbo_session post_run failed",
                    exc_info=True,
                )


# PPT 加速流水线经常超过 AbilityManager 默认 300s 工具超时；与 deepresearch_stream 一样
# 豁免外层 deadline。timeout_s=None 后仍受 MAX_TOOL_CALL_TIMEOUT_HARD_LIMIT（默认 3600s）约束。
skill_turbo.card.properties["resilience"] = {"timeout_s": None}


def get_skill_turbo_tools() -> list:
    """返回 SkillTurbo 工具列表，供 interface_deep.py 注册。"""
    return [skill_turbo]
