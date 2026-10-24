"""Public DeepResearch surface with one interactive entry and low-level tools."""

from typing import Any

from jiuwenswarm.common.config import get_config

from .tools import (
    deepresearch_stream,
    push_deepresearch_route,
    reset_deepresearch_route,
)
from .execution import deepresearch_execute


def enable_deepresearch() -> bool:
    """Enable only for a missing/default or explicit boolean true setting."""
    try:
        configured = get_config().get("enable_deepresearch", True)
    except Exception:
        return False
    return configured if isinstance(configured, bool) else False


def get_deepresearch_tools() -> list[Any]:
    """Return exactly the supported formal tools without probing the SDK."""
    if not enable_deepresearch():
        return []
    from .rewrite_tools import (
        deepresearch_commit_rewrite,
        deepresearch_generate_rewrite_html,
        deepresearch_prepare_rewrite,
    )

    return [
        deepresearch_execute,
        deepresearch_stream,
        deepresearch_prepare_rewrite,
        deepresearch_commit_rewrite,
        deepresearch_generate_rewrite_html,
    ]

__all__ = [
    "deepresearch_execute",
    "deepresearch_stream",
    "enable_deepresearch",
    "get_deepresearch_tools",
    "push_deepresearch_route",
    "reset_deepresearch_route",
]
