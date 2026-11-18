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


def __getattr__(name):
    # Lazy package façade: each split PR carries an import-safe copy so the
    # package imports regardless of which split PR has merged (module files
    # land incrementally). Mirror of the previous eager re-export surface.
    import importlib

    lazy_map = {

        "DEFAULTS": "config",

        "clamp_deadlines": "config",

        "get_cron_guard_config": "config",

        "CronRunContext": "identity",

        "RunBudget": "identity",

        "RunRegistry": "identity",

        "current_cron_run": "identity",

        "get_run_registry": "identity",

        "sign_run_token": "identity",

        "verify_run_token": "identity",

        "STATE_FINISHED": "ledger",

        "STATE_FAILED": "ledger",

        "STATE_ORPHANED": "ledger",

        "STATE_RUNNING": "ledger",

        "STATE_TRIPPED": "ledger",

        "CronRunLedger": "ledger",

        "get_run_ledger": "ledger",

    }
    module_name = lazy_map.get(name)
    if module_name is None:
        raise AttributeError(f"package has no attribute {name!r}")
    # review root 200900677: split PRs land module files incrementally — a
    # missing submodule must surface as AttributeError (the getattr
    # protocol) so hasattr()/getattr(pkg, name, default) feature probing
    # stays fail-open instead of dying with ModuleNotFoundError.
    try:
        return getattr(importlib.import_module(f"{__name__}.{module_name}"), name)
    except ModuleNotFoundError as exc:
        raise AttributeError(f"package has no attribute {name!r}") from exc
