# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DesignRail — SDD (Spec-Driven Development) state machine for Code mode.

Inherits ``RailStateMachineBase`` (shared state-machine + advance-tool +
skill-injection pattern). DesignRail contributes:
  * stages = the 6-stage SDD flow (init -> analysis -> analysis_review ->
    design -> design_review -> done), loaded from ``config.yaml``.
  * SKILLS_DIR = ``skills/`` (embedded aet-req-analysis/review/design).
  * ``ask_user`` review handling (approve -> forward, reject -> rework).

The advance tool ``sdd_advance`` is registered by the base class when the
rail is mounted; it lives only while DesignRail is mounted (code mode +
``modes.code.sdd.enabled=true``), so team/deep agents never see it.

Rework (analysis_review -> analysis, design_review -> design) is driven by the
StructuredAskUserTool "reject" answer in ``after_tool_call``; the
declarative ``stages.<name>.next`` lists forward edges only. A production
stage declares two forward targets: the review stage, and the stage reached
when the user declines the review (the review stage has no skip entry point
before its own user gate, so "skip the review" has to be a forward edge).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import re

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail import config_loader
from jiuwenswarm.agents.harness.code.rails.sdd.common.rail_state_machine import (
    RailStateMachineBase,
)
from jiuwenswarm.common.utils import logger

__all__ = ["DesignRail"]

_SECTION_NAME = "sdd_skill"
# Answer-text heuristics for the StructuredAskUserTool confirmation point.
# A user selection containing any of these triggers rework (RDS §3.3 reject
# branch); anything else is treated as "approve, advance forward".
_REJECT_KEYWORDS = ("返工", "拒绝", "reject", "rework")
# Pre-compiled word-boundary patterns for ASCII keywords (prevents "project"
# matching "reject"). Chinese keywords use substring matching (no \b in CJK).
_REJECT_PATTERNS = tuple(
    re.compile(fr"\b{re.escape(kw)}\b", re.IGNORECASE)
    for kw in _REJECT_KEYWORDS
    if kw.isascii()
)
_REJECT_CJK = tuple(kw for kw in _REJECT_KEYWORDS if not kw.isascii())
# Re-gate keywords: SKILL.md R3 "需要复审" option — return to R1 for another
# gate loop WITHOUT advancing. Distinct from reject (rework to production
# stage) and approve (advance forward). "跳过复审" is approve, NOT regate,
# so the keyword must be the full "需要复审" phrase, not bare "复审".
_REGATE_KEYWORDS = ("需要复审", "re-gate", "regate")
_REGATE_PATTERNS = tuple(
    re.compile(fr"\b{re.escape(kw)}\b", re.IGNORECASE)
    for kw in _REGATE_KEYWORDS
    if kw.isascii()
)
_REGATE_CJK = tuple(kw for kw in _REGATE_KEYWORDS if not kw.isascii())
# Gate-answer markers for the R3 user gate (SKILL.md). The gate answers with
# a STRUCTURED question, so the answer text carries the chosen option label:
#   "无需修改，进入下一阶段"       -> approve (no modifications)
#   "用户已跳过此题，未选择任何选项。" -> approve (skipped == no modifications)
#   "用户已取消本次问答，未作答。"     -> approve (cancelled == no modifications)
#   "跳过复审"                     -> approve (re-gate declined)
# Anything else is modification feedback (the "需要修改（请补充具体意见）"
# option or the free text typed into the "Other" field).
_GATE_APPROVE_MARKERS = (
    "无需修改，进入下一阶段",
    "用户已跳过此题，未选择任何选项。",
    "用户已取消本次问答，未作答。",
    "跳过复审",
)
# Fixed rework map (config lists forward edges only).
# Wave-2 stage additions that introduce new *_review stages MUST update this map.
_REWORK_TARGETS = {
    "analysis_review": "analysis",
    "design_review": "design",
}
_STAGE_LABELS = {
    "analysis": "需求分析 (Requirements Analysis)",
    "analysis_review": "分析评审 (Analysis Review)",
    "design": "系统设计 (System Design)",
    "design_review": "设计评审 (Design Review)",
}


class DesignRail(RailStateMachineBase):
    """SDD state machine for Code mode (wave-1: requirements + design)."""

    ADVANCE_TOOL = "sdd_advance"
    SKILLS_DIR = "skills/"
    SECTION_NAME = _SECTION_NAME
    _STAGE_LABELS = _STAGE_LABELS

    def __init__(
        self,
        *,
        rail_pkg_dir: Path,
        project_dir: Path,
        priority: int = 60,
    ) -> None:
        super().__init__(
            rail_pkg_dir=rail_pkg_dir,
            project_dir=project_dir,
            priority=priority,
        )
        # Load + validate the design-specific config.yaml. Raise on invalid
        # config so the builder (BC-005) catches it and returns None — the
        # agent never mounts a rail with a broken state machine.
        cfg = config_loader.load(rail_pkg_dir / "config.yaml")
        result = config_loader.validate(cfg, rail_pkg_dir=rail_pkg_dir)
        if not result.ok:
            raise ValueError(
                f"DesignRail config validation failed: {result.errors}"
            )
        self.stages = cfg.get("stages") or {}
        self._priority = int(cfg.get("priority", priority))

    # ------------------------------------------------------------------
    # ask_user review handling (design-specific)
    # ------------------------------------------------------------------
    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:  # type: ignore[override]
        """Resolve StructuredAskUserTool confirmation points.

        For ``ask_user`` completions: an approve answer advances to the
        forward ``next`` stage; a reject answer triggers rework. Empty
        answer (failed / non-interactive) -> no transition.
        """
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "").strip()
        if tool_name != "ask_user":
            return

        answer = self._extract_answer(ctx)
        stage = self._current_stage()
        if stage == "done":
            logger.info("[DesignRail] ask_user resolved but stage=done; skip")
            return

        # Only review stages (analysis_review / design_review) drive
        # approve/reject transitions via ask_user. A clarification ask_user
        # during a PRODUCTION stage (analysis / design) must NOT advance —
        # otherwise the rail would skip the declared artifact gate.
        if stage not in _REWORK_TARGETS:
            logger.info(
                "[DesignRail] ask_user outside review stage %s; skip transition",
                stage,
            )
            return

        # No answer text (ask_user failed / interrupted / non-interactive
        # stdin) -> do NOT transition (prevents false-advance).
        if not answer:
            logger.info(
                "[DesignRail] ask_user completed with no answer text; skip transition"
            )
            return

        if self._is_reject(answer):
            target = self._rework_target(stage)
            if target is not None:
                self._transition_to(target)
                logger.info(
                    "[DesignRail] review rejected -> rework to %s", target
                )
        elif self._is_regate(answer):
            # SKILL.md R3 "需要复审": loop back to R1 for re-gate inside the
            # review skill. The rail does NOT transition (re-gate is internal
            # to the review skill's R1->R2->R3 loop) and must NOT log
            # "approved" — that would mislead the agent into calling
            # sdd_advance prematurely, skipping the re-gate loop.
            logger.info(
                "[DesignRail] review re-gate requested (需要复审); "
                "skill owns R3->R1 loop, no rail transition"
            )
        elif self._is_gate_revision(answer):
            # SKILL.md R3: the user asks for changes ("需要修改（请补充具体意见）"
            # or free text typed into the "Other" field). The deliverable is
            # revised in place by the skill and the gate may re-run; the rail
            # does NOT transition and must NOT log "approved" — the review is
            # not finished yet.
            logger.info(
                "[DesignRail] review revision requested; "
                "skill owns the in-place revision, no rail transition"
            )
        else:
            # Approve: do NOT auto-transition here. The SKILL.md R4 step
            # instructs the agent to call sdd_advance explicitly. Auto-
            # transitioning would cause a double-advance (after_tool_call
            # transitions + agent calls sdd_advance → "not a valid next"
            # error because already in the next stage).
            logger.info(
                "[DesignRail] review approved; waiting for agent to call "
                "sdd_advance to advance"
            )

    # ------------------------------------------------------------------
    # Helpers (design-specific; base provides the shared ones)
    # ------------------------------------------------------------------
    def _extract_answer(self, ctx: AgentCallbackContext) -> str:
        """Best-effort extraction of the user's answer text from ctx."""
        extra = getattr(ctx, "extra", None) or {}
        if isinstance(extra, dict):
            answers = extra.get("answers")
            if isinstance(answers, dict):
                parts: list[str] = []
                for value in answers.values():
                    if isinstance(value, str):
                        parts.append(value)
                    elif isinstance(value, list):
                        parts.append(", ".join(str(v) for v in value))
                if parts:
                    return "\n".join(parts)
            elif isinstance(answers, str) and answers:
                return answers
        tool_result = getattr(ctx, "tool_result", None)
        if isinstance(tool_result, str) and tool_result:
            return tool_result
        # Fallback: check ctx.inputs for tool_result
        inputs = getattr(ctx, "inputs", None)
        if inputs is not None:
            inputs_tr = getattr(inputs, "tool_result", None)
            if isinstance(inputs_tr, str) and inputs_tr:
                return inputs_tr
            inputs_tr_dict = getattr(inputs, "tool_result", None)
            if isinstance(inputs_tr_dict, dict):
                for v in inputs_tr_dict.values():
                    if isinstance(v, str) and v:
                        return v
        return ""

    def _is_reject(self, answer: str) -> bool:
        text = (answer or "").lower()
        for pattern in _REJECT_PATTERNS:
            if pattern.search(text):
                return True
        for kw in _REJECT_CJK:
            if kw in text:
                return True
        return False

    def _is_regate(self, answer: str) -> bool:
        text = (answer or "").lower()
        for pattern in _REGATE_PATTERNS:
            if pattern.search(text):
                return True
        for kw in _REGATE_CJK:
            if kw in text:
                return True
        return False

    def _is_gate_revision(self, answer: str) -> bool:
        """True when the R3 gate answer is modification feedback.

        The gate asks a structured question, so an answer that carries none
        of the approve markers (approve option / skip / cancel / 跳过复审) is
        the user asking for changes.
        """
        text = answer or ""
        if not text.strip():
            return False
        for marker in _GATE_APPROVE_MARKERS:
            if marker in text:
                return False
        return True

    def _next_stage(self, current: str) -> Optional[str]:
        stage_cfg = self.stages.get(current)
        if not isinstance(stage_cfg, dict):
            return None
        nxt = stage_cfg.get("next") or []
        if isinstance(nxt, list) and nxt:
            first = nxt[0]
            return first if isinstance(first, str) else None
        return None

    def _rework_target(self, current: str) -> Optional[str]:
        return _REWORK_TARGETS.get(current)

    def _domain_description(self) -> str:
        """SDD domain hint for the base class's bootstrap / tool description."""
        return "SDD (Spec-Driven Development)"
