# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility fixes for the OpenJiuWen persistent subagent runtime."""

from __future__ import annotations

from dataclasses import replace

from openjiuwen.harness.subagent_runtime.control import (  # type: ignore[import-untyped]
    SubagentControl,
)
from openjiuwen.harness.subagent_runtime.models import (  # type: ignore[import-untyped]
    ResumeResult,
    SubagentStatus,
    SubagentStatusKind,
    WaitResult,
)
from openjiuwen.harness.tools.subagent import _control_registry  # type: ignore[import-untyped]

# An errored status whose carrier exception carried no message leaves the main
# agent unable to tell *why* the subagent died (it ends up reverse-engineering
# completion from file mtimes - issue #6685 P-4). Sanitize reasonless errored
# statuses at the compat boundary so errored always carries a reason.
REASONLESS_ERROR_MESSAGE = "unspecified runtime error (exception carried no message)"


def with_error_reason(status: SubagentStatus) -> SubagentStatus:
    """Backfill a reason on errored statuses that arrive without one."""
    if status.kind is SubagentStatusKind.ERRORED and not (status.message or "").strip():
        return replace(
            status,
            message=REASONLESS_ERROR_MESSAGE,
            error_code=status.error_code or "UNKNOWN",
        )
    return status


class CompatibleSubagentControl(SubagentControl):
    """Report a restored subagent as idle until its next turn is submitted."""

    async def resume(self, subagent_id: str) -> ResumeResult:
        result = await super().resume(subagent_id)
        if (
            not result.restored
            or result.status.kind is not SubagentStatusKind.PENDING_INIT
        ):
            return result

        instance = self._manager.find(subagent_id)  # pylint: disable=protected-access
        if instance is None:
            return result

        current = instance.agent_status()
        if current.kind is not SubagentStatusKind.PENDING_INIT:
            return replace(result, status=current)

        # OpenJiuWen represents a live, input-ready instance with a terminal
        # turn status; COMPLETED is mapped to the public ``idle`` state.
        idle = SubagentStatus.completed()
        await instance.status.set(idle)
        return replace(result, status=idle)

    def get_status(self, subagent_id: str) -> SubagentStatus:
        return with_error_reason(super().get_status(subagent_id))

    async def wait(self, subagent_ids, *args, **kwargs) -> WaitResult:
        result = await super().wait(subagent_ids, *args, **kwargs)
        if any(
            status.kind is SubagentStatusKind.ERRORED and not (status.message or "").strip()
            for status in result.statuses.values()
        ):
            result = replace(
                result,
                statuses={
                    sid: with_error_reason(status) for sid, status in result.statuses.items()
                },
            )
        return result


def install_subagent_control_compat_patch() -> None:
    """Use the compatible control for newly created parent-session runtimes."""
    _control_registry.SubagentControl = CompatibleSubagentControl


__all__ = [
    "CompatibleSubagentControl",
    "install_subagent_control_compat_patch",
    "with_error_reason",
]
