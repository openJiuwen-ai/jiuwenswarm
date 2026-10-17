"""model_routing.rail — ModelRoutingRail."""
from __future__ import annotations
import os
from collections import OrderedDict
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
# 分类器给任务打 0-100 分，且分数**离散**（SCORE_TABLE：chat 5/20/40，coding 25/45/65，
# reasoning 20/40/60，summarization 15/25/45，format 5/10/30，另有兜底 25/40/60）。
#
# 单 session 内「尽可能不切换」靠两条腿：
#   1) 弱档（基准模型 flash）**不换模型**，改用思考深度承接难度（见 _DEPTH_BANDS）：
#      score <= 10 → off，<= 20 → low，<= 30 → medium，< 45 → high。于是 40
#      （chat/hard、reasoning/medium）不再换模型，只是「想得更久」。
#   2) 只有 score >= _SWITCH_SCORE_THRESHOLD 才真换 glm-5.3。
#
# 升档不设去抖：45 是常态分（coding/medium），去抖会把「持续中等 coding」锁在弱档。
# 降档用**欠债计分制**而不是连续轮数 —— 越闲欠得越快，接近门槛的任务欠得慢：
#   每轮 score < 45：debt += 45 - score   （5 → +40，25 → +20，40 → +5）
#   每轮 score >= 45：debt = 0            （达标清零）
#   debt >= _SESSION_DEBT_LIMIT → 降回弱档
# 连续轮数制分不清「一句你好」和「一直 40 分」（两者都算 1 轮）；计分制里前者 3 轮
# 欠满，后者要 20 轮 —— 这才是「除非分数差别太大才切」要的形状。
_SWITCH_SCORE_THRESHOLD: int = 45
_SESSION_DEBT_LIMIT: int = 100
_CLASSIFIER_TARGET_MODEL: str = "glm-5.3"

# 弱档（flash）思考深度带：(分数上限, 产品语义深度)。分数离散，实际落在
# 5/10 → off、15/20 → low、25/30 → medium、40 → high。
_DEPTH_BANDS: tuple[tuple[int, str], ...] = ((10, "off"), (20, "low"), (30, "medium"))
_WEAK_DEPTH_FALLBACK: str = "high"

# 强档（glm-5.3）固定用产品语义的 ``default``：不是「不注入」，而是把深度交给
# thinking 适配层的既定策略 —— glm-5.3 属于 ``_AUTO_DEFAULT_DEPTH_STYLES``，``default``
# 会落成 ``reasoning_effort: low``（见 vendor_map._GLM53_EFFORT / DEFAULT_THINKING_DEPTH）。
#
# 为什么不按分数给强档调深度：glm-5.3 模型本身支持 ``low|high|max``，但 jiuwenswarm
# 刻意限制了它 —— 该模型**始终思考**（``thinking.type=disabled`` 不被接受，``off``
# 只能退化成 ``low``），且 ``high``/``max`` 耗时明显偏长。所以强档没有「轻量档」可用，
# 升到 glm-5.3 就意味着实打实的长思考；要省，只能靠**不升档**，这也正是 45 这个
# 阈值的取法依据。将来若放开限制，这里换成 ``_depth_for_score(score)`` 即可。
_THINKING_DEFAULT: str = "default"

# session 档位表容量上限（rail 实例级，按 conversation_id 记档）：长跑进程里会话
# 数量无上界，超限按 LRU 淘汰最久未用的，避免无界增长。
_SESSION_TIER_CAPACITY: int = 128

# session 档位取值：weak = 基准模型（deepseek-v4.1-flash），strong = glm-5.3。
_TIER_WEAK: str = "weak"
_TIER_STRONG: str = "strong"

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
    """模型路由 Rail —— auto 档跑分类器，按 session 档位决定换模型还是换思考深度。

    auto 档（带 ``enable_routing`` 开关）跑分类器打分，档位规则是**升快降慢 + 欠债计分**：

    - 首轮（本 session 无记录）：``score >= 45`` 进强档（glm-5.3），否则弱档；
    - 弱档 → 强档：**单轮** ``score >= 45`` 立即升（不设去抖）；
    - 弱档不换模型，改用思考深度承接难度：``<=10 off / <=20 low / <=30 medium /
      <45 high``，写 ``ctx.extra["llm_call_kwargs"]`` 由 ``before_model_call`` 注入；
    - 强档 → 弱档：**欠债** ``debt += 45 - score``，``score >= 45`` 清零，
      ``debt >= 100`` 才降回弱档。

    升档不设去抖是必须的：分类器分数离散且常态就在 40–45（coding/medium=45），
    若升档也要高分，「先一句你好再持续做中等 coding」的 session 会永久卡在弱档。
    降档用计分制而不是连续轮数：一句「你好」(5) 欠 40、3 轮欠满，而一直 40 分只欠 5、
    要 20 轮才降 —— 连续轮数制把这两种情况当成一样的。强档是 glm-5.3，jiuwenswarm 对
    它的思考深度做了限制（始终思考、``off`` 不可用、``high``/``max`` 耗时偏长），故强档
    固定产品语义的 ``default``（实际落成 ``reasoning_effort: low``），不按分数映射。

    **每轮重放**：adapter 在 ``process_message_*`` 里每请求都会把模型重置回档位基准
    （``_apply_model_to_react_agent``），所以「保持强档」不能靠"什么都不做"，必须每轮
    重新 ``set_llm`` 一次；续写指令（``CONTINUATION_CATEGORY``）同样重放 session 档位，
    否则强档 session 里说一句「继续」就会静默掉回基准模型。续写没有分数可映射，故其
    思考深度回落 ``default``。

    档位按 ``ctx.inputs.conversation_id``（= ``request.session_id``）记在 rail 实例上；
    拿不到该 id（team 成员 / 单测）时退回无状态语义（每轮按首轮阈值独立决策）。用户
    切离 auto（具体模型 / 其它档位）时清掉该 session 档位，再切回 auto 重新决策。

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
        # session 档位表：conversation_id -> (档位, 欠债分)，LRU 淘汰。
        self._session_tiers: "OrderedDict[str, tuple[str, int]]" = OrderedDict()

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

    # ---- session 档位（升快降慢 + 降档去抖） ---- #

    def _session_key(self, ctx: AgentCallbackContext) -> str:
        """取本 session 的档位键：``ctx.inputs.conversation_id``（= request.session_id）。

        拿不到（team 成员 / 单测假 ctx）→ 空串，调用方退回无状态语义，不写档位表。
        """
        raw = getattr(getattr(ctx, "inputs", None), "conversation_id", None)
        return str(raw).strip() if raw else ""

    def _get_session_tier(self, key: str) -> tuple[Optional[str], int]:
        """读 session 档位状态：``(档位, 欠债分)``；无记录返回 ``(None, 0)``。"""
        if not key:
            return None, 0
        entry = self._session_tiers.get(key)
        if entry is None:
            return None, 0
        self._session_tiers.move_to_end(key)
        return entry

    def _set_session_tier(self, key: str, tier: str, debt: int = 0) -> None:
        """写 session 档位状态，并按 LRU 淘汰超容量的旧条目。"""
        if not key:
            return
        self._session_tiers[key] = (tier, debt)
        self._session_tiers.move_to_end(key)
        while len(self._session_tiers) > _SESSION_TIER_CAPACITY:
            self._session_tiers.popitem(last=False)

    def _clear_session_tier(self, ctx: AgentCallbackContext) -> None:
        """清掉本 session 档位（用户切离 auto 时调用，回 auto 后重新决策）。"""
        key = self._session_key(ctx)
        if key:
            self._session_tiers.pop(key, None)

    @staticmethod
    def _depth_for_score(score: int) -> str:
        """弱档思考深度带：``<=10 off / <=20 low / <=30 medium / <45 high``。"""
        for upper, level in _DEPTH_BANDS:
            if score <= upper:
                return level
        return _WEAK_DEPTH_FALLBACK

    @staticmethod
    def _set_thinking_level(ctx: AgentCallbackContext, level: str) -> None:
        """把本轮要注入的思考深度写进 ``ctx.extra``，交给 ``before_model_call`` 消费。

        每轮都显式写一次（含 ``default``），避免 ctx 复用时读到上一轮的档位。
        """
        if isinstance(ctx.extra, dict):
            ctx.extra["_model_routing_thinking_level"] = level

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

            # --- auto 档：分类器打分，按 session 档位决定是否 set_llm 换模型 ---
            if self._resolve_request_routing_enabled(ctx):
                await self._route_by_classifier(
                    ctx,
                    prompt_text=prompt_text,
                    input_tokens=input_tokens,
                    agent_info=agent_info,
                )
                return

            # --- 具体模型 / 空：不路由，保留 adapter 已应用的模型 ---
            # 用户切离 auto（选具体模型或别的档位）→ 清掉 session 档位，避免下次切回
            # auto 时莫名继承上一轮的强/弱档。
            selection = self._resolve_request_selection(ctx)
            self._clear_session_tier(ctx)
            self._set_thinking_level(ctx, _THINKING_DEFAULT)
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
        """按本轮路由决策注入思考参数（经 vendor_map）。

        路由决定「换模型还是换深度」：弱档不换模型，按分类器分给 flash 注入思考深度
        （见 ``_DEPTH_BANDS``）；强档是 glm-5.3，jiuwenswarm 限制了它的深度（见
        ``_THINKING_DEFAULT`` 注释），固定产品语义的 ``default``。
        深度由 ``before_invoke`` 经 ``ctx.extra["_model_routing_thinking_level"]`` 传来。
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
        thinking = _THINKING_DEFAULT
        if isinstance(extra_probe, dict):
            level = extra_probe.get("_model_routing_thinking_level")
            if isinstance(level, str) and level.strip():
                thinking = level.strip()
        try:
            profile = adapt_thinking(thinking, model_name=model_name)
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
        """auto 档：跑分类器给任务打分，按 session 档位决定换模型还是换思考深度。

        档位规则见 ``_decide_tier``（升快降慢 + 欠债计分）。续写指令不参与打分，
        直接重放 session 当前档位。分类器缺失/异常 → 保持现状（不动档位，也不换模型）。

        换模型时用**当前模型**的 client config 克隆出 glm-5.3（api_base / api_key /
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

        key = self._session_key(ctx)
        tier, debt = self._get_session_tier(key)

        if category == CONTINUATION_CATEGORY:
            # 「继续/同意」不参与打分，但必须重放 session 档位：adapter 每轮都把模型
            # 重置回档位基准，不重放就会让强档 session 静默掉回基准模型。
            self._replay_session_tier(
                ctx, tier, input_tokens=input_tokens, agent_info=agent_info
            )
            return

        score = validate_score(raw_score)
        next_tier, next_debt, reason = self._decide_tier(score, tier, debt)

        if next_tier == _TIER_WEAK:
            # 弱档 = 基准模型，adapter 每轮已重置到它，无需 set_llm；改用思考深度承接难度。
            depth = self._depth_for_score(score)
            self._set_thinking_level(ctx, depth)
            self._emit_decision(
                ctx,
                recommended_cap=None,
                category=category,
                difficulty=difficulty,
                target_score=score,
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning=f"{reason}; flash thinking depth={depth}",
            )
            self._set_session_tier(key, next_tier, next_debt)
            logger.info(
                "[ModelRouting] enable_routing: score=%d (category=%s difficulty=%s) %s -> depth=%s",
                score, category, difficulty, reason, depth,
            )
            return

        cap = self._build_switch_cap(ctx, _CLASSIFIER_TARGET_MODEL)
        if cap is None or cap.model is None:
            # 换不过去就还留在 flash 上：按弱档深度兜底，别让这轮裸奔。
            depth = self._depth_for_score(score)
            self._set_thinking_level(ctx, depth)
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
                    f"keep current with flash thinking depth={depth}"
                ),
            )
            logger.warning(
                "[ModelRouting] enable_routing: score=%d but cannot build %s; keep current model",
                score, _CLASSIFIER_TARGET_MODEL,
            )
            return

        # 真的切到 glm-5.3：深度固定产品语义的 default（实际落成 reasoning_effort=low，
        # 见 _THINKING_DEFAULT 注释 —— 该模型 jiuwenswarm 侧限制了深度档）。
        self._set_thinking_level(ctx, _THINKING_DEFAULT)
        self._emit_decision(
            ctx,
            recommended_cap=cap,
            category=category,
            difficulty=difficulty,
            target_score=score,
            input_tokens=input_tokens,
            agent_info=agent_info,
            reasoning=f"{reason} -> set_llm {_CLASSIFIER_TARGET_MODEL}",
        )
        self._set_session_tier(key, next_tier, next_debt)
        logger.info(
            "[ModelRouting] enable_routing: score=%d (category=%s difficulty=%s) %s -> set_llm %s",
            score, category, difficulty, reason, _CLASSIFIER_TARGET_MODEL,
        )

    @staticmethod
    def _decide_tier(score: int, tier: Optional[str], debt: int) -> tuple[str, int, str]:
        """按「升快降慢 + 欠债计分」算出本轮档位，返回 ``(档位, 新欠债, 原因)``。

        - 首轮（``tier is None``）：``score >= 45`` 进强档，否则弱档。
        - ``score >= 45``：立即（或保持）强档，欠债清零。
        - ``score < 45``：弱档保持弱档（欠债恒 0）；强档累计 ``45 - score`` 欠债，
          满 ``_SESSION_DEBT_LIMIT`` 才降回弱档，未满则继续持强档。
        """
        if tier is None:
            if score >= _SWITCH_SCORE_THRESHOLD:
                return _TIER_STRONG, 0, (
                    f"classifier score={score} >= {_SWITCH_SCORE_THRESHOLD} (first turn)"
                )
            return _TIER_WEAK, 0, (
                f"classifier score={score} < {_SWITCH_SCORE_THRESHOLD} (first turn)"
            )
        if score >= _SWITCH_SCORE_THRESHOLD:
            if tier == _TIER_STRONG:
                return _TIER_STRONG, 0, (
                    f"classifier score={score} >= {_SWITCH_SCORE_THRESHOLD}, "
                    f"hold session tier strong, debt cleared"
                )
            return _TIER_STRONG, 0, (
                f"classifier score={score} >= {_SWITCH_SCORE_THRESHOLD}, "
                f"raise session tier weak -> strong"
            )
        if tier == _TIER_WEAK:
            return _TIER_WEAK, 0, (
                f"classifier score={score} < {_SWITCH_SCORE_THRESHOLD}, hold session tier weak"
            )
        debt_next = debt + (_SWITCH_SCORE_THRESHOLD - score)
        if debt_next >= _SESSION_DEBT_LIMIT:
            return _TIER_WEAK, 0, (
                f"classifier score={score} < {_SWITCH_SCORE_THRESHOLD}, session debt "
                f"{debt_next} >= {_SESSION_DEBT_LIMIT}, drop session tier strong -> weak"
            )
        return _TIER_STRONG, debt_next, (
            f"classifier score={score} < {_SWITCH_SCORE_THRESHOLD}, session debt "
            f"{debt_next}/{_SESSION_DEBT_LIMIT}, hold session tier strong"
        )

    def _replay_session_tier(
        self,
        ctx: AgentCallbackContext,
        tier: Optional[str],
        *,
        input_tokens: int,
        agent_info: dict[str, Any],
    ) -> None:
        """续写指令：不重新打分，直接重放 session 当前档位。

        弱档 / 无记录 → 什么都不做（adapter 每轮已把模型重置回基准，正是弱档要的模型）；
        强档 → 重新 set_llm 换回 glm-5.3，否则「继续」会让强档 session 掉回基准模型。

        续写没有分数可映射思考深度，故深度回落 ``default``（vendor 原样）。
        """
        self._set_thinking_level(ctx, _THINKING_DEFAULT)
        if tier != _TIER_STRONG:
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

        cap = self._build_switch_cap(ctx, _CLASSIFIER_TARGET_MODEL)
        if cap is None or cap.model is None:
            self._emit_decision(
                ctx,
                recommended_cap=None,
                category="skipped",
                difficulty="skipped",
                input_tokens=input_tokens,
                agent_info=agent_info,
                reasoning=(
                    f"continuation utterance but cannot rebuild {_CLASSIFIER_TARGET_MODEL}; "
                    f"keep current"
                ),
            )
            logger.warning(
                "[ModelRouting] enable_routing: continuation but cannot build %s; keep current model",
                _CLASSIFIER_TARGET_MODEL,
            )
            return

        self._emit_decision(
            ctx,
            recommended_cap=cap,
            category="skipped",
            difficulty="skipped",
            input_tokens=input_tokens,
            agent_info=agent_info,
            reasoning=(
                f"continuation utterance, replay session tier=strong -> "
                f"set_llm {_CLASSIFIER_TARGET_MODEL}"
            ),
        )
        logger.info(
            "[ModelRouting] enable_routing: continuation, replay session tier=strong -> set_llm %s",
            _CLASSIFIER_TARGET_MODEL,
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
