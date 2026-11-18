# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for issue #6685 PR-B: subagent stop cascade (F-4) and
reason-bearing errored statuses (F-5)."""

from __future__ import annotations

import pytest
from openjiuwen.harness.subagent_runtime.models import (
    SubagentStatus,
    SubagentStatusKind,
)

from jiuwenswarm.agents.harness.common.tools.subagent_compat import (
    REASONLESS_ERROR_MESSAGE,
    CompatibleSubagentControl,
    with_error_reason,
)

# ---------------------------------------------------------------- F-5


def test_reasonless_errored_gets_a_reason():
    s = SubagentStatus(kind=SubagentStatusKind.ERRORED, message="")
    out = with_error_reason(s)
    assert out.kind is SubagentStatusKind.ERRORED
    assert out.message == REASONLESS_ERROR_MESSAGE
    assert out.error_code == "UNKNOWN"


def test_errored_with_message_untouched():
    s = SubagentStatus(kind=SubagentStatusKind.ERRORED, message="boom", error_code="E1")
    assert with_error_reason(s) is s


def test_non_errored_statuses_untouched():
    for status in (
        SubagentStatus.completed(),
        SubagentStatus(kind=SubagentStatusKind.RUNNING),
    ):
        assert with_error_reason(status) is status


@pytest.mark.asyncio
async def test_get_status_backfills_reason(monkeypatch):
    raw = SubagentStatus(kind=SubagentStatusKind.ERRORED, message=None)

    monkeypatch.setattr(
        "openjiuwen.harness.subagent_runtime.control.SubagentControl.get_status",
        lambda self, sid: raw,
        raising=True,
    )
    ctl = CompatibleSubagentControl.__new__(CompatibleSubagentControl)
    out = ctl.get_status("sub-1")
    assert out.kind is SubagentStatusKind.ERRORED
    assert (out.message or "").strip() != ""


@pytest.mark.asyncio
async def test_wait_backfills_reason_in_batch(monkeypatch):
    from openjiuwen.harness.subagent_runtime.models import WaitResult

    ok = SubagentStatus.completed()
    bad = SubagentStatus(kind=SubagentStatusKind.ERRORED, message="   ")

    async def fake_wait(self, ids, *a, **kw):
        return WaitResult(statuses={"a": ok, "b": bad}, results={}, output_files={}, timed_out=False)

    monkeypatch.setattr(
        "openjiuwen.harness.subagent_runtime.control.SubagentControl.wait",
        fake_wait,
        raising=True,
    )
    ctl = CompatibleSubagentControl.__new__(CompatibleSubagentControl)
    result = await ctl.wait(["a", "b"])
    assert result.statuses["a"] is ok
    assert result.statuses["b"].kind is SubagentStatusKind.ERRORED
    assert (result.statuses["b"].message or "").strip() != ""


# ---------------------------------------------------------------- F-4


def _load_stop_helper():
    """Load stop_session_subagents off the deep adapter class without
    constructing the adapter (import already exercised by the test env)."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    fn = JiuWenSwarmDeepAdapter.__dict__["stop_session_subagents"]
    return fn


class _FakeControl:
    def __init__(self, closed=None, fail=False):
        self.calls: list[str] = []
        self._closed = closed if closed is not None else ["sub-1", "sub-2"]
        self._fail = fail

    async def cancel_all(self, reason: str):
        self.calls.append(reason)
        if self._fail:
            raise RuntimeError("core exploded")
        return list(self._closed)


class _FakeSelf:
    def __init__(self, control, sid_key="sess-1"):
        self._control = control
        self._sid_key = sid_key
        self.cleared: list[str] = []
        self._instance = None
        self.deep_agent = type(
            "DA", (), {"_subagent_controls": {sid_key: control}}
        )()

    def _session_adapter_key(self, sid):
        return self._sid_key if sid else ""

    def get_live_session_instance(self, sid):
        return self.deep_agent

    def _clear_subagent_progress_batch(self, sid):
        self.cleared.append(sid)


@pytest.mark.asyncio
async def test_stop_cascades_cancel_all_and_reports_ids():
    fn = _load_stop_helper()
    ctl = _FakeControl(closed=["sub-1", "sub-2"])
    fake = _FakeSelf(ctl)
    out = await fn(fake, "sess-1")
    assert out == ["sub-1", "sub-2"]
    assert ctl.calls == ["user_cancel"]
    assert fake.cleared == ["sess-1"]


@pytest.mark.asyncio
async def test_stop_is_best_effort_on_core_failure():
    fn = _load_stop_helper()
    ctl = _FakeControl(fail=True)
    fake = _FakeSelf(ctl)
    out = await fn(fake, "sess-1")
    assert out == []


@pytest.mark.asyncio
async def test_stop_noop_without_control_or_session():
    fn = _load_stop_helper()
    ctl = _FakeControl()
    fake = _FakeSelf(ctl)
    assert await fn(fake, "") == []
    fake2 = _FakeSelf(ctl)
    fake2.deep_agent = type("DA", (), {})()  # no controls attr
    assert await fn(fake2, "sess-1") == []


class _StatefulControl(_FakeControl):
    """带状态面的 control 替身：list_live＋get_status.kind（str 枚举取 .value）。"""

    def __init__(self, kinds):
        super().__init__()
        self._kinds = kinds

    def list_live(self):
        return [type("M", (), {"subagent_id": sid})() for sid in self._kinds]

    def get_status(self, sid):
        kind = self._kinds[sid]
        return type("S", (), {"kind": type("K", (), {"value": kind})()})()


@pytest.mark.asyncio
async def test_stop_skips_purely_idle_resident_subagents():
    """P2 回归（atomgit 2026-11-18）：纯 idle 常驻子代理不得被停止级联强杀。

    #3625 常驻契约——常驻子代理存活到显式 close；cancel-before-send 常规流程
    下 stop 未停任何忙碌工作时，cancel_all 必须不触发。修复前：无条件强杀=红。
    """
    fn = _load_stop_helper()
    ctl = _StatefulControl({"sub-idle": "completed", "sub-wait": "interrupted"})
    fake = _FakeSelf(ctl)
    out = await fn(fake, "sess-1")
    assert out == []
    assert ctl.calls == []  # cancel_all 未被调用


@pytest.mark.asyncio
async def test_stop_cascades_when_any_subagent_busy():
    """P2 回归镜像：存在 running 忙碌活体时级联照旧触发（#6685 P-2 语义保持）。"""
    fn = _load_stop_helper()
    ctl = _StatefulControl({"sub-idle": "completed", "sub-run": "running"})
    fake = _FakeSelf(ctl)
    out = await fn(fake, "sess-1")
    assert out == ["sub-1", "sub-2"]
    assert ctl.calls == ["user_cancel"]
    assert fake.cleared == ["sess-1"]


@pytest.mark.asyncio
async def test_stop_conservative_when_control_lacks_status_api():
    """P2 回归兜底：最小 control 无 list_live/get_status（旧替身形态）时
    保守级联，行为不得静默回退为放行。"""
    fn = _load_stop_helper()
    ctl = _FakeControl(closed=["sub-x"])
    fake = _FakeSelf(ctl)
    out = await fn(fake, "sess-1")
    assert out == ["sub-x"]
    assert ctl.calls == ["user_cancel"]
