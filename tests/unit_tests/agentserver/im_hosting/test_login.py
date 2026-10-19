"""ImLoginManager / FeishuLoginStrategy 单元测试（不拉真实 CLI 子进程）。"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.im.im_hosting import login as login_mod
from jiuwenswarm.server.im.im_hosting.login import (
    DeviceAuthInfo,
    ImCliUnavailableError,
    ImLoginError,
    ImLoginManager,
    ImLoginUnsupportedError,
    PHASE_APP_SETUP,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_LOGGED_IN,
    PHASE_USER_AUTH,
)


class _FakeStrategy:
    CLI_NAME = "lark-cli"

    def __init__(self, probe_results: list[str]) -> None:
        self._probe_results = list(probe_results)
        self.calls: list[tuple] = []

    async def probe(self, cli_path: str) -> str:
        self.calls.append(("probe",))
        return self._probe_results.pop(0)

    async def begin_app_setup(self, cli_path: str):
        self.calls.append(("begin_app_setup",))
        return "https://example.com/config", object()

    async def begin_user_auth(self, cli_path: str) -> DeviceAuthInfo:
        self.calls.append(("begin_user_auth",))
        return DeviceAuthInfo(device_code="dc_1", verification_url="https://example.com/verify")

    async def finish_user_auth(self, cli_path: str, device_code: str) -> None:
        self.calls.append(("finish_user_auth", device_code))


def _manager(strategy: _FakeStrategy) -> ImLoginManager:
    return ImLoginManager(
        strategies={"feishu": strategy},
        cli_path_resolver=lambda channel_id: "C:/fake/lark-cli.cmd",
    )


@pytest.fixture
def fake_wait_proc(monkeypatch):
    """app_setup 阶段对阻塞子进程的等待替换为立即成功退出。"""

    async def _fake(proc, *, timeout_s: float, cli_name: str) -> int:
        return 0

    monkeypatch.setattr(login_mod, "_wait_proc", _fake)


@pytest.mark.asyncio
async def test_two_phase_run_reaches_logged_in(fake_wait_proc):
    strategy = _FakeStrategy(["need_app_setup", "need_user_auth", "logged_in"])
    mgr = _manager(strategy)
    first = await mgr.start("feishu")
    assert first["phase"] == PHASE_IDLE  # monitor 尚未探活
    task = mgr._sessions["feishu"].task
    assert task is not None
    await task
    final = await mgr.status("feishu")
    assert final["phase"] == PHASE_LOGGED_IN
    assert final["url"] == "https://example.com/verify"
    assert [c[0] for c in strategy.calls] == [
        "probe",
        "begin_app_setup",
        "probe",
        "begin_user_auth",
        "finish_user_auth",
        "probe",
    ]


@pytest.mark.asyncio
async def test_user_auth_only_when_app_configured(fake_wait_proc):
    strategy = _FakeStrategy(["need_user_auth", "logged_in"])
    mgr = _manager(strategy)
    task_holder: dict[str, object] = {}

    async def _start() -> None:
        await mgr.start("feishu")
        task_holder["task"] = mgr._sessions["feishu"].task

    await _start()
    await task_holder["task"]  # type: ignore[misc]
    final = await mgr.status("feishu")
    assert final["phase"] == PHASE_LOGGED_IN
    assert ("begin_app_setup",) not in strategy.calls


@pytest.mark.asyncio
async def test_start_single_flight_reuses_running_session(fake_wait_proc):
    strategy = _FakeStrategy(["need_user_auth", "logged_in"])
    mgr = _manager(strategy)
    first = await mgr.start("feishu")
    task = mgr._sessions["feishu"].task
    second = await mgr.start("feishu")
    assert second["started_at_ms"] == first["started_at_ms"]
    assert task is not None
    await task
    # 第二次 start 不应重复探活：仍是 monitor 自己的 2 次 probe。
    assert strategy.calls.count(("probe",)) == 2


@pytest.mark.asyncio
async def test_strategy_failure_lands_failed_phase():
    strategy = _FakeStrategy(["need_user_auth"])

    async def _boom(cli_path: str) -> DeviceAuthInfo:
        raise ImLoginError("cli 崩了")

    strategy.begin_user_auth = _boom  # type: ignore[method-assign]
    mgr = _manager(strategy)
    await mgr.start("feishu")
    task = mgr._sessions["feishu"].task
    assert task is not None
    await task
    final = await mgr.status("feishu")
    assert final["phase"] == PHASE_FAILED
    assert final["error"] == "cli 崩了"


@pytest.mark.asyncio
async def test_start_can_retry_after_terminal_phase(fake_wait_proc):
    strategy = _FakeStrategy(["need_user_auth", "logged_in"])
    mgr = _manager(strategy)
    await mgr.start("feishu")
    task = mgr._sessions["feishu"].task
    assert task is not None
    await task
    assert (await mgr.status("feishu"))["phase"] == PHASE_LOGGED_IN
    # 终态后允许重新发起（新会话）。
    strategy.calls.clear()
    strategy._probe_results = ["logged_in"]
    restarted = await mgr.start("feishu")
    assert restarted["started_at_ms"] >= 0
    task2 = mgr._sessions["feishu"].task
    assert task2 is not None
    await task2
    assert (await mgr.status("feishu"))["phase"] == PHASE_LOGGED_IN


@pytest.mark.asyncio
async def test_unsupported_channel_raises_with_code():
    mgr = ImLoginManager(strategies={}, cli_path_resolver=lambda cid: "x")
    with pytest.raises(ImLoginUnsupportedError) as excinfo:
        await mgr.start("dingtalk")
    assert excinfo.value.code == "IM_LOGIN_UNSUPPORTED"


@pytest.mark.asyncio
async def test_cli_unavailable_raises_with_code():
    mgr = ImLoginManager(
        strategies={"feishu": _FakeStrategy([])},
        cli_path_resolver=lambda cid: None,
    )
    with pytest.raises(ImCliUnavailableError) as excinfo:
        await mgr.start("feishu")
    assert excinfo.value.code == "IM_CLI_UNAVAILABLE"


@pytest.mark.asyncio
async def test_status_without_session_probes():
    mgr = _manager(_FakeStrategy(["logged_in"]))
    assert (await mgr.status("feishu"))["phase"] == PHASE_LOGGED_IN

    mgr2 = _manager(_FakeStrategy(["need_user_auth"]))
    payload = await mgr2.status("feishu")
    assert payload["phase"] == PHASE_IDLE
    assert payload["url"] is None


@pytest.mark.asyncio
async def test_feishu_probe_classifies_payloads(monkeypatch):
    strategy = login_mod.FeishuLoginStrategy()

    def _patch(stdout: str):
        async def _fake(cli_path, args, *, timeout_s, cli_name):
            return 0, stdout, ""

        monkeypatch.setattr(login_mod, "_run_once", _fake)

    _patch(json.dumps({"ok": True}))
    assert await strategy.probe("x") == login_mod._PROBE_LOGGED_IN
    _patch(json.dumps({"ok": False, "error": {"type": "config", "subtype": "not_configured"}}))
    assert await strategy.probe("x") == login_mod._PROBE_NEED_APP_SETUP
    _patch(json.dumps({"ok": False, "error": {"type": "auth"}}))
    assert await strategy.probe("x") == login_mod._PROBE_NEED_USER_AUTH


@pytest.mark.asyncio
async def test_feishu_probe_raises_on_unparseable_failure(monkeypatch):
    strategy = login_mod.FeishuLoginStrategy()

    async def _fake(cli_path, args, *, timeout_s, cli_name):
        return 3, "garbage", "boom"

    monkeypatch.setattr(login_mod, "_run_once", _fake)
    with pytest.raises(ImLoginError):
        await strategy.probe("x")


@pytest.mark.asyncio
async def test_feishu_begin_user_auth_parses_nested_payload(monkeypatch):
    strategy = login_mod.FeishuLoginStrategy()
    stdout = json.dumps(
        {"data": {"device_code": "dc_9", "verification_url": "https://open.feishu.cn/verify?x=1"}}
    )

    async def _fake(cli_path, args, *, timeout_s, cli_name):
        return 0, stdout, ""

    monkeypatch.setattr(login_mod, "_run_once", _fake)
    info = await strategy.begin_user_auth("x")
    assert info.device_code == "dc_9"
    assert info.verification_url == "https://open.feishu.cn/verify?x=1"


@pytest.mark.asyncio
async def test_feishu_begin_user_auth_rejects_missing_url(monkeypatch):
    strategy = login_mod.FeishuLoginStrategy()

    async def _fake(cli_path, args, *, timeout_s, cli_name):
        return 0, json.dumps({"error": {"type": "config"}}), ""

    monkeypatch.setattr(login_mod, "_run_once", _fake)
    with pytest.raises(ImLoginError, match="验证链接"):
        await strategy.begin_user_auth("x")
