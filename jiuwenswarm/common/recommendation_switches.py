# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""YAML switches for topic recommendation and in-chat prompt suggestion.

``null`` (or a missing key) follows the code default so a release can change
the product default without rewriting user config. An explicit ``true`` /
``false`` is the user's choice.

The switches are plain configuration, so they live in ``common``: the settings
panel and the Front config query read and write them without loading agent code.
Turning topic recommendation on also freezes the history boundary, which needs
the recommendation store; the agent tier registers that side effect through
``register_topic_enabled_hook``.
"""

from __future__ import annotations

import time
from typing import Any, Callable

# ``jiuwenswarm.common.config`` is imported lazily (see ``_section``): it pulls OpenJiuwen
# KV-cache types, and the Front config-query service resolves these switches without loading it.

# Product defaults while YAML is ``null``. Flip these to change first-run behavior.
DEFAULT_TOPIC_RECOMMENDATION_ENABLED = False
DEFAULT_PROMPT_SUGGESTION_ENABLED = False

_TOPIC_ENABLED_HOOKS: list[Callable[[float], None]] = []


def register_topic_enabled_hook(hook: Callable[[float], None]) -> None:
    """Run ``hook`` with the switch time whenever topic recommendation is turned on."""

    if hook not in _TOPIC_ENABLED_HOOKS:
        _TOPIC_ENABLED_HOOKS.append(hook)


def resolve_recommendation_switch(value: Any, default: bool) -> bool:
    """Resolve an explicit switch or inherit ``default`` for ``null``."""

    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _section(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(config, dict):
        source = config
    else:
        from jiuwenswarm.common.config import get_config
        source = get_config()
    section = source.get("recommendation")
    return section if isinstance(section, dict) else {}


def _switch_value(section: dict[str, Any], key: str, default: bool) -> bool:
    """An explicit switch wins, including an explicit YAML ``null``."""

    if key in section:
        return resolve_recommendation_switch(section.get(key), default)
    return default


def is_topic_recommendation_enabled(config: dict[str, Any] | None = None) -> bool:
    """Whether topic cards may be prepared and shown."""

    return _switch_value(
        _section(config), "topic_enabled", DEFAULT_TOPIC_RECOMMENDATION_ENABLED,
    )


def is_prompt_suggestion_enabled(config: dict[str, Any] | None = None) -> bool:
    """Whether the composer may show next-input chips."""

    return _switch_value(
        _section(config), "prompt_enabled", DEFAULT_PROMPT_SUGGESTION_ENABLED,
    )


def save_recommendation_switches(
    *,
    topic_enabled: bool | None = None,
    prompt_enabled: bool | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Persist explicit switch values. ``None`` leaves that key unchanged.

    Turning topic recommendation on also freezes the history boundary, in the same call: a
    boundary recorded any later would leave the feature reading as on while every recorded
    turn is still hidden.
    """

    from jiuwenswarm.common.config import update_config

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        section = config.get("recommendation")
        if not isinstance(section, dict):
            section = config["recommendation"] = {}
        if topic_enabled is not None:
            section["topic_enabled"] = topic_enabled
        if prompt_enabled is not None:
            section["prompt_enabled"] = prompt_enabled
        return config

    update_config(mutate)
    if topic_enabled:
        moment = now if now is not None else time.time()
        for hook in _TOPIC_ENABLED_HOOKS:
            hook(moment)
    return dict(_section())


def flatten_recommendation_for_config_panel(raw: dict[str, Any]) -> dict[str, str]:
    """Resolved switch values for the settings panel; ``null`` becomes the code default."""

    return {
        "topic_recommendation_enabled": (
            "true" if is_topic_recommendation_enabled(raw) else "false"
        ),
        "prompt_suggestion_enabled": (
            "true" if is_prompt_suggestion_enabled(raw) else "false"
        ),
    }
