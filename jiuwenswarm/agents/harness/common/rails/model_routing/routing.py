"""model_routing.routing — _decide_and_select + _detect_model_type."""
from __future__ import annotations
from typing import Optional
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from .capability import ModelCapability


# --------------------------------------------------------------------------- #
# 通用 model_type 检测
# --------------------------------------------------------------------------- #

def _detect_model_type(ctx: AgentCallbackContext) -> str:
    """从上下文检测当前请求需要的 model_type。

    仅从 ctx.extra["_required_model_type"] 读取显式注入值。
    无匹配 → 返回空字符串 ""（表示不约束 model_type，走通用模型）。

    注入方式：在 before_invoke 阶段由 adapter / 前端 / 其他 rail 写入
    ctx.extra["_required_model_type"]，例如：
        - "vision"  → 路由到视觉模型
        - "coding"  → 路由到编程模型
        - "audio"   → 路由到音频模型
    """
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        required = extra.get("_required_model_type")
        if isinstance(required, str) and required.strip():
            return required.strip().lower()
    return ""


def _model_score(cap: ModelCapability) -> float:
    """模型综合评分（0-100）；非数/缺失/NaN -> 0。"""
    try:
        v = float(cap.model_score)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v else 0.0  # NaN -> 0


def _pick_closest_score(caps: list[ModelCapability], target: float) -> ModelCapability:
    """选 model_score 最接近 target 的模型；等距时偏高分（质量优先）。"""
    return min(caps, key=lambda c: (abs(_model_score(c) - target), -_model_score(c)))


def _decide_and_select(
    target_score: float,
    capability_table: list[ModelCapability],
    ctx: AgentCallbackContext,
    *,
    category: str = "",
    difficulty: str = "",
    required_model_type: str = "",
) -> tuple[Optional[ModelCapability], str]:
    """按目标分数匹配 model_score 最接近的模型。

    - target_score: 分类器返回的目标分数（0-100）；50 为兜底。
    - category / difficulty: 分类器给出的任务类型和难度。
    - expertise：difficulty=="hard" 时优先选 model_expertise_category 含 category 的模型；
      无匹配特长模型则保持原表兜底（与 vision 约束同样的 fallback 策略）。
    - required_model_type：通用 model_type 约束。
      非空（如 "vision"/"coding"）→ 候选限到 model_type == required_model_type 的模型；无匹配则保持原表兜底。
      空（默认）→ 排除 model_type 非空的专用模型；全为专用模型则保持原表兜底。
    - 在候选里取 |model_score - target| 最小者，等距偏高分。
    """
    if not capability_table:
        return None, "empty capability table"

    # Hard 难度特长约束：优先选 model_expertise_category 含任务 category 的模型
    # （如 coding/hard → 只选标了 coding 专长的模型；无特长匹配则全表兜底）
    expertise_caps: list[ModelCapability] = []
    if difficulty == "hard" and category:
        expertise_caps = [c for c in capability_table if category in c.model_expertise_category]
        if expertise_caps:
            capability_table = expertise_caps

    # 通用 model_type 约束
    if required_model_type:
        # 指定了 model_type → 候选限到 model_type == required_model_type 的模型
        type_caps = [c for c in capability_table if c.model_type == required_model_type]
        if type_caps:
            capability_table = type_caps
    else:
        # 未指定 model_type → 排除专用模型（model_type 非空）；全为专用模型则保持原表兜底
        non_specialized = [c for c in capability_table if not c.model_type]
        if non_specialized:
            capability_table = non_specialized

    pick = _pick_closest_score(capability_table, float(target_score))
    reason_parts = [f"score match: target={target_score} -> {pick.model_name}(score={_model_score(pick):g})"]
    if difficulty == "hard" and category and expertise_caps:
        reason_parts.append(f"expertise={category}")
    if required_model_type:
        reason_parts.append(f"model_type={required_model_type}")
    return pick, "; ".join(reason_parts)
