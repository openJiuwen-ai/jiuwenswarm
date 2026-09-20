# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ProactiveEngine 初始化与适配层。

把主动推荐的适配逻辑从 app_agentserver 抽出，集中管理：
- build_proactive_agent: 建专用决策 agent（无 tools、单轮、输出 JSON）
- trigger_main_agent: 触发主 agent 跑一轮生成话术 → stream 推前端
- init_proactive_engine: 组装 ProactiveEngine + 注入 agent + callback

app_agentserver 只需调 init_proactive_engine(server, config)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.runtime.context import get_current_agent_manager
from jiuwenswarm.runtime.host_services import (
    RuntimeHostPushTransport,
    send_runtime_push,
)

logger = logging.getLogger(__name__)

# 后台主 agent 推送任务的 inflight 集合，防止同 session 重复并发触发 stream。
# fire-and-forget 后 cron 可能在一个后台 tick 还没跑完时又来下一次，靠这个
# 标志跳过，避免同 session 并发 process_message_stream（不支持并发）。
_proactive_push_inflight: set[str] = set()


def resolve_proactive_adapter(agent: Any) -> Any | None:
    """Resolve the execution adapter without importing a Server implementation."""
    if agent is None:
        return None
    for attr in ("_adapter", "adapter", "_active_adapter"):
        adapter = getattr(agent, attr, None)
        if adapter is not None and (
            hasattr(adapter, "apply_sandbox_runtime_patch")
            or hasattr(adapter, "is_deep_agent_executing_for_session")
        ):
            return adapter
    if hasattr(agent, "apply_sandbox_runtime_patch") or hasattr(
        agent,
        "is_deep_agent_executing_for_session",
    ):
        return agent
    return None


def _resolve_agent_manager(server: Any, explicit: Any | None = None) -> Any | None:
    """Prefer the active Runtime context, then explicit or host injection."""
    current = get_current_agent_manager()
    if current is not None:
        return current
    if explicit is not None:
        return explicit
    getter = getattr(server, "get_agent_manager", None)
    return getter() if callable(getter) else None


@dataclass
class ProactiveTriggerRequest:
    """一次主动推荐触发请求的具名参数封装（G.FNM.03：多相关参数具名化）。

    把 session_id / query / decision / channel_id / on_delivered / rec_id /
    style_rules_section 这一组"本次推荐请求"相关参数封装到一起，让
    trigger_main_agent 与 _trigger_main_agent 的签名都降到 2 参
    （trigger_callback/server + request）。server / trigger_callback 作为运行时
    依赖不入此结构。query 可由 _trigger_main_agent 内部用 DIRECTIVE_PROMPT 拼好后
    填入，故外部构造时允许为空。
    """
    session_id: str
    decision: Any
    channel_id: str | None = None
    query: str = ""
    on_delivered: Any = None
    rec_id: str = ""  # Unique recommendation ID for frontend feedback
    style_rules_section: str = ""  # 话术层梯度，注入 DIRECTIVE_PROMPT


def build_proactive_agent():
    """Build the lightweight proactive agent for proactive recommendation decisions.

    无 tools、无 task_loop、单轮、输出 JSON。复用 _get_model 的模型选择逻辑。
    替代 proactive_actions._analyze_and_decide 里的裸 model.invoke —— 走 agent
    框架的 invoke 链路（rails / 模型选择 / 观测），不再手搓 Model/SystemMessage。
    """
    try:
        from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import _get_model
        from openjiuwen.harness.factory import create_deep_agent
        from openjiuwen.core.single_agent import AgentCard
    except ImportError as exc:
        logger.warning("[AgentServer] proactive agent imports failed: %s", exc)
        return None

    model = _get_model(temperature=0.0)
    if model is None:
        logger.warning("[AgentServer] proactive agent: no model configured")
        return None
    try:
        return create_deep_agent(
            model=model,
            card=AgentCard(name="proactive_agent", id="proactive_agent"),
            system_prompt="你是用户洞察与推荐助手。严格输出 JSON 对象。",
            tools=[],
            rails=[],
            enable_task_loop=False,
            max_iterations=1,
            add_general_purpose_agent=False,
        )
    except Exception as exc:
        logger.warning("[AgentServer] proactive agent build failed: %s", exc)
        return None


async def trigger_main_agent(
    server: Any,
    request: ProactiveTriggerRequest,
    *,
    agent_manager: Any | None = None,
    push_transport: Any | None = None,
) -> bool:
    """Drive the main agent to run one round with the directive-style query.

    避让：目标 session 正在跑 stream 时跳过（同 session 不支持并发 stream）。
    触发后主 agent 自己生成话术 → 进 context engine → stream 推前端。

    fire-and-forget：主 agent 跑一轮被丢到后台 task，本函数立即返回 True
    （表示"已触发"，让调用方 cron 秒回不超时）。``request.on_delivered`` 回调在后台
    task **真正跑完**（主 agent 输出流尽、未抛异常）后才被调用——用于让
    调用方在"推荐确实送达"时再做计数/状态持久化，避免后台失败却已计数。
    后台 task 失败时不会调 on_delivered，调用方据此知道本次未真正送达。

    Args:
        server: 运行时依赖（不入 ProactiveTriggerRequest）。
        request: 本次触发请求的具名参数（session_id/channel_id/query/decision/on_delivered）。

    Returns:
        True if the main agent was triggered (后台异步跑), False on busy/missing
        adapter/failure/duplicate-inflight.
    """
    session_id = request.session_id
    channel_id = request.channel_id
    query = request.query
    decision = request.decision
    on_delivered = request.on_delivered
    rec_id = request.rec_id

    try:
        from jiuwenswarm.common.schema.agent import AgentRequest
    except ImportError as exc:
        logger.warning("[AgentServer] trigger_main_agent import failed: %s", exc)
        return False

    cid = channel_id or "web"
    # 用外层 agent（JiuWenSwarm）调 process_message_stream——它做 session 管理、
    # history 落盘、mode 解析等前置后委托给内层 adapter 的 process_message_stream_impl。
    # 内层 adapter 只有 impl 方法，直接调会 AttributeError。
    #
    # 用 get_agent_nowait（不自动创建）：tick 不应替用户建 agent——自动建时 cache_key
    # （mode:sub_mode:project_dir）和用户对话实际用的可能不一致（用户对话带 project_dir/
    # sub_mode），会建出第二个 agent，导致推荐进的不是用户对话用的 context。
    # agent 不在内存 = 用户尚未在该 channel 发过消息（无活跃 context 可投递）→ 跳过本次 tick，
    # 等用户用过一次、agent 建好后下个 tick 自然拿到。
    manager = _resolve_agent_manager(server, agent_manager)
    if manager is None:
        logger.info("[ProactiveEngine] trigger: no runtime agent manager, skipping")
        return False
    agent = manager.get_agent_nowait(cid)
    if agent is None or not hasattr(agent, "process_message_stream"):
        logger.info("[ProactiveEngine] trigger: no agent for channel=%s "
                    "(user hasn't used this channel yet), skipping", cid)
        return False
    # 内层 adapter 用于避让检查（is_deep_agent_executing_for_session 在 adapter 上）
    # 用公开 resolve_adapter 避开 protected-access
    adapter = resolve_proactive_adapter(agent)

    # 避让：目标 session 正忙 → 跳过本次 tick
    if adapter is not None and hasattr(adapter, "is_deep_agent_executing_for_session"):
        try:
            if adapter.is_deep_agent_executing_for_session(session_id):
                logger.info("[ProactiveEngine] trigger: session %s busy, skipping", str(session_id)[:20])
                return False
        except Exception as exc:
            logger.debug("[ProactiveEngine] is_deep_agent_executing_for_session check failed: %s", exc)

    agent_request = AgentRequest(
        request_id=f"proactive_{decision.type}_{int(time.time() * 1000)}",
        channel_id=cid,
        session_id=session_id,
        # source=proactive_recommendation 标记这是系统触发的推荐指令，不是用户说的话。
        # process_message_stream 写 user history 时据此跳过——否则刷新页面会看到
        # "[主动推荐指令] xxx" 这条用户没说过的消息。
        # proactive_type/target 给 assistant 写 history 时透传用（待通用流程支持）。
        params={
            "query": query,
            "mode": "agent",
            "source": "proactive_recommendation",
            "proactive_type": decision.type,
            "proactive_target": decision.target,
            # 透传 rec_id：写 assistant history 时一并持久化（interface.py 白名单
            # 读 request.params），刷新页面后前端靠 payload.proactive_rec_id 还原
            # 赞/踩按钮，否则历史消息上按钮不出现。
            **({"proactive_rec_id": rec_id} if rec_id else {}),
        },
        is_stream=True,
    )

    # fire-and-forget：主 agent 跑一轮 + 流式推前端这一段可能很慢（LLM + 工具调用 +
    # post_run 持久化），如果串行 await 会把调用方（_handle_proactive_tick → cron）一起拖到
    # 600 秒超时。这里把它丢后台 task，立即返回 True（表示"已触发"），让 cron 秒回。
    # 推荐内容本来就走 server.send_push 独立流式推前端，不依赖 cron 的请求-响应通道。
    inflight_key = f"{cid}:{session_id}"
    if inflight_key in _proactive_push_inflight:
        logger.info("[ProactiveEngine] trigger: previous push still running for %s, skipping",
                    inflight_key)
        return False
    _proactive_push_inflight.add(inflight_key)
    transport = (
        RuntimeHostPushTransport() if push_transport is None else push_transport
    )

    async def _push_chunks() -> None:
        """后台消费主 agent 的流式输出并推 Gateway。"""
        delivered = False
        logged_rec_id = False
        # 累积主 agent 生成的话术全文，供 on_delivered 回写 recommendation_history.content。
        # chat.final 的 payload.content 是该轮完整文本；chat.delta 是增量。优先取 final
        # 的 content，没有 final 才退化为拼接 delta。delta 拼接顺序未必与展示一致，仅作兜底。
        final_content = ""
        delta_parts: list[str] = []
        # 主 agent 这轮是否产出了失败 chunk（chat.error）。LLM 调用失败（429 budget、
        # [181001] model call failed 等）时流里会带 chat.error 而非正文，0 token 产出。
        # 此前 delivered 仅判"循环未抛异常"，把这种失败也当送达 → on_delivered 写进
        # history（content 空）+ _mark_recommended 给 target 上 24h cooldown，但用户
        # 从没收到卡片（前端按 chat.error 显示报错气泡）。故必须识别 chat.error 为
        # 未送达：不计 delivered、不写 history、不占 cooldown。
        had_chat_error = False
        # proactive 标记只覆盖"推荐话术那一轮"：第一个非空 assistant 文本 final 之前
        # 的 chunk（delta/reasoning/final）才注入 source=proactive_recommendation 等标记，
        # 其后的 chunk（工具调用、进度正文、结果 final）一律不打标 → 前端按无 source
        # 处理 = 普通气泡/工具块，不退化成推荐卡片。
        # 根因：前端卡片渲染由"逐 chunk 的 source"决定（useWebSocket.ts:2709 /
        # MessageItem.tsx:513 / buildTurnTimeline.ts:284），不由 request_id 决定。此前
        # 无脑给每个 chunk setdefault(source) 导致主 agent 在话术后继续跑工具推进用户
        # 任务时，后续进度/工具消息也被渲染成技能推荐卡片（共享同一 request_id）。
        proactive_marking_closed = False
        push_failed = False
        try:
            async for chunk in agent.process_message_stream(agent_request):
                # chunk 经 server.send_push 推 Gateway。send_push 内部已用
                # _current_send_lock 串行化 ws 发送，且 build_server_push_wire 走
                # chunk 分支（无 response_kind）正确编码——这里只需带齐 chunk 的
                # request_id / payload / is_complete，Gateway 才能按 request_id 路由。
                try:
                    chunk_payload = dict(getattr(chunk, "payload", None) or {})
                    # event_type 字段名也可能写作 event，兼容两种。
                    evt = chunk_payload.get("event_type") or chunk_payload.get("event") or ""
                    is_chat_error = isinstance(evt, str) and evt.endswith(".error")
                    if is_chat_error:
                        had_chat_error = True
                        logger.warning(
                            "[ProactiveEngine] main agent emitted chat.error during push "
                            "(rec_id=%s, target=%s): %s",
                            rec_id, decision.target, chunk_payload.get("error", ""),
                        )
                    is_nonempty_assistant_final = (
                        isinstance(evt, str) and evt.endswith(".final")
                        and isinstance(chunk_payload.get("content"), str)
                        and bool(chunk_payload.get("content"))
                    )
                    if not proactive_marking_closed and not is_chat_error:
                        # 注入 source/proactive_type：主 agent 的 chunk 是普通对话格式，
                        # 不带主动推荐标记。前端靠 payload.source==='proactive_recommendation'
                        # 识别卡片、payload.proactive_type 选颜色，缺这俩会退化成普通白色气泡。
                        # decision.type 在手上，给话术这一轮的 chunk 补上。
                        # chat.error 不打标：错误 chunk 带 source 会被前端当推荐卡片渲染，
                        # 应让用户看到错误提示而非异常卡片。错误 chunk 仍透传前端（无 source）。
                        chunk_payload.setdefault("source", "proactive_recommendation")
                        chunk_payload.setdefault("proactive_type", decision.type)
                        chunk_payload.setdefault("proactive_target", decision.target)
                        if rec_id:
                            chunk_payload["proactive_rec_id"] = rec_id
                            if not logged_rec_id:
                                logger.info("[ProactiveEngine] injecting proactive_rec_id=%s into chunks", rec_id)
                                logged_rec_id = True
                        # 话术那一轮的非空 final = 推荐正文已发完，其后不再打标。
                        if is_nonempty_assistant_final:
                            proactive_marking_closed = True
                    # 累积话术文本：第一个非空 final（=推荐正文，与打标关闭点是同一处）
                    # 即话术全文。其后工具轮次的进度 final 不覆盖——on_delivered 要的是
                    # 推荐正文，不是后续工具轮次的产出文本。delta 在话术轮内兜底拼接，
                    # 同样只在标记未关闭前（即首个非空 final 之前）累积。
                    if is_nonempty_assistant_final:
                        if not final_content:
                            final_content = chunk_payload.get("content")
                    elif isinstance(evt, str) and evt.endswith(".delta") and not proactive_marking_closed:
                        c = chunk_payload.get("content")
                        if isinstance(c, str):
                            delta_parts.append(c)
                    pushed_ok = await transport.send_push({
                        "request_id": getattr(chunk, "request_id", "") or agent_request.request_id,
                        "channel_id": cid,
                        "session_id": session_id,
                        "payload": chunk_payload,
                        "is_complete": bool(getattr(chunk, "is_complete", False)),
                    })
                    # send_push 失败信号有两种（不同 transport 实现各异）：
                    #   - AgentWebSocketServer.send_push 返回 bool，False = 无 ws/降级/发送失败
                    #   - RuntimeHostPushTransport.send_push 失败时抛 RuntimeError（成功返回 None）
                    # 显式返回 False = 推送失败；None/True = 成功（None 来自不返回值的 transport）。
                    # 失败时 chunk 没真到前端，不算送达，记 push_failed 让下方判定排除，
                    # 防止"假送达"：ws 短断时仍写 history（幽灵卡片）+ 误占 24h cooldown。
                    if pushed_ok is False:
                        push_failed = True
                        logger.debug("[ProactiveEngine] send_push reported not-pushed (ws down?)")
                except Exception as exc:
                    # 兜底：send_push 抛异常（RuntimeHostPushTransport 失败时抛/测试 mock）同样标失败。
                    push_failed = True
                    logger.debug("[ProactiveEngine] send_push chunk failed: %s", exc)
            # 送达判定收紧：循环跑完且未产 chat.error 且确实拿到话术正文（final/delta）
            # 且推送未失败。缺任何一条 = 未真正送达（LLM 失败/0 token/推送异常），
            # 不计 delivered → 不写 history、不占 cooldown，避免空记录 + 误占 24h 冷却位。
            phrasing_text = final_content or "".join(delta_parts)
            if phrasing_text and not had_chat_error and not push_failed:
                delivered = True
            elif had_chat_error:
                logger.info("[ProactiveEngine] not delivered (main agent chat.error, no phrasing text)")
        except Exception as exc:
            logger.warning("[ProactiveEngine] trigger: process_message_stream failed: %s",
                           exc, exc_info=True)
        finally:
            _proactive_push_inflight.discard(inflight_key)
            # 只在真正送达时回调，让调用方据此做计数/状态持久化（名实相符）。
            # 后台失败（delivered=False）不回调，调用方下次 tick 仍可重试。
            # 传回累积的话术全文，让 _on_delivered 写进 recommendation_history.content
            # （便于观察话术演化、对比梯度生效前后文风）。
            if delivered and on_delivered is not None:
                generated = final_content or "".join(delta_parts)
                try:
                    on_delivered(generated)
                except Exception as exc:
                    logger.warning("[ProactiveEngine] on_delivered callback failed: %s",
                                   exc, exc_info=True)

    asyncio.create_task(_push_chunks())
    logger.info("[ProactiveEngine] trigger: fired background push for %s (fire-and-forget)",
                inflight_key)
    return True


async def init_proactive_engine(server, config: dict[str, Any] | None = None) -> None:
    """组装 ProactiveEngine + 注入专用 agent + 触发回调，挂到 server 上。

    app_agentserver 启动时调用，把所有 proactive 适配逻辑集中在此。
    """
    from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import ProactiveEngine

    try:
        proactive_config = config or {}
        proactive_engine = ProactiveEngine(proactive_config)

        # 专用 agent：只做决策（无 tools、无 task_loop、单轮、输出 JSON），
        # 替代 proactive_actions._analyze_and_decide 里的裸 model.invoke。
        proactive_agent = build_proactive_agent()
        proactive_engine.set_proactive_agent(proactive_agent)

        # 检查 agent 是否活跃——在调 LLM 之前检查，避免 agent 被 evict 后白调 LLM。
        def _check_agent_cb(channel_id):
            cid = channel_id or "web"
            manager = _resolve_agent_manager(server)
            if manager is None:
                return False
            agent = manager.get_agent_nowait(cid)
            return agent is not None and hasattr(agent, "process_message_stream")
        proactive_engine.set_check_agent_available_callback(_check_agent_cb)

        # 推送通知回调——直接推文本到前端，不经过主 agent（不进 context）。
        # 用于"今日推荐已达上限"等系统提醒。
        async def _send_notification_cb(channel_id, text):
            cid = channel_id or "web"
            import time as _time
            try:
                sent = await send_runtime_push({
                    "request_id": f"proactive_notification_{int(_time.time() * 1000)}",
                    "channel_id": cid,
                    "payload": {
                        "content": text,
                        "event_type": "chat.final",
                        "role": "assistant",
                        "source": "proactive_notification",
                    },
                })
                return sent
            except Exception as exc:
                logger.debug("[ProactiveEngine] send_notification push failed: %s", exc)
                return False
        proactive_engine.set_send_notification_callback(_send_notification_cb)

        # 触发主 agent 回调：tick 决策后，_trigger_main_agent 已把决策包成
        # 指令式 query 并填进 ProactiveTriggerRequest，这里透传给 trigger_main_agent
        # 跑主 agent 生成话术 → 进 context engine → stream 推前端。
        # _trigger_cb 收单参 request（G.FNM.03：契约具名化，从 6 位置参降到 1 参）。
        async def _trigger_cb(request: ProactiveTriggerRequest):
            return await trigger_main_agent(server, request)

        proactive_engine.set_trigger_main_agent_callback(_trigger_cb)
        server.set_proactive_engine(proactive_engine)
    except Exception as exc:
        logger.warning("[AgentServer] ProactiveEngine initialization failed: %s", exc)
