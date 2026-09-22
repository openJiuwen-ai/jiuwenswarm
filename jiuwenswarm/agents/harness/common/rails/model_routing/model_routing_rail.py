"""model_routing.rail — ModelRoutingRail."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from openjiuwen.core.context_engine import TiktokenCounter
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.common.utils import logger
from .capability import ModelCapability, build_capability_table_from_config
from .stats import _ModelUsageStats, get_stats_store
from .types import (
    PriorModelCall, TaskAnalysis, RoutingDecision,
    _agent_model_name, _extract_agent_info, _new_trace_id,
    _unwrap_user_message,
)


# 四档模式（先写死）：模式名 → 固定 (模型名, 思考深度)。
#   fast      → deepseek-v4-flash-0731 关闭思考
#   balanced  → deepseek-v4-flash-0731 中等思考
#   extreme   → glm-5.2                 深度思考
#   auto      → deepseek-v4-flash-0731 中等思考
# 具体模型名不在此表内（走 skip 分支，保留 adapter 已应用的具体/默认模型）。
_MODE_MODEL_MAP: dict[str, str] = {
    "fast": "deepseek-v4-flash-0731",
    "balanced": "deepseek-v4-flash-0731",
    "extreme": "glm-5.2",
    "auto": "deepseek-v4-flash-0731",
}

_MODE_THINKING_MAP: dict[str, str] = {
    "fast": "off",      # 关闭思考
    "balanced": "medium",  # 中等思考
    "extreme": "deep",  # 深度思考
    "auto": "medium",   # 中等思考
}

# 思考深度 → 直接注入的 llm_call_kwargs（不经 vendor 白名单 / 语义适配层，短路）。
#   off    → extra_body.thinking.type=disabled（DeepSeek/GLM 通用“关闭思考”）
#   medium → extra_body.thinking.type=enabled（开启思考，默认深度）
#   deep   → extra_body.thinking.type=enabled + reasoning_effort=high（思考开到最高）
# 核心 allowlist 直接透传 extra_body / reasoning_effort。
_THINKING_KWARGS: dict[str, dict[str, Any]] = {
    "off": {"extra_body": {"thinking": {"type": "disabled"}}},
    "medium": {"extra_body": {"thinking": {"type": "enabled"}}},
    "deep": {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "high"},
}


class ModelRoutingRail(DeepAgentRail):
    """模型路由 Rail —— 产出推荐模型 + 任务分析 + token 统计；按请求的模型选择路由。

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
        stats: Optional[_ModelUsageStats] = None,
        stats_path: Optional[str] = None,
        apply_routing: bool = True,
    ) -> None:
        super().__init__()
        self._capability_table: list[ModelCapability] = capability_table or []
        self._call_history: list[PriorModelCall] = []
        self._token_counter = TiktokenCounter()
        self._stats: _ModelUsageStats = stats or get_stats_store(stats_path)
        self._apply_routing: bool = apply_routing
        self._request_thinking: str = "default"
        self._request_mode: str = ""
        self._trace_id: str = _new_trace_id()
        self._load_persisted_table(persist=False)

    # ---- 生命周期钩子 ---- #
    def _load_persisted_table(self, *, persist: bool = True) -> None:
        """加载持久化模型表（含 token_used）合并进能力表；persist=True 时回写整表。

        启动时调 persist=False（只读合并，不写文件——避免 env 未解析的默认模型覆盖真实统计）；
        reload 时调 persist=True（合并 + 回写整表，sync config 模型 + 保留 token_used）。
        """
        try:
            models = self._stats.snapshot().get("models", {})
        except Exception as exc:
            logger.debug("[ModelRouting] stats snapshot failed: %s", exc)
            return
        for cap in self._capability_table:
            key = cap.model_id or cap.model_name
            entry = models.get(key) or models.get(cap.model_name)
            if not isinstance(entry, dict):
                continue
            tu = entry.get("token_used") if isinstance(entry.get("token_used"), dict) else entry
            cap.token_used = {
                "input_tokens": int(tu.get("input_tokens", 0) or 0),
                "output_tokens": int(tu.get("output_tokens", 0) or 0),
                "call_count": int(tu.get("call_count", 0) or 0),
                "last_used": tu.get("last_used"),
            }
        # 回写整表（仅在 reload 时；启动 persist=False 不写，避免默认值覆盖 + 保留文件里已有但不在当前 caps 的模型统计）
        if not persist:
            return
        try:
            self._stats.persist_table(self._capability_table)
        except Exception as exc:
            logger.debug("[ModelRouting] persist_table failed: %s", exc)

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

    def _find_cap_by_name(self, name: str) -> Optional[ModelCapability]:
        """按 model_name（忽略大小写/空白）在能力表中查找。"""
        key = (name or "").strip().lower()
        for cap in self._capability_table:
            if (cap.model_name or "").strip().lower() == key:
                return cap
        return None

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """invoke 开始时：重置 trace_id / call_history，执行路由决策。"""
        self._trace_id = _new_trace_id()
        self._call_history = []
        self._request_thinking = "default"
        self._request_mode = ""

        try:
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
                cap = self._find_cap_by_name(mode_model)
                if cap is None:
                    logger.warning(
                        "[ModelRouting] mode=%s model=%s not in capability table (%d models); keep current model, thinking=%s",
                        selection, mode_model, len(self._capability_table), thinking,
                    )
                    self._emit_decision(
                        ctx,
                        recommended_cap=None,
                        category=selection,
                        difficulty=thinking,
                        input_tokens=input_tokens,
                        agent_info=agent_info,
                        reasoning=f"mode={selection} model={mode_model} not found; keep current model, thinking={thinking}",
                    )
                    return
                if cap.model is None:
                    logger.warning(
                        "[ModelRouting] mode=%s model=%s has no Model object (builder missing); cannot switch, thinking=%s",
                        selection, mode_model, thinking,
                    )
                    self._emit_decision(
                        ctx,
                        recommended_cap=None,
                        category=selection,
                        difficulty=thinking,
                        input_tokens=input_tokens,
                        agent_info=agent_info,
                        reasoning=f"mode={selection} model={mode_model} has no Model object; keep current, thinking={thinking}",
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
        """按请求档位直接注入思考深度（fast=off / balanced=medium / extreme=deep / auto=medium）。

        具体模型（skip 分支）时 ``_request_thinking`` 保持 default，本方法 no-op。
        直接写 ``ctx.extra["llm_call_kwargs"]``，由 ReActAgent 每次 model call 消费
        （pop 后合入调用 kwargs）。不经 vendor 白名单：off 用 DeepSeek/GLM 通用的
        ``extra_body.thinking.type=disabled``，medium 用 ``thinking.type=enabled``，
        deep 用 ``thinking.type=enabled`` + ``reasoning_effort=high``（核心 allowlist 直接透传）。
        """
        kwargs = _THINKING_KWARGS.get(self._request_thinking)
        if kwargs is None:
            return
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict):
                return
            extra["llm_call_kwargs"] = deepcopy(kwargs)
            logger.info(
                "[ModelRouting] thinking inject mode=%s thinking=%s kwargs=%r",
                self._request_mode or "(none)",
                self._request_thinking,
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
            # 2) 持久化 per-model token 用量（按 client_id；优先用实际切到的 cap，回退 model_name 查）
            used_cap = ctx.extra.get("_model_routing_used_cap") if isinstance(ctx.extra, dict) else None
            cap = used_cap or next((c for c in self._capability_table if c.model_name == model_name), None)
            mid = (cap.model_id if cap and cap.model_id else model_name) or model_name
            self._stats.record(
                mid,
                model_name,
                input_tokens,
                output_tokens,
                model_provider=cap.model_provider if cap else "unknown",
                model_group=cap.model_group if cap else "unknown",
                is_trusted=cap.is_trusted if cap else False,
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
                react_agent.set_llm(recommended_cap.model)
                # 记下实际切到的 cap，供 after_model_call 按 client_id 记 token（同模型多 API 场景）
                if isinstance(ctx.extra, dict):
                    ctx.extra["_model_routing_used_cap"] = recommended_cap
                mname = (
                    getattr(getattr(recommended_cap.model, "model_config", None), "model_name", None)
                    or recommended_cap.model_name
                )
                if mname:
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
                logger.info("[ModelRouting] applied set_llm -> %s", mname or "unknown")
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
            model_usage_stats=self._stats.snapshot(),
        )
        ctx.extra["model_routing_decision"] = asdict(decision)

    def _count_text_tokens(self, text: str) -> int:
        """独立计算文本 token（tiktoken），不依赖模型上报。"""
        try:
            return int(self._token_counter.count(text))
        except Exception:
            return max(0, len(text) // 4)

    def reload_capability_table(
        self,
        config: dict[str, Any] | None,
        *,
        model_builder: Optional[Callable[[dict, dict], Any]] = None,
    ) -> None:
        """从 config 重新加载能力表（配置/env 更新时调用，无需重建整个 rail）。

        启动时由 ``_build_model_routing_rail`` 构建；热重载若走整 rail 重建则自动刷新，
        否则可显式调本方法。model_builder 传 ``JiuWenClawDeepAdapter._build_model_from_entry``。
        """
        self._capability_table = build_capability_table_from_config(
            config, model_builder=model_builder
        )
        logger.info(
            "[ModelRouting] capability table reloaded: %d models",
            len(self._capability_table),
        )
        self._load_persisted_table()


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
