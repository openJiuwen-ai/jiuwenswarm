# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""服务端 plan_execute 幂等续跑（issue 5223，方案 A）。

Web「执行」批准轮结束且 plan→normal 检出后，由服务端代发执行消息开启执行轮，
不再依赖前端补发——客户端 15s 超时 / 断连不再导致"已批准永不执行"。

调度：``_check_post_process_plan_exit`` 检出 plan 退出后调用
:func:`schedule_plan_execute_continuation`，仅当 resume 载荷含 ``plan_execute``
取值时登记（TUI approve / 跳过 / 修订不触发；mode 未退出不进本分支）。

幂等：记账键 ``(session_id, request_id)``，request_id 即审批 interrupt 的
tool_call id。同一审批轮重复触发（双事件 / 对账并发）凭记账只续跑一次；双
标签页重复批准由既有 ``PermissionResponseLedger`` 先行去重。记账是进程内的，
重启丢失窗口等价旧行为（无兜底），不劣化（同 ``handlers/_shared.py`` CR-9
的取舍）。

grace window：登记后等待 :data:`PLAN_EXECUTE_CONTINUATION_GRACE_SECONDS` 才
代发；窗口内该会话到达非 interrupt-resume 的普通 ``chat.send`` 即取消，由那条
消息充当执行消息——兼容滚动发布期旧前端缓存包的补发，零双发。取消面还包括
显式中断 ``chat.interrupt``（cancel/supplement 意图，``handlers/chat.py`` 挂点，
评审 CR-3）与 session delete / rewind（同 ``_plan_exited_sessions`` 清理位点）；
客户端断连**不**取消（服务端兜底正是为此）。

代发：合成 ``AgentRequest(is_stream=True)``，mode 取批准请求的
``normal_mode``，project_dir 等上下文与身份/记忆白名单键（user_id/routing/
enable_memory——不抄则配额按 fallback 额度兜底、记忆开关失真；评审 CR-2）
及 thread_id/chat_id 归档维度抄自原请求，query 为服务端双语执行文案；
``source=plan_execute_continuation`` 不在 ``_should_record_user_history`` 跳过
名单内，用户历史照常落盘（重连 / 刷新经历史恢复可见）。chunk 逐个经
``server.send_push`` 下行（proactive_adapter 同配方），并先推一帧带 content 的
``chat.processing_status`` 回显帧，前端据此渲染用户气泡（heartbeat automation
回显同款）。执行流抛异常时补推 ``chat.error`` + ``processing_status=false``
终态帧——前端无本地 ``chat.send`` 超时兜底，缺终态帧会停在 processing=true。
契约见 ``server/docs/features/F_02_plan-execute-continuation.md``
与 ``agents/harness/docs/specs/S_04_plan-approval.md``（N3/P1）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_EXECUTE_OPTION_VALUES,
    PLAN_EXECUTE_PROMPT_OUTPUT,
)

logger = logging.getLogger(__name__)

# grace window（秒）：窗口内该会话到达普通消息即取消续跑。
PLAN_EXECUTE_CONTINUATION_GRACE_SECONDS = 3.0

# busy 退避：批准轮刚收尾理论上不应再忙；少量重试后放弃，用户可手动补发。
_BUSY_RETRY_ATTEMPTS = 3
_BUSY_RETRY_INTERVAL_SECONDS = 2.0

# 从原请求 params 抄进续跑请求的上下文键（白名单，防夹带一次性载荷）。
_COPIED_PARAM_KEYS = ("project_dir", "project_id", "work_mode", "agent_scope")

# 从原请求 metadata 抄进续跑请求的可信键（白名单，防夹带一次性载荷；评审
# CR-2）。只覆盖配额身份（顶层 user_id + routing 三元组，
# ``_bind_tenant_request_context`` 经 ``web_routing_identity`` 读取）与记忆
# 开关（interface 按 ``metadata["enable_memory"]`` 读取）；thread_id/chat_id
# 是 AgentRequest 顶层字段，单列在记账上。
_COPIED_METADATA_KEYS = ("user_id", "routing", "enable_memory")

# (session_id:request_id) → 待执行的续跑记账。
_pending: dict[str, "_ContinuationPlan"] = {}

# 已领取记账、尚未代发收尾的键（评审 CR-3 双保险）：领取与取消存在竞态
# 窗口——取消到达时若条目已被领取，仅从记账移除拦不住代发，取消侧写入
# 墓碑、代发前复核。收尾（成功/失败/被取消）统一清理，不累积。
_claimed: set[str] = set()
_cancelled_keys: set[str] = set()

# 后台续跑 task 强引用：事件循环只持弱引用，不持会被 GC 静默回收
# （asyncio 官方要求调用方保存 create_task 返回值）；done 回调自清理。
# 仅防回收，不用于强杀——取消语义仍以领取记账为权威判据。
_continuation_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class _ContinuationPlan:
    """一次待执行的续跑记账（冻结数据，task 引用单独存）。"""

    key: str
    session_id: str
    channel_id: str
    round_id: str
    plan_slug: str | None
    mode: str
    query: str
    copied_params: dict[str, Any]
    # 身份/记忆白名单抄送（评审 CR-2）：配额策略、记忆开关与普通请求同源。
    trusted_metadata: dict[str, Any] = field(default_factory=dict)
    # 业务归档维度（PC OfficeAce 记忆按 thread_id 归档；session_id 是其
    # sha256，不可逆，须单独透传）。
    thread_id: str | None = None
    chat_id: str | None = None


def is_plan_execute_resume(params: Any) -> bool:
    """resume 载荷是否为 Web「执行」批准。

    ``plan_execute`` 是 confirm_interrupt 选项映射里的额外键（N6），在到达
    rail 之前于 answers 的 ``selected_options`` 里即可识别——这里在请求层判，
    不依赖 run 内部的 ``ctx.extra``。
    """
    if not isinstance(params, dict):
        return False
    if params.get("source") != "confirm_interrupt":
        return False
    answers = params.get("answers")
    if not isinstance(answers, list):
        return False
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        options = answer.get("selected_options")
        if isinstance(options, list) and any(
            option in PLAN_EXECUTE_OPTION_VALUES for option in options
        ):
            return True
    return False


def _trusted_metadata_from(request: Any) -> dict[str, Any]:
    """按白名单从原请求 metadata 抄可信身份/记忆键（评审 CR-2）。

    续跑是服务端合成请求，不透传 metadata 会丢身份：配额按
    ``bind_quota_identity`` 绑定（缺省回退 fallback 大额度，策略失效）、
    ``enable_memory`` 缺省回 True、PC 记忆归档维度丢失。逐键按类型校验，
    routing 整体浅拷贝防别名，其余键不夹带。
    """
    metadata = getattr(request, "metadata", None)
    trusted: dict[str, Any] = {}
    if not isinstance(metadata, dict):
        return trusted
    for key in _COPIED_METADATA_KEYS:
        value = metadata.get(key)
        if key == "routing":
            if isinstance(value, dict) and value:
                trusted[key] = dict(value)
        elif key == "enable_memory":
            if isinstance(value, bool):
                trusted[key] = value
        elif isinstance(value, str) and value:
            trusted[key] = value
    return trusted


def schedule_plan_execute_continuation(
    ctx: Any,
    request: Any,
    agent: Any,
    *,
    plan_slug: str | None = None,
) -> bool:
    """批准轮收尾后登记幂等续跑；重复触发返回 False。

    幂等键为 ``(session_id, request_id)``：同一审批轮只会登记一次。
    """
    if not is_plan_execute_resume(getattr(request, "params", None)):
        return False
    session_id = str(getattr(request, "session_id", "") or "")
    params = request.params or {}
    round_id = str(params.get("request_id") or "").strip()
    if not session_id or not round_id:
        logger.warning(
            "[plan_execute_continuation] missing session/round id, skip: "
            "session=%r round=%r",
            session_id, round_id,
        )
        return False

    from jiuwenswarm.server.handlers._shared import resolve_request_runtime_mode

    resolved = resolve_request_runtime_mode(request)
    copied = {
        key: params[key]
        for key in _COPIED_PARAM_KEYS
        if isinstance(params.get(key), str) and params.get(key)
    }
    plan = _ContinuationPlan(
        key=f"{session_id}:{round_id}",
        session_id=session_id,
        channel_id=str(getattr(request, "channel_id", "") or "default"),
        round_id=round_id,
        plan_slug=plan_slug,
        mode=resolved.normal_mode,
        query=PLAN_EXECUTE_PROMPT_OUTPUT.get(_resolve_language(agent)),
        copied_params=copied,
        trusted_metadata=_trusted_metadata_from(request),
        thread_id=str(getattr(request, "thread_id", "") or "") or None,
        chat_id=str(getattr(request, "chat_id", "") or "") or None,
    )
    existing = _pending.get(plan.key)
    if existing is not None:
        logger.info(
            "[plan_execute_continuation] duplicate schedule ignored: %s", plan.key
        )
        return False

    _pending[plan.key] = plan
    task = asyncio.create_task(_grace_then_run(ctx, agent, plan))
    _continuation_tasks.add(task)
    task.add_done_callback(_continuation_tasks.discard)
    logger.info(
        "[plan_execute_continuation] scheduled (grace %.1fs): %s plan_slug=%s",
        PLAN_EXECUTE_CONTINUATION_GRACE_SECONDS, plan.key, plan_slug,
    )
    return True


def cancel_plan_execute_continuation(session_id: str, *, reason: str = "") -> int:
    """取消该会话待执行的续跑，返回取消的待执行条数。

    供普通 ``chat.send`` 到达（消息即执行消息）、显式中断 ``chat.interrupt``
    （cancel/supplement 意图，``handlers/chat.py`` 挂点）与 session delete /
    rewind 清理调用；客户端断连**不**调用——断连兜底是本模块的存在理由。

    取消即从记账移除：grace 计时醒来后领取不到记账，静默退出（领取是权威
    判据；后台 task 引用仅防 GC 回收，不用于强杀）。已领取未代发的条目
    （领取与取消的竞态窗口）写入取消墓碑、由代发前复核拦下（评审 CR-3
    双保险），不计入返回值。
    """
    cancelled = 0
    tombstoned = 0
    for key, plan in list(_pending.items()):
        if plan.session_id != session_id:
            continue
        _pending.pop(key, None)
        if key in _claimed:
            _cancelled_keys.add(key)
            tombstoned += 1
        else:
            cancelled += 1
    if cancelled or tombstoned:
        logger.info(
            "[plan_execute_continuation] cancelled %d pending (+%d claimed "
            "tombstoned) for session=%s (%s)",
            cancelled, tombstoned, session_id, reason or "unspecified",
        )
    return cancelled


async def _grace_then_run(ctx: Any, agent: Any, plan: _ContinuationPlan) -> None:
    """grace 计时 → 领取记账（权威判据，取消后领取不到即静默退出）→ busy 避让 → 代发。"""
    try:
        await asyncio.sleep(PLAN_EXECUTE_CONTINUATION_GRACE_SECONDS)
    except asyncio.CancelledError:
        _pending.pop(plan.key, None)
        logger.info("[plan_execute_continuation] grace cancelled: %s", plan.key)
        return

    if not await _wait_until_session_idle(agent, plan.session_id):
        _pending.pop(plan.key, None)
        logger.warning(
            "[plan_execute_continuation] session still busy after %d retries, "
            "giving up (user can resend manually): %s",
            _BUSY_RETRY_ATTEMPTS, plan.key,
        )
        return

    # 领取记账：标记领取态但暂留记账表——取消侧对已领取条目写入墓碑
    # （评审 CR-3 双保险），代发前复核拦下；代发收尾（成功/失败/被取消）
    # 统一在 finally 清理。领取至收尾期间同一审批轮的重复调度被记账挡住
    # （幂等窗口从 grace 延长到整轮）。
    if _pending.get(plan.key) is None:
        return
    _claimed.add(plan.key)

    # request_id 在此生成并贯穿整轮（回显帧 / chunk 帧 / 失败终态帧共用），
    # 前端按 request_id 对齐续跑事件。
    request_id = f"plan_execute_continuation_{plan.round_id}_{int(time.time() * 1000)}"
    try:
        # 双保险复核：取消落在「已领取、未代发」竞态窗口时凭墓碑拦下。
        if plan.key in _cancelled_keys:
            logger.info(
                "[plan_execute_continuation] cancelled between claim and "
                "dispatch: %s", plan.key,
            )
            return
        await _run_continuation_stream(ctx, agent, plan, request_id)
    except Exception as exc:  # noqa: BLE001
        # fire-and-forget 后台任务：失败落日志，并向前端补终态帧后即可手动
        # 补发；不向外抛——未被 await 的 task 异常会触发 asyncio 未检索告警。
        logger.exception(
            "[plan_execute_continuation] continuation run failed: %s", plan.key
        )
        await _push_failure_terminal_frames(ctx, plan, request_id, exc)
    finally:
        _pending.pop(plan.key, None)
        _claimed.discard(plan.key)
        _cancelled_keys.discard(plan.key)


async def _wait_until_session_idle(agent: Any, session_id: str) -> bool:
    """会话空闲判定 + 退避重试；无法判定（无该方法）视为空闲。"""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    try:
        adapter = AgentWebSocketServer.resolve_adapter(agent)
    except Exception:  # noqa: BLE001
        adapter = None
    checker = getattr(adapter, "is_deep_agent_executing_for_session", None)
    if not callable(checker):
        return True
    for attempt in range(_BUSY_RETRY_ATTEMPTS):
        try:
            if not checker(session_id):
                return True
        except Exception:  # noqa: BLE001
            return True
        if attempt < _BUSY_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_BUSY_RETRY_INTERVAL_SECONDS)
    return False


async def _push_failure_terminal_frames(
    ctx: Any, plan: "_ContinuationPlan", request_id: str, exc: Exception
) -> None:
    """异常终态兜底：补推 ``chat.error`` + ``processing_status=false``。

    续跑由 server 发起，前端没有本地 ``chat.send`` 的 15s 超时兜底；执行流
    中途抛异常（配额拦截 / adapter 初始化失败 / 流式生成异常等）时若不补
    终态帧，前端会停留在 processing=true（输入区转圈、执行轮永不再收口），
    只能刷新页面恢复。推帧自身失败仅落日志（通道已坏时无从兜底）。
    """
    for payload in (
        {
            "event_type": "chat.error",
            "error": str(exc) or exc.__class__.__name__,
            "source": "plan_execute_continuation",
        },
        {
            "event_type": "chat.processing_status",
            "is_processing": False,
            "source": "plan_execute_continuation",
        },
    ):
        try:
            await ctx.services.send_push({
                "request_id": request_id,
                "channel_id": plan.channel_id,
                "session_id": plan.session_id,
                "payload": payload,
            })
        except Exception:  # noqa: BLE001
            logger.debug(
                "[plan_execute_continuation] terminal frame push failed: %s %s",
                payload.get("event_type"), plan.key,
            )


async def _run_continuation_stream(
    ctx: Any, agent: Any, plan: _ContinuationPlan, request_id: str | None = None
) -> None:
    """代发执行消息并消费执行轮流式输出，chunk 经 send_push 下行。"""
    from jiuwenswarm.common.schema.agent import AgentRequest

    if not request_id:
        request_id = f"plan_execute_continuation_{plan.round_id}_{int(time.time() * 1000)}"
    # 身份/记忆白名单抄送（评审 CR-2）：不抄则配额回退 fallback 额度、
    # enable_memory 回 True、PC 记忆归档维度丢失——与普通请求同源读取。
    metadata = dict(plan.trusted_metadata) if plan.trusted_metadata else None
    agent_request = AgentRequest(
        request_id=request_id,
        channel_id=plan.channel_id,
        session_id=plan.session_id,
        thread_id=plan.thread_id,
        chat_id=plan.chat_id,
        params={
            "query": plan.query,
            "display_query": plan.query,
            "mode": plan.mode,
            "source": "plan_execute_continuation",
            **plan.copied_params,
        },
        metadata=metadata,
        is_stream=True,
    )
    logger.info(
        "[plan_execute_continuation] dispatching execution round: session=%s "
        "mode=%s request_id=%s",
        plan.session_id, plan.mode, request_id,
    )

    # 回显帧：前端在 processing_status=true 分支按 source 渲染用户气泡
    # （heartbeat automation 回显同款）；续跑是 server 发起的，没有本地 sendMessage。
    await ctx.services.send_push({
        "request_id": request_id,
        "channel_id": plan.channel_id,
        "session_id": plan.session_id,
        "payload": {
            "event_type": "chat.processing_status",
            "is_processing": True,
            "content": plan.query,
            "source": "plan_execute_continuation",
        },
    })

    async for chunk in agent.process_message_stream(agent_request):
        try:
            await ctx.services.send_push({
                "request_id": getattr(chunk, "request_id", "") or request_id,
                "channel_id": plan.channel_id,
                "session_id": plan.session_id,
                "payload": getattr(chunk, "payload", None),
                "is_complete": bool(getattr(chunk, "is_complete", False)),
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[plan_execute_continuation] send_push chunk failed: %s", exc
            )


def _resolve_language(agent: Any) -> str:
    """沿 agent 配置解析执行文案语言；取不到回退 ``"cn"``。

    与 rail 侧 ``_detect_language`` 同源：``system_prompt_builder`` 挂在
    adapter 的内层 ``_instance``（openjiuwen DeepAgent）上——``interface_deep.
    _update_prompt_for_mode`` 正是写 ``self._instance.system_prompt_builder``
    （评审 2026-11-09：adapter 自身无该属性，直接读恒为 None 恒回退 cn）。
    facade 经 ``resolve_adapter`` 拿 adapter 再入 ``_instance``（惰性导入防循环）。
    """
    try:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        adapter = AgentWebSocketServer.resolve_adapter(agent)
        builder = getattr(
            getattr(adapter, "_instance", None), "system_prompt_builder", None
        )
        language = getattr(builder, "language", None)
        if language in ("cn", "en"):
            return language
    except Exception:  # noqa: BLE001
        pass
    return "cn"
