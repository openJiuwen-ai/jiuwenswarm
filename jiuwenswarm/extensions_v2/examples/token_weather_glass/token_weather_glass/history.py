# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded, session-owned observations, without deriving consumption from context."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.extensions_v2.event_types.context_usage import ContextUsageUpdate

HISTORY_LIMIT = 12
BARS = "▁▂▃▄▅▆▇█"
CONTEXT_CATEGORY_NAMES = ("system_prompt", "tools", "skills", "messages")


@dataclass(frozen=True, slots=True)
class ContextCategory:
    """A copied category observation from the same turn's public summary."""

    name: str
    tokens: int | float


def _context_categories(context: dict[str, Any]) -> tuple[ContextCategory, ...] | None:
    breakdown = context.get("breakdown")
    if breakdown is None:
        return None
    if not isinstance(breakdown, dict) or not isinstance(breakdown.get("categories"), list):
        raise ValueError("invalid context category summary")
    categories: dict[str, ContextCategory] = {}
    for item in breakdown["categories"]:
        if not isinstance(item, dict):
            raise ValueError("invalid context category")
        name, tokens = item.get("name"), item.get("tokens")
        if (not isinstance(name, str) or not name.strip() or name in categories
                or type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens < 0):
            raise ValueError("invalid context category")
        categories[name] = ContextCategory(name, tokens)
    if not math.isfinite(sum(item.tokens for item in categories.values())):
        raise ValueError("invalid context category total")
    names = [name for name in CONTEXT_CATEGORY_NAMES if name in categories]
    names.extend(name for name in categories if name not in CONTEXT_CATEGORY_NAMES)
    return tuple(categories[name] for name in names) or None


@dataclass(frozen=True, slots=True)
class TurnReading:
    request_id: str
    number: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    context_tokens: int | float | None
    context_categories: tuple[ContextCategory, ...] | None = None


@dataclass(slots=True)
class TurnHistory:
    readings: list[TurnReading] = field(default_factory=list)
    count: int = 0
    _numbers: dict[str, int] = field(default_factory=dict, repr=False)

    def observe(self, update: ContextUsageUpdate, usage: dict[str, Any]) -> TurnReading | None:
        """Keep stable turn identities; an evicted request cannot become a new round."""
        values = (update.input_tokens, update.output_tokens, update.total_tokens, update.cache_tokens)
        if not update.request_id or any(type(value) is not int or value < 0 for value in values):
            raise ValueError("invalid per-turn token observation")
        tokens = usage["context"].get("tokens")
        if tokens is not None and (type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens < 0):
            raise ValueError("invalid context token observation")
        categories = _context_categories(usage["context"])
        existing = next((index for index, item in enumerate(self.readings) if item.request_id == update.request_id), None)
        if existing is None and update.request_id in self._numbers:
            return None
        if existing is None:
            self.count += 1
            self._numbers[update.request_id] = self.count
        number = self.count if existing is None else self.readings[existing].number
        reading = TurnReading(update.request_id, number, *values, tokens, categories)
        if existing is None:
            self.readings.append(reading)
            del self.readings[:-HISTORY_LIMIT]
        else:
            self.readings[existing] = reading
        return reading


def sparkline(readings: list[TurnReading] | tuple[TurnReading, ...]) -> str:
    """Preserve missing readings as gaps and retain real zero values."""
    top = max((item.context_tokens for item in readings if item.context_tokens is not None), default=0)
    scale = max(top, 1)
    return "".join("·" if item.context_tokens is None else BARS[min(7, int(item.context_tokens / scale * 7))]
                   for item in readings)
