# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight AgentServer Front: transport, protocol, router, readiness."""

from __future__ import annotations

from typing import Any

__all__ = ["AgentServerFront"]


def __getattr__(name: str) -> Any:
    if name == "AgentServerFront":
        from jiuwenswarm.server.front.server import AgentServerFront

        return AgentServerFront
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
