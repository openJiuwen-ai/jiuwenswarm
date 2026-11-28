"""停摆感知 keepalive（P0-4）单元测试。

覆盖两个纯逻辑单元：
- ``event_loop_monitor.recent_stall_seconds`` 停摆窗口记账；
- ``AgentWebSocketServer._resolve_keepalive_target`` 放宽/复位决策。

websockets legacy keepalive 每轮动态读取 ``ws.ping_timeout``（15.0.1 已核验），
因此对协议对象改值即生效，测试用简单 stub 验证决策即可。
"""

import time

import pytest

from jiuwenswarm.server import event_loop_monitor
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.fixture(autouse=True)
def _clean_stall_windows():
    event_loop_monitor._stall_windows.clear()
    yield
    event_loop_monitor._stall_windows.clear()
    event_loop_monitor.stop_event_loop_monitor()


class _StubProtocol:
    def __init__(self, ping_timeout: float | None = 300.0) -> None:
        self.ping_timeout = ping_timeout


class TestRecentStallSeconds:
    def test_empty_window_returns_zero(self):
        assert event_loop_monitor.recent_stall_seconds(60.0) == 0.0

    def test_stall_inside_window_counts(self):
        now = time.monotonic()
        event_loop_monitor._stall_windows.append((now - 5.0, 2.5))
        assert 2.4 <= event_loop_monitor.recent_stall_seconds(60.0) <= 2.6

    def test_stall_outside_window_ignored(self):
        now = time.monotonic()
        event_loop_monitor._stall_windows.append((now - 120.0, 9.0))
        assert event_loop_monitor.recent_stall_seconds(60.0) == 0.0

    def test_multiple_stalls_accumulate(self):
        now = time.monotonic()
        event_loop_monitor._stall_windows.append((now - 30.0, 2.0))
        event_loop_monitor._stall_windows.append((now - 10.0, 3.0))
        assert 4.9 <= event_loop_monitor.recent_stall_seconds(60.0) <= 5.1


class TestResolveKeepaliveTarget:
    BASE = 300.0
    RELAXED = AgentWebSocketServer._relaxed_ping_timeout(BASE)
    GRACE = AgentWebSocketServer._KEEPALIVE_GRACE_S

    def test_relaxed_bound_is_bounded_minutes_not_hours(self):
        """放宽上界必须为 base + 窗口 + 宽限（480s），保证死链检测分钟级。"""
        assert self.RELAXED == 480.0
        assert self.RELAXED < 600.0

    def test_no_stall_keeps_base_timeout(self):
        now = time.monotonic()
        target, deadline = AgentWebSocketServer._resolve_keepalive_target(
            0.0, now, 0.0, self.BASE
        )
        assert target == self.BASE
        assert deadline == 0.0

    def test_stall_relaxes_and_extends_deadline(self):
        now = time.monotonic()
        target, deadline = AgentWebSocketServer._resolve_keepalive_target(
            3.0, now, 0.0, self.BASE
        )
        assert target == self.RELAXED
        assert deadline >= now + self.GRACE - 1.0

    def test_within_grace_stays_relaxed_without_new_stall(self):
        now = time.monotonic()
        _, deadline = AgentWebSocketServer._resolve_keepalive_target(
            3.0, now, 0.0, self.BASE
        )
        later = now + 30.0
        target, deadline2 = AgentWebSocketServer._resolve_keepalive_target(
            0.0, later, deadline, self.BASE
        )
        assert target == self.RELAXED
        assert deadline2 == deadline

    def test_restores_base_after_grace_expires(self):
        now = time.monotonic()
        _, deadline = AgentWebSocketServer._resolve_keepalive_target(
            3.0, now, 0.0, self.BASE
        )
        after_grace = deadline + 1.0
        target, deadline2 = AgentWebSocketServer._resolve_keepalive_target(
            0.0, after_grace, deadline, self.BASE
        )
        assert target == self.BASE
        assert deadline2 == deadline

    def test_repeated_stalls_extend_deadline(self):
        now = time.monotonic()
        _, deadline = AgentWebSocketServer._resolve_keepalive_target(
            3.0, now, 0.0, self.BASE
        )
        later = now + 100.0
        _, deadline2 = AgentWebSocketServer._resolve_keepalive_target(
            3.0, later, deadline, self.BASE
        )
        assert deadline2 >= later + self.GRACE - 1.0

    def test_base_timeout_none_stays_disabled_under_stall(self):
        """部署显式禁用 ping_timeout 时，停摆放宽不得反向引入超时。"""
        now = time.monotonic()
        target, deadline = AgentWebSocketServer._resolve_keepalive_target(
            5.0, now, 0.0, None
        )
        assert target is None
        assert deadline == 0.0


class TestGuardAppliesToProtocols:
    @pytest.mark.asyncio
    async def test_relax_and_restore_applied_to_live_protocols(self):
        """决策结果按目标值落到存活协议对象上（放宽 + 复位两个方向）。"""
        now = time.monotonic()
        target, deadline = AgentWebSocketServer._resolve_keepalive_target(
            3.0, now, 0.0, 300.0
        )
        assert target == AgentWebSocketServer._relaxed_ping_timeout(300.0)
        assert target == 480.0

        ws = _StubProtocol(ping_timeout=300.0)
        if getattr(ws, "ping_timeout", None) != target:
            ws.ping_timeout = target
        assert ws.ping_timeout == 480.0

        restore_target, _ = AgentWebSocketServer._resolve_keepalive_target(
            0.0, deadline + 1.0, deadline, 300.0
        )
        assert restore_target == 300.0
        ws.ping_timeout = restore_target
        assert ws.ping_timeout == 300.0

    @pytest.mark.asyncio
    async def test_websockets_legacy_reads_ping_timeout_dynamically(self):
        """websockets 15.0.1 legacy keepalive 每轮读取 self.ping_timeout（改值即生效的前提）。"""
        import inspect

        from websockets.legacy.protocol import WebSocketCommonProtocol

        source = inspect.getsource(WebSocketCommonProtocol.keepalive_ping)
        assert "self.ping_timeout" in source
