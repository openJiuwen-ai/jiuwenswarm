"""Adapter 装配 / 热更阶段耗时的日志级别选择。

职责：
- 提供 ``_SLOW_RUNTIME_CONFIG_MS`` / ``_SLOW_RAIL_BUILD_MS`` 默认阈值
- 按总耗时与 ``JIUWENSWARM_SLOW_STAGE_MS`` 覆盖，决定 stage breakdown
  走 ``server_logger.info`` 还是 ``debug``

只服务计时日志，不参与实际装配逻辑。
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from openjiuwen.core.common.logging import server_logger

logger = logging.getLogger(__name__)

# Total ``_update_runtime_config`` cost above which its per-stage breakdown is
# worth an INFO line. It runs once per turn ahead of the model call, so anything
# at this scale is directly visible in time-to-first-token.
_SLOW_RUNTIME_CONFIG_MS = 50.0

# Rail construction reports unconditionally: it happens once per agent, not per
# turn, so a line per cold start is not noise, and cold start is precisely the
# budget nobody can otherwise account for. The earlier 100 ms bar was above the
# real cost, which meant the breakdown never appeared at INFO on a normal run.
_SLOW_RAIL_BUILD_MS = 0.0

# Profiling escape hatch: overrides every stage-breakdown threshold, in
# milliseconds. Set it to 0 to report all breakdowns at INFO. The thresholds
# above are tuned to stay quiet on a healthy run, which is the wrong setting
# when the question is "where did this un-slow half second go".
_STAGE_LOG_THRESHOLD_ENV = "JIUWENSWARM_SLOW_STAGE_MS"


def _stage_breakdown_logger(total_ms: float, threshold_ms: float) -> Callable[..., None]:
    """Pick the level a stage breakdown should be reported at.

    Both branches go through ``server_logger`` on purpose. The quiet branch
    used to go to this module's standard-logging logger, which put it in a
    different sink running at INFO — so the sub-threshold breakdown, the one
    needed to explain time that is not obviously slow, was never visible
    anywhere.

    Args:
        total_ms: Measured total for the stage group.
        threshold_ms: Site-specific bar above which the breakdown is INFO.

    Returns:
        The logging callable to emit the breakdown with.
    """
    override = os.environ.get(_STAGE_LOG_THRESHOLD_ENV)
    if override is not None:
        try:
            threshold_ms = float(override)
        except ValueError:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ignoring non-numeric %s=%s",
                _STAGE_LOG_THRESHOLD_ENV,
                override,
            )
    return server_logger.info if total_ms >= threshold_ms else server_logger.debug
