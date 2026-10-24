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
import time
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import get_current_task_id
from jiuwenswarm.server.runtime.skill_turbo.task_slot_guard import (
    claim_task_slot,
    peek_leaked_task_slot,
    release_task_slot,
)

if TYPE_CHECKING:
    from openjiuwen.core.session.agent import Session

logger = logging.getLogger(__name__)


# ── 技能清单 TTL 缓存：避免每次模型调用前重复扫盘 ──────────────────────────
# SkillTurboPromptRail.before_model_call 每轮模型调用都触发 refresh ->
# _collect_skill_entries 扫盘（iterdir + meta.json 读 + glob，同步 IO 阻塞
# 事件循环）。同一会话连续多轮调用间技能目录集合不变，重复扫描无意义。
_ENTRIES_CACHE_TTL_SECONDS = 60.0
_entries_cache: dict[frozenset[str], tuple[float, dict[str, str]]] = {}


def _resolve_skill_roots() -> frozenset[str]:
    """解析当前上下文可见的技能根目录集合（仅作清单缓存 key）。

    与 discover_external_turbo_skills 内部解析同源；失败返回空集
    （空集 key 只在"解析失败"的场景间共享，语义一致）。
    """
    try:
        from jiuwenswarm.common.utils import resolve_agent_registered_skill_dirs

        return frozenset(
            str(p) for p in resolve_agent_registered_skill_dirs()
        )
    except Exception:
        return frozenset()


def _collect_skill_entries() -> tuple[dict[str, str], bool]:
    """双源扫描当前可见技能，返回 (external_name -> 条目文案, 外部源扫描是否成功)。

    内置 skill_codes 目录 + 外部技能目录（turbo/turbo_codes，经
    ``turbo_package_loader.discover_external_turbo_skills`` 走
    ``resolve_agent_registered_skill_dirs`` 标准链路）下的 meta.json；
    external_name 同名时外部覆盖内置。外部源扫描异常时返回 ok=False
    （调用方可据此沿用上次成功清单，避免瞬时故障导致清单闪断）。

    带 TTL 缓存（见 ``_entries_cache``）：同一根目录集合在 TTL 窗口内的
    重复调用直接返回上次成功结果，不重复扫盘；仅成功结果落缓存。
    """
    from pathlib import Path

    from jiuwenswarm.server.runtime.skill_turbo.skill_meta import (
        find_skill_root_file,
        load_skill_meta,
    )

    roots = _resolve_skill_roots()
    now = time.monotonic()
    cached = _entries_cache.get(roots)
    if cached is not None and now - cached[0] < _ENTRIES_CACHE_TTL_SECONDS:
        return dict(cached[1]), True

    # external_name -> 条目文案（外部源后写入覆盖内置同名条目）
    entries_by_external: dict[str, str] = {}

    def _add_entry(external_name: str, description: str, match_keywords: list) -> None:
        trigger_hint = ""
        if match_keywords:
            trigger_hint = f"（触发词：{'、'.join(match_keywords[:5])}）"
        entries_by_external[external_name] = f"{external_name}（{description}{trigger_hint}）"

    # ─── 源一：内置 skill_codes 目录 ───────────────────────────
    try:
        skill_codes_dir = Path(__file__).resolve().parent / "skill_codes"
        if skill_codes_dir.is_dir():
            for skill_dir in sorted(skill_codes_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                name = skill_dir.name
                if name.startswith("_") or name.startswith("."):
                    continue
                if find_skill_root_file(skill_dir) is None:
                    continue
                meta = load_skill_meta(name, skill_dir)
                _add_entry(
                    str(meta.get("external_name") or name.replace("_", "-")),
                    str(meta.get("description") or f"{name} 任务流"),
                    list(meta.get("match_keywords") or [name]),
                )
    except Exception:
        logger.warning(
            "[ToolsLoader] builtin skill_codes description scan failed",
            exc_info=True,
        )

    # ─── 源二：外部 turbo同名覆盖内置 ──
    external_ok = True
    try:
        from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import (
            discover_external_turbo_skills,
        )

        for item in discover_external_turbo_skills():
            _add_entry(
                item.external_name,
                str(item.meta.get("description") or f"{item.skill_name} 任务流"),
                list(item.meta.get("match_keywords") or [item.skill_name]),
            )
    except Exception:
        external_ok = False
        logger.warning(
            "[ToolsLoader] external turbo description scan failed",
            exc_info=True,
        )

    if external_ok:
        _entries_cache[roots] = (now, dict(entries_by_external))
    return entries_by_external, external_ok


def _render_tool_description(entries: dict[str, str]) -> str:
    """把技能条目渲染为工具描述的清单主体。"""
    ordered = [entries[k] for k in sorted(entries)]
    skills_list = "、".join(ordered) if ordered else "暂无"
    return (
        "技能加速模块。当用户意图涉及技能类任务（如生成 PPT、文档转换等结构化产出）时，"
        "应优先调用此工具以获得更快的生成流程。工具内部会二次判断是否真正匹配已支持的技能，"
        "不匹配时自动降级为普通对话。当前内部支持："
        + skills_list
        + "。"
        "【重要】每次调用仅处理一个独立任务。若用户要求生成多个同类产物（如多份不同主题的 PPT），"
        "必须为每个产物分别发起独立调用，且严格串行：等待前一次调用完全结束并收到返回结果后，"
        "才能发起下一次调用。严禁在同一轮对话中并行发起多次调用。"
    )


# 静态尾部：模板排除段（临时措施，与清单无关，刷新时原样保留）
_TEMPLATE_EXCLUSION_SUFFIX = (
    "【临时排除】当任务需要使用自定义/指定模板时，不要调用此工具--"
    "无论该模板路径是出现在当前用户消息里、引用历史会话中生成或上传的模板"
    "（如“使用刚才生成的风格模板”“用之前那个模板”等自然语言引用）、"
    "还是消息中含 模板目录路径、模板主文件路径、pack_dir 等字段名。"
    "判断“是否涉及自定义模板”应基于语义。"
    "当前 skill 加速器暂未适配模板包功能，此类请求应改用 skill_tool 走 pptx-craft 标准流程。"
    "此排除规则为临时措施，待 skill 加速器适配模板功能后移除。"
)


def _build_tool_description() -> str:
    """从已注册 skill 生成 tool description（内置 skill_codes + 外部 turbo 双源）。

    模块导入时（@tool 装饰器求值 description 参数）执行一次作为初始值；
    运行期由 ``refresh_skill_acceleration_description`` 在每次模型调用前
    请求期刷新（见 SkillTurboPromptRail.before_model_call）——外部目录的
    可见性依赖请求级上下文（session 绑定目录 / env），启动时刻不可见，
    固化的初始清单可能为"暂无"，请求期刷新后即为当前真实清单。
    """
    entries, _external_ok = _collect_skill_entries()
    return _render_tool_description(entries) + _TEMPLATE_EXCLUSION_SUFFIX


# 最近一次外部源扫描成功的技能条目（进程级；瞬时扫描失败时沿用，防清单闪断）
_last_good_entries: dict[str, str] = {}


def refresh_skill_acceleration_description() -> list[str]:
    """请求期刷新 skill_acceleration_exec 的 ToolCard.description，返回当前技能清单。

    - 请求级上下文（session 绑定目录 / env）此时可见，外部发现能拿到真实清单；
    - 幂等：清单无变化时不产生 card 写入；
    - 异常安全：外部源扫描失败时沿用最近一次成功清单（``_last_good_entries``），
      绝不让描述闪断为"暂无"；整体异常时保留 card 旧值并返回最近已知清单；
    - 并发语义：ToolCard 为进程级共享对象，多请求并发下"最后写入生效"
      （per-user sidecar 内并发请求可见技能目录一致，无实际冲突）。

    Returns:
        当前可见技能的 external_name 列表（排序后；空列表表示无加速技能）。
    """
    global _last_good_entries
    try:
        entries, external_ok = _collect_skill_entries()
        if external_ok:
            _last_good_entries = dict(entries)
        elif _last_good_entries:
            entries = _last_good_entries
        card = globals().get("skill_turbo")
        card_obj = getattr(card, "card", None)
        if card_obj is not None:
            new_desc = _render_tool_description(entries) + _TEMPLATE_EXCLUSION_SUFFIX
            if card_obj.description != new_desc:
                card_obj.description = new_desc
        return sorted(entries)
    except Exception:
        logger.warning(
            "[ToolsLoader] refresh skill_acceleration_exec description failed, keep old",
            exc_info=True,
        )
        return sorted(_last_good_entries)

# 通用交付确认收尾：非 PPT 技能（docx/xlsx 等）交付节点产物 info 声明
# send_file_status=sent 时使用（与 NEUTRAL 的区别：明确文件已发送，
# 禁止重复调用/重复发送）。PPT 技能不走本提示（有专属骨架路径）。
# 按语言切换（语言源复用 request metadata；与 NEUTRAL 保持同语言）。
_SKILL_TURBO_STOP_HINT_CONFIRMED = {
    "cn": (
        "\n\n[SYSTEM] skill_acceleration_exec 任务已结束，内部交付流水线已确认文件"
        "生成并发送给用户。请基于上方产物摘要向用户总结结果。禁止对本任务再次调用 "
        "skill_acceleration_exec、skill_tool 或 send_file_to_user。"
    ),
    "en": (
        "\n\n[SYSTEM] The skill_acceleration_exec task has finished, and the internal "
        "delivery pipeline confirmed that the file(s) have been generated and sent "
        "to the user. Summarize the result for the user now based on the artifact "
        "summary above. Do NOT call skill_acceleration_exec, skill_tool, or "
        "send_file_to_user again for this task."
    ),
}


# 中立收尾（按语言切换，语言源复用 request metadata；两份停止提示保持同语言）
_SKILL_TURBO_STOP_HINT_NEUTRAL = {
    "cn": (
        "\n\n[SYSTEM] skill_acceleration_exec 任务已结束，但内部交付流水线未能确认"
        "文件已生成并发送给用户。请基于上方产物摘要如实向用户总结：明确说明哪些部分"
        "未完成或失败、文件尚未交付。不要声称文件已发送。除非用户明确要求重试，"
        "不要再次调用 skill_acceleration_exec 或 skill_tool。也不要调用 "
        "send_file_to_user；文件交付由内部流水线处理。"
    ),
    "en": (
        "\n\n[SYSTEM] The skill_acceleration_exec task has finished, but the internal "
        "delivery pipeline did NOT confirm that the file(s) were generated and sent "
        "to the user. You should now summarize this result to the user HONESTLY "
        "based on the artifact summary above: clearly state which parts are "
        "incomplete or failed and that the file has NOT been delivered. Do NOT "
        "claim the file was sent. Do NOT call skill_acceleration_exec or skill_tool "
        "again for this task unless the user asks for a retry. Do NOT call "
        "send_file_to_user either; file delivery is handled by the internal pipeline."
    ),
}

_PPT_DELIVERY_SUMMARY_POST_TOOL_HINT = {
    "cn": (
        "\n\n[SYSTEM] PPT 交付总结骨架将由系统在本工具结果之后通过流式通道发送给用户，"
        "无需在本回合重复输出交付总结。禁止再调 "
        "send_file_to_user / skill_tool / skill_acceleration_exec。"
        "若待办仍是 in_progress，下一轮先调 todo_modify 把对应步骤标为 completed，"
        "再给最终回复。"
    ),
    "en": (
        "\n\n[SYSTEM] The PPT delivery summary skeleton will be streamed to the user "
        "by the system after this tool result; do NOT repeat the delivery summary "
        "in this turn. Do NOT call send_file_to_user / skill_tool / "
        "skill_acceleration_exec again. If a todo is still in_progress, call "
        "todo_modify on the next turn to mark that step completed, then send "
        "the final reply."
    ),
}

# 续跑版：外层存在多项 todo 时，PPT 成功交付后引导模型先标记当前项完成再继续下一项，
# 而非一刀切终止本轮。必须显式"产物已交付"（防 R7 变体），必须含"先标记完成再继续"
# （对齐串行 todo 语义 _has_earlier_incomplete）。
_PPT_DELIVERY_SUMMARY_CONTINUE_HINT = {
    "cn": (
        "\n\n[SYSTEM] PPT 任务已完成且产物已由内部流水线交付，无需重复交付。"
        "请先将当前待办标记为完成（todo_modify），再继续执行下一待办项"
        "（下一项：{next_summary}）。"
        "禁止对本任务重调 skill_acceleration_exec / skill_tool / send_file_to_user。"
    ),
    "en": (
        "\n\n[SYSTEM] The PPT task is complete and the artifact has been delivered "
        "by the internal pipeline; do NOT re-deliver. First mark the current todo as "
        "completed (todo_modify), then proceed to the next todo item "
        "(next item: {next_summary}). "
        "Do NOT re-invoke skill_acceleration_exec / skill_tool / send_file_to_user for this task."
    ),
}

_EN_LANGUAGE_VALUES = frozenset({"en", "en-us", "english"})
_CN_LANGUAGE_VALUES = frozenset({"zh", "cn", "zh-cn", "chinese"})


def _resolve_stop_hint_language() -> str:
    """停止提示语言：读 request metadata 的 language/preferred_language，默认中文。"""
    try:
        meta = get_current_request_metadata()
    except Exception:
        return "cn"
    if isinstance(meta, dict):
        for key in ("language", "preferred_language"):
            raw = str(meta.get(key) or "").strip().lower()
            if raw in _EN_LANGUAGE_VALUES:
                return "en"
            if raw in _CN_LANGUAGE_VALUES:
                return "cn"
    return "cn"

# HITL 续跑没有外层 tool_result / DeliverySummaryRail；可见终稿用骨架或安全短句，
# 禁止把产物摘要账本发给用户。
# HITL 续跑无 P10 骨架（交付未确认）时的用户可见终稿。
PPT_TURBO_UNCONFIRMED_FINISH_TEXT = (
    "PPT 任务已结束，但未能确认文件已生成并发送，部分环节可能未完成。"
    "请查看上方过程信息，或让我重试补齐。"
)
_SKILL_TURBO_ARTIFACT_SUMMARY_MARKER = "[SkillAccelerationExec 产物摘要]"

# HITL 暂停时工具不再返回本占位文案（改返回 _SKILL_TURBO_HITL_RESULT_KEY 标记
# dict，见 build_skill_turbo_hitl_result）。本常量保留两个用途：
# 1) after_tool_call 改写后的 tool_msg 文案基准（"已暂停等待回答"，非"被用户打断"，
#    防止外层模型把暂停误读为失败而放弃加速通道）；
# 2) _fix_incomplete_tool_context 的中断占位识别模板。
# 文案是"相位盲"的：写入时机是 HITL 暂停，但它留在会话历史中被所有后续相位的
# LLM 读到，故必须按用户意图分叉而非无条件禁止行动：
# - 作答 / 暂停仍有效 → 系统自动恢复，禁止重调本工具恢复（防子代理"重调即续跑"
#   误读，F_01 首版教训）；
# - 非作答消息（停止/取消/调整/新任务，即 cancel 已清断点、无自动恢复）→ 必须
#   按消息意图行动，禁止被动等待（v2 文案"也无需其他操作"曾诱导 LLM 在 cancel
#   后回复"正在继续生成"然后空转收尾——前端显示任务完成但什么都没跑，F_01 二次
#   教训）。修订本文案时两条约束都必须保持。
_SKILL_TURBO_HITL_PLACEHOLDER = (
    "[技能加速已暂停] 工具 skill_acceleration_exec 正在等待用户回答，"
    "没有失败。用户回答后系统将自动恢复执行，"
    "请勿重新调用本工具来恢复执行。"
    "若后续用户消息不是对该问题的回答（如要求停止、取消、调整或另起新任务），"
    "说明本次暂停已被终止、不存在自动恢复，"
    "必须按该消息的用户意图正常处理；"
    "若需继续原任务，可基于会话中已生成的产物文件继续。"
)
# 历史版本文案，仅用于识别已持久化的历史 tool_msg（流式恢复/上下文修复场景
# 需匹配历史消息中的占位文本），新消息一律使用上面的新文案。
# v1（bbc64b25c 前）："回答后将自动继续执行"——诱导子代理"重调即续跑"。
_SKILL_TURBO_HITL_PLACEHOLDER_LEGACY = (
    "[技能加速已暂停] 工具 skill_acceleration_exec 正在等待用户回答，"
    "回答后将自动继续执行，没有失败。"
)
# v2（F_01 首版修订）："请勿再次调用，也无需其他操作"——cancel 后相位诱导被动空转。
_SKILL_TURBO_HITL_PLACEHOLDER_LEGACY_V2 = (
    "[技能加速已暂停] 工具 skill_acceleration_exec 正在等待用户回答，"
    "没有失败。用户回答后系统将自动恢复执行，"
    "请勿再次调用 skill_acceleration_exec，也无需其他操作。"
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

# 仅 task.update 可带外推送：它是全量 taskProgress 快照，后到 FIFO 覆盖先到，幂等。
# task.start/task.complete 驱动前端 taskStack，必须与 chat.* 保持 FIFO 顺序；
# 带外抢先 complete 会导致迟到的思考/工具调用丢 segment（见外层 todo 注释）。
_SKILL_TURBO_OOB_TASK_EVENT_TYPES: frozenset[str] = frozenset({"task.update"})


def _without_inner_task_routing(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a parent-bound event without its SkillTurbo-only task id."""
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith("task_"):
        return payload
    cleaned = dict(payload)
    cleaned.pop("task_id", None)
    return cleaned


def _task_ids_from_snapshot(payload: Any) -> set[str]:
    """从 task.update 快照提取任务集 task_id 集（槽位登记簿的观测值）。"""
    if not isinstance(payload, dict):
        return set()
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return set()
    return {
        str(t.get("task_id")).strip()
        for t in tasks
        if isinstance(t, dict) and str(t.get("task_id") or "").strip()
    }


def _is_terminal_task_snapshot(payload: Any) -> bool:
    """终态快照判定：tasks 非空且全部状态落在 {completed, failed}。

    依据 tasks 列表逐项 status，不依赖计数字段（in_progress_tasks 等）——
    事件构造方（executor）虽有计数，但以逐项判定为唯一口径可容忍缺计数的
    快照来源。空列表不算终态：无内容可关账。
    """
    if not isinstance(payload, dict):
        return False
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False
    statuses: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            return False
        statuses.add(str(t.get("status") or "").strip())
    return bool(statuses) and statuses.issubset({"completed", "failed"})


def _parent_session_slot_key(parent_session: Any) -> str:
    """槽位登记簿键：外层父会话 session_id。

    与外层 rail 侧（task_execution_rail._emit_task_update_event 的
    session.get_session_id()）同一 session 对象、同一 id 口径——两侧键不对齐
    则外层夺回时的注销永远打不中。
    """
    if parent_session is None:
        return ""
    getter = getattr(parent_session, "get_session_id", None)
    sid = ""
    if callable(getter):
        try:
            sid = str(getter() or "").strip()
        except Exception:
            sid = ""
    if not sid:
        sid = str(getattr(parent_session, "session_id", "") or "").strip()
    return sid


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


async def _push_task_event_out_of_band(
    event_type: str,
    payload: dict[str, Any],
    *,
    request_id: str,
    channel_id: str,
    session_id: str,
) -> None:
    """Bypass the parent stream FIFO for task.update snapshots only.

    ``chat.file`` already uses PushRegistry, so PPT can appear while
    ``write_stream`` is still draining thousands of page-gen deltas. The
    right-hand task list is driven by ``task.update`` snapshots that used
    only that FIFO, leaving Stage 11 in_progress after delivery. Later FIFO
    ``task.update`` snapshots remain idempotent. ``task.start`` /
    ``task.complete`` stay on the FIFO so they keep pace with ``chat.*``.
    """
    if event_type not in _SKILL_TURBO_OOB_TASK_EVENT_TYPES:
        return
    if not request_id or not channel_id or not session_id:
        logger.debug(
            "[SkillTurboTool] skip send_push %s: missing ids request_id=%s "
            "channel_id=%s session_id=%s",
            event_type,
            bool(request_id),
            bool(channel_id),
            bool(session_id),
        )
        return
    try:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        server = AgentWebSocketServer.get_instance()
    except Exception:
        logger.debug(
            "[SkillTurboTool] skip send_push %s: AgentWebSocketServer unavailable",
            event_type,
            exc_info=True,
        )
        return

    push_payload = dict(payload)
    push_payload.setdefault("event_type", event_type)
    msg = {
        "request_id": request_id,
        "channel_id": channel_id,
        "session_id": session_id,
        "payload": push_payload,
        "is_complete": False,
    }
    try:
        delivered = await server.send_push(msg)
        logger.info(
            "[SkillTurboTool] send_push %s delivered=%s request_id=%s session_id=%s",
            event_type,
            delivered,
            request_id,
            session_id,
        )
    except Exception:
        logger.warning(
            "[SkillTurboTool] send_push %s failed request_id=%s",
            event_type,
            request_id,
            exc_info=True,
        )


async def _settle_leaked_task_slot(
    parent_session: Any,
    request_id: Any,
    channel_id: Any,
    external_session_id: Any,
    last_task_update: Any,
) -> None:
    """B_03：skip 入口终止性出口的槽位清算。

    入口 gate 误判 forwarded 的竞态会让 turbo 任务集顶掉前端唯一的
    taskProgress 槽位；若该轮终态前被打断（HITL），续跑入口判 skip，
    终态快照被抑制——槽位永驻 in_progress。本函数在**终止性出口**
    （成功 / 未处理 / 普通异常；AbortError 暂停除外，暂停后任务集还要续跑）
    检查登记簿（task_slot_guard）：有未闭合占用且本入口缓存到终态快照时
    补发关账。

    只放行 task.update 全量快照（幂等、后到覆盖先到、可 OOB）；task.start /
    task.complete 仍禁——skip 语义下放行它们会破坏 chat.* 归段（taskStack
    嵌套 / segment 分裂，正是 skip 机制要防的）。FIFO 写失败保留登记，
    下次终止性出口重试；写成功后 OOB 尽力直推（右侧任务列表由 OOB 驱动）。
    """
    if parent_session is None or not isinstance(last_task_update, dict):
        return
    if not _is_terminal_task_snapshot(last_task_update):
        # 非终态快照关不了账（流中途异常等）；登记保留，待后续运行治愈。
        return
    slot_key = _parent_session_slot_key(parent_session)
    if not slot_key:
        return
    leaked_ids = peek_leaked_task_slot(slot_key)
    if leaked_ids is None:
        return
    from openjiuwen.core.session.stream.base import OutputSchema

    payload = dict(last_task_update)
    output_type, mapped = _prepare_parent_stream_output("task.update", payload)
    try:
        await parent_session.write_stream(
            OutputSchema(type=output_type, index=0, payload=mapped)
        )
    except Exception:
        logger.warning(
            "[SkillTurboTool] leaked slot settle write_stream failed "
            "session=%s leaked_ids=%s",
            slot_key,
            sorted(leaked_ids),
            exc_info=True,
        )
        return
    release_task_slot(slot_key)
    logger.info(
        "[SkillTurboTool] settled leaked task slot: session=%s "
        "leaked_ids=%s settled_tasks=%d",
        slot_key,
        sorted(leaked_ids),
        len(payload.get("tasks") or []),
    )
    await _push_task_event_out_of_band(
        "task.update",
        mapped,
        request_id=str(request_id or ""),
        channel_id=str(channel_id or ""),
        session_id=_resolve_skill_turbo_resume_session_id(
            external_session_id, parent_session
        ),
    )


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


# 外层 todo 当前绑定项之后的未完成项快照（供停止提示条件化）。
# task_execution_rail.before_tool_call 导出，context_binding 绑定到此 ContextVar，
# _wrap_skill_turbo_result 读取后选择续跑版 / 停止版停止提示。
# 值形态：{"count": N, "next_summary": "..."} 或 None（无后续未完成项）。
_skill_turbo_outer_todo_pending: ContextVar[dict[str, Any] | None] = ContextVar(
    "skill_turbo_outer_todo_pending", default=None
)


def set_skill_turbo_outer_todo_pending(pending: dict[str, Any] | None) -> Token:
    return _skill_turbo_outer_todo_pending.set(pending)


def get_skill_turbo_outer_todo_pending() -> dict[str, Any] | None:
    return _skill_turbo_outer_todo_pending.get()


def reset_skill_turbo_outer_todo_pending(token: Token) -> None:
    _skill_turbo_outer_todo_pending.reset(token)


# 外层 todo 当前绑定项之前已完成的项数（B_04 fresh 守卫放行信号）。
# task_execution_rail.before_tool_call 计算导出，context_binding 绑定到此
# ContextVar，_fresh_call_artifacts_guard 读取：≥1 表示本次调用是清单推进
# 的后续交付物（首个交付物已收尾），残留产物属于上一交付物而非本任务——
# 放行 fresh 调用，产物清理交 executor 开跑时的 _clear_stale_node_artifacts。
# 值形态：int（0=绑定项之前无已完成项）或 None（无外层 todo 绑定）。
_skill_turbo_outer_todo_completed_before: ContextVar[int | None] = ContextVar(
    "skill_turbo_outer_todo_completed_before", default=None
)


def set_skill_turbo_outer_todo_completed_before(count: int | None) -> Token:
    """Bind how many todo items before the bound one are already done."""
    return _skill_turbo_outer_todo_completed_before.set(count)


def get_skill_turbo_outer_todo_completed_before() -> int | None:
    return _skill_turbo_outer_todo_completed_before.get()


def reset_skill_turbo_outer_todo_completed_before(token: Token) -> None:
    _skill_turbo_outer_todo_completed_before.reset(token)


# ── B_05：后续交付物 fresh 放行的「绑定项切换」登记（模块级跨请求单例）──
# completed_before 只是绑定时刻算出的静态计数（绑定项存续期内不变），只表达
# "清单推进到了第 2+ 项"，无法区分"清单推进到新交付物的首次调用"与"同一交付物
# 已成功后的盲目重跑"。本登记记录每个会话上次放行时绑定的外层 todo id，守卫放行
# 前比对：绑定项相较上次**切换**才放行，相同（同项盲目重跑）落回产物检查——F_01
# 对"同任务盲目重跑"的保护面对第 2+ 项不放松。键 = 守卫的 sid 口径（外层父会话
# id）；值 = 上次放行时的 get_current_task_id()。仅随后续交付物放行分支写；上限
# 淘汰同 task_slot_guard 先例（防长生命周期进程无界增长）。
_SUBSEQUENT_DELIVERABLE_LAST_TODO_ID: dict[str, str | None] = {}
_MAX_TRACKED_SUBSEQUENT_DELIVERABLE_SESSIONS = 4096


def _release_subsequent_deliverable(
    sid: str, current_todo_id: str | None,
) -> bool:
    """判定并登记"后续交付物 fresh 放行"：仅绑定项相较上次放行切换才放行。

    - 拿不到会话键或绑定项 id（含测试桩 / 退化态）→ 退回旧语义无条件放行；
    - 绑定项相同（同项盲目重跑）→ 返回 False，调用方落回产物检查；
    - 绑定项不同 / 首次 → 放行并登记（返回 True）。
    """
    key = str(sid or "").strip()
    if not key or not current_todo_id:
        return True
    if _SUBSEQUENT_DELIVERABLE_LAST_TODO_ID.get(key) == current_todo_id:
        return False
    if (
        key not in _SUBSEQUENT_DELIVERABLE_LAST_TODO_ID
        and len(_SUBSEQUENT_DELIVERABLE_LAST_TODO_ID)
        >= _MAX_TRACKED_SUBSEQUENT_DELIVERABLE_SESSIONS
    ):
        _SUBSEQUENT_DELIVERABLE_LAST_TODO_ID.pop(
            next(iter(_SUBSEQUENT_DELIVERABLE_LAST_TODO_ID))
        )
    _SUBSEQUENT_DELIVERABLE_LAST_TODO_ID[key] = current_todo_id
    return True


def reset_subsequent_deliverable_registry() -> None:
    """清空后续交付物放行登记（测试隔离用；生产零调用）。"""
    _SUBSEQUENT_DELIVERABLE_LAST_TODO_ID.clear()


# ── ContextVar：SkillTurbo HITL 中断信号 ──
# skill_turbo_tools catch AbortError 后提取 ToolInterruptException 存入此 ContextVar，
# StreamEventRail.after_tool_call 读取后改写 ctx.inputs.tool_result 为 TIE，
# 使 harness 原生 HITL 机制（build_interrupt_state）检测并触发暂停。
# ContextVar 依赖工具体与 after_tool_call 同执行上下文，跨上下文时不可见，
# 仅作为旧路径兜底；主信号走工具返回值中的 _SKILL_TURBO_HITL_RESULT_KEY 标记。
_skill_turbo_hitl_tic: ContextVar[Any] = ContextVar("skill_turbo_hitl_tic", default=None)

# HITL 暂停信号随工具返回值传递：SDK tracer 在工具结束时会对含返回值的
# span 执行 copy.deepcopy，故标记值必须是可深拷贝的纯数据（dict/JSON），
# 禁止直接返回 ToolInterruptException 等异常对象（无法按 SDK 方式重建）。
_SKILL_TURBO_HITL_RESULT_KEY = "__skill_turbo_hitl__"


def set_skill_turbo_hitl_tic(tic: Any) -> Token:
    return _skill_turbo_hitl_tic.set(tic)


def get_skill_turbo_hitl_tic() -> Any:
    return _skill_turbo_hitl_tic.get()


def build_skill_turbo_hitl_result(tic: Any) -> dict[str, Any]:
    """构造随工具返回值传递的 HITL 暂停标记（deepcopy 安全的纯数据）。"""
    request = getattr(tic, "request", None)
    dump = request.model_dump(mode="json") if request is not None else {}
    return {_SKILL_TURBO_HITL_RESULT_KEY: True, "request": dump}


def set_pending_ppt_delivery_summary(summary: str) -> None:
    text = str(summary or "").strip()
    _pending_ppt_delivery_summary.set(text or None)


def clear_pending_ppt_delivery_summary() -> None:
    _pending_ppt_delivery_summary.set(None)


def take_pending_ppt_delivery_summary() -> str:
    text = str(_pending_ppt_delivery_summary.get() or "").strip()
    _pending_ppt_delivery_summary.set(None)
    return text


def _ppt_delivery_summary_start() -> str:
    """PPT 交付总结骨架起始标记（常量归 ppt code 所有，经动态包引用）。

    包名经 ``dynamic_package_prefix("ppt")`` 构造（前缀单一来源，
    禁止手写 ``skill_turbo_codes_`` 字面量，防止技能改名时骨架标记
    静默漂移）。ppt 技能不在场（动态包未注册）时返回空串——调用方
    对空标记的 startswith 判断显式短路，走各自的兜底文案路径。

    .. note::
        当前为 PPT 专属交付确认机制（resume 骨架 + send_file 判定）。
        泛化路线图（meta.json ``deliver_skeleton`` 声明 + 引擎按声明路由）
        见设计方案 §10 后续扩展，届时新增带交付确认的技能无需改引擎。
    """
    try:
        import importlib

        from jiuwenswarm.server.runtime.skill_turbo.turbo_package_loader import (
            dynamic_package_prefix,
        )

        module = importlib.import_module(
            f"{dynamic_package_prefix('ppt')}ppt.delivery_summary"
        )
        return str(module.DELIVERY_SUMMARY_START)
    except Exception:
        return ""


async def emit_pending_ppt_delivery_summary(session: "Session") -> bool:
    """在外层 tool_result 之后发出无 task_id 的交付总结 chat.delta。

    返回是否实际发出。session 为 None 或骨架非法时静默跳过。
    """
    from openjiuwen.core.session.stream.base import OutputSchema

    summary = take_pending_ppt_delivery_summary()
    start_marker = _ppt_delivery_summary_start()
    if not start_marker or not summary.startswith(start_marker):
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
    lines = [_SKILL_TURBO_ARTIFACT_SUMMARY_MARKER]
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


def _ppt_delivery_failed_error(artifact_holder: dict[str, Any] | None) -> str:
    """P10 明确交付失败时，禁止把 skill_acceleration_exec 收成 success。"""
    node = (artifact_holder or {}).get("p10_delivery")
    if not isinstance(node, dict):
        return ""
    info = node.get("info") if isinstance(node.get("info"), dict) else {}
    if str(info.get("delivery_status") or "") != "failed":
        return ""
    return (
        "PPT 生成失败：未产出可交付的 pptx 文件。"
        "请根据流水线失败阶段重试，不要告知用户已经生成成功。"
    )


def _generic_delivery_confirmed(artifact_holder: dict[str, Any] | None) -> bool:
    """非 PPT 技能的通用交付确认：任一节点产物 info 声明 send_file_status=sent。

    仅认交付节点显式写入的确认信号（docx D8 / xlsx X9 的
    ``__artifact__.info.send_file_status == "sent"``），不做模糊推断。
    PPT 技能不经过本函数（有专属 p10_delivery 骨架路径，且 sent 必伴随
    骨架，先行分支已拦截）。
    """
    for node_info in (artifact_holder or {}).values():
        if not isinstance(node_info, dict):
            continue
        info = node_info.get("info")
        if not isinstance(info, dict):
            continue
        if str(info.get("send_file_status") or "") == "sent":
            return True
    return False


def visible_ppt_turbo_finish_text(
    holder: dict[str, Any] | None,
    *,
    success: bool,
    detail: str = "",
) -> str:
    """HITL 续跑的用户可见终稿（无外层 skill_acceleration_exec 工具循环）。

    成功时优先发 P10 已填好的交付骨架；没有骨架则用交付未确认的中立短句。
    失败只回可读错误。产物账本不得出现在返回值里。
    """
    if success:
        skeleton = _ppt_delivery_summary(holder)
        start_marker = _ppt_delivery_summary_start()
        if start_marker and skeleton.startswith(start_marker):
            return skeleton
        return PPT_TURBO_UNCONFIRMED_FINISH_TEXT
    text = str(detail or "").strip() or "任务未完成"
    if _SKILL_TURBO_ARTIFACT_SUMMARY_MARKER in text:
        return "任务未完成"
    return text


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
        lang = _resolve_stop_hint_language()
        parts = [result_dict.get("result") or ""]
        if artifact_text:
            parts.append(artifact_text)
        if ppt_summary:
            # 骨架不进 tool_result 正文、也不在流水线内提前 chat.delta；
            # 挂到 ContextVar，由 after_tool_call 在外层 tool_result 之后流式发出。
            set_pending_ppt_delivery_summary(ppt_summary)
            # 外层存在多项 todo 时，引导模型先标记当前项完成再继续下一项，
            # 而非一刀切终止本轮（防外层 todo 续跑阻断）。
            pending = get_skill_turbo_outer_todo_pending()
            if pending and pending.get("count"):
                parts.append(
                    _PPT_DELIVERY_SUMMARY_CONTINUE_HINT[lang].format(
                        next_summary=pending.get("next_summary") or "见待办列表"
                    )
                )
            else:
                parts.append(_PPT_DELIVERY_SUMMARY_POST_TOOL_HINT[lang])
        else:
            clear_pending_ppt_delivery_summary()
            if _generic_delivery_confirmed(artifact_holder):
                # 非 PPT 技能交付节点已确认发送（case_3 docx D8 send=sent
                # 却被"未确认"提示误导重复发送）：按已确认收尾。
                parts.append(_SKILL_TURBO_STOP_HINT_CONFIRMED[lang])
            else:
                # 无 P10 骨架且无通用确认信号 = 交付未确认，不宣称文件已发送。
                parts.append(_SKILL_TURBO_STOP_HINT_NEUTRAL[lang])
        result_dict["result"] = "\n\n".join(p for p in parts if p)
    else:
        clear_pending_ppt_delivery_summary()
        parts = [result_dict.get("error") or ""]
        if artifact_text:
            parts.append(artifact_text)
        result_dict["error"] = "\n\n".join(p for p in parts if p)
    return result_dict


# ────────────────── fresh 调用守卫（中断残留产物拦截，fresh_start 显式放行） ──────────────────
# cancel/中断按 [[S_06]] 语义保留 node_artifacts（"供降级 DeepAgent 复用"），本
# 守卫是其消费方：fresh 调用 skill_acceleration_exec 时若会话有残留产物记录，
# 返回失败 + 产物摘要，阻止新 executor 从 Stage 1 清盘重跑。
#
# 机制性拦截语义（F_01 修订）：产物记录存在期间，每次 fresh 调用（未携带
# fresh_start=true）一律拦截——拦截不清库，任何调用者（外层 LLM / 子代理 /
# 其它入口）重复调用均持续被拦；仅当调用显式携带 fresh_start=true（LLM 明确
# 全新任务）时清空产物记录并放行。早期"一次性拦截"（拦一次即清库/登记 sid）
# 被外层派发的子代理隔层重试击穿：子代理看不到外层的拒绝 tool_result，拿不到
# 一次性语义，重调即放行导致清盘重跑（case: turbo ppt 暂停后继续从头重跑）。


def _build_fresh_artifacts_guard_error(summary: str, skill: str) -> str:
    """构造 fresh 调用守卫拒绝文案（引导基于产物继续，或显式 fresh_start 重跑）。"""
    skill_hint = f"（技能：{skill}）" if skill else ""
    return (
        f"检测到本会话上一轮 SkillAccelerationExec{skill_hint}留有已完成节点产物：\n\n"
        f"{summary}\n\n"
        "直接重新调用会丢弃以上产物记录并从 Stage 1 全量重跑，"
        "已完成的工作将被重复执行。\n"
        "- 若用户想继续、完善或检查之前的任务：请勿调用 skill_acceleration_exec，"
        "改用 skill_tool 走标准流程（可基于以上产物文件继续），"
        "或先用 read_file 查看产物文件当前状态后再决定后续操作；\n"
        "- 仅当用户明确要求与以上产物无关的全新任务时，才重新调用 "
        "skill_acceleration_exec 并显式携带参数 fresh_start=true"
        "（表示确认丢弃旧产物、从头执行）。"
    )


async def _fresh_call_artifacts_guard(
    *,
    adapter: Any,
    parent_session: Any,
    external_session_id: str,
    fresh_start: bool = False,
) -> dict[str, Any] | None:
    """fresh 调用守卫：会话残留 SkillTurbo 节点产物时拦截全新 turbo 调用。

    仅在非 resume（resume_ctx 为 None）路径调用——resume 重放由
    ``_inherit_node_artifacts_for_replay`` 按 plan_code_hash 继承产物，无需拦截。
    返回 None 表示放行；返回 ``{"success": False, "error": ...}`` 表示拦截。
    全链 fail-open：任何异常放行，不影响主流程。

    拦截/放行语义（机制性，F_01 修订版）：
    - ``fresh_start=False`` 且产物记录存在 → 拦截，**不清库**（产物记录是
      P2-2 hash 继承与后续拦截的数据源，拦截本身不消耗它）；
    - ``fresh_start=True`` → 显式全新任务：清空产物记录后放行（清库失败仅
      warning，executor 的 _clear_stale_node_artifacts 仍会在 fresh run 兜底清）。

    B_04 后续交付物放行：外层 todo 清单推进到第二项及以后（早于绑定项存在
    已完成项，``outer_todo_completed_before`` 通道 ≥1）时，残留产物属于已
    收尾的上一交付物而非本任务——放行且**不在守卫内清库**（executor 开跑时
    ``_clear_stale_node_artifacts`` 按同序兜底清理）；无绑定（None）或绑定项
    之前无已完成项（0）仍拦，F_01 对"同任务盲目重跑"的保护不放松。
    """
    from openjiuwen.core.session.agent import create_agent_session
    from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import (
        clear_node_artifacts,
        load_node_artifacts,
    )
    from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
        set_skill_turbo_id,
    )

    sid = _resolve_skill_turbo_resume_session_id(external_session_id, parent_session)

    # B_04 后续交付物放行（快路径，免开 session）：外层 todo 清单推进到
    # 第二项及以后——早于绑定项存在已完成项时，残留产物属于已收尾的上一
    # 交付物而非本任务，fresh 调用是清单推进的正常流程而非盲目重跑。
    # 仅在未显式携带 fresh_start 时走本分支（fresh_start=true 的显式清库
    # 语义优先，两者都放行但清理时机不同：本分支交 executor 开跑时清理）。
    #
    # B_05 收紧：completed_before 是绑定时刻算出的静态计数，绑定项存续期内
    # 不变，无法区分"清单推进到新交付物（应放行）"与"同一交付物已成功后的
    # 盲目重跑（应拦）"。故仅在绑定项相较上次放行**切换**时放行；同绑定项
    # 重复 fresh 调用落回下方产物检查——命中残留产物即拦并提示 fresh_start
    # （F_01 对"同任务盲目重跑"的保护面对第 2+ 项不放松）。
    if not fresh_start:
        completed_before = get_skill_turbo_outer_todo_completed_before()
        if isinstance(completed_before, int) and completed_before > 0:
            if _release_subsequent_deliverable(sid, get_current_task_id()):
                logger.info(
                    "[SkillTurboTool] fresh guard bypassed by outer todo "
                    "progression (subsequent deliverable, bound item switched) "
                    "sid=%s completed_before=%d",
                    sid,
                    completed_before,
                )
                return None
            logger.info(
                "[SkillTurboTool] fresh guard same-bound-item re-invoke "
                "(subsequent deliverable blind rerun) sid=%s completed_before=%d; "
                "falling through to artifact check",
                sid,
                completed_before,
            )

    card = getattr(getattr(adapter, "_instance", None), "card", None)
    guard_session = None
    try:
        guard_session = (
            create_agent_session(session_id=sid, card=card)
            if sid
            else create_agent_session(card=card)
        )
        # 必须与 executor 一致使用 __skill_turbo 隔离 checkpointer key，
        # 否则读不到产物记录（同 _inherit_node_artifacts_for_replay 范式）。
        set_skill_turbo_id(guard_session, card)
        await guard_session.pre_run(inputs=None)
    except Exception:
        # session 打不开（含测试桩场景）：fail-open 放行。session 未经 pre_run
        # 成功打开，不做 post_run（未初始化的 emitter 可能有副作用）。
        logger.warning(
            "[SkillTurboTool] fresh guard session setup failed (fail-open) sid=%s",
            sid,
            exc_info=True,
        )
        return None

    try:
        if fresh_start:
            try:
                await clear_node_artifacts(guard_session)
                logger.info(
                    "[SkillTurboTool] fresh guard bypassed by fresh_start=true sid=%s",
                    sid,
                )
            except Exception:
                logger.warning(
                    "[SkillTurboTool] fresh_start clear artifacts failed "
                    "(executor stale-clear still applies) sid=%s",
                    sid,
                    exc_info=True,
                )
            return None

        state = await load_node_artifacts(guard_session)
        if not state:
            return None
        nodes = state.get("nodes") or {}
        summary = _build_artifact_summary(nodes)
        if not summary:
            return None
        skill = str(state.get("skill") or "").strip()
        logger.info(
            "[SkillTurboTool] fresh guard fired sid=%s skill=%s nodes=%d",
            sid,
            skill,
            len(nodes),
        )
        return {
            "success": False,
            "error": _build_fresh_artifacts_guard_error(summary, skill),
        }
    except Exception:
        logger.warning(
            "[SkillTurboTool] fresh guard failed (fail-open, fresh run proceeds) sid=%s",
            sid,
            exc_info=True,
        )
        return None
    finally:
        try:
            await guard_session.post_run()
        except Exception:
            logger.debug(
                "[SkillTurboTool] fresh guard post_run failed sid=%s",
                sid,
                exc_info=True,
            )


@tool(
    name="skill_acceleration_exec",
    description=_build_tool_description(),
    stateless=True,
)
async def skill_turbo(query: str, fresh_start: bool = False) -> dict[str, Any] | str:
    """执行 SkillAccelerationExec 任务。

    Args:
        query: 对单个任务的忠实总结，须严格基于用户原话与历史上下文中已有的信息，不得自行扩写、脑补或补充用户未提及的内容细节（如擅自罗列章节大纲、技术要点、子主题等）。仅在用户表达零散时做必要的凝练与指代消解，确保任务目标、产物与约束完整可执行，但不新增任何信息。每次调用只处理一个任务；若用户要求多个任务，必须串行调用：等待前一次调用完成并收到返回结果后，再发起下一次调用。
        fresh_start: 显式确认全新任务。仅当用户明确要求一个与本会话已有加速产物完全无关的新任务时才设为
            true（将丢弃旧产物记录并从头执行）；默认 false。若调用被拒绝并提示"存在已完成节点产物"，
            继续旧任务应改用 skill_tool，不要靠本参数重试。
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
    # user message）。两层引用同一份关键词（region_edit.py），叠加降低误进概率。
    # 删除方式：待 pptx-craft 流水线支持"编辑已有 PPT"短路分支后，搜索 [REGION-EDIT-BYPASS] 删除本块。
    from jiuwenswarm.server.runtime.skill_turbo.region_edit import REGION_EDIT_KEYWORDS
    if any(kw in query for kw in REGION_EDIT_KEYWORDS):
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

    # B_03 槽位登记簿键 + skip 抑制快照缓存：转发分支放行 task.update 时按此键
    # 登记"槽位被本任务集占用"；skip 分支被抑制的 task.update 缓存最后一条，
    # 终止性出口（_settle_leaked_task_slot）用它关账。
    slot_key = _parent_session_slot_key(parent_session)
    last_suppressed_task_update: dict[str, Any] | None = None

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
        # 用户上传附件：从 metadata 提取 files 并传入 inputs["files"]，
        # 供流水线 intent_classify 检测文档路径（has_documents 门控 P3 文档解析）。
        # files 源于 request.params["files"]，由 _update_runtime_config 注入 metadata。
        # 可能为 dict（平台 files.uploaded 格式）或 list（结构化附件）；
        # intent_classify._collect_files_paths 统一展平，此处原样透传。
        _files = request_metadata.get("files")
        if _files is not None:
            inputs["files"] = _files

    turbo_session = None
    resume_ctx = None
    resume_answers = get_skill_turbo_resume_answers()
    if resume_answers is not None:
        try:
            from openjiuwen.core.session.agent import create_agent_session
            from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
                load_resume_ctx,
                mark_resume_in_flight,
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

    if resume_ctx is not None:
        try:
            adapter.restore_skill_turbo_model_from_resume_ctx(resume_ctx)
        except Exception:
            logger.exception(
                "[SkillTurboTool] resume model restore failed; aborting resume",
            )
            if turbo_session is not None:
                try:
                    await turbo_session.post_run()
                except Exception:
                    logger.debug(
                        "[SkillTurboTool] resume model restore post_run failed",
                        exc_info=True,
                    )
                turbo_session = None
            raise

    # fresh 调用守卫：上一轮 turbo 中断/取消后 node_artifacts 按 [[S_06]] 语义保留
    # （"供降级 DeepAgent 复用"），本守卫是其唯一消费方——有残留产物且未显式声明
    # fresh_start 时返回失败并附摘要，引导 LLM 走 skill_tool / read_file 基于产物
    # 继续，阻止盲目 fresh 重启 turbo 从 Stage 1 清盘重跑；仅 fresh_start=true
    # （LLM 明确全新任务）清空产物记录后放行。
    if resume_ctx is None:
        guard_reject = await _fresh_call_artifacts_guard(
            adapter=adapter,
            parent_session=parent_session,
            external_session_id=external_session_id,
            fresh_start=fresh_start,
        )
        if guard_reject is not None:
            return _wrap_skill_turbo_result(guard_reject)

    # 构造 config 和 SkillTurbo 实例；resume 模式必须在此之前恢复原模型。
    config = adapter.build_skill_turbo_config()
    skill_turbo_inst = SkillTurbo(config)

    hitl_interrupt = False

    # 仅当本次请求实际调用了 mark_resume_in_flight 时才在 finally 清除，防重复
    # 作答（resume_in_flight 命中提前返回）误清原始请求设置的 in-flight 标志。
    _did_mark_in_flight = False

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

            # 防重：resume_in_flight 命中 = 双击/网关重试作答，跳过本次避免
            # 两次 resume_stream 重复生成（对齐 Path A 的 resume_in_flight 防重）。
            if resume_ctx.get("resume_in_flight"):
                logger.warning(
                    "[SkillTurboTool] duplicate resume answers while in flight; ignoring"
                )
                # 裸 dict 返回，不经 _wrap_skill_turbo_result：本分支的
                # SkillTurbo 是 fresh 实例（artifact_holder 为空），wrap 必然
                # 追加 NEUTRAL 停止提示（"任务已结束…不要声称文件已发送"），
                # 与"任务正在处理中"语义矛盾——原始请求仍在执行、稍后会交付。
                # 且不携带 success 键：stream_event_rail._emit_tool_result 仅在
                # _infer_tool_result_error 可判定时才写帧级 success，本 dict 形态
                # 判 None → chat.tool_result 帧不带 success 字段 → 上层应用侧
                # isSuccessfulPptTurboToolResult（结构匹配 success===true）不会
                # 把占位帧误判成 PPT turbo 成功、替换最终正文（[[B_01]]）。
                return {
                    "result": "任务正在处理中，本次重复作答已忽略，请等待原始请求完成",
                }
            # mark in_flight（防重起点）：resume 处理期间第二次作答会被上方判重
            await mark_resume_in_flight(turbo_session, resume_ctx)
            _did_mark_in_flight = True

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
                # B_03：被抑制的 task.update 仍需缓存最后一条——本入口跑到
                # 终止性出口时若登记簿显示槽位被本引擎任务集占用未闭合，
                # 由 _settle_leaked_task_slot 用它补发关账。
                if event_type == "task.update" and isinstance(chunk.payload, dict):
                    last_suppressed_task_update = dict(chunk.payload)
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
                        if event_type == "task.update" and slot_key:
                            # B_03 槽位登记：转发分支放行快照 = 槽位被本任务集
                            # 占用；终态快照（全 completed/failed）= 任务集自然
                            # 闭合，即行注销（后续 skip 入口不误清算）。
                            if _is_terminal_task_snapshot(chunk.payload):
                                release_task_slot(slot_key)
                            else:
                                claim_task_slot(
                                    slot_key,
                                    _task_ids_from_snapshot(chunk.payload),
                                )
                except Exception:
                    logger.warning(
                        "[SkillTurboTool] write_stream failed for event_type=%s",
                        event_type,
                        exc_info=True,
                    )
                    continue
                if event_type in _SKILL_TURBO_OOB_TASK_EVENT_TYPES:
                    await _push_task_event_out_of_band(
                        event_type,
                        payload,
                        request_id=str(request_id or ""),
                        channel_id=str(channel_id or ""),
                        session_id=_resolve_skill_turbo_resume_session_id(
                            external_session_id, parent_session
                        ),
                    )
            elif event_type in _SKILL_TURBO_TASK_EVENT_TYPES:
                logger.warning(
                    "[SkillTurboTool] drop %s: parent_session is None "
                    "(task list will not reach frontend)",
                    event_type,
                )

        # B_03：终止性出口清算——skip 抑制掉的终态快照若对应未闭合的槽位
        # 占用（首入口误判 forwarded 的竞态遗留），在此补发关账。AbortError
        # 暂停不清算（见 except 分支）：任务集还要续跑，关账等于谎报终态。
        await _settle_leaked_task_slot(
            parent_session,
            request_id,
            channel_id,
            external_session_id,
            last_suppressed_task_update,
        )

        # 过程输出已通过 write_stream 实时推给前端。交付失败时不得返回 success，
        # 否则外层 LLM / 前端会把「任务已完成」当成 PPT 已生成。
        release_checkpoint = True
        ppt_fail = _ppt_delivery_failed_error(skill_turbo_inst.artifact_holder)
        if ppt_fail:
            return _wrap_skill_turbo_result(
                {"success": False, "error": ppt_fail},
                artifact_holder=skill_turbo_inst.artifact_holder,
            )
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
            return build_skill_turbo_hitl_result(tic)
        # Fallback: AbortError 无 ToolInterruptException cause，返回错误
        logger.warning("[SkillTurboTool] AbortError without ToolInterruptException cause")
        return _wrap_skill_turbo_result(
            {"success": False, "error": f"任务中断: {e}"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )
    except SkillTurboNotHandled as exc:
        logger.info("[SkillTurboTool] SkillTurbo 未处理: %s", exc)
        release_checkpoint = True
        # 终止性出口（B_03）：清算逻辑自守——无登记 / 无终态快照均静默
        await _settle_leaked_task_slot(
            parent_session,
            request_id,
            channel_id,
            external_session_id,
            last_suppressed_task_update,
        )
        return _wrap_skill_turbo_result(
            {"success": False, "error": f"SkillAccelerationExec 未处理: {exc}"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )
    except Exception as exc:
        logger.warning("[SkillTurboTool] 执行失败: %s", exc, exc_info=True)
        # 终止性出口（B_03）：executor 异常分支已 flush task 事件（S_08 约束 6），
        # 缓存快照可能已带 failed 终态——仍尝试关账；非终态则登记保留
        await _settle_leaked_task_slot(
            parent_session,
            request_id,
            channel_id,
            external_session_id,
            last_suppressed_task_update,
        )
        return _wrap_skill_turbo_result(
            {"success": False, "error": f"执行失败: {exc}"},
            artifact_holder=skill_turbo_inst.artifact_holder,
        )
    finally:
        # clear in_flight：无论 resume 成功/中断/失败都必须清除，防下次作答被误判
        # 重复（意外异常后重试作答场景）。isolated 通道强制落盘，失败 warning 可观测。
        if turbo_session is not None and resume_ctx is not None and _did_mark_in_flight:
            from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
                clear_resume_in_flight,
            )

            try:
                await clear_resume_in_flight(turbo_session)
            except Exception:
                logger.warning(
                    "[SkillTurboTool] clear_resume_in_flight failed",
                    exc_info=True,
                )
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
