# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def ms_to_s(ms: float) -> float:
    """Convert milliseconds to seconds (3 decimal places)."""
    return round(ms / 1000.0, 3)


def percentile_s(values: list[float], pct: float) -> float:
    """Return percentile duration in seconds from millisecond samples."""
    if not values:
        return 0.0
    if len(values) == 1:
        return ms_to_s(values[0])
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * pct))))
    return ms_to_s(ordered[rank])


@dataclass
class LlmStatsAccumulator:
    count: int = 0
    total_ms: float = 0.0
    fail_count: int = 0
    max_ms: float = 0.0
    durations: list[float] = field(default_factory=list)

    def record(
        self,
        *,
        duration_ms: float,
        status: str,
    ) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.durations.append(duration_ms)
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        if status != "ok":
            self.fail_count += 1

    def to_dict(self) -> dict[str, Any]:
        if self.count == 0:
            return {
                "count": 0,
                "total_s": 0.0,
                "fail_count": 0,
                "max_s": 0.0,
                "p90_duration_s": 0.0,
            }
        return {
            "count": self.count,
            "total_s": ms_to_s(self.total_ms),
            "fail_count": self.fail_count,
            "max_s": ms_to_s(self.max_ms),
            "p90_duration_s": percentile_s(self.durations, 0.90),
        }


@dataclass
class ToolStatsAccumulator:
    count: int = 0
    total_ms: float = 0.0
    fail_count: int = 0
    max_ms: float = 0.0
    durations: list[float] = field(default_factory=list)
    iterations: set[int] = field(default_factory=set)
    by_name: dict[str, int] = field(default_factory=dict)

    def record(
        self,
        *,
        duration_ms: float,
        status: str,
        name: str = "",
        iteration: int = 0,
    ) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.durations.append(duration_ms)
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        if status != "ok":
            self.fail_count += 1
        if iteration > 0:
            self.iterations.add(iteration)
        if name:
            self.by_name[name] = self.by_name.get(name, 0) + 1

    def _tool_list(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "count": count}
            for name, count in sorted(self.by_name.items(), key=lambda item: (-item[1], item[0]))
        ]

    def to_dict(self) -> dict[str, Any]:
        if self.count == 0:
            return {
                "count": 0,
                "round_count": 0,
                "total_s": 0.0,
                "fail_count": 0,
                "max_s": 0.0,
                "p90_duration_s": 0.0,
                "list": [],
            }
        return {
            "count": self.count,
            "round_count": len(self.iterations),
            "total_s": ms_to_s(self.total_ms),
            "fail_count": self.fail_count,
            "max_s": ms_to_s(self.max_ms),
            "p90_duration_s": percentile_s(self.durations, 0.90),
            "list": self._tool_list(),
        }


def maintain_top_n(
    entries: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    top_n: int,
    duration_key: str = "duration_s",
) -> list[dict[str, Any]]:
    """Keep the slowest top_n entries sorted by duration descending."""
    merged = [*entries, candidate]
    merged.sort(key=lambda item: item.get(duration_key, 0), reverse=True)
    trimmed = merged[:top_n]
    for idx, item in enumerate(trimmed, start=1):
        item["rank"] = idx
    return trimmed
