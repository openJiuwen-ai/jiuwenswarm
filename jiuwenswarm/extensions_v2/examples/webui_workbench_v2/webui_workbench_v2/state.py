# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic demonstration data and activation-local business state."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DemoItem:
    """One fixed task shown by the workbench."""

    key: str
    title: str
    category: str
    project: str
    score: int


DEMO_ITEMS = (
    DemoItem("review", "协议评审 / Protocol review", "design", "platform", 95),
    DemoItem("input", "中文输入验证 / Chinese input check", "test", "platform", 85),
    DemoItem("guide", "使用指南 / User guide", "design", "examples", 75),
    DemoItem("layout", "窄屏布局检查 / Narrow layout check", "test", "examples", 65),
)


@dataclass(slots=True)
class TurnState:
    """A snapshot keyed by the actual request id received from the host."""

    summary: str
    result: str
    refreshes: int = 0


@dataclass(slots=True)
class SessionState:
    """Filters and mounted targets for one real conversation."""

    query: str = ""
    category: str | None = None
    project: str | None = None
    threshold: int = 60
    auto_summary: bool = True
    destroy_hidden: bool = False
    image_failure: bool = False
    active_tab: str = "form"
    selected: str = "review"
    search_hint: str = ""
    operations: int = 0
    last_result: str = "准备就绪 / Ready"
    mounted: set[tuple[str, str]] = field(default_factory=set)
    turns: dict[str, TurnState] = field(default_factory=dict)

    def results(self) -> tuple[DemoItem, ...]:
        """Apply all filters without calling external services."""
        query = self.query.strip().casefold()
        return tuple(
            item for item in DEMO_ITEMS
            if (not query or query in item.title.casefold())
            and (self.category is None or item.category == self.category)
            and (self.project is None or item.project == self.project)
            and item.score >= self.threshold
        )

    def summary(self) -> str:
        """Return the same concise filter summary for every slot."""
        return (
            f"{len(self.results())} 项 / items · ≥ {self.threshold} · "
            f"{self.category or '全部 / all'} · {self.project or '全部项目 / all projects'}"
            + (f" · {self.query}" if self.query else "")
        )

    def turn(self, request_id: str) -> TurnState:
        """Initialize a round once; later ready events restore its own snapshot."""
        if request_id not in self.turns:
            self.turns[request_id] = TurnState(self.summary(), self.last_result)
        return self.turns[request_id]

    def reset(self) -> None:
        """Reset session controls while preserving mounted targets and round history."""
        initial = SessionState()
        self.query = initial.query
        self.category = initial.category
        self.project = initial.project
        self.threshold = initial.threshold
        self.auto_summary = initial.auto_summary
        self.destroy_hidden = initial.destroy_hidden
        self.image_failure = initial.image_failure
        self.active_tab = initial.active_tab
        self.selected = initial.selected
        self.search_hint = initial.search_hint
