# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry point for LAS-inspired MACRO mode routing."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.macro_routing.config import load_macro_routing_config
from jiuwenswarm.agents.harness.macro_routing.gate import route_with_gate
from jiuwenswarm.agents.harness.macro_routing.llm_scheduler import route_with_llm_scheduler
from jiuwenswarm.agents.harness.macro_routing.schemas import (
    MacroRoutingDecision,
    is_auto_mode,
    normalize_macro_mode,
)

logger = logging.getLogger(__name__)


async def route_macro_mode(
    query: str,
    *,
    requested_mode: str | None,
    config_base: dict[str, Any] | None = None,
) -> MacroRoutingDecision:
    """Resolve Auto MACRO mode; pass through forced agent / team.

    Precedence:
    1. Forced non-auto mode → return as-is (source=forced)
    2. Auto + macro_routing.enabled=false → agent (source=disabled)
    3. Auto + rules/hybrid gate → optionally escalate to LLM scheduler
    """
    requested = str(requested_mode or "").strip().lower()
    if not is_auto_mode(requested):
        mode = normalize_macro_mode(requested, default="agent")
        return MacroRoutingDecision(
            mode=mode,
            confidence=1.0,
            rationale=f"User-forced MACRO mode: {mode}.",
            source="forced",
            features={},
            gate_confident=True,
        )

    routing_cfg = load_macro_routing_config(config_base)
    if not routing_cfg.get("enabled", True):
        return MacroRoutingDecision(
            mode="agent",
            confidence=1.0,
            rationale="MACRO Auto is selected but macro_routing is disabled — using Agent Mode.",
            source="disabled",
            features={},
            gate_confident=True,
        )

    strategy = str(routing_cfg.get("strategy") or "hybrid")
    gate = route_with_gate(query, config=routing_cfg)

    if strategy == "rules":
        gate.source = "rules"
        return gate

    if strategy == "llm":
        decision = await route_with_llm_scheduler(
            query,
            gate=gate,
            model_name=str(routing_cfg.get("model_name") or ""),
        )
        # Keep scheduler-reported source (llm | fallback); do not clobber fallback.
        if decision.source not in {"llm", "fallback"}:
            decision.source = "llm"
        return decision

    # hybrid: escalate only when gate is uncertain
    if gate.gate_confident:
        gate.source = "hybrid"
        return gate

    decision = await route_with_llm_scheduler(
        query,
        gate=gate,
        model_name=str(routing_cfg.get("model_name") or ""),
    )
    decision.source = "hybrid"
    logger.info(
        "[MacroRouter] hybrid escalated to LLM: gate=%s -> %s conf=%.2f",
        gate.mode,
        decision.mode,
        decision.confidence,
    )
    return decision


__all__ = ["route_macro_mode"]
