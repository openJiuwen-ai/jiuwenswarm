# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agent tier's view of the recommendation switches.

The switches are plain configuration and live in
``jiuwenswarm.common.recommendation_switches``; this module re-exports them and
registers the store-side effect that belongs to this tier: turning topic
recommendation on freezes the history boundary in the same call.
"""

from __future__ import annotations

from jiuwenswarm.common.recommendation_switches import (
    DEFAULT_PROMPT_SUGGESTION_ENABLED,
    DEFAULT_TOPIC_RECOMMENDATION_ENABLED,
    flatten_recommendation_for_config_panel,
    is_prompt_suggestion_enabled,
    is_topic_recommendation_enabled,
    register_topic_enabled_hook,
    resolve_recommendation_switch,
    save_recommendation_switches,
)

__all__ = [
    "DEFAULT_PROMPT_SUGGESTION_ENABLED",
    "DEFAULT_TOPIC_RECOMMENDATION_ENABLED",
    "flatten_recommendation_for_config_panel",
    "is_prompt_suggestion_enabled",
    "is_topic_recommendation_enabled",
    "resolve_recommendation_switch",
    "save_recommendation_switches",
]


def _freeze_consent_boundary(now: float) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        freeze_consent_boundary,
    )

    freeze_consent_boundary(now)


register_topic_enabled_hook(_freeze_consent_boundary)
