"""回归测试：判死级联收流。

修复前：死亡探针/停摆看门狗补发失败终态后只广播、不动运行时——首请求流
继续活着，追问被判 follow-up，真产出走旧流（帧带旧 request_id），前端
新 run 归属断链（先「本轮无响应」再永久「正在思考」）。

修复后：补终态后级联 park 运行时（pause_session_runtime），kernel
close_stream → runner 流自然结束 → 追问按新首请求走 RESUME_FROM_PAUSE；
pause 挂死（A3 ack 无超时）时降级 stop；流任务仍存活时兜底直杀。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_helpers, team_stall_watchdog


class _CascadeFakeManager:
    """级联路径的最小 TeamManager fake。"""

    def __init__(self, stream_alive: bool = True, pause_hangs: bool = False) -> None:
        self._stream_alive = stream_alive
        self._pause_hangs = pause_hangs
        self.pause_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.pop_calls = 0

    def has_stream_task(self, _session_id: str) -> bool:
        return self._stream_alive

    def get_monitor_handler(self, _session_id: str):
        return None

    async def pause_session_runtime(self, _session_id: str, reason: str = "") -> bool:
        self.pause_calls.append(reason)
        if self._pause_hangs:
            await asyncio.Event().wait()  # 模拟 A3：harness ack 永不返回
        self._stream_alive = False
        return True

    async def cancel_session_runtime(self, _session_id: str, reason: str = "") -> bool:
        self.stop_calls.append(reason)
        self._stream_alive = False
        return True

    def pop_stream_task(self, _session_id: str):
        self.pop_calls += 1
        return None


class _ProbeFakeManager:
    """死亡探针的最小 fake（级联函数被 patch 掉，无需 pause/stop）。"""

    def has_stream_task(self, _session_id: str) -> bool:
        return True

    def get_monitor_handler(self, _session_id: str):
        return None


@pytest.mark.asyncio
async def test_death_probe_triggers_terminal_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针判死补终态后必须触发级联收流（source=leader-death-probe）。"""
    monkeypatch.setattr(team_helpers, "_LEADER_ROUND_DEATH_PROBE_SEC", 0.02)
    monkeypatch.setattr(
        team_helpers, "get_team_manager", lambda _cid: _ProbeFakeManager()
    )
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", lambda *a, **k: asyncio.sleep(0)
    )
    cascade_calls: list[tuple] = []
    monkeypatch.setattr(
        team_helpers,
        "_cascade_park_runtime_after_terminal",
        lambda *a, **k: cascade_calls.append((a, k)),
    )

    task = team_helpers._schedule_leader_round_death_probe(
        "desktop",
        "s1",
        1,
        error_text="[181001] model call failed",
        liveness=lambda: 10,
        completion_signals=lambda: 0,
        previous=None,
    )
    await task
    assert len(cascade_calls) == 1
    args, kwargs = cascade_calls[0]
    assert args[:3] == ("desktop", "s1", 1)
    assert kwargs["source"] == "leader-death-probe"


@pytest.mark.asyncio
async def test_stall_watchdog_invokes_on_terminal_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """停摆看门狗判死：广播失败终态后必须调注入的 on_terminal。"""
    monkeypatch.setattr(team_stall_watchdog, "TEAM_STALL_WATCHDOG_SEC", 0.02)
    monkeypatch.setattr(team_stall_watchdog, "TEAM_STALL_CONFIRM_WINDOWS", 1)
    monkeypatch.setattr(
        team_stall_watchdog,
        "get_team_manager",
        lambda _cid: _ProbeFakeManager(),
    )

    async def _progress(_cid, _sid):
        return (0, 1, False)  # 零在途 + 有 pending + 窗口内无流转

    async def _liveness(_cid, _sid):
        return False  # leader kernel 确停

    async def _db_progress(_cid, _sid):
        return None  # DB 对照不可用，退回 live-only

    monkeypatch.setattr(team_stall_watchdog, "team_progress_snapshot", _progress)
    monkeypatch.setattr(team_stall_watchdog, "team_runtime_liveness", _liveness)
    monkeypatch.setattr(team_stall_watchdog, "team_progress_snapshot_db", _db_progress)

    broadcasted: list[dict] = []

    async def _broadcast(_cid, _sid, event):
        broadcasted.append(event)

    terminal_calls: list[str] = []

    task = team_stall_watchdog.schedule_team_stall_watchdog(
        "desktop",
        "s1",
        1,
        liveness=lambda: 5,
        completion_signals=lambda: 0,
        broadcast=_broadcast,
        on_terminal=lambda: terminal_calls.append("fired"),
    )
    await task
    assert len(broadcasted) == 1
    assert broadcasted[0]["event_type"] == "chat.processing_status"
    assert broadcasted[0]["is_complete"] is True
    assert "停摆" in broadcasted[0]["error"]
    assert terminal_calls == ["fired"]


@pytest.mark.asyncio
async def test_cascade_pauses_runtime_and_ends_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """级联主路：pause 成功、流任务消失、不走 stop 兜底。"""
    manager = _CascadeFakeManager(stream_alive=True)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    task = team_helpers._cascade_park_runtime_after_terminal(
        "desktop", "s1", 1, source="leader-death-probe"
    )
    await task
    assert len(manager.pause_calls) == 1
    assert "leader-death-probe" in manager.pause_calls[0]
    assert manager.stop_calls == []
    assert manager.pop_calls == 0  # 流已被 pause 路径收掉，无兜底直杀


@pytest.mark.asyncio
async def test_cascade_falls_back_to_stop_when_pause_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pause 挂死（A3）超时时降级 stop，保证流一定被收掉。"""
    monkeypatch.setattr(team_helpers, "_TERMINAL_CASCADE_PAUSE_TIMEOUT_SEC", 0.02)
    manager = _CascadeFakeManager(stream_alive=True, pause_hangs=True)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    task = team_helpers._cascade_park_runtime_after_terminal(
        "desktop", "s1", 1, source="stall-watchdog"
    )
    await task
    assert len(manager.pause_calls) == 1
    assert len(manager.stop_calls) == 1
    assert "pause-timeout-fallback" in manager.stop_calls[0]


@pytest.mark.asyncio
async def test_cascade_noop_when_stream_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流已不在（正常收尾竞态）→ 级联空转，不 pause 不 stop。"""
    manager = _CascadeFakeManager(stream_alive=False)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _cid: manager)

    task = team_helpers._cascade_park_runtime_after_terminal(
        "desktop", "s1", 1, source="leader-death-probe"
    )
    await task
    assert manager.pause_calls == []
    assert manager.stop_calls == []


class _FreshStartFakeManager:
    """fresh-start 判定的最小 fake。"""

    def __init__(self, stream_alive: bool, round_terminal: bool) -> None:
        self._stream_alive = stream_alive
        self._round_terminal = round_terminal
        self.stop_calls: list[str] = []
        self.cleared: list[str] = []

    def has_stream_task(self, _session_id: str) -> bool:
        return self._stream_alive

    def is_stream_round_terminal(self, _session_id: str) -> bool:
        return self._round_terminal

    async def cancel_session_runtime(self, _session_id: str, reason: str = "") -> bool:
        self.stop_calls.append(reason)
        self._stream_alive = False
        return True

    def session_degraded_detail(self, _session_id: str) -> str | None:
        return None

    def mark_session_degraded(self, _session_id: str, detail: str = "") -> None:
        pass

    def clear_session_initialized(self, _session_id: str) -> None:
        self.cleared.append("initialized")

    def clear_waiters(self, _session_id: str) -> None:
        self.cleared.append("waiters")

    def clear_stream_round_terminal(self, _session_id: str) -> None:
        self.cleared.append("terminal")
        self._round_terminal = False


@pytest.mark.asyncio
async def test_fresh_start_when_stream_alive_and_round_terminal() -> None:
    """流活着且回合已终态 → stop 运行时 + 清标记，消息按新流首请求处理。

    stop 而非 pause：cold recover 重建是唯一实证可靠的重入路径。
    """
    manager = _FreshStartFakeManager(stream_alive=True, round_terminal=True)
    result = await team_helpers._fresh_start_if_round_terminal(manager, "s1", "再问一个")
    assert result is True
    assert len(manager.stop_calls) == 1
    assert "fresh start" in manager.stop_calls[0]
    assert manager.cleared == ["initialized", "waiters", "terminal"]


@pytest.mark.asyncio
async def test_no_fresh_start_when_round_still_running() -> None:
    """流活着但回合未终态（团队在跑）→ 维持 follow-up，不动运行时。"""
    manager = _FreshStartFakeManager(stream_alive=True, round_terminal=False)
    result = await team_helpers._fresh_start_if_round_terminal(manager, "s1", "催一下")
    assert result is False
    assert manager.stop_calls == []
    assert manager.cleared == []


@pytest.mark.asyncio
async def test_no_fresh_start_when_stream_gone() -> None:
    """流已收（正常收尾）→ 无需处理，走既有 first-request 判定。"""
    manager = _FreshStartFakeManager(stream_alive=False, round_terminal=True)
    result = await team_helpers._fresh_start_if_round_terminal(manager, "s1", "问")
    assert result is False
    assert manager.stop_calls == []


@pytest.mark.asyncio
async def test_no_fresh_start_for_interactive_input() -> None:
    """HITL 答案必须走既有暂停恢复链，不得被 fresh-start 拦截。"""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    manager = _FreshStartFakeManager(stream_alive=True, round_terminal=True)
    result = await team_helpers._fresh_start_if_round_terminal(
        manager, "s1", InteractiveInput()
    )
    assert result is False
    assert manager.stop_calls == []


@pytest.mark.asyncio
async def test_stall_watchdog_not_established_falls_back_and_still_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 对照抛 TeamNotEstablishedError（live 视图仍有 pending 的滞后形态）：
    退回 live-only 判定并保留击杀能力——不得静默停守（静默停守=零收口，
    settle 复核在"leader 不再产出 final"的形态下不会被调度）。
    """
    from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
        TeamNotEstablishedError,
    )

    monkeypatch.setattr(team_stall_watchdog, "TEAM_STALL_WATCHDOG_SEC", 0.02)
    monkeypatch.setattr(team_stall_watchdog, "TEAM_STALL_CONFIRM_WINDOWS", 1)
    monkeypatch.setattr(
        team_stall_watchdog,
        "get_team_manager",
        lambda _cid: _ProbeFakeManager(),
    )

    async def _progress(_cid, _sid):
        return (0, 1, False)  # live 视图：零在途 + 有 pending + 无流转

    async def _liveness(_cid, _sid):
        return False

    async def _db_not_established(_cid, _sid):
        raise TeamNotEstablishedError("team_info has no row")

    monkeypatch.setattr(team_stall_watchdog, "team_progress_snapshot", _progress)
    monkeypatch.setattr(team_stall_watchdog, "team_runtime_liveness", _liveness)
    monkeypatch.setattr(
        team_stall_watchdog, "team_progress_snapshot_db", _db_not_established
    )

    broadcasted: list[dict] = []

    async def _broadcast(_cid, _sid, event):
        broadcasted.append(event)

    terminal_calls: list[str] = []
    task = team_stall_watchdog.schedule_team_stall_watchdog(
        "desktop",
        "s1",
        1,
        liveness=lambda: 5,
        completion_signals=lambda: 0,
        broadcast=_broadcast,
        on_terminal=lambda: terminal_calls.append("fired"),
    )
    await task
    # 退回 live-only 后按既有路径击杀：广播失败终态 + on_terminal 级联
    assert len(broadcasted) == 1
    assert broadcasted[0]["event_type"] == "chat.processing_status"
    assert broadcasted[0].get("error")
    assert terminal_calls == ["fired"]
