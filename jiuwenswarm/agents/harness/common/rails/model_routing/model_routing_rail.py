"""model_routing.rail — ModelRoutingRail."""
from __future__ import annotations
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from openjiuwen.core.common.background_tasks import create_background_task, BackgroundTask
from openjiuwen.core.context_engine import TiktokenCounter
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.common.utils import logger
from .capability import ModelCapability, build_capability_table_from_config, _capability_rank
from .classifier import task_score
from .stats import _ModelUsageStats, get_stats_store, reset_stats_store_for_test
from .routing import _decide_and_select, _detect_model_type
from .health_check import ModelHealthChecker, HealthCheckConfig
from .types import (
    PriorModelCall, TaskAnalysis, RoutingDecision,
    _extract_prompt_text, _message_text, _agent_model_name,
    _extract_agent_info, _get_session_id, _new_trace_id, _new_span_id,
    _unwrap_user_message,
)


class ModelRoutingRail(DeepAgentRail):
    """模型路由 Rail —— 产出推荐模型 + 任务分析 + token 统计；apply_routing 控制是否真切换。

    路由在 before_invoke 中执行（每个 invoke 一次），before_model_call 不再需要。
    健康检查以后台循环运行，首次 before_invoke 时懒启动；路由直接读缓存，不阻塞。
    """

    priority: int = 95  # 早于 TaskPlanningRail(90)，确保路由先生效

    def __init__(
        self,
        capability_table: Optional[list[ModelCapability]] = None,
        *,
        classifier: Optional[Any] = None,
        mapper: Optional[dict] = None,
        stats: Optional[_ModelUsageStats] = None,
        stats_path: Optional[str] = None,
        apply_routing: bool = False,
        health_check_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._capability_table: list[ModelCapability] = capability_table or []
        self._classifier = classifier
        self._mapper: dict = mapper or {}
        self._call_history: list[PriorModelCall] = []
        self._token_counter = TiktokenCounter()
        self._stats: _ModelUsageStats = stats or get_stats_store(stats_path)
        self._apply_routing: bool = apply_routing
        self._trace_id: str = _new_trace_id()
        self._health_checker = ModelHealthChecker(HealthCheckConfig.from_dict(health_check_config))
        self._bg_tasks: set[BackgroundTask] = set()
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

    # ---- 健康检查后台循环 ---- #

    async def _ensure_health_check_loop(self) -> None:
        """确保健康检查后台循环在运行；若无运行中的任务则启动。

        在 before_invoke 中调用，利用 _bg_tasks 是否为空判断（无需额外标志位）。
        路由决策直接读取缓存的 _status_map，不阻塞。
        """
        self._bg_tasks = {t for t in self._bg_tasks if not t.done()}
        if self._bg_tasks:
            return
        bg_task = await create_background_task(
            self._health_check_loop(),
            name="model-routing-health",
            group="model_routing",
        )
        self._bg_tasks.add(bg_task)
        logger.info("[ModelRouting] health check background loop started")

    async def _health_check_loop(self) -> None:
        """后台周期性健康检查循环。

        按 interval_seconds 间隔调用 update_health 刷新 _status_map 缓存。
        路由决策读取缓存即可，不会被阻塞。
        """
        interval = self._health_checker.interval_seconds
        while True:
            try:
                await self._health_checker.update_health(self._capability_table)
            except Exception as exc:
                logger.debug("[ModelRouting] health check loop error: %s", exc)
            await asyncio.sleep(interval)

    async def cleanup_background_tasks(self) -> None:
        """取消并清理所有后台任务。宿主通过 getattr(rail, 'cleanup_background_tasks') 鸭子类型调用。"""
        for task in self._bg_tasks:
            if not task.done():
                await task.cancel(reason="model_routing_shutdown")
        self._bg_tasks.clear()
        logger.info("[ModelRouting] health check background loop stopped")

    # ---- 路由 ---- #

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """invoke 开始时：重置 trace_id / call_history，执行路由决策。"""
        self._trace_id = _new_trace_id()
        self._call_history = []

        try:
            # 从 ctx.inputs.query 提取用户查询文本（before_invoke 时无 messages 列表）
            query = getattr(getattr(ctx, "inputs", None), "query", None) or ""
            prompt_text = _unwrap_user_message(str(query)) if query else ""
            input_tokens = self._count_text_tokens(prompt_text)
            agent_info = _extract_agent_info(ctx)
            session_id = _get_session_id(ctx)

            # --- 健康检查：确保后台循环运行，直接读缓存（不阻塞）---
            await self._ensure_health_check_loop()
            routing_caps = self._health_checker.get_healthy_caps(self._capability_table)

            # --- 单模型跳过 ---
            if len(routing_caps) <= 1:
                single = routing_caps[0] if routing_caps else None
                self._emit_decision(
                    ctx,
                    recommended_cap=single,
                    category="skipped",
                    difficulty="skipped",
                    input_tokens=input_tokens,
                    agent_info=agent_info,
                    reasoning="single model available, routing skipped",
                )
                logger.info(
                    "[ModelRouting] skipped (single model); in_tok=%d",
                    input_tokens,
                )
                return

            # --- 正常路由（含图请求会在 _decide_and_select 里限到 model_type=="vision" 候选）---
            if self._classifier is not None:
                raw_score, category, difficulty = await self._classifier(prompt_text)
                cls_reasoning = f"classifier score={raw_score}"
            else:
                raw_score, category, difficulty = 50, "unknown", "hard"
                cls_reasoning = "no classifier, fallback"
            target = task_score(category, difficulty, self._mapper)
            required_model_type = _detect_model_type(ctx)
            recommended_cap, reason = _decide_and_select(
                target, routing_caps, ctx,
                category=category, difficulty=difficulty,
                required_model_type=required_model_type,
            )
            self._emit_decision(
                ctx,
                recommended_cap=recommended_cap,
                category=category,
                difficulty=difficulty,
                target_score=target,
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning=f"{cls_reasoning}; {reason}",
            )
            rec_id = (
                recommended_cap.model_id or recommended_cap.model_name
                if recommended_cap
                else None
            )
            logger.info(
                "[ModelRouting] classifier: [%s,%s] score=%d in_tok=%d model_type=%s -> recommend=%s",
                category,
                difficulty,
                target,
                input_tokens,
                required_model_type or "(none)",
                rec_id,
            )
        except Exception as exc:
            logger.warning("[ModelRouting] before_invoke failed: %s", exc, exc_info=True)

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
