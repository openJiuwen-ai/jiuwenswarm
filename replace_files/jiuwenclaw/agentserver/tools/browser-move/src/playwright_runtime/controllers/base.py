#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Base controller contract for action dispatchers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseController(ABC):
    """Abstract contract for controller implementations."""

    @abstractmethod
    def bind_runtime(self, runtime: Any) -> None:
        """Bind a runtime object used by runtime-backed actions."""

    @abstractmethod
    def bind_runtime_runner(self, runner: Any | None) -> None:
        """Bind a runtime runner callable."""

    @abstractmethod
    def clear_runtime_runner(self) -> None:
        """Clear any currently bound runtime runner."""

    @abstractmethod
    def register_action(self, name: str, handler: Any, *, overwrite: bool = True) -> None:
        """Register an action handler."""

    @abstractmethod
    def register_action_spec(
        self,
        name: str,
        *,
        summary: str = "",
        when_to_use: str = "",
        params: dict[str, str] | None = None,
    ) -> None:
        """Register metadata for an action."""

    @abstractmethod
    def list_actions(self) -> list[str]:
        """List registered action names."""

    @abstractmethod
    def describe_actions(self) -> dict[str, dict[str, Any]]:
        """Return metadata for registered actions."""

    @abstractmethod
    async def run_action(
        self,
        action: str,
        session_id: str = "",
        request_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a registered action."""
