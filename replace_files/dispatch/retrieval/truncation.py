from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Sequence, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class ScoreTruncationConfig:
    min_score: float | None = None
    relative_min_score: float | None = None
    autocut_jumps: int = 0
    autocut_min_relative_drop: float = 0.0


@dataclass(frozen=True)
class ScoreTruncationDecision:
    initial_count: int
    kept_count: int
    cutoff_index: int | None = None
    reason: str = ""
    best_score: float | None = None
    min_score: float | None = None
    relative_min_score: float | None = None
    autocut_jumps: int = 0
    autocut_min_relative_drop: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def truncate_sorted_by_score(
    items: Sequence[T],
    *,
    score_getter: Callable[[T], float],
    config: ScoreTruncationConfig,
) -> tuple[list[T], ScoreTruncationDecision]:
    values = list(items)
    if not values:
        return [], ScoreTruncationDecision(initial_count=0, kept_count=0)

    best_score = float(score_getter(values[0]))
    cutoff = len(values)
    reason = ""

    if config.min_score is not None:
        candidate_cutoff = _first_score_below(values, score_getter=score_getter, threshold=float(config.min_score))
        if candidate_cutoff < cutoff:
            cutoff = candidate_cutoff
            reason = "min_score"

    if config.relative_min_score is not None and best_score > 0.0:
        relative_threshold = best_score * max(0.0, float(config.relative_min_score))
        candidate_cutoff = _first_score_below(values, score_getter=score_getter, threshold=relative_threshold)
        if candidate_cutoff < cutoff:
            cutoff = candidate_cutoff
            reason = "relative_min_score"

    if config.autocut_jumps > 0:
        candidate_cutoff = _autocut_cutoff(
            values,
            score_getter=score_getter,
            jumps=max(1, int(config.autocut_jumps)),
            min_relative_drop=max(0.0, float(config.autocut_min_relative_drop)),
        )
        if candidate_cutoff < cutoff:
            cutoff = candidate_cutoff
            reason = "autocut"

    kept = values[:cutoff]
    return kept, ScoreTruncationDecision(
        initial_count=len(values),
        kept_count=len(kept),
        cutoff_index=cutoff if cutoff < len(values) else None,
        reason=reason,
        best_score=best_score,
        min_score=config.min_score,
        relative_min_score=config.relative_min_score,
        autocut_jumps=max(0, int(config.autocut_jumps)),
        autocut_min_relative_drop=max(0.0, float(config.autocut_min_relative_drop)),
    )


def _first_score_below(
    items: Sequence[T],
    *,
    score_getter: Callable[[T], float],
    threshold: float,
) -> int:
    for index, item in enumerate(items):
        if float(score_getter(item)) < threshold:
            return index
    return len(items)


def _autocut_cutoff(
    items: Sequence[T],
    *,
    score_getter: Callable[[T], float],
    jumps: int,
    min_relative_drop: float,
) -> int:
    if len(items) <= 1:
        return len(items)
    observed_jumps = 0
    for index in range(1, len(items)):
        previous = float(score_getter(items[index - 1]))
        current = float(score_getter(items[index]))
        absolute_drop = previous - current
        if absolute_drop <= 0.0:
            continue
        relative_drop = absolute_drop / max(abs(previous), 1e-9)
        if relative_drop < min_relative_drop:
            continue
        observed_jumps += 1
        if observed_jumps >= jumps:
            return index
    return len(items)


__all__ = [
    "ScoreTruncationConfig",
    "ScoreTruncationDecision",
    "truncate_sorted_by_score",
]
