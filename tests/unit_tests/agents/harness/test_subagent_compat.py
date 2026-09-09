# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for OpenJiuWen subagent runtime compatibility."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.harness.subagent_runtime.control import SubagentControl
from openjiuwen.harness.subagent_runtime.models import (
    ResumeResult,
    SubagentStatus,
    SubagentStatusKind,
)
from openjiuwen.harness.subagent_runtime.status import StatusChannel
from openjiuwen.harness.subagent_runtime.status_events import (
    build_subagent_updated_payload,
)

from jiuwenswarm.agents.harness.common.tools.subagent_compat import (
    CompatibleSubagentControl,
    install_subagent_control_compat_patch,
)


class _RestoredInstance:
    def __init__(self) -> None:
        self.status = StatusChannel()

    def agent_status(self) -> SubagentStatus:
        return self.status.current()


@pytest.mark.asyncio
async def test_restored_subagent_is_reported_idle_and_accepts_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed instance has no queued turn and must be externally idle."""
    restored = _RestoredInstance()
    control = object.__new__(CompatibleSubagentControl)
    setattr(control, "_manager", SimpleNamespace(find=lambda subagent_id: restored))

    async def _resume(_self: SubagentControl, subagent_id: str) -> ResumeResult:
        assert subagent_id == "sub-1"
        return ResumeResult(status=SubagentStatus.pending_init(), restored=True)

    monkeypatch.setattr(SubagentControl, "resume", _resume)

    result = await control.resume("sub-1")

    assert result.status.kind is SubagentStatusKind.COMPLETED
    payload = build_subagent_updated_payload(
        subagent_id="sub-1",
        subagent_type="explore_agent",
        display_name="Explorer",
        role="Explore code",
        parent_session_id="parent-1",
        task_description="Inspect the repository",
        created_at_ms=1.0,
        updated_at_ms=2.0,
        closed_at_ms=None,
        status=restored.agent_status(),
        revision=restored.status.version(),
    )
    assert payload["status"] == "idle"
    assert payload["can_send_input"] is True
    assert payload["needs_resume"] is False


def test_install_patch_rebinds_control_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """New parent sessions must be created with the compatible control."""
    from openjiuwen.harness.tools.subagent import _control_registry

    monkeypatch.setattr(_control_registry, "SubagentControl", SubagentControl)

    install_subagent_control_compat_patch()

    assert _control_registry.SubagentControl is CompatibleSubagentControl
