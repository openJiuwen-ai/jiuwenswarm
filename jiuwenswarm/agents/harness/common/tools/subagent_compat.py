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
)
from openjiuwen.harness.tools.subagent import _control_registry  # type: ignore[import-untyped]


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


def install_subagent_control_compat_patch() -> None:
    """Use the compatible control for newly created parent-session runtimes."""
    _control_registry.SubagentControl = CompatibleSubagentControl


__all__ = [
    "CompatibleSubagentControl",
    "install_subagent_control_compat_patch",
]
