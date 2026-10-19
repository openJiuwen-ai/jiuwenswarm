# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive recommendation system — background engine for task reminder,
need exploration, and skill matching based on current conversation.

Modules:
- profile_extractor.py  — RecommendationState (history + buffer + gradients) persistence
- proactive_engine.py    — Background tick loop (ProactiveEngine)
- proactive_actions.py   — Decision types, skill discovery, rate limiting, trigger
- proactive_prompts.py   — LLM prompt templates
- proactive_adapter.py   — Agent build + trigger + init
- situation_report.py    — Current conversation + calendar + skills aggregation
- calendar_source.py     — MCP calendar events

The exports are resolved on first use: importing any submodule (for instance the config
switches from the settings panel) must not drag the engine and its model dependencies in.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profile_extractor import (
        RecommendationState,
        load_recommendation_state,
        save_recommendation_state,
    )
    from .proactive_engine import ProactiveEngine

__all__ = [
    "RecommendationState",
    "load_recommendation_state",
    "save_recommendation_state",
    "ProactiveEngine",
]

_EXPORTS = {
    "RecommendationState": ("profile_extractor", "RecommendationState"),
    "load_recommendation_state": ("profile_extractor", "load_recommendation_state"),
    "save_recommendation_state": ("profile_extractor", "save_recommendation_state"),
    "ProactiveEngine": ("proactive_engine", "ProactiveEngine"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{target[0]}", __name__), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
