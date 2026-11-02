"""HITL 卡片登记、相位标记与 stale 守卫。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    strip_hitl_seq_suffix,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interrupt_state_machine import (
    PHASE_PAUSED,
    PHASE_RESUMED,
    get_interrupt_phase,
    is_interrupt_terminal,
    mark_interrupt_idle,
    mark_interrupt_paused,
    mark_interrupt_resumed,
)
from jiuwenswarm.server.runtime.agent_adapter.session_flag_proxy import build_flag_proxy

logger = logging.getLogger(__name__)


async def _commit_hitl_pause_before_card(adapter, session_id: str, request_id: str) -> None:
    """审批卡发出前先把暂停状态落盘，保证恢复请求能读到本轮上下文。"""
    if adapter._instance is None:
        return
    session = getattr(adapter._instance, "_interaction_session", None)
    if session is None:
        return
    react_agent = getattr(adapter._instance, "react_agent", None)
    context_engine = getattr(react_agent, "context_engine", None) if react_agent is not None else None
    committed = False
    msg_count = 0
    if context_engine is not None:
        try:
            ctx = context_engine.get_context(session_id=session_id)
            msg_count = len(list(ctx.get_messages() or [])) if ctx is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HITL-CTX] context probe before permission card failed: "
                "session_id=%s request_id=%s error=%s",
                session_id, request_id, exc,
            )
            ctx = None
            msg_count = 0
        if ctx is None or msg_count <= 0:
            logger.info(
                "[HITL-CTX] checkpoint skip: no persistable context: "
                "session_id=%s request_id=%s msg_count=%s",
                session_id, request_id, msg_count,
            )
        else:
            try:
                await context_engine.save_contexts(session)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[HITL-CTX] save contexts before permission card failed: "
                    "session_id=%s request_id=%s error=%s",
                    session_id, request_id, exc,
                )
            else:
                try:
                    await session.commit()
                    committed = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[HITL-CTX] interrupt commit before permission card failed: "
                        "session_id=%s request_id=%s error=%s",
                        session_id, request_id, exc,
                    )
    try:
        adapter.mark_session_awaiting_tool_permission(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[HITL-CTX] mark awaiting permission failed: "
            "session_id=%s request_id=%s error=%s",
            session_id, request_id, exc,
        )
    if committed:
        logger.info(
            "[HITL-CTX] interrupt committed before permission card: "
            "session_id=%s request_id=%s msg_count=%s",
            session_id, request_id, msg_count,
        )


def _dedupe_ask_user_card(
    adapter,
    parsed: dict | None,
    emitted_request_ids: set[str],
    emitted_questions: dict[str, str],
) -> bool:
    """ask_user 卡片去重与序号区分；返回 True 表示跳过本张卡。

    - 同一中断的多通道重复（独立 ``__interaction__`` 与 controller 嵌套）：
      同 call id + 同 questions → 跳过。
    - 挂起表复用（跨请求）：该 base 仍有同 questions 的活卡挂起（作答
      消费时销毁，见 _consume_hitl_live_card_for_answer）→ 重发原卡
      而非生成新代。典型场景：乱序点击后 resume 重放会重新走到未作答
      interrupt 的发卡点，若无此分支将诞生 ``{id}#{n+1}`` 新卡取代原卡
      ——用户点回原卡即被 stale 守卫拒绝，空流被 relay-claw 空回弹启发式
      误判中止（full.log 2026-09-28 事故形态）。重发同 request_id 对
      relay-claw 五层幂等（注册早退 + 前端 requestId 去重），wire 协议
      零变动；序号计数不动（不新增代次）。
    - 同一外层 skill_acceleration_exec 内的第 N 次中断共用同一 call id：
      questions 不同 → 卡片 request_id 改为 ``{id}#{n}`` 放行（前端才显示为
      独立卡片）；作答回传时由 _build_interactive_input_from_answers 入口
      剥掉后缀对齐 harness 原始 id。序号计数在 adapter 实例上跨请求存续。
    """

    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _ask_user_questions_key,
    )

    if not isinstance(parsed, dict):
        return False
    if parsed.get("event_type") != "chat.ask_user_question":
        return False
    request_id = str(parsed.get("request_id") or "").strip()
    if not request_id:
        return False
    questions_key = _ask_user_questions_key(parsed)
    if any(
        (fid == request_id or fid.startswith(f"{request_id}#"))
        and qk == questions_key
        for fid, qk in emitted_questions.items()
    ):
        return True
    # 挂起表复用：活卡还挂着且 questions 一致 → 重发原卡（不改代次、
    # 不动相位）；questions 不同则落回下方新代逻辑（合法 re-ask 场景）。
    live_final = getattr(adapter, "_hitl_base_live_instance", {}).get(request_id)
    live_questions = getattr(adapter, "_hitl_base_live_questions", {}).get(request_id)
    if live_final and live_questions is not None and live_questions == questions_key:
        parsed["request_id"] = live_final
        emitted_request_ids.add(live_final)
        emitted_questions[live_final] = questions_key
        return False
    final_id = adapter._outer_call_dedup.next_final_id(request_id)
    emitted_request_ids.add(final_id)
    emitted_questions[final_id] = questions_key
    if final_id != request_id:
        parsed["request_id"] = final_id
    # 中断实例活性登记：新卡发出即取代同 base 的旧卡（旧卡应答将成为
    # stale 被守卫拒绝）。同一中断的多通道重复已在上方 questions_key
    # 判定中跳过，不会走到这里。
    adapter._register_hitl_card_instance(
        base_id=request_id, final_id=final_id, questions_key=questions_key
    )
    # 中断状态机：新卡片发出 → 相位重开为 paused（取消上一代终态）。
    # 只标内存态（runtime/loop session）：HITL 中断时 checkpointer 的
    # interrupt_agent_execute 会随 loop session 状态落盘；卡片本身是
    # 低频事件，异步磁盘写不在此处（同步方法）展开。
    adapter._mark_interrupt_paused_inmemory(card_id=final_id, source=str(parsed.get("source") or ""))
    return False


def _register_hitl_card_instance(
    adapter,
    *,
    base_id: str,
    final_id: str,
    questions_key: str = "",
) -> None:
    """登记新卡片实例并取代同 base 的旧卡（旧卡标记为死卡）。

    questions_key 随活卡一起登记（挂起表复用判定用）；空串表示未知
    questions（外部直接调用兼容路径），永不命中复用分支——落回新代
    逻辑，保守安全。

    死卡集供 guard_stale_interrupt_response 拒绝旧卡应答；被取代的
    旧卡不再向前端广播失效事件——旧卡应答会被守卫拒绝，屏幕上的
    旧卡由新卡到达自行覆盖或随会话清理。
    """
    try:
        adapter._hitl_card_instances[final_id] = base_id
        prev_live = adapter._hitl_base_live_instance.get(base_id)
        if prev_live is not None and prev_live != final_id:
            adapter._hitl_dead_card_ids.add(prev_live)
        adapter._hitl_base_live_instance[base_id] = final_id
        adapter._hitl_base_live_questions[base_id] = questions_key
        # 有界注册表：防止长生命周期 adapter 无限增长
        if len(adapter._hitl_card_instances) > 1024:
            for old_id in list(adapter._hitl_card_instances.keys())[:512]:
                old_base = adapter._hitl_card_instances.pop(old_id, None)
                adapter._hitl_dead_card_ids.discard(old_id)
                # 淘汰条目若恰为该 base 的当前活卡，live/questions 一并
                # 摘除（否则两表泄漏指向已淘汰 final_id 的悬空条目）。
                if (
                    old_base is not None
                    and adapter._hitl_base_live_instance.get(old_base) == old_id
                ):
                    adapter._hitl_base_live_instance.pop(old_base, None)
                    adapter._hitl_base_live_questions.pop(old_base, None)
            if len(adapter._hitl_dead_card_ids) > 2048:
                adapter._hitl_dead_card_ids.clear()
    except Exception:  # noqa: BLE001 — 登记失败不影响发卡主流程
        logger.debug(
            "[JiuWenClaw] register hitl card instance failed base=%s final=%s",
            base_id,
            final_id,
            exc_info=True,
        )


def _invalidate_all_hitl_card_instances(adapter) -> None:
    """终态/轮次结束：全部活卡转死卡（注册表未初始化时 no-op）。"""
    live = getattr(adapter, "_hitl_base_live_instance", None)
    dead = getattr(adapter, "_hitl_dead_card_ids", None)
    if not isinstance(live, dict) or not isinstance(dead, set):
        return
    for final_id in live.values():
        dead.add(final_id)
    live.clear()
    questions = getattr(adapter, "_hitl_base_live_questions", None)
    if isinstance(questions, dict):
        questions.clear()


def _consume_hitl_live_card_for_answer(adapter, request: AgentRequest) -> None:
    """作答被接受（stale 守卫放行后）：销毁该 base 的挂起表活条目。

    挂起表的语义是"还挂着没有"而非"发过没有"：作答被接受即不再挂起。
    条目销毁后，resume 重放若重新走到同 base 发卡点，说明答案未注入该
    interrupt（或新 questions），按新代兜底而非误判复用。销毁不进死卡
    集——同代卡的重复应答由 PermissionResponseLedger 按原始 id 幂等
    去重（语义锚点 test_stale_answer_rejects_non_live_generation_of_
    known_base：销毁后该卡应答 base 未登记，fail-open）。
    """
    params = request.params if isinstance(getattr(request, "params", None), dict) else {}
    base_id = strip_hitl_seq_suffix(str(params.get("request_id") or ""))
    if not base_id:
        return
    live = getattr(adapter, "_hitl_base_live_instance", None)
    if isinstance(live, dict):
        live.pop(base_id, None)
    questions = getattr(adapter, "_hitl_base_live_questions", None)
    if isinstance(questions, dict):
        questions.pop(base_id, None)


def _is_stale_hitl_card_answer(adapter, final_id: str) -> bool:
    """应答是否命中已死卡片实例（未登记的卡片 fail-open 由相位守卫裁决）。

    序号后缀正判扩展：未登记的 ``{base}#{n}`` 应答，若其 base 的当前代卡
    登记在案且不是该 id，同样判陈旧——同权限 re-ask 场景下，上层对旧代卡
    的迟到应答与伪造代次不得进入 runtime（BUG20260928373903 复读风暴）；
    当前代卡（同 base 顺序下一个权限门）不受影响（CR-1）。base 未登记仍
    fail-open（重启恢复 / 未走登记发卡路径）。
    """
    if not final_id:
        return False
    dead_ids = getattr(adapter, "_hitl_dead_card_ids", frozenset())
    if final_id in dead_ids:
        return True
    base_id = getattr(adapter, "_hitl_card_instances", {}).get(final_id)
    if base_id is None:
        live = getattr(adapter, "_hitl_base_live_instance", {})
        suffix_base = strip_hitl_seq_suffix(final_id)
        if suffix_base != final_id and suffix_base in live:
            return live.get(suffix_base) != final_id
        return False
    live = getattr(adapter, "_hitl_base_live_instance", {}).get(base_id)
    return live is not None and live != final_id


def _mark_interrupt_paused_inmemory(adapter, *, card_id: str, source: str = "") -> None:
    """新 HITL 卡片发出的内存态相位标记（best-effort，失败不阻断发卡）。"""
    # getattr 防御：__new__ 构造的 adapter（单测）无 _instance 属性时
    # no-op，保持 best-effort 约定（AttributeError 不得逃逸阻断发卡）。
    instance = getattr(adapter, "_instance", None)
    if instance is None:
        return
    try:
        proxy = build_flag_proxy(
            getattr(instance, "_interaction_session", None),
            getattr(instance, "loop_session", None),
        )
        if proxy.sessions:
            mark_interrupt_paused(proxy, card_id=card_id, source=source)
    except Exception:  # noqa: BLE001 — 相位标记失败不影响发卡主流程
        logger.debug(
            "[JiuWenClaw] mark interrupt paused failed card_id=%s",
            card_id,
            exc_info=True,
        )


def _mark_interrupt_resumed_inmemory(adapter, *, source: str = "") -> None:
    """应答受理并进入重放（RESUME_SIGNAL 到达）：相位 → resumed。

    仅在当前相位为 paused 时转换（幂等：重复 resume 信号 / 其他相位
    no-op），避免把状态机警告变成信号噪音。与 paused 标记同力度：只写
    内存态，跨重启相位恢复由新卡发出的重开语义兜底。
    """
    instance = getattr(adapter, "_instance", None)
    if instance is None:
        return
    try:
        proxy = build_flag_proxy(
            getattr(instance, "_interaction_session", None),
            getattr(instance, "loop_session", None),
        )
        if not proxy.sessions:
            return
        phase = get_interrupt_phase(proxy)
        if (phase or {}).get("phase") != PHASE_PAUSED:
            return
        mark_interrupt_resumed(proxy, source=source)
    except Exception:  # noqa: BLE001 — 相位标记失败不影响重放主流程
        logger.debug(
            "[JiuWenClaw] mark interrupt resumed failed",
            exc_info=True,
        )


def _mark_interrupt_idle_inmemory(adapter) -> None:
    """重放轮次正常完成且无新卡在飞：相位 → idle。

    仅在当前相位为 resumed 时转换——paused（卡在飞等待作答）与终态
    （cancel/supplement 的守卫语义需跨请求存活）均保持原状，普通轮次
    （idle）no-op 避免 idle→idle 噪音。
    """
    instance = getattr(adapter, "_instance", None)
    if instance is None:
        return
    try:
        proxy = build_flag_proxy(
            getattr(instance, "_interaction_session", None),
            getattr(instance, "loop_session", None),
        )
        if not proxy.sessions:
            return
        phase = get_interrupt_phase(proxy)
        if (phase or {}).get("phase") != PHASE_RESUMED:
            return
        mark_interrupt_idle(proxy)
    except Exception:  # noqa: BLE001 — 相位标记失败不影响轮次收尾
        logger.debug(
            "[JiuWenClaw] mark interrupt idle failed",
            exc_info=True,
        )


def _mark_round_end_inmemory(adapter) -> None:
    """轮次正常完成且无新卡在飞：相位回 idle + 全部活卡转死卡。

    idle 标记仅 resumed 时转换（paused/终态/普通轮次 no-op）；活卡
    失效无条件执行——走到本调用点时本轮流内已无 pending 卡（在飞的
    轮次走 paused 收尾分支），注册表残留的活卡必然是历史轮已答/被
    绕开的死卡。失效后其应答由 ``guard_stale_interrupt_response``
    回 ``stale_interrupt_response`` 拒绝，不再 fail-open 静默吞答案
    （兑现注册表注释「轮次结束」失效语义）。
    """
    adapter._mark_interrupt_idle_inmemory()
    adapter._invalidate_all_hitl_card_instances()


async def guard_stale_interrupt_response(adapter, request: AgentRequest) -> bool:
    """终态守卫：HITL 应答是否落在已被取消/补充的会话上（True = 过期，应拒绝）。

    读取磁盘态（临时 session pre_run）与内存态（runtime session）的中断
    相位；cancelled / supplemented 终态表示上一代中断已死——其卡片应答
    一律拒绝，防止"点一张旧卡 → resume 重放 → 再弹新卡"的反馈循环与
    死卡点击进入 runtime。守卫只读不写，失败时 fail-open（放行）。

    P2 追加：活性卡守卫——应答 request_id 命中活性注册表中的死卡实例
    （同 base 已被新卡取代）时同样拒绝；未登记的卡片 fail-open 交给
    相位守卫裁决。
    """
    params = request.params if isinstance(getattr(request, "params", None), dict) else {}
    answer_card_id = str(params.get("request_id") or "").strip()
    if adapter._is_stale_hitl_card_answer(answer_card_id):
        logger.info(
            "[JiuWenClaw] stale hitl card answer rejected: "
            "session_id=%s card_id=%s request_id=%s",
            getattr(request, "session_id", ""),
            answer_card_id,
            getattr(request, "request_id", ""),
        )
        return True
    session_id = str(getattr(request, "session_id", "") or "").strip()
    if not session_id or adapter._instance is None:
        return False
    try:
        from openjiuwen.core.session.agent import create_agent_session
        session = create_agent_session(session_id=session_id, card=adapter._instance.card)
        await session.pre_run(inputs=None)
        try:
            runtime_session = getattr(adapter._instance, "_interaction_session", None)
            proxy = build_flag_proxy(session, runtime_session)
            stale = is_interrupt_terminal(proxy)
            if stale:
                phase = get_interrupt_phase(proxy) or {}
                logger.info(
                    "[JiuWenClaw] stale interrupt response rejected: "
                    "session_id=%s phase=%s card_id=%s request_id=%s",
                    session_id,
                    phase.get("phase"),
                    phase.get("card_id"),
                    getattr(request, "request_id", ""),
                )
            return stale
        finally:
            try:
                await session.post_run()
            except Exception:
                logger.debug("[JiuWenClaw] guard post_run failed", exc_info=True)
    except Exception:  # noqa: BLE001 — 守卫失败 fail-open
        logger.warning(
            "[JiuWenClaw] guard_stale_interrupt_response failed session_id=%s "
            "(fail-open)",
            session_id,
            exc_info=True,
        )
        return False
