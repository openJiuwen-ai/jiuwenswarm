# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Defer Team-mode code until a Runtime operation needs its controller."""
from __future__ import annotations

from typing import Any, Callable


class LazyTeamExecutionController:
    """Borrow the existing process Team owner without constructing it at startup."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._controller: Any = None
        self._inactive_reporter: Any = None

    def set_session_inactive_reporter(self, reporter: Any) -> None:
        self._inactive_reporter = reporter
        if self._controller is not None:
            self._controller.set_session_inactive_reporter(reporter)

    def activate(self) -> Any:
        if self._controller is None:
            self._controller = self._factory()
            if self._inactive_reporter is not None:
                setter = getattr(self._controller, "set_session_inactive_reporter", None)
                if callable(setter):
                    setter(self._inactive_reporter)
        return self._controller

    def __getattr__(self, name: str) -> Any:
        return getattr(self.activate(), name)


def process_team_controller() -> LazyTeamExecutionController:
    def factory() -> Any:
        from jiuwenswarm.agents.harness.team import get_team_manager

        return get_team_manager(None)

    return LazyTeamExecutionController(factory)
