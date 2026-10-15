"""model_routing.rail — ModelRoutingRail."""
from __future__ import annotations
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from openjiuwen.core.context_engine import TiktokenCounter
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail
from jiuwenswarm.common.utils import logger
from .capability import ModelCapability
from .classifier import CONTINUATION_CATEGORY, validate_score
from .compressor_sync import sync_agent_compressor_models
from .types import (
    PriorModelCall, TaskAnalysis, RoutingDecision,
    _agent_model_name, _extract_agent_info, _new_trace_id,
    _unwrap_user_message,
)


# auto 档（relay 下发 enable_routing=True）的分类器阈值与切换目标：
# 分类器给任务打 0-100 分，大于等于 40 才把模型换成 glm-5.3，否则保留当前模型。
_CLASSIFIER_SCORE_THRESHOLD: int = 40
_CLASSIFIER_TARGET_MODEL: str = "glm-5.3"

# ``classifier`` 未显式传入时的哨兵，与显式 ``None``（关闭 auto 档路由）区分开。
_UNSET: Any = object()

# 懒加载的默认分类器缓存（``_UNSET`` 表示尚未装载）。
_DEFAULT_CLASSIFIER: Any = _UNSET


def model_routing_enabled(config: Any = None) -> bool:
    """model_routing 总开关：config ``model_routing.enabled``（默认 false）+ env 覆盖。

    单一事实来源：单 agent adapter（``interface_deep._model_routing_enabled``）与 swarm
    成员 provider（``swarm.model_routing``）共用同一判定，避免两处各读一次开关而漂移。
    relay 可经 spawn env 注入 ``JIUWENSWARM_MODEL_ROUTING_ENABLED``（进程级，仿
    ``JIUWENSWARM_CODE_COAUTHOR_HEADER_ENABLED``）；env 显式设置时优先于 config。
    """
    from jiuwenswarm.common.kv_cache_affinity_config import parse_bool

    raw = config.get("model_routing") if isinstance(config, dict) else None
    enabled = parse_bool(raw.get("enabled")) if isinstance(raw, dict) else False
    env_val = os.getenv("JIUWENSWARM_MODEL_ROUTING_ENABLED")
    if env_val is not None and env_val.strip():
        enabled = parse_bool(env_val)
    return enabled


def _load_default_classifier() -> Optional[Any]:
    """懒加载内置分类器（``.classifier.classify``），import 失败返回 ``None`` 降级。"""
    global _DEFAULT_CLASSIFIER
    if _DEFAULT_CLASSIFIER is _UNSET:
        try:
            from .classifier import classify

            _DEFAULT_CLASSIFIER = classify
        except Exception as exc:  # noqa: BLE001 - 装载失败必须降级而非冒泡
            logger.warning("[ModelRouting] default classifier unavailable: %s", exc)
            _DEFAULT_CLASSIFIER = None
    return _DEFAULT_CLASSIFIER


class ModelRoutingRail(DeepAgentRail):
    """模型路由 Rail —— auto 档跑分类器，超阈值时切换模型。

    auto 档（带 ``enable_routing`` 开关）跑分类器打分，score >= 40 换成 glm-5.3，
    否则保留 adapter 已应用的模型；具体模型名 / 空选择走 skip 分支。

    分类器与 Model 构造器都不需要 adapter 注入：未注入时懒加载内置
    ``rails/model_routing/classifier.py``，构造器用 openjiuwen 直构兜底。

    路由在 before_invoke 中执行（每个 invoke 一次）。
    """

    priority: int = 95  # 早于 TaskPlanningRail(90)，确保路由先生效

    def __init__(
        self,
        capability_table: Optional[list[ModelCapability]] = None,
        *,
        classifier: Any = _UNSET,
        model_builder: Optional[Callable[[dict, dict], Any]] = None,
        apply_routing: bool = True,
    ) -> None:
        super().__init__()
        self._capability_table: list[ModelCapability] = capability_table or []
        # auto 档打分器：async (prompt_text) -> (score, category, difficulty)。
        # 缺省（_UNSET）→ 懒加载内置实现；显式传 None → 关闭 auto 档路由。
        self._classifier: Any = classifier
        # 换 glm-5.3 用的 Model 构造器；不传则用 openjiuwen 直构。入参是当前已建好
        # 模型的 client config 克隆，凭证（api_base / api_key / 鉴权头）已在其中。
        self._model_builder: Optional[Callable[[dict, dict], Any]] = model_builder
        self._call_history: list[PriorModelCall] = []
        self._token_counter = TiktokenCounter()
        self._apply_routing: bool = apply_routing
        self._trace_id: str = _new_trace_id()

    # ---- 路由 ---- #

    def _resolve_request_selection(self, ctx: AgentCallbackContext) -> str:
        """每请求读取前端下拉选择值（adapter 经 ``run_context.extra["model_selection"]`` 注入）。

        仅用于日志/决策留痕，不参与路由决策。
        """
        run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
        extra = getattr(run_context, "extra", None)
        if isinstance(extra, dict):
            raw = extra.get("model_selection")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return ""

    def _resolve_request_routing_enabled(self, ctx: AgentCallbackContext) -> bool:
        """每请求读取 auto 档开关（``enable_routing``），先 run_context、再 team 侧兜底。

        单 agent 走 ``run_context.extra``；team 成员拿不到 run_context，回落到成员
        自身的 ``BuildContext.request_metadata``（见 :func:`_team_request_metadata`）。

        严格判 ``is True``：任何非布尔真值（含字符串 "true"）都不算开启。
        """
        run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
        extra = getattr(run_context, "extra", None)
        if isinstance(extra, dict) and extra.get("enable_routing") is True:
            return True
        return _team_request_metadata(ctx).get("enable_routing") is True

    def _bind_request_id(self, ctx: AgentCallbackContext) -> None:
        """把本请求的 request_id 重绑到日志 ContextVar，修正 ``[req=]`` 标签串号。

        agent 执行跑在与 handler **不同的 task**（warm-pool / 长生命周期复用），日志
        record factory 读到的 ContextVar 可能是**上一个请求**捕获的陈旧值。adapter 已把
        ``request_id`` 经 ``run_context.extra`` 逐请求注入（与 enable_routing 同一通道，
        已被证明逐请求正确），这里在 invoke 起点重新绑定，使 rail 及同 task 后续日志
        带上正确的 ``[req=]`` 标签。

        绑定失败（openjiuwen 未暴露该 API）只降级为 debug 日志，不影响路由决策。
        """
        run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
        extra = getattr(run_context, "extra", None)
        rid = extra.get("request_id") if isinstance(extra, dict) else None
        if not rid:
            return
        try:
            from openjiuwen.extensions.observability.span_context import (
                set_current_request_id,
            )

            set_current_request_id(str(rid))
        except Exception as exc:  # noqa: BLE001 - 重绑失败不影响路由
            logger.debug("[ModelRouting] rebind request_id failed: %s", exc)

    def _resolve_classifier(self) -> Optional[Any]:
        """取本次打分用的分类器：显式注入优先，未注入（``_UNSET``）则懒加载内置实现。"""
        if self._classifier is _UNSET:
            return _load_default_classifier()
        return self._classifier

    def _find_cap_by_name(self, name: str) -> Optional[ModelCapability]:
        """按 model_name（忽略大小写/空白）在能力表中查找，返回首个同名条目。"""
        key = (name or "").strip().lower()
        for cap in self._capability_table:
            if (cap.model_name or "").strip().lower() == key:
                return cap
        return None

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """invoke 开始时：重置 trace_id / call_history，执行路由决策。"""
        self._bind_request_id(ctx)
        self._trace_id = _new_trace_id()
        self._call_history = []

        try:
            # 从 ctx.inputs.query 提取用户查询文本（before_invoke 时无 messages 列表）
            query = getattr(getattr(ctx, "inputs", None), "query", None) or ""
            prompt_text = _unwrap_user_message(str(query)) if query else ""
            input_tokens = self._count_text_tokens(prompt_text)
            agent_info = _extract_agent_info(ctx)

            # --- auto 档：分类器打分，超阈值才 set_llm 换模型 ---
            if self._resolve_request_routing_enabled(ctx):
                await self._route_by_classifier(
                    ctx,
                    prompt_text=prompt_text,
                    input_tokens=input_tokens,
                    agent_info=agent_info,
                )
                return

            # --- 具体模型 / 空：不路由，保留 adapter 已应用的模型 ---
            selection = self._resolve_request_selection(ctx)
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
        """按模型族注入思考参数（经 vendor_map）。

        路由只选模型，不管思考深度：档位关键字已全部收回 relay 侧，这里只剩
        「具体模型」一种输入 → 仅 GLM-5.3 族注入产品默认 depth（low），其余 no-op。
        写 ``ctx.extra["llm_call_kwargs"]``，由 ReActAgent 每次 model call 消费。
        不支持思考控制的模型：adapt 降级为空 kwargs，本方法不写入。
        """
        from jiuwenswarm.common.thinking.adapter import adapt_thinking
        from jiuwenswarm.common.thinking.types import thaw_llm_call_kwargs

        model_name = _agent_model_name(ctx)
        extra_probe = getattr(ctx, "extra", None)
        used_cap = extra_probe.get("_model_routing_used_cap") if isinstance(extra_probe, dict) else None
        if used_cap is not None:
            cap_name = getattr(used_cap, "model_name", None)
            if isinstance(cap_name, str) and cap_name.strip():
                model_name = cap_name.strip()
        try:
            profile = adapt_thinking("default", model_name=model_name)
            if not profile.injected or not profile.llm_call_kwargs:
                return
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict):
                return
            kwargs = thaw_llm_call_kwargs(profile.llm_call_kwargs)
            extra["llm_call_kwargs"] = kwargs
            logger.info(
                "[ModelRouting] thinking inject thinking=%s model=%r kwargs=%r",
                profile.thinking,
                model_name,
                kwargs,
            )
        except Exception as exc:
            logger.debug("[ModelRouting] thinking inject failed: %s", exc)

    async def _route_by_classifier(
        self,
        ctx: AgentCallbackContext,
        *,
        prompt_text: str,
        input_tokens: int,
        agent_info: dict[str, Any],
    ) -> None:
        """auto 档：跑分类器给任务打分，score >= 40 时把模型换成 glm-5.3。

        分类器缺失/异常、续写指令（``CONTINUATION_CATEGORY``）、低分 → 保留当前模型。
        切换时用**当前模型**的 client config 克隆出 glm-5.3（api_base / api_key /
        鉴权头逐字不变，只换 model_name），经 ``_emit_decision`` 走 set_llm。
        """
        classifier = self._resolve_classifier()
        if classifier is None:
            logger.warning(
                "[ModelRouting] enable_routing=true but classifier unavailable; keep current model"
            )
            return
        try:
            raw_score, category, difficulty = await classifier(prompt_text)
        except Exception as exc:
            logger.warning("[ModelRouting] classifier failed: %s", exc, exc_info=True)
            return

        if category == CONTINUATION_CATEGORY:
            # 多轮对话里的「继续/同意」不能触发换模型
            self._emit_decision(
                ctx,
                recommended_cap=None,
                category="skipped",
                difficulty="skipped",
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning="continuation utterance, keep current model",
            )
            logger.info("[ModelRouting] enable_routing: continuation, keep current model")
            return

        score = validate_score(raw_score)
        if score < _CLASSIFIER_SCORE_THRESHOLD:
            self._emit_decision(
                ctx,
                recommended_cap=None,
                category=category,
                difficulty=difficulty,
                target_score=score,
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning=(
                    f"classifier score={score} < {_CLASSIFIER_SCORE_THRESHOLD}, keep current model"
                ),
            )
            logger.info(
                "[ModelRouting] enable_routing: score=%d < %d (category=%s difficulty=%s), keep current model",
                score, _CLASSIFIER_SCORE_THRESHOLD, category, difficulty,
            )
            return

        cap = self._build_switch_cap(ctx, _CLASSIFIER_TARGET_MODEL)
        if cap is None or cap.model is None:
            self._emit_decision(
                ctx,
                recommended_cap=None,
                category=category,
                difficulty=difficulty,
                target_score=score,
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning=(
                    f"classifier score={score} but {_CLASSIFIER_TARGET_MODEL} model unavailable; "
                    f"keep current"
                ),
            )
            logger.warning(
                "[ModelRouting] enable_routing: score=%d but cannot build %s; keep current model",
                score, _CLASSIFIER_TARGET_MODEL,
            )
            return

        self._emit_decision(
            ctx,
            recommended_cap=cap,
            category=category,
            difficulty=difficulty,
            target_score=score,
            input_tokens=input_tokens,
            agent_info=agent_info,
            reasoning=(
                f"classifier score={score} >= {_CLASSIFIER_SCORE_THRESHOLD} "
                f"-> switch to {_CLASSIFIER_TARGET_MODEL}"
            ),
        )
        logger.info(
            "[ModelRouting] enable_routing: score=%d >= %d (category=%s difficulty=%s) -> set_llm %s",
            score, _CLASSIFIER_SCORE_THRESHOLD, category, difficulty, _CLASSIFIER_TARGET_MODEL,
        )

    def _build_switch_cap(
        self, ctx: AgentCallbackContext, target_model: str
    ) -> Optional[ModelCapability]:
        """用**当前模型**的 client config 克隆出 ``target_model`` 的 cap（凭证不变）。

        只换 ``model_name``，其余字段（api_base / api_key / client_provider /
        Authorization 头 / 超时 / verify_ssl 等）逐字保留 —— 换模型不换 api/key。
        取不到当前模型配置 → 返回 None，调用方保留当前模型。
        """
        try:
            react_agent = _resolve_react_agent(ctx.agent)
            cfg = getattr(react_agent, "_config", None) or getattr(react_agent, "config", None)
            mcc = getattr(cfg, "model_client_config", None) if cfg is not None else None
            mrc = getattr(cfg, "model_config_obj", None) if cfg is not None else None
            if mcc is None:
                # 回退：按当前 model_name 在能力表里找同款（同 api_base/api_key）的 Model
                current_name = _agent_model_name(ctx)
                cur_cap = self._find_cap_by_name(current_name) if current_name else None
                if cur_cap is None or cur_cap.model is None:
                    return None
                mcc = cur_cap.model.model_client_config
                mrc = cur_cap.model.model_config
            new_mcc = _clone_cfg_with_model_name(mcc, target_model)
            if new_mcc is None:
                return None
            new_mrc = _clone_cfg_with_model_name(mrc, target_model) or {}
            builder = self._model_builder or _default_model_builder
            model = builder(new_mcc, new_mrc)
            if model is None:
                return None
            return ModelCapability(model_name=target_model, model_id=target_model, model=model)
        except Exception as exc:
            logger.warning(
                "[ModelRouting] build %s model failed: %s", target_model, exc, exc_info=True
            )
            return None

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        try:
            response = getattr(getattr(ctx, "inputs", None), "response", None)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            model_name = _agent_model_name(ctx) or "unknown"
            end_time = datetime.now(tz=timezone.utc).isoformat()
            # 累积到本次 invoke 的前置调用链（完整 OTel span）
            self._call_history.append(
                PriorModelCall(
                    model=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    iteration=len(self._call_history),
                    trace_id=self._trace_id,
                    start_time=end_time,  # 无独立起始钩子，用 end_time 近似
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
                    # 供 RuntimePromptRail 注入「当前模型」用。外层 rail 写、内层 rail 读，
                    # 只能落进按引用透传的 run_context（ctx.extra 不跨层）。
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
                # 主对话换模后，同步已缓存的上下文压缩器 / 会话记忆的 Model。它们
                # 在 agent 创建时把 model_config 烤了进去，不跟着换就会继续拿旧 id
                # 调上游 → 模型无效、压缩 noop、上下文膨胀。
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


def _clone_cfg_with_model_name(cfg_obj: Any, model_name: str) -> Optional[dict]:
    """把 openjiuwen 配置对象（pydantic）或 dict 摊成 dict，只替换 ``model_name``。

    其余字段原样保留，实现「换模型不换 api/key」。
    既没有 ``model_dump`` 也不是 dict → 返回 None（调用方放弃切换）。
    """
    if cfg_obj is None:
        return None
    dump = getattr(cfg_obj, "model_dump", None)
    if callable(dump):
        try:
            fields = dump()
        except Exception:
            return None
        if not isinstance(fields, dict):
            return None
        return {**fields, "model_name": model_name}
    if isinstance(cfg_obj, dict):
        return {**cfg_obj, "model_name": model_name}
    return None


def _team_request_metadata(ctx: AgentCallbackContext) -> dict[str, Any]:
    """从成员自身的 ``BuildContext`` 取 ``request_metadata``（team 成员拿不到 run_context）。

    ``ctx.agent`` 就是成员的 ``NativeHarness``，其 ``build_context`` 是 jiuwenswarm 的
    ``SwarmBuildContext``，``request_metadata`` 由 assembly 塞进 ``build_context_seed``
    还原到成员侧。取不到（单 agent / 非 swarm）→ 空 dict。
    """
    build_context = getattr(getattr(ctx, "agent", None), "build_context", None)
    metadata = getattr(build_context, "request_metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _default_model_builder(mcc_fields: dict, mco_fields: dict) -> Any:
    """无注入 builder 时的兜底构造（openjiuwen 直构）。

    入参是当前已建好模型的 client config 克隆，鉴权头已在其中，故直构也带得上凭证。
    """
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig
    from openjiuwen.core.foundation.llm.model_clients.base_model_client import ModelRequestConfig

    return Model(
        model_client_config=ModelClientConfig(**mcc_fields),
        model_config=ModelRequestConfig(**mco_fields),
    )


def _resolve_react_agent(agent: Any) -> Any:
    """Return the inner ReActAgent if *agent* is a DeepAgent, else *agent* itself.

    DeepAgent does not expose ``set_llm``; its inner ``_react_agent`` does.
    """
    if callable(getattr(agent, "set_llm", None)):
        return agent
    inner = getattr(agent, "_react_agent", None)
    if inner is not None and callable(getattr(inner, "set_llm", None)):
        return inner
    return agent
