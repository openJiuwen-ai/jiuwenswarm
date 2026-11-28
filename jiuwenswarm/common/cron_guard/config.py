# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration for the cron runaway guard (``execution_guard.cron_guard``).

The configuration is re-read on every call so changes in ``config.yaml`` take
effect without a restart (hot reload).  Invalid values fall back to defaults
with a warning; a missing section means the guard is **disabled** (fail-open:
no config → no behavior change).
"""

from __future__ import annotations

import copy
from typing import Any

from jiuwenswarm.common.utils import logger

#: Default configuration.  ``enabled`` defaults to False so that deploying the
#: code without an explicit config section changes nothing at all.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "max_iterations": 30,
    "default_timeout_seconds": 3600,
    "trust": {
        "run_token_secret_env": "JIUWENSWARM_CRON_RUN_SECRET",
    },
    "deadline": {
        "enabled": True,
        "soft_ratio": 0.85,
        "hard_ratio": 0.95,
        "gateway_margin_seconds": 10,
        "reserve_seconds": 5,
        "kill_grace_seconds": 3,
    },
    "tools": {
        "code_tool": "clamp",
        "background_commands": "register_and_reap",
        "script_scan": "off",
    },
    "sleep": {
        "mode": "enforce",
        "max_single_seconds": 10,
        "max_total_seconds": 30,
        "max_sleep_calls": 3,
        "unknown_duration": "block",
        "unknown_assumed_seconds": 10,
        "max_nesting_depth": 3,
    },
    "wall_clock": {
        "tool_wait_budget_seconds": 300,
    },
    "checkpoint": {
        "enabled": True,
        "on_trip": "quarantine_and_fail",
        "interactive_policy": "allow",
        "quarantine_retention_days": 30,
    },
    "budget_ledger": {
        "path": None,
    },
    "concurrency": {
        "max_concurrent_cron_runs": None,
    },
}


def _merged(section: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(defaults)
    for key, value in section.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merged(value, out[key])
        else:
            out[key] = value
    return out


def get_cron_guard_config() -> dict[str, Any]:
    """Return the merged cron_guard config (defaults + user overrides).

    Never raises: on any failure reading the app config, returns a copy of the
    defaults with ``enabled=False`` (fail-open).
    """
    try:
        from jiuwenswarm.common.config import get_config

        app_cfg = get_config() or {}
        guard_cfg = (app_cfg.get("execution_guard") or {}).get("cron_guard")
        if not isinstance(guard_cfg, dict):
            return copy.deepcopy(DEFAULTS)
        merged = _merged(guard_cfg, DEFAULTS)
        _validate(merged)
        return merged
    except Exception as exc:  # noqa: BLE001 — guard config failure must not break callers
        logger.warning("[cron_guard] config read failed, guard disabled: %s", exc)
        disabled = copy.deepcopy(DEFAULTS)
        disabled["enabled"] = False
        return disabled


def _validate(cfg: dict[str, Any]) -> None:
    """Coerce numeric fields to positive numbers; fall back on garbage."""
    for section_key, keys in (
        (
            "deadline",
            ("soft_ratio", "hard_ratio", "gateway_margin_seconds", "reserve_seconds", "kill_grace_seconds"),
        ),
        (
            "sleep",
            (
                "max_single_seconds",
                "max_total_seconds",
                "max_sleep_calls",
                "unknown_assumed_seconds",
                "max_nesting_depth",
            ),
        ),
        ("wall_clock", ("tool_wait_budget_seconds",)),
    ):
        section = cfg.get(section_key)
        if not isinstance(section, dict):
            continue
        for key in keys:
            try:
                value = float(section[key])
            except (TypeError, ValueError, KeyError):
                logger.warning("[cron_guard] invalid %s.%s=%r, using default", section_key, key, section.get(key))
                section[key] = DEFAULTS[section_key][key]
                continue
            if value <= 0:
                logger.warning("[cron_guard] non-positive %s.%s=%r, using default", section_key, key, value)
                section[key] = DEFAULTS[section_key][key]
            else:
                section[key] = value
    # Top-level numeric keys (review root 200900608: `default_timeout_seconds`
    # and `max_iterations` were consumed unvalidated).
    for key in ("max_iterations", "default_timeout_seconds"):
        # `max_iterations: null` is documented as "unlimited" (shipped
        # deployment template comment; identity.py consumes None as no limit
        # via RunBudget.iterations_exceeded() -> False). It must reach the
        # consumer unchanged instead of float(None) silently rewriting it to
        # the default (atomgit 2026-11-18 #10209). A missing key still falls
        # into the except-branch default below.
        if key == "max_iterations" and "max_iterations" in cfg and cfg["max_iterations"] is None:
            continue
        try:
            value = float(cfg[key])
        except (TypeError, ValueError, KeyError):
            logger.warning("[cron_guard] invalid %s=%r, using default", key, cfg.get(key))
            cfg[key] = DEFAULTS[key]
            continue
        if value <= 0:
            logger.warning("[cron_guard] non-positive %s=%r, using default", key, value)
            cfg[key] = DEFAULTS[key]
        else:
            cfg[key] = value


def clamp_deadlines(
    base_seconds: float,
    cfg: dict[str, Any] | None = None,
) -> tuple[float, float]:
    """Compute ``(hard, soft)`` deadline seconds from a base timeout.

    P1 invariant (final review): the deadline inequality must never produce
    ``soft >= hard`` and both must stay strictly positive, even for tiny bases:

    * ``hard = max(base * hard_ratio, base - 30)`` clamped to ``[1, base]``
    * ``soft = min(base * soft_ratio, hard - soft_reserve)`` clamped to
      ``[1, hard - 1]``, where ``soft_reserve = deadline.gateway_margin_seconds``
      capped at the proportional gap ``hard * (1 - soft_ratio)``.  The reserve
      used to be a fixed 10s, which collapsed ``soft`` to ~1s for small bases
      (review root 200900612) and left the configured key unconsumed
      (review root 200900620).

    ``deadline.reserve_seconds`` and ``deadline.kill_grace_seconds`` are NOT
    consumed here: they belong to the L-WD watchdog (sister change) which
    enforces the hard deadline at the process level.

    Returns ``(hard_seconds, soft_seconds)`` with ``0 < soft < hard`` and
    ``hard <= max(base, 2.0)`` (bases ``<= 2`` get the smallest legal window
    ``(2.0, 1.0)``, which may exceed the base itself by design).
    """
    cfg = cfg or get_cron_guard_config()
    dl = cfg.get("deadline") or {}
    soft_ratio = float(dl.get("soft_ratio", 0.85))
    hard_ratio = float(dl.get("hard_ratio", 0.95))
    gateway_margin = max(float(dl.get("gateway_margin_seconds", 10.0)), 0.0)
    base = max(float(base_seconds), 0.0)
    if base <= 2.0:
        # Degenerate base: keep the invariant with the smallest legal window.
        return 2.0, 1.0
    hard = max(base * hard_ratio, base - 30.0)
    hard = min(hard, base)
    hard = max(hard, 1.0)
    soft_reserve = max(0.0, min(gateway_margin, hard * (1.0 - soft_ratio)))
    soft = min(base * soft_ratio, hard - soft_reserve)
    soft = max(soft, 1.0)
    if soft >= hard:
        soft = max((hard - 1.0), 1.0)
    # Final assertion of the invariant; if rounding ever breaks it, force it.
    if not (0.0 < soft < hard):  # pragma: no cover - defensive
        hard = max(hard, 2.0)
        soft = 1.0
    return hard, soft
