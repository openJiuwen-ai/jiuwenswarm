# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron runaway guard (issue #5018).

Layered defense against cron agent sleep-polling runaways:

* L0 (this package): trusted cron run identity, run registry, run ledger,
  hot-reloadable configuration.
* L1: ``sleep_guard`` — static sleep/poll interception on shell entry points.
* L-WD: request-level hard deadline watchdog wired at the AgentServer stream
  entry (``cron_guard/watchdog.py`` helpers).
* L3/L4: ``CronBudgetRail`` — iteration and wall-clock soft finishing.
* L2: checkpoint quarantine helpers (ledger-driven, reversible).

All guards are **fail-open**: any failure inside the guard itself must let the
protected operation proceed with a warning, never block it.  Interactive
requests (no valid cron run identity) are completely unaffected.
"""

from .config import (
    DEFAULTS,
    clamp_deadlines,
    get_cron_guard_config,
)
from .identity import (
    CronRunContext,
    RunBudget,
    RunRegistry,
    current_cron_run,
    get_run_registry,
    sign_run_token,
    verify_run_token,
)
from .ledger import (
    STATE_FINISHED,
    STATE_FAILED,
    STATE_ORPHANED,
    STATE_RUNNING,
    STATE_TRIPPED,
    CronRunLedger,
    get_run_ledger,
)

__all__ = [
    "DEFAULTS",
    "clamp_deadlines",
    "get_cron_guard_config",
    "CronRunContext",
    "RunBudget",
    "RunRegistry",
    "current_cron_run",
    "get_run_registry",
    "sign_run_token",
    "verify_run_token",
    "STATE_FINISHED",
    "STATE_FAILED",
    "STATE_ORPHANED",
    "STATE_RUNNING",
    "STATE_TRIPPED",
    "CronRunLedger",
    "get_run_ledger",
]
