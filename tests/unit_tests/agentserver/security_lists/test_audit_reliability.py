# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""审计可靠性兜底（设计 5.7 / M5-3）：环形缓冲、30s 事件驱动重试、
恢复补写 audit_recovered、连续失败/缓冲水位健康告警、启动自检。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.security_lists import audit, notify


@pytest.fixture
def env(monkeypatch):
    """隔离可靠性状态；fake 写盘（可控成败+记录序列）+ 告警与假时钟记录器。"""
    audit._reset_for_test()
    writes: list[dict] = []
    state = SimpleNamespace(available=True)
    alerts: list[dict] = []
    clock = SimpleNamespace(now=1000.0)

    def fake_write(event):
        if state.available:
            writes.append(event)
            return True
        return False

    monkeypatch.setattr(audit, "_write_event", fake_write)
    monkeypatch.setattr(
        notify,
        "report_security_event_detached",
        lambda *, stage, detail: alerts.append({"stage": stage, "detail": detail}),
    )
    monkeypatch.setattr(audit, "time", SimpleNamespace(monotonic=lambda: clock.now))
    # 锚定重试时钟：log_event 默认不触发 flush，推进时钟 ≥30s 才重试
    audit._last_retry = clock.now
    yield SimpleNamespace(writes=writes, state=state, alerts=alerts, clock=clock)
    audit._reset_for_test()


def _fail(env, kind: str) -> bool:
    env.state.available = False
    return audit.log_event(kind)


# ---------------------------------------------------------------------------
# 缓冲与重试
# ---------------------------------------------------------------------------


def test_write_failure_buffers_event_and_returns_false(env):
    assert _fail(env, "k1") is False
    assert len(audit._buffer) == 1
    assert audit._buffer[0]["kind"] == "k1"
    assert audit._consecutive_failures == 1
    assert env.writes == []


def test_flush_after_30s_replays_in_order_then_recovers(env):
    _fail(env, "a")
    _fail(env, "b")
    assert len(audit._buffer) == 2

    env.state.available = True
    env.clock.now += 31
    assert audit.log_event("c") is True

    kinds = [e["kind"] for e in env.writes]
    assert kinds == ["a", "b", audit.AUDIT_RECOVERED, "c"]  # 缓冲按序先刷 + 恢复补写
    assert env.writes[2]["dropped"] == 0
    assert len(audit._buffer) == 0
    assert audit._consecutive_failures == 0
    assert audit._degraded is False


def test_no_retry_within_30s(env):
    _fail(env, "a")
    # 先制造一次成功 flush，把 last_retry 锚定到当前时钟
    env.state.available = True
    env.clock.now += 31
    audit.log_event("b")
    assert [e["kind"] for e in env.writes] == ["a", audit.AUDIT_RECOVERED, "b"]

    _fail(env, "c")  # 再次失败入缓冲
    env.state.available = True
    env.clock.now += 10  # 距上次重试仅 10s < 30s
    audit.log_event("d")
    kinds = [e["kind"] for e in env.writes]
    assert "c" not in kinds  # 缓冲未刷：30s 内不重复重试
    assert kinds[-1] == "d"
    assert [e["kind"] for e in audit._buffer] == ["c"]


def test_buffer_overflow_drops_oldest_and_counts(env):
    for i in range(audit._BUFFER_CAPACITY):
        audit._buffer.append({"kind": f"e{i}"})
    audit._dirty = True
    audit._degraded = True  # 抑制 80% 水位告警的连带 health 事件，聚焦溢出计数

    _fail(env, "new")
    assert audit._dropped == 1
    assert len(audit._buffer) == audit._BUFFER_CAPACITY
    assert audit._buffer[0]["kind"] == "e1"  # 最旧的 e0 被丢
    assert audit._buffer[-1]["kind"] == "new"


# ---------------------------------------------------------------------------
# 健康告警
# ---------------------------------------------------------------------------


def test_degraded_after_5_consecutive_failures(env):
    for i in range(4):
        _fail(env, f"e{i}")
    assert audit._degraded is False
    assert env.alerts == []

    _fail(env, "e5")
    assert audit._degraded is True
    assert len(env.alerts) == 1
    assert env.alerts[0]["stage"] == "security.degraded"
    # audit_health 写不进则入缓冲
    health = [e for e in audit._buffer if e["kind"] == audit.AUDIT_HEALTH]
    assert len(health) == 1
    assert health[0]["status"] == "degraded"
    assert health[0]["consecutive_failures"] == 5

    _fail(env, "e6")  # 已降级不重复告警
    assert len(env.alerts) == 1


def test_degraded_at_80_percent_buffer(env):
    for i in range(audit._BUFFER_WARN_COUNT):
        audit._buffer.append({"kind": f"e{i}"})
    audit._dirty = True

    _fail(env, "trigger")  # 仅 1 次失败，但缓冲占用 ≥80%
    assert audit._degraded is True
    assert len(env.alerts) == 1
    assert "缓冲占用" in env.alerts[0]["detail"]


def test_recovery_writes_recovered_with_dropped_and_clears_degraded(env):
    for i in range(5):
        _fail(env, f"e{i}")
    assert audit._degraded is True
    audit._dropped = 3  # 模拟此前溢出丢 3 条

    env.state.available = True
    env.clock.now += 31
    audit.log_event("x")

    recovered = [e for e in env.writes if e["kind"] == audit.AUDIT_RECOVERED]
    assert len(recovered) == 1
    assert recovered[0]["dropped"] == 3
    assert audit._degraded is False
    assert audit._dropped == 0
    assert audit._consecutive_failures == 0


def test_notify_error_does_not_propagate(env, monkeypatch):
    monkeypatch.setattr(
        notify,
        "report_security_event_detached",
        lambda **_: (_ for _ in ()).throw(RuntimeError("bridge down")),
    )
    for i in range(5):
        assert _fail(env, f"e{i}") is False  # 不抛异常
    assert audit._degraded is True


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------


def test_self_check_success_writes_audit_start(env):
    assert audit.self_check() is True
    assert [e["kind"] for e in env.writes] == [audit.AUDIT_START]
    assert audit._degraded is False


def test_self_check_failure_alerts_immediately(env):
    env.state.available = False
    assert audit.self_check() is False
    # 不等 5 次阈值：首次失败即降级告警
    assert audit._degraded is True
    assert len(env.alerts) == 1
    assert [e["kind"] for e in audit._buffer] == [audit.AUDIT_START, audit.AUDIT_HEALTH]
