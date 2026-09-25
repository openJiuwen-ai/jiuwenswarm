# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MACRO mode schemas for the lightweight scheduler."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# Auto lanes: single agent vs cluster. Plan (agent.plan) is a user-forced
# toggle, not a scheduler option. Legacy ``agent.fast`` normalizes to ``agent``.
MACRO_MODES = frozenset({"agent", "team"})
AUTO_MODE_ALIASES = frozenset({"auto", "agent.auto", "macro.auto"})
MACRO_MODE_LABELS = {
    "agent": "Agent Mode",
    "team": "Cluster Mode",
}


def is_auto_mode(mode: str | None) -> bool:
    text = str(mode or "").strip().lower()
    return text in AUTO_MODE_ALIASES


def normalize_macro_mode(mode: str | None, *, default: str = "agent") -> str:
    text = str(mode or "").strip().lower()
    if text in MACRO_MODES:
        return text
    if text in {"plan", "planning", "agent.plan"}:
        return "agent"
    if text in {"fast", "performance", "agent.fast"}:
        return "agent"
    if text in {"cluster", "agent.team"}:
        return "team"
    return default if default in MACRO_MODES else "agent"


def macro_mode_label(mode: str | None) -> str:
    """Human-readable MACRO lane name for logs / UI notices."""
    normalized = normalize_macro_mode(mode)
    return MACRO_MODE_LABELS.get(normalized, "Agent Mode")


@dataclass
class MacroRoutingDecision:
    """Resolved top-level execution lane (Agent / Cluster)."""

    mode: str
    confidence: float
    rationale: str
    source: str  # rules | llm | hybrid | forced | disabled | fallback
    features: dict[str, Any] = field(default_factory=dict)
    gate_confident: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mode"] = normalize_macro_mode(self.mode)
        payload["mode_label"] = macro_mode_label(self.mode)
        return payload


__all__ = [
    "AUTO_MODE_ALIASES",
    "MACRO_MODE_LABELS",
    "MACRO_MODES",
    "MacroRoutingDecision",
    "is_auto_mode",
    "macro_mode_label",
    "normalize_macro_mode",
]
