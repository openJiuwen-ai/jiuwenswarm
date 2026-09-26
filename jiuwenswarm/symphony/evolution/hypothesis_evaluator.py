"""多维度假设评估器 — 在 Agent 自演进过程中对研究假设进行多维度评分

原始 JiuwenSwarm 的演进模块（symphony/evolution/）只做简单的可行性判断，
缺乏对假设创新性和影响力的评估。本模块新增多维度评分机制：

1. 可行性 (feasibility): 在当前资源约束下能否实现和验证
2. 创新性 (novelty): 与现有工作的差异程度
3. 影响力 (impact): 对 Agent 社区的实际价值

评估结果用于筛选高价值假设，避免 FARS 指出的"假设筛选能力弱"问题。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

LOGGER = logging.getLogger(__name__)

# Optional LLM refinement callback: it receives the hypothesis mapping and
# returns a mapping with ``feasibility``/``novelty``/``impact``/``reasoning``.
LLMCallback = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]

# 评分维度
DIM_FEASIBILITY = "feasibility"
DIM_NOVELTY = "novelty"
DIM_IMPACT = "impact"

# 评分阈值
RECOMMEND_THRESHOLD = 21  # 7+7+7，总分≥21推荐执行
MIN_THRESHOLD = 15  # 总分<15则丢弃


@dataclass
class HypothesisScore:
    """假设评分结果"""

    feasibility: int = 5  # 1-10
    novelty: int = 5
    impact: int = 5
    reasoning: str = ""  # 评分理由

    @property
    def total(self) -> int:
        return self.feasibility + self.novelty + self.impact

    @property
    def recommendation(self) -> str:
        if self.total >= RECOMMEND_THRESHOLD:
            return "strong_recommend"
        elif self.total >= MIN_THRESHOLD:
            return "conditional"
        else:
            return "reject"

    def to_dict(self) -> dict:
        return {
            "feasibility": self.feasibility,
            "novelty": self.novelty,
            "impact": self.impact,
            "total": self.total,
            "recommendation": self.recommendation,
            "reasoning": self.reasoning,
        }


class HypothesisEvaluator:
    """多维度假设评估器

    使用规则 + 语义分析对假设进行三维度评分。
    可选接入 LLM 进行深度评估（通过 call_llm 回调）。
    """

    # 创新性关键词
    NOVEL_KEYWORDS: ClassVar[list[str]] = [
        "novel",
        "new",
        "first",
        "unique",
        "unprecedented",
        "新",
        "首次",
        "独特",
        "创新",
        "突破",
    ]

    # 可行性关键词
    FEASIBLE_KEYWORDS: ClassVar[list[str]] = [
        "experiment",
        "implement",
        "evaluate",
        "benchmark",
        "validate",
        "实验",
        "实现",
        "评估",
        "验证",
        "对比",
    ]

    # 影响力关键词
    IMPACT_KEYWORDS: ClassVar[list[str]] = [
        "significant",
        "important",
        "practical",
        "real-world",
        "deployment",
        "重要",
        "实际",
        "部署",
        "应用",
        "产业",
    ]

    def __init__(self, llm_callback=None):
        """
        Args:
            llm_callback: 可选的 LLM 评估回调，签名为 (hypothesis_text) -> dict
                          如果提供，将使用 LLM 进行深度评估
        """
        self._llm_callback = llm_callback
        self._history: list[dict] = []  # 评估历史

    def evaluate(self, hypothesis: dict) -> HypothesisScore:
        """评估单个假设

        Args:
            hypothesis: 包含 title, problem, approach, novelty, feasibility, impact 等字段

        Returns:
            HypothesisScore
        """
        # 先做规则评分
        score = self._rule_based_score(hypothesis)

        # 如果有 LLM 回调，用 LLM 深度评估并融合
        if self._llm_callback:
            llm_score = self._llm_based_score(hypothesis)
            if llm_score:
                # 融合：规则和 LLM 各占 50%
                score.feasibility = (score.feasibility + llm_score.feasibility) // 2
                score.novelty = (score.novelty + llm_score.novelty) // 2
                score.impact = (score.impact + llm_score.impact) // 2
                score.reasoning = f"Rule+LLM融合评估。{llm_score.reasoning}"

        # 记录评估历史
        entry = {"hypothesis": hypothesis.get("title", ""), **score.to_dict()}
        self._history.append(entry)

        return score

    def evaluate_batch(
        self, hypotheses: list[dict]
    ) -> list[tuple[dict, HypothesisScore]]:
        """批量评估并按总分排序"""
        scored = []
        for h in hypotheses:
            s = self.evaluate(h)
            scored.append((h, s))
        # 按总分降序
        scored.sort(key=lambda x: x[1].total, reverse=True)
        return scored

    def select_best(
        self, hypotheses: list[dict], min_score: int = MIN_THRESHOLD
    ) -> dict | None:
        """从假设列表中选择评分最高的"""
        scored = self.evaluate_batch(hypotheses)
        for h, s in scored:
            if s.total >= min_score:
                # Copy-on-score: never mutate the caller's dict in place.
                best = dict(h)
                best["_score"] = s.to_dict()
                return best
        return None

    def _rule_based_score(self, hypothesis: dict) -> HypothesisScore:
        """基于规则的评分"""
        title = hypothesis.get("title", "")
        problem = hypothesis.get("problem", "")
        approach = hypothesis.get("approach", "")
        novelty_text = hypothesis.get("novelty", "")
        feasibility_text = hypothesis.get("feasibility", "")
        impact_text = hypothesis.get("impact", "")

        all_text = f"{title} {problem} {approach} {novelty_text} {feasibility_text} {impact_text}".lower()

        # 可行性评分
        feasibility = 5  # 基线
        for kw in self.FEASIBLE_KEYWORDS:
            if kw in all_text:
                feasibility += 1
        feasibility = min(feasibility, 10)

        # 创新性评分
        novelty = 5
        for kw in self.NOVEL_KEYWORDS:
            if kw in all_text:
                novelty += 1
        # 文本长度也暗示了详细程度
        if len(novelty_text) > 100:
            novelty += 1
        novelty = min(novelty, 10)

        # 影响力评分
        impact = 5
        for kw in self.IMPACT_KEYWORDS:
            if kw in all_text:
                impact += 1
        if len(impact_text) > 100:
            impact += 1
        impact = min(impact, 10)

        return HypothesisScore(
            feasibility=feasibility,
            novelty=novelty,
            impact=impact,
            reasoning=f"规则评分：可行性={feasibility}(关键词匹配), 创新性={novelty}, 影响力={impact}",
        )

    def _llm_based_score(self, hypothesis: dict) -> HypothesisScore | None:
        """使用 LLM 进行深度评估"""
        if not self._llm_callback:
            return None
        try:
            result = self._llm_callback(hypothesis)
            if result and isinstance(result, dict):
                return HypothesisScore(
                    feasibility=result.get("feasibility", 5),
                    novelty=result.get("novelty", 5),
                    impact=result.get("impact", 5),
                    reasoning=result.get("reasoning", ""),
                )
        except Exception as e:  # noqa: BLE001 - a bad LLM callback must not break scoring
            LOGGER.warning("LLM evaluation failed: %s", e)
        return None

    @property
    def history(self) -> list[dict]:
        return self._history


def evaluate_hypothesis(
    hypothesis: Mapping[str, Any],
    *,
    llm_callback: LLMCallback | None = None,
) -> HypothesisScore:
    """Score one hypothesis with the rule engine (and optional LLM refinement).

    This is the stateless, pure-function entry point: it builds a throwaway
    :class:`HypothesisEvaluator` so callers do not need to manage instance
    state just to grade a single hypothesis.  It is dependency-free (standard
    library only) and therefore safe to call from the evolution layer or from
    a rail hook.

    Args:
        hypothesis: Mapping with optional ``title``/``problem``/``approach``/
            ``novelty``/``feasibility``/``impact`` text fields.
        llm_callback: Optional refinement callback; when supplied its scores
            are averaged with the rule scores.

    Returns:
        The aggregated :class:`HypothesisScore`.
    """

    return HypothesisEvaluator(llm_callback=llm_callback).evaluate(hypothesis)


__all__ = [
    "DIM_FEASIBILITY",
    "DIM_IMPACT",
    "DIM_NOVELTY",
    "MIN_THRESHOLD",
    "RECOMMEND_THRESHOLD",
    "HypothesisEvaluator",
    "HypothesisScore",
    "LLMCallback",
    "evaluate_hypothesis",
]
