"""model_routing.rail — ModelRoutingRail."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional
from openjiuwen.core.context_engine import TiktokenCounter
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.common.utils import logger
from jiuwenswarm.agents.harness.common.model_gears import (
    MODE_MODEL_MAP,
    MODE_PREFERRED_PROVIDER,
    MODE_THINKING_MAP,
    sync_agent_compressor_models,
)
from .capability import ModelCapability
from .types import (
    PriorModelCall, TaskAnalysis, RoutingDecision,
    _agent_model_name, _extract_agent_info, _new_trace_id,
    _unwrap_user_message,
)


# 四档模式（先写死）：模式名 → 固定 (模型名, 思考深度)。
#   fast/balanced/auto → deepseek-v4.1-flash（思考 off/medium/medium）
#   extreme            → glm-5.2（思考 deep）
# 具体模型名不在此表内（走 skip 分支，保留 adapter 已应用的具体/默认模型）。
# 与宿主产品侧档位映射同表同源（见 model_gears.MODE_MODEL_MAP）。
# 表本体在 jiuwenswarm.agents.harness.common.model_gears（deepresearch 隔离子进程
# 也要用同一张表把档位翻成具体模型），此处只做别名，避免两处各写一份漂移。
_MODE_MODEL_MAP: dict[str, str] = MODE_MODEL_MAP

_MODE_THINKING_MAP: dict[str, str] = MODE_THINKING_MAP

# 四档写死映射里的「官方模型」标记：宿主写 models.json 时给官方供应商标记
# model_provider（见 model_gears.MODE_PREFERRED_PROVIDER；config.yaml 亦可手写）。
# 四档查找优先命中此标，避免撞到用户自定义的同名模型；无标则退回第一个同名。
_MODE_PREFERRED_PROVIDER = MODE_PREFERRED_PROVIDER

# 四档附带的思考语义 → 产品 depth（再经 vendor_map 按模型族翻译）。
# 具体模型 / default：仅 GLM-5.3 族自动注入产品默认 depth（当前 low）；其余 no-op。
_GEAR_THINKING_TO_DEPTH: dict[str, str] = {
    "off": "off",
    "medium": "medium",
    "deep": "high",
    "default": "default",
}


class ModelRoutingRail(DeepAgentRail):
    """模型路由 Rail —— 产出推荐模型 + 任务分析；按请求的模型选择路由。

    请求模型选择（前端下拉框，经 relay frame ``params.model_name`` → request.params →
    ``run_context.extra["model_selection"]``）取值决定行为：
    - "fast"/"balanced"/"extreme"/"auto" → 四档模式（写死）→ 固定 (模型, 思考深度)；
    - 具体模型名（非关键字）→ 跳过路由，保留 adapter 已应用的具体/默认模型。
    始终真切换（set_llm）；日志里体现所选模式（mode=…）。

    路由在 before_invoke 中执行（每个 invoke 一次）；before_model_call 用于按档位
    注入思考深度（off/medium/deep）。
    """

    priority: int = 95  # 早于 TaskPlanningRail(90)，确保路由先生效

    def __init__(
        self,
        capability_table: Optional[list[ModelCapability]] = None,
        *,
        apply_routing: bool = True,
    ) -> None:
        super().__init__()
        self._capability_table: list[ModelCapability] = capability_table or []
        self._call_history: list[PriorModelCall] = []
        self._token_counter = TiktokenCounter()
        self._apply_routing: bool = apply_routing
        self._request_thinking: str = "default"
        self._request_mode: str = ""
        self._trace_id: str = _new_trace_id()

    # ---- 路由 ---- #

    def _resolve_request_selection(self, ctx: AgentCallbackContext) -> str:
        """每请求读取前端下拉选择值（经 relay frame params.model_name → run_context.extra 注入）。

        - fast/balanced/extreme/auto → 四档模式（_MODE_MODEL_MAP 写死映射）；
        - 其它（具体模型名 / 空）→ 跳过路由（返回 ""，before_invoke 走 skip 分支）。
        注入链：adapter 把 request.params["model_name"] 写入
        ``inputs["run"]["context"]["extra"]["model_selection"]``，DeepAgent
        ``_normalize_inputs`` 再把它带进 ``InvokeInputs.run_context.extra``。
        """
        run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
        extra = getattr(run_context, "extra", None)
        if isinstance(extra, dict):
            raw = extra.get("model_selection")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return ""

    def _resolve_request_routing_enabled(self, ctx: AgentCallbackContext) -> bool:
        """每请求读取 relay 的 auto 档开关（``params.enable_routing`` → run_context.extra）。

        注入链：adapter 把 ``request.params["enable_routing"]`` 写入
        ``inputs["run"]["context"]["extra"]["enable_routing"]``，DeepAgent
        ``_normalize_inputs`` 再把它带进 ``InvokeInputs.run_context.extra``。

        严格判 ``is True``：adapter 侧已按 ``is True`` 过滤，这里再兜一层，
        任何非布尔真值（含字符串 "true"）都不算开启。

        注意：``run_context`` 经 ``effective_inputs`` **按引用**透传进内层
        ReActAgent，但 ``ctx.extra`` 不跨层 —— 开关只能从 ``run_context.extra`` 读。
        """
        run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
        extra = getattr(run_context, "extra", None)
        return isinstance(extra, dict) and extra.get("enable_routing") is True

    def _find_cap_by_name(
        self, name: str, *, prefer_provider: Optional[str] = None
    ) -> Optional[ModelCapability]:
        """按 model_name（忽略大小写/空白）在能力表中查找。

        ``prefer_provider`` 非空时，在所有同名 cap 里优先返回 ``model_provider``
        匹配的条目（四档写死映射用，优先带官方供应商标记的同名模型）；找不到标才退回第一个
        同名（向后兼容：无标能力表行为不变）。
        """
        key = (name or "").strip().lower()
        preferred = (prefer_provider or "").strip().lower()
        fallback: Optional[ModelCapability] = None
        for cap in self._capability_table:
            if (cap.model_name or "").strip().lower() != key:
                continue
            if fallback is None:
                fallback = cap
            if preferred and (cap.model_provider or "").strip().lower() == preferred:
                return cap
        return fallback

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """invoke 开始时：重置 trace_id / call_history，执行路由决策。"""
        self._trace_id = _new_trace_id()
        self._call_history = []
        self._request_thinking = "default"
        self._request_mode = ""

        try:
            # --- auto 档：本阶段只观察、不路由 ---
            # relay 的 auto 档下发保底具体模型名 + enable_routing=True。此处**只打日志**，
            # 并在任何决策逻辑之前 return：候选池注入 / 分类器 / 实际 set_llm 均为后续阶段。
            # 状态已在上面复位，故 return 后 before_model_call 仍是空操作。
            if self._resolve_request_routing_enabled(ctx):
                logger.info(
                    "[ModelRouting] enable_routing=true (log-only, no switch); selection=%s",
                    self._resolve_request_selection(ctx) or "(none)",
                )
                return

            # 从 ctx.inputs.query 提取用户查询文本（before_invoke 时无 messages 列表）
            query = getattr(getattr(ctx, "inputs", None), "query", None) or ""
            prompt_text = _unwrap_user_message(str(query)) if query else ""
            input_tokens = self._count_text_tokens(prompt_text)
            agent_info = _extract_agent_info(ctx)

            # --- 四档模式（写死）→ 固定 (模型, 思考深度)；具体模型/空 → 跳过 ---
            selection = self._resolve_request_selection(ctx)
            mode_model = _MODE_MODEL_MAP.get(selection)
            if mode_model is not None:
                thinking = _MODE_THINKING_MAP.get(selection, "medium")
                self._request_thinking = thinking
                self._request_mode = selection
                cap = self._find_cap_by_name(mode_model, prefer_provider=_MODE_PREFERRED_PROVIDER)
                if cap is None:
                    # keep current：不注入档位 thinking kwargs（当前模型 vendor 未知，
                    # DeepSeek/GLM 风格 extra_body.thinking 对 OpenAI 官方端点会 400）
                    self._request_thinking = "default"
                    logger.warning(
                        "[ModelRouting] mode=%s model=%s not in capability table (%d models); "
                        "keep current model, thinking=%s",
                        selection, mode_model, len(self._capability_table), thinking,
                    )
                    self._emit_decision(
                        ctx,
                        recommended_cap=None,
                        category=selection,
                        difficulty=thinking,
                        input_tokens=input_tokens,
                        agent_info=agent_info,
                        reasoning=(
                            f"mode={selection} model={mode_model} not found; "
                            f"keep current model, thinking={thinking}"
                        ),
                    )
                    return
                if cap.model is None:
                    # keep current（无 Model 对象可切）：同上，不注入 thinking kwargs
                    self._request_thinking = "default"
                    logger.warning(
                        "[ModelRouting] mode=%s model=%s has no Model object (builder missing); "
                        "cannot switch, thinking=%s",
                        selection, mode_model, thinking,
                    )
                    self._emit_decision(
                        ctx,
                        recommended_cap=None,
                        category=selection,
                        difficulty=thinking,
                        input_tokens=input_tokens,
                        agent_info=agent_info,
                        reasoning=(
                            f"mode={selection} model={mode_model} has no Model object; "
                            f"keep current, thinking={thinking}"
                        ),
                    )
                    return
                self._emit_decision(
                    ctx,
                    recommended_cap=cap,
                    category=selection,
                    difficulty=thinking,
                    input_tokens=input_tokens,
                    agent_info=agent_info,
                    reasoning=f"mode={selection} -> model={cap.model_name} thinking={thinking}",
                )
                logger.info(
                    "[ModelRouting] mode=%s -> model=%s thinking=%s",
                    selection, cap.model_name, thinking,
                )
                return

            # --- 具体模型 / 空：不路由，保留 adapter 已应用的具体/默认模型 ---
            self._emit_decision(
                ctx,
                recommended_cap=None,
                category="skipped",
                difficulty="skipped",
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning=f"concrete model selected ({selection or 'default'}), routing skipped",
            )
            logger.info(
                "[ModelRouting] skipped (concrete model); selection=%s",
                selection or "(none)",
            )
        except Exception as exc:
            logger.warning("[ModelRouting] before_invoke failed: %s", exc, exc_info=True)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """按请求档位 / 模型族注入思考参数（经 vendor_map）。

        - 四档：fast=off / balanced|auto=medium / extreme=deep→high
        - 具体模型：``_request_thinking=default`` → 仅 GLM-5.3 族注入产品默认 depth（low）
        写 ``ctx.extra["llm_call_kwargs"]``，由 ReActAgent 每次 model call 消费。
        不支持思考控制的模型：adapt 降级为空 kwargs，本方法不写入。
        """
        from jiuwenswarm.common.thinking.adapter import adapt_thinking
        from jiuwenswarm.common.thinking.types import thaw_llm_call_kwargs

        depth = _GEAR_THINKING_TO_DEPTH.get(
            self._request_thinking, self._request_thinking or "default"
        )
        model_name = _agent_model_name(ctx)
        used_cap = None
        extra_probe = getattr(ctx, "extra", None)
        if isinstance(extra_probe, dict):
            used_cap = extra_probe.get("_model_routing_used_cap")
        if used_cap is not None:
            cap_name = getattr(used_cap, "model_name", None)
            if isinstance(cap_name, str) and cap_name.strip():
                model_name = cap_name.strip()
        try:
            profile = adapt_thinking(depth, model_name=model_name)
            if not profile.injected or not profile.llm_call_kwargs:
                return
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict):
                return
            kwargs = thaw_llm_call_kwargs(profile.llm_call_kwargs)
            extra["llm_call_kwargs"] = kwargs
            logger.info(
                "[ModelRouting] thinking inject mode=%s thinking=%s model=%r kwargs=%r",
                self._request_mode or "(none)",
                profile.thinking,
                model_name,
                kwargs,
            )
        except Exception as exc:
            logger.debug("[ModelRouting] thinking inject failed: %s", exc)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        try:
            response = getattr(getattr(ctx, "inputs", None), "response", None)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            model_name = _agent_model_name(ctx) or "unknown"
            end_time = datetime.now(tz=timezone.utc).isoformat()
            # 1) 累积到本次 invoke 的前置调用链（完整 OTel span）
            self._call_history.append(
                PriorModelCall(
                    model=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    iteration=len(self._call_history),
                    trace_id=self._trace_id,
                    start_time=end_time,  # before_model_call 已移除，无精确 start_time
                    end_time=end_time,
                )
            )
        except Exception as exc:
            logger.debug("[ModelRouting] after_model_call failed: %s", exc)

    # ---- 内部 ---- #

    def _emit_decision(
        self,
        ctx: AgentCallbackContext,
        *,
        recommended_cap: Optional[ModelCapability],
        category: str,
        difficulty: str,
        target_score: int = 0,
        input_tokens: int,
        agent_info: dict[str, Any],
        reasoning: str,
    ) -> None:
        recommended_model_id = (
            recommended_cap.model_id or recommended_cap.model_name
            if recommended_cap
            else None
        )
        # 真切换：apply_routing 打开且能力表带 Model 对象时，切换 agent 当前模型
        # （仿 openjiuwen TaskPlanningRail：set_llm + 同步 config.model_name）
        if (
            self._apply_routing
            and recommended_cap is not None
            and recommended_cap.model is not None
        ):
            try:
                react_agent = _resolve_react_agent(ctx.agent)
                previous_model = getattr(react_agent, "_llm", None)
                react_agent.set_llm(recommended_cap.model)
                # 记下实际切到的 cap（同模型多 API 场景靠 model_id 区分）
                if isinstance(ctx.extra, dict):
                    ctx.extra["_model_routing_used_cap"] = recommended_cap
                mname = (
                    getattr(getattr(recommended_cap.model, "model_config", None), "model_name", None)
                    or recommended_cap.model_name
                )
                if mname:
                    # 供 RuntimePromptRail 注入「当前模型」用：档位关键字（fast/balanced/
                    # extreme/auto）是作为 model_name 下发的，runtime_state 与 agent.model_name
                    # 记的都是路由前的取值，这里落一份路由后真正生效的模型名。
                    # 必须落进按引用透传的 RunContext：本 rail 的 before_invoke 只注册在外层
                    # DeepAgent 的 ctx 上，读取方 RuntimePromptRail.before_model_call 却跑在
                    # 内层 ReActAgent 新建的 ctx 上（extra 是另一个 dict），外层 ctx.extra
                    # 传不过去；RunContext 则经 effective_inputs 原样带进内层，内层可从
                    # ctx.extra["run_context"].extra 读回。
                    run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
                    rc_extra = getattr(run_context, "extra", None)
                    if isinstance(rc_extra, dict):
                        rc_extra["model_routing_applied_model"] = str(mname)
                    # Sync inner ReActAgent config
                    cfg = getattr(react_agent, "_config", None) or getattr(react_agent, "config", None)
                    if cfg is not None:
                        try:
                            setattr(cfg, "model_name", mname)
                            setattr(cfg, "model_client_config", recommended_cap.model.model_client_config)
                            setattr(cfg, "model_config_obj", recommended_cap.model.model_config)
                        except Exception as exc:
                            logger.debug("[ModelRouting] setattr inner config failed: %s", exc)
                    # Sync outer DeepAgent config too (if ctx.agent is the wrapper)
                    if ctx.agent is not react_agent:
                        outer_cfg = getattr(ctx.agent, "_config", None) or getattr(ctx.agent, "config", None)
                        if outer_cfg is not None:
                            try:
                                setattr(outer_cfg, "model_name", mname)
                                setattr(outer_cfg, "model_client_config", recommended_cap.model.model_client_config)
                                setattr(outer_cfg, "model_config_obj", recommended_cap.model.model_config)
                            except Exception as exc:
                                logger.debug("[ModelRouting] setattr outer config failed: %s", exc)
                        deep_cfg = getattr(ctx.agent, "_deep_config", None) or getattr(ctx.agent, "deep_config", None)
                        if deep_cfg is not None:
                            try:
                                setattr(deep_cfg, "model", recommended_cap.model)
                            except Exception as exc:
                                logger.debug("[ModelRouting] setattr deep_config.model failed: %s", exc)
                    # Sync agent.model_name
                    try:
                        if hasattr(react_agent, "model_name"):
                            setattr(react_agent, "model_name", mname)
                        if ctx.agent is not react_agent and hasattr(ctx.agent, "model_name"):
                            setattr(ctx.agent, "model_name", mname)
                    except Exception as exc:
                        logger.debug("[ModelRouting] setattr model_name failed: %s", exc)
                # 主对话换模后，同步已缓存的上下文压缩器 Model，避免压缩仍用档位关键字
                # / 过期 id → 模型调用失败，压缩 noop。
                synced = sync_agent_compressor_models(
                    react_agent,
                    recommended_cap.model,
                    host=ctx.agent,
                    previous_model=previous_model,
                )
                logger.info(
                    "[ModelRouting] applied set_llm -> %s compressor_synced=%s",
                    mname or "unknown",
                    synced,
                )
            except Exception as exc:
                logger.warning("[ModelRouting] set_llm failed: %s", exc)
        decision = RoutingDecision(
            recommended_model_id=recommended_model_id,
            analysis=TaskAnalysis(
                category=category,
                difficulty=difficulty,
                target_score=target_score,
                predicted_input_tokens=input_tokens,
                agent_info=agent_info,
            ),
            reasoning=reasoning,
            prior_calls_otel=[c.to_otel_span() for c in self._call_history],
        )
        if isinstance(ctx.extra, dict):
            ctx.extra["model_routing_decision"] = asdict(decision)

    def _count_text_tokens(self, text: str) -> int:
        """独立计算文本 token（tiktoken），不依赖模型上报。"""
        try:
            return int(self._token_counter.count(text))
        except Exception:
            return max(0, len(text) // 4)


# ---- Helpers ---- #


def _resolve_react_agent(agent: Any) -> Any:
    """Return the inner ReActAgent if *agent* is a DeepAgent, else *agent* itself.

    DeepAgent does not expose ``set_llm``; its inner ``_react_agent``
    (a ReActAgent) does.  This mirrors the adapter-level pattern in
    ``interface_deep._apply_model_to_react_agent``.
    """
    if callable(getattr(agent, "set_llm", None)):
        return agent
    inner = getattr(agent, "_react_agent", None)
    if inner is not None and callable(getattr(inner, "set_llm", None)):
        return inner
    return agent
