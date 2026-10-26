# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Attach RequestSummaryRail to TaskTool/create_subagent children.

Enterprise wires record-only rails inside ``subagent_executor``; jiuwenswarm
uses core ``DeepAgent.create_subagent`` instead. Patching that entry point
covers TaskTool, SessionSpawnExecutor, and the /debug TaskTool reimplementation.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.utils import logger
from jiuwenswarm.perf.config import get_perf_summary_config
from jiuwenswarm.perf.guard import run_perf_safe

_COMPONENT = "perf.subagent_hooks"
_PATCH_APPLIED = False


def _agent_has_request_summary_rail(agent: Any) -> bool:
    configured = agent.configured_rails() if hasattr(agent, "configured_rails") else []
    for rail in configured or []:
        if rail.__class__.__name__ == "RequestSummaryRail":
            return True
    return False


def attach_request_summary_rail(subagent: Any) -> bool:
    """Attach ``RequestSummaryRail(record_only=True)`` after create_subagent.

    Idempotent and best-effort: never raises into the spawn path.
    Returns True when a rail was newly attached.
    """
    if subagent is None:
        return False
    if not get_perf_summary_config().enabled:
        return False

    attached = {"ok": False}

    def _attach() -> None:
        from jiuwenswarm.perf.request_summary_rail import RequestSummaryRail

        if _agent_has_request_summary_rail(subagent):
            return
        add_rail = getattr(subagent, "add_rail", None)
        if not callable(add_rail):
            logger.debug(
                "[perf] subagent has no add_rail; skip RequestSummaryRail attach"
            )
            return
        rail = RequestSummaryRail(record_only=True)
        add_rail(rail)
        attached["ok"] = True
        logger.info(
            "[perf] RequestSummaryRail(record_only) attached to subagent id=%s",
            getattr(getattr(subagent, "card", None), "id", None),
        )

    run_perf_safe(_COMPONENT, "attach RequestSummaryRail to subagent", _attach)
    return bool(attached["ok"])


def apply_create_subagent_perf_patch() -> None:
    """Wrap ``DeepAgent.create_subagent`` to attach record-only perf rail.

    Idempotent. Safe when perf.summary.enabled is false (attach is a no-op).
    """
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    try:
        from openjiuwen.harness.deep_agent import DeepAgent
    except Exception as exc:  # noqa: BLE001
        logger.warning("[perf] DeepAgent unavailable; skip create_subagent patch: %s", exc)
        return

    if getattr(DeepAgent, "perf_summary_subagent_patch_applied", False):
        _PATCH_APPLIED = True
        return

    _orig_create_subagent = DeepAgent.create_subagent

    def _create_subagent_with_perf(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        subagent = _orig_create_subagent(self, *args, **kwargs)
        try:
            attach_request_summary_rail(subagent)
        except Exception as exc:  # noqa: BLE001 — never break spawn
            logger.warning("[perf] create_subagent rail attach skipped: %s", exc)
        return subagent

    DeepAgent.create_subagent = _create_subagent_with_perf  # type: ignore[method-assign]
    DeepAgent.perf_summary_subagent_patch_applied = True
    _PATCH_APPLIED = True
    logger.info("[perf] DeepAgent.create_subagent RequestSummaryRail patch applied")


__all__ = [
    "attach_request_summary_rail",
    "apply_create_subagent_perf_patch",
]
