"""Whether Gateway should emit agent.prewarm.sync.

Copied from AgentServer ``agent_warm_pool.prewarm_enabled_by_env`` so Gateway
does not import the warm-pool module.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PREWARM_ENABLED_ENV_KEY = "JIUWENSWARM_AGENT_PREWARM"
_PREWARM_ON_VALUES = frozenset({"1", "true", "yes", "on"})
_PREWARM_OFF_VALUES = frozenset({"0", "false", "no", "off"})


def prewarm_enabled_by_env() -> bool:
    """Return whether background session prewarming is switched on.

    Returns:
        True only when the environment explicitly opts in; False when the
        switch is unset or carries an unrecognized value.
    """
    raw = str(os.environ.get(_PREWARM_ENABLED_ENV_KEY, "") or "").strip().lower()
    if raw in _PREWARM_ON_VALUES:
        return True
    if raw and raw not in _PREWARM_OFF_VALUES:
        logger.warning(
            "Ignoring unrecognized %s value %r; keeping prewarming disabled.",
            _PREWARM_ENABLED_ENV_KEY,
            raw,
        )
    return False
