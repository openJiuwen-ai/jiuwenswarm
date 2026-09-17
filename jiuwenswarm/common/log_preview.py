# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded text previews for single-line log entries.

The message-pipeline instrumentation logs a fragment of the user's query so a
slow turn can be tied back to the request that caused it. That is user content
landing in a long-lived log file, so it is gated: set
``logging.preview_user_content: false`` in ``config.yaml`` (or
``JIUWENSWARM_LOG_PREVIEW_USER_CONTENT=0``) to keep the timing data and drop the
text, which is replaced by a length-only placeholder.
"""

from __future__ import annotations

import os
from typing import Any

# Default number of characters kept when previewing user content in a log line.
DEFAULT_PREVIEW_MAX_CHARS = 200

# Env override, checked before config so an operator can turn previews off
# without editing config.yaml. Any of these values disables them.
_PREVIEW_ENV_VAR = "JIUWENSWARM_LOG_PREVIEW_USER_CONTENT"
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _preview_user_content_enabled() -> bool:
    """Return whether log previews may include user-authored text.

    Returns:
        True unless the env var or ``logging.preview_user_content`` disables it.
        Config failures fall back to True: instrumentation must not be the thing
        that breaks a request path.
    """
    env_value = os.environ.get(_PREVIEW_ENV_VAR)
    if env_value is not None:
        return env_value.strip().lower() not in _FALSE_VALUES
    try:
        from jiuwenswarm.common.config import get_config

        logging_config = get_config().get("logging")
    except Exception:
        return True
    if not isinstance(logging_config, dict):
        return True
    return bool(logging_config.get("preview_user_content", True))


def preview_text(value: Any, limit: int = DEFAULT_PREVIEW_MAX_CHARS) -> str:
    """Render a value as a bounded single-line log fragment.

    Args:
        value: Value to preview; non-string values are rendered with ``str``.
        limit: Max characters kept from the rendered text.

    Returns:
        The rendered text when short enough, otherwise a clipped form with the
        number of omitted characters appended. When user-content previews are
        disabled, a length-only placeholder instead of any of the text.
    """
    text = value if isinstance(value, str) else str(value)
    if not _preview_user_content_enabled():
        return f"<{len(text)} chars omitted>"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(+{len(text) - limit} chars)"


__all__ = ["DEFAULT_PREVIEW_MAX_CHARS", "preview_text"]
