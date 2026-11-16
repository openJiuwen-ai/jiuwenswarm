# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-governance rail for research-agent workflows.

Implements the B5 process-governance layer described in ResearchFlowBench:
each research agent is instructed to emit typed scholarly objects with
explicit support states before advancing to the next workflow stage.
Unsupported claims, missing baselines, and stale evidence markers are
surfaced as gate decisions rather than silently passed through.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.team.rails.response_text import response_text

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

logger = logging.getLogger(__name__)

_UNSUPPORTED_SIGNAL_RE = re.compile(
    r"\b(novel|first|only|unique|state.of.the.art|outperform|significantly better)\b",
    re.IGNORECASE,
)
_CITATION_RE = re.compile(r"\[[\w\s,]+\]|\\cite", re.IGNORECASE)
_STALE_SIGNAL_RE = re.compile(
    r"\b(previous(ly)?|earlier|above|as noted|as mentioned)\b", re.IGNORECASE
)

_GOVERNANCE_PROMPT_CN = """\
# 过程治理规则（Process-Governance Rules）

在每次生成研究内容时，你必须遵守以下学术对象治理规则：

1. **引用支撑（Citation Support）**：每条断言性claim必须附带文献引用。
   存在引用 ≠ 引用支撑claim。请标注支撑程度：完全支撑 / 部分支撑 / 不支撑。
2. **新颖性基线（Novelty Baseline）**：声称"首创"或"优于现有方法"时，必须列出
   最相关的对比基线，并说明差异。缺少基线 = 虚假新颖性（false novelty）。
3. **证据新鲜度（Evidence Freshness）**：引用上游信息时，若该信息已被修订或
   撤回，必须标注证据失效（stale），并触发下游对象的连锁检查。
4. **门控决策（Gate Decision）**：每个研究阶段结束时，输出一个明确的门控结论：
   ADVANCE（可推进）/ BLOCK（需阻断）/ REVIEW（需人工审阅）。

违反上述规则的输出将被治理审阅者标记为流程异常。
"""

_GOVERNANCE_PROMPT_EN = """\
# Process-Governance Rules

When producing research content, you must follow these scholarly-object governance rules:

1. **Citation Support**: Every assertive claim must be backed by a reference.
   A citation existing is not the same as that citation supporting the claim.
   Label support level: FULL / PARTIAL / UNSUPPORTED.
2. **Novelty Baseline**: When claiming "first" or "outperforms prior work",
   list the most directly competing baselines and explain the delta.
   Missing baseline = false novelty.
3. **Evidence Freshness**: When referencing upstream information, if that
   information has been revised or retracted, mark it STALE and trigger
   a downstream propagation check.
4. **Gate Decision**: At the end of each research phase, emit an explicit gate:
   ADVANCE / BLOCK / REVIEW.

Outputs that violate these rules will be flagged as process anomalies by the
governance reviewer.
"""


class GovernanceReviewRail(DeepAgentRail):
    """Inject process-governance constraints into research-agent prompts.

    This rail implements the B5 process-governance condition from
    ResearchFlowBench. It:
    - Injects scholarly-object governance rules before each model call.
    - After each call, scans for potential governance violations (novelty
      claims without citations, stale-evidence signals) and logs them.

    The rail is designed to surface process-reliability failures that
    outcome-only evaluation would miss, as defined in ResearchFlowBench's
    five task families (T01--T05): citation support, open-world attribution,
    partial-support classification, stale propagation, and false novelty.
    """

    priority = 90
    SECTION_NAME = "process_governance"
    SECTION_PRIORITY = 45

    def __init__(
        self,
        *,
        language: str = "cn",
        emit_gate_decisions: bool = True,
        log_violations: bool = True,
    ) -> None:
        super().__init__()
        self._language = language
        self._emit_gate_decisions = emit_gate_decisions
        self._log_violations = log_violations
        self.system_prompt_builder = None
        self._agent_id: str = ""
        self._violation_count: int = 0

    def init(self, agent: "DeepAgent") -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._agent_id = str(getattr(agent.card, "id", None) or getattr(agent.card, "name", "unknown"))
        logger.info("[GovernanceReviewRail] Initialized for agent_id=%s", self._agent_id)

    def uninit(self, agent: "DeepAgent") -> None:
        _ = agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None
        if self._violation_count > 0:
            logger.info(
                "[GovernanceReviewRail] agent_id=%s accumulated %d governance signals",
                self._agent_id,
                self._violation_count,
            )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx
        if self.system_prompt_builder is None:
            return
        content_text = (
            _GOVERNANCE_PROMPT_CN if self._language == "cn" else _GOVERNANCE_PROMPT_EN
        )
        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={self._language: content_text},
                priority=self.SECTION_PRIORITY,
            )
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self._log_violations:
            return

        # The model response is on ctx.inputs.response (the ModelCallInputs for
        # this event), not on ctx itself; reading ctx.response scanned an empty
        # string on every call, which is indistinguishable from a clean draft.
        text = response_text(ctx)
        if not text:
            return

        violations: list[str] = []

        # Check for novelty/superiority claims without citations
        novelty_hits = _UNSUPPORTED_SIGNAL_RE.findall(text)
        citation_hits = _CITATION_RE.findall(text)
        if novelty_hits and not citation_hits:
            violations.append(
                f"novelty/superiority claim(s) {novelty_hits[:3]} detected without citation support"
            )

        # Check for stale-evidence signals
        stale_hits = _STALE_SIGNAL_RE.findall(text)
        if stale_hits and "stale" not in text.lower() and "freshen" not in text.lower():
            violations.append(
                f"possible stale-reference signal(s) ({stale_hits[:2]}) without explicit freshness check"
            )

        if violations:
            self._violation_count += len(violations)
            for v in violations:
                logger.warning(
                    "[GovernanceReviewRail] GOVERNANCE SIGNAL agent_id=%s: %s",
                    self._agent_id,
                    v,
                )


__all__ = ["GovernanceReviewRail"]
