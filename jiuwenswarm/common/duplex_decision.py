# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared validation for typed APPEND / INTERRUPT decisions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def probability(value: Any, *, provider: str = "router") -> float:
    """Validate and normalize a provider probability or confidence value."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f"{provider} probabilities and confidence must be finite numbers in [0, 1]")
    return float(value)


def action_from_answer(
    answer: Mapping[str, Any],
    threshold: float,
    *,
    provider: str = "router",
) -> dict[str, str]:
    """Validate one typed Choice answer and apply the interrupt threshold."""
    if answer.get("type") != "choice" or answer.get("choice") not in ("APPEND", "INTERRUPT"):
        raise ValueError(f"invalid {provider} action choice")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, Mapping) or set(probabilities) != {"APPEND", "INTERRUPT"}:
        raise ValueError(f"invalid {provider} action probabilities")
    append = probability(probabilities["APPEND"], provider=provider)
    interrupt = probability(probabilities["INTERRUPT"], provider=provider)
    probability(answer.get("confidence"), provider=provider)
    if not math.isclose(append + interrupt, 1.0, rel_tol=0, abs_tol=1e-5):
        raise ValueError(f"{provider} action probabilities must sum to one")
    choice = answer["choice"]
    if probabilities[choice] < max(append, interrupt):
        raise ValueError(f"{provider} choice disagrees with its probabilities")
    action = "INTERRUPT" if choice == "INTERRUPT" and interrupt >= threshold else "APPEND"
    return {"action": action}
