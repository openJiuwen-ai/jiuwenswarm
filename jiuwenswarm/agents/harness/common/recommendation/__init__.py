# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive recommendation system — background engine for task reminder,
need exploration, and skill matching based on current conversation.

Modules:
- profile_extractor.py  — RecommendationState (cooldown + history) persistence
- proactive_engine.py    — Background tick loop (ProactiveEngine)
- proactive_actions.py   — Decision types, skill discovery, rate limiting, trigger
- proactive_prompts.py   — LLM prompt templates
- proactive_adapter.py   — Agent build + trigger + init
- situation_report.py    — Current conversation + calendar + skills aggregation
- calendar_source.py     — MCP calendar events
"""

from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
    RecommendationState,
    load_recommendation_state,
    save_recommendation_state,
)
from jiuwenswarm.agents.harness.common.recommendation.proactive_engine import (
    ProactiveEngine,
)

__all__ = [
    "RecommendationState",
    "load_recommendation_state",
    "save_recommendation_state",
    "ProactiveEngine",
]
