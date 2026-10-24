"""request_identity_rail — 常挂的请求身份重绑 rail。

为什么需要这个 rail（而不是只在埋点处读 ContextVar）：

- 模型调用不发生在 handler task 上，而发生在 supervisor 派生的 round task
  （或 warm-pool worker）上；``ContextVar.set`` 只改当前 task 的副本，
  handler 上逐请求的绑定传不进**早已创建**的长生命周期 task——supervisor
  创建时冻结的是首个请求的值，后续请求再怎么 set 也到达不了。
- request_id 唯一可靠的跨 task 通道是 ``run_context.extra``（adapter 逐请求
  注入，与 enable_routing 同一通道，已被证明逐请求正确）。本 rail 在
  ``before_invoke``（round task 上、模型调用前）把该值抄进请求身份
  ContextVar，使 ``before_model_http`` 埋点与 llm_io_trace 在本 task 上
  读到正确身份。读者修不了错的数据，必须在读的那个 task 上被 set 过一次。
- 与 ``ModelRoutingRail._bind_request_id`` 的关系：那条路径只在
  model_routing 开启时挂载；计费/归因的正确性不应系于路由开关，故此处
  无条件常挂，并与 ModelRoutingRail 共享同一绑定实现（幂等，重复绑定无害）。
"""
from __future__ import annotations

import sys
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

# llm_io_trace 请求归因 ContextVar 所在模块。interface_deep 是重模块且
# 非 AgentServer 进程可能未加载，这里用 sys.modules 探测而非 import：
# 已加载（模型调用经过 deep 路径时必然如此）则零成本取用，未加载则跳过。
_LLM_TRACE_MODULE = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"


def bind_request_id_from_run_context(ctx: Any) -> str | None:
    """从 ``run_context.extra`` 读 request_id 并重绑 openjiuwen 当前请求上下文。

    供 :class:`RequestIdentityRail` 与 ``ModelRoutingRail._bind_request_id``
    共用（单一实现，避免两处漂移）。返回重绑成功的 rid；未注入或绑定失败
    返回 ``None``，调用方可据此决定是否联动其他通道。全程 fail-open：
    任何失败只降级 debug 日志，不影响请求执行。
    """
    run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
    extra = getattr(run_context, "extra", None)
    rid = extra.get("request_id") if isinstance(extra, dict) else None
    if not rid:
        return None
    try:
        from openjiuwen.extensions.observability.span_context import (
            set_current_request_id,
        )

        set_current_request_id(str(rid))
        return str(rid)
    except Exception as exc:  # noqa: BLE001 - 重绑失败不影响请求
        logger.debug("[RequestIdentity] rebind request_id failed: %s", exc)
        return None


def bind_llm_trace_request_id(rid: str) -> None:
    """把同一 rid 重绑进 llm_io_trace 的 ``_LLM_TRACE_REQUEST_ID``（探测式）。

    round task 上该 ContextVar 同样是 supervisor 创建时冻结的陈旧值，这里
    顺路修正 llm_io_trace 的归因列。模块未加载 / 未暴露该属性（上游改名）
    时静默跳过——探测失败绝不冒泡。
    """
    try:
        module = sys.modules.get(_LLM_TRACE_MODULE)
        var = getattr(module, "_LLM_TRACE_REQUEST_ID", None) if module else None
        if var is None:
            return
        var.set(rid)
    except Exception as exc:  # noqa: BLE001 - 探测失败不影响请求
        logger.debug("[RequestIdentity] rebind llm_io_trace request_id failed: %s", exc)


class RequestIdentityRail(DeepAgentRail):
    """把 ``run_context.extra["request_id"]`` 重绑到请求身份 ContextVar 的轻量 rail。

    - 无条件挂载：deep 基类表首位、code 固定表首位、flash 白名单保留
      （三 profile 冷启动/热更新共用 ``_build_agent_rails`` 装配），
      不受 model_routing 开关约束。
    - 只重绑 request_id：session_id 在 agent task 上不会陈旧（一个 supervisor
      只服务一个 session，创建时绑定的值在会话生命周期内恒为真值），无需重绑。
    - 绑定发生在 round task 上、模型调用前；round task 结束即随上下文消亡，
      不跨请求泄漏。全程 fail-open，绝不影响请求执行。
    """

    # 早于 ModelRoutingRail(95)/TaskPlanningRail(90)：重绑必须在路由决策、
    # 以及任何依赖 [req=] 标签的日志之前完成。
    priority: int = 100

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """invoke 起点：重绑 openjiuwen request_id，并联动 llm_io_trace 归因。"""
        rid = bind_request_id_from_run_context(ctx)
        if rid:
            bind_llm_trace_request_id(rid)
            # 调试观测：确认本轮 round task 身份已修正（排查归因串号时开 debug 即见）
            logger.debug(
                "[RequestIdentity] request_id rebinded on round task: %s", rid
            )
        else:
            # extra 未注入（cron/heartbeat 轮或无 request 请求）：保留继承上下文，
            # 不写空值覆盖。这条日志用于排查「扩展看到空 request_id」的场景。
            logger.debug(
                "[RequestIdentity] no request_id in run_context.extra; keep inherited context"
            )
