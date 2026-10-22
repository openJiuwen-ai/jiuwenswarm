# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session usage accumulation for the Token Weather example."""

from __future__ import annotations

from dataclasses import dataclass, field

from jiuwenswarm.extensions_v2.events.context_usage import ContextUsageUpdate

MAX_ROUNDS = 200


@dataclass(slots=True)
class SessionUsage:
    """Cumulative usage plus the context facts of the latest round."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
    usage_percent: float | None = None
    context_window_tokens: int | None = None
    model: str | None = None
    rounds: dict[str, ContextUsageUpdate] = field(default_factory=dict)

    def apply(self, update: ContextUsageUpdate) -> bool:
        """Fold one round in; return False for a repeated request id."""

        if not update.request_id or update.request_id in self.rounds:
            return False
        self.turns += 1
        self.input_tokens += update.input_tokens
        self.output_tokens += update.output_tokens
        self.total_tokens += update.total_tokens
        self.cache_tokens += update.cache_tokens
        self.rounds[update.request_id] = update
        while len(self.rounds) > MAX_ROUNDS:
            self.rounds.pop(next(iter(self.rounds)))
        if update.usage_percent is not None:
            self.usage_percent = update.usage_percent
        if update.context_window_tokens is not None:
            self.context_window_tokens = update.context_window_tokens
        if update.model:
            self.model = update.model
        return True

    @property
    def cache_rate(self) -> float | None:
        return cache_rate(self.cache_tokens, self.input_tokens)


def format_tokens(value: int) -> str:
    """Render a token count with one decimal for thousands and millions."""

    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M tok"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K tok"
    return f"{value} tok"


def format_rate(rate: float | None) -> str | None:
    return None if rate is None else f"{rate * 100:.0f}%"


def cache_rate(cache_tokens: int, input_tokens: int) -> float | None:
    if input_tokens <= 0:
        return None
    return cache_tokens / input_tokens
