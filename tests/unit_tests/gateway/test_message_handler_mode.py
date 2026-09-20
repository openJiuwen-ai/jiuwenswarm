# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P3.1 单元测试：MessageHandler.handle_mode_switch 查表分发。

守两条门控：
1. 前置白名单 ``_VALID_MODE_INPUTS = NEW_CANONICAL_MODES | DEPRECATION_MAP.keys()``
   - 新 canonical 直通；旧 canonical / legacy 别名也直通（不报「非法指令」）。
2. 分发查表 ``deprecate_mode(mode_str) → ChannelMode(new_mode_str)``
   - 旧 canonical 静默映射到新 canonical；新模式在 ChannelMode 枚举里直接构造。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.mode_matrix import (
    DEPRECATION_MAP,
    MODE_ALIASES,
    NEW_AGENT_WORK_NORMAL,
    NEW_AGENT_WORK_PLAN,
    NEW_AGENT_CODE_NORMAL,
    NEW_AGENT_CODE_PLAN,
    NEW_TEAM_WORK_NORMAL,
    NEW_TEAM_WORK_PLAN,
    NEW_TEAM_CODE_NORMAL,
    NEW_TEAM_CODE_PLAN,
    NEW_CANONICAL_MODES,
)
from jiuwenswarm.gateway.message_handler.message_handler import (
    ChannelControlState,
    ChannelMode,
    MessageHandler,
    _VALID_MODE_INPUTS,
)
from jiuwenswarm.common.schema.message import Message


class _FakeAgentClient:
    @staticmethod
    async def send_request(env: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True, payload={})

    @staticmethod
    async def send_request_stream(env: object):
        if False:  # pragma: no cover
            yield env


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls) -> "_TestMessageHandler":
        # 清掉单例 initialized flag，让 __init__ 跑完整初始化（_get_config_raw 等
        # 实例属性在 set_outbound_pipeline 才赋值，这里直接 stub 上去）。
        setattr(MessageHandler, "_instance", None)
        setattr(cls, "_instance", None)
        if "_singleton_initialized" in MessageHandler.__dict__:
            del MessageHandler._singleton_initialized
        if "_singleton_initialized" in cls.__dict__:
            del cls._singleton_initialized
        handler = cls(_FakeAgentClient())
        handler.published = []
        # _get_config_raw 由 set_outbound_pipeline 赋值；测试不调 pipeline，
        # 这里直接给个返回空配置的 stub 供 _get_channel_default_state 调用。
        handler._get_config_raw = lambda: {}  # type: ignore[assignment]
        return handler

    async def publish_robot_messages(self, msg: object) -> None:
        self.published.append(msg)


# ── 分发查表：旧 → 新 canonical ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("input_mode", "expected_state"),
    [
        # 新 canonical 直通
        (NEW_AGENT_WORK_NORMAL, "agent.work.normal"),
        (NEW_AGENT_WORK_PLAN, "agent.work.plan"),
        (NEW_AGENT_CODE_NORMAL, "agent.code.normal"),
        (NEW_AGENT_CODE_PLAN, "agent.code.plan"),
        (NEW_TEAM_WORK_NORMAL, "team.work.normal"),
        (NEW_TEAM_WORK_PLAN, "team.work.plan"),
        (NEW_TEAM_CODE_NORMAL, "team.code.normal"),
        (NEW_TEAM_CODE_PLAN, "team.code.plan"),
        # 旧 canonical → 新 canonical（DEPRECATION_MAP）
        ("agent", "agent.work.normal"),
        ("agent.plan", "agent.work.plan"),
        ("agent.fast", "agent.work.normal"),
        ("code", "agent.code.normal"),       # 裸 code 语义等价旧 code.normal（review 回归）
        ("code.normal", "agent.code.normal"),
        ("code.plan", "agent.code.plan"),
        ("code.team", "team.code.normal"),
        ("team", "team.work.normal"),
        ("team.plan.normal", "team.work.plan"),
        ("team.plan.code", "team.code.plan"),
        # legacy 别名经 canonicalize → DEPRECATION_MAP 两步映射
        ("team.plan", "team.work.plan"),   # team.plan → team.plan.normal → team.work.plan
        ("team.code", "team.code.normal"),  # team.code → code.team → team.code.normal
    ],
)
def test_mode_switch_uses_deprecation_map(input_mode: str, expected_state: str) -> None:
    """handle_mode_switch 走 deprecate_mode + ChannelMode 查表分发。"""
    handler = _TestMessageHandler.create()
    state = ChannelControlState()

    # user_infos=None 跳过通知调度，单测只校验 state.mode 写入。
    processed = handler.handle_mode_switch(input_mode, state=state)

    assert processed is True
    assert state.mode.value == expected_state
    # 状态字段必须落在 ChannelMode 枚举内（P2 已加 8 个新成员）
    assert isinstance(state.mode, ChannelMode)


def test_mode_switch_unknown_input_does_not_touch_state() -> None:
    """非法输入不写 state.mode，且方法返回 True（消息已被消费）。"""
    handler = _TestMessageHandler.create()
    state = ChannelControlState(mode=ChannelMode.AGENT)
    before = state.mode

    processed = handler.handle_mode_switch("totally.bogus.mode", state=state)

    assert processed is True
    assert state.mode is before
    assert state.mode == ChannelMode.AGENT


def test_valid_mode_inputs_covers_new_and_legacy() -> None:
    """_VALID_MODE_INPUTS 必须是新 canonical、旧 canonical、正式别名的并集。"""
    assert NEW_CANONICAL_MODES <= _VALID_MODE_INPUTS
    assert set(DEPRECATION_MAP.keys()) <= _VALID_MODE_INPUTS
    assert set(MODE_ALIASES.keys()) <= _VALID_MODE_INPUTS
    # 额外 sanity：8 + 10 + 2 = 20（DEPRECATION_MAP 含裸 code），三集合互不相交
    assert len(_VALID_MODE_INPUTS) == (
        len(NEW_CANONICAL_MODES)
        + len(DEPRECATION_MAP.keys())
        + len(MODE_ALIASES.keys())
    )


# ── 默认 channel state 走 deprecate_mode 查表（P3.1 第二份白名单） ─────────
# 注：default_state 与 mode_switch 的 15+11=26 条参数化几乎一一对应，
# ``test_mode_switch_uses_deprecation_map`` 已覆盖旧/新 canonical 全表。
# 此处只留两条 switch 不覆盖的差异点：空串兜底为 agent.work.normal、
# 非法串不映射 + 不在 NEW_CANONICAL_MODES → 落到 ChannelMode.AGENT。

@pytest.mark.parametrize(
    ("default_mode_raw", "expected_state"),
    [
        # 空 / 缺失 → mode_raw 兜底为 "agent" → deprecate → agent.work.normal
        ("", "agent.work.normal"),
        # 非法 → deprecate_mode 不映射 → 不在 NEW_CANONICAL_MODES → 兜底 AGENT
        ("nonsense", "agent"),
    ],
)
def test_get_channel_default_state_uses_deprecate_mode(
    default_mode_raw: str,
    expected_state: str,
) -> None:
    """_get_channel_default_state 不再手抄 mode_map，改用 deprecate_mode + ChannelMode 构造。"""
    handler = _TestMessageHandler.create()

    def _fake_config() -> dict:
        return {
            "channels": {
                "feishu": {"default_mode": default_mode_raw, "default_session_id": ""},
            }
        }

    handler._get_config_raw = _fake_config  # type: ignore[assignment]
    state = handler._get_channel_default_state("feishu")
    assert state.mode.value == expected_state


# ── /switch 子指令（SWITCH_OK 分支）──────────────────────────────────────
# /switch plan|fast|normal|team 的判据查 state.mode 经 handle_mode_switch 落定后
# 的新 canonical（_SWITCH_AGENT_WORK_MODES / _SWITCH_CODE_MODES）：
# - plan / fast：agent.work.* 保持 agent.work.normal（plan/fast 已合并）；
#   code profile 下 plan → agent.code.plan。
# - normal：code profile → agent.code.normal。
# - team：code profile → team.code.normal。
# work profile 的 normal / team、以及 fast 对 code profile 均无分支 → 非法指令，
# state.mode 保持原值。

_SWITCH_OK_CASES: list[tuple[str, str, str | None]] = [
    # (start_mode, /switch 子指令, 期望 canonical；None 表示非法指令 state 不变)
    # agent work profile：plan / fast 保持 agent.work.normal
    (NEW_AGENT_WORK_NORMAL, "plan", NEW_AGENT_WORK_NORMAL),
    (NEW_AGENT_WORK_PLAN, "plan", NEW_AGENT_WORK_NORMAL),
    (NEW_AGENT_WORK_NORMAL, "fast", NEW_AGENT_WORK_NORMAL),
    (NEW_AGENT_WORK_PLAN, "fast", NEW_AGENT_WORK_NORMAL),
    # code profile：/switch plan → agent.code.plan
    (NEW_AGENT_CODE_NORMAL, "plan", NEW_AGENT_CODE_PLAN),
    (NEW_AGENT_CODE_PLAN, "plan", NEW_AGENT_CODE_PLAN),
    (NEW_TEAM_CODE_NORMAL, "plan", NEW_AGENT_CODE_PLAN),
    # code profile：/switch normal → agent.code.normal
    (NEW_AGENT_CODE_NORMAL, "normal", NEW_AGENT_CODE_NORMAL),
    (NEW_AGENT_CODE_PLAN, "normal", NEW_AGENT_CODE_NORMAL),
    (NEW_TEAM_CODE_NORMAL, "normal", NEW_AGENT_CODE_NORMAL),
    # code profile：/switch team → team.code.normal
    (NEW_AGENT_CODE_NORMAL, "team", NEW_TEAM_CODE_NORMAL),
    (NEW_AGENT_CODE_PLAN, "team", NEW_TEAM_CODE_NORMAL),
    (NEW_TEAM_CODE_NORMAL, "team", NEW_TEAM_CODE_NORMAL),
    # 无对应 profile 分支 → 非法指令，state.mode 不变
    (NEW_AGENT_WORK_NORMAL, "normal", None),
    (NEW_AGENT_WORK_NORMAL, "team", None),
    (NEW_AGENT_WORK_PLAN, "normal", None),
    (NEW_AGENT_WORK_PLAN, "team", None),
    (NEW_AGENT_CODE_NORMAL, "fast", None),
    (NEW_AGENT_CODE_PLAN, "fast", None),
    (NEW_TEAM_WORK_NORMAL, "plan", None),
    (NEW_TEAM_WORK_NORMAL, "fast", None),
    (NEW_TEAM_WORK_NORMAL, "normal", None),
    (NEW_TEAM_WORK_NORMAL, "team", None),
    (NEW_TEAM_CODE_PLAN, "plan", None),
]


def _control_message(query: str, channel_id: str = "feishu") -> Message:
    return Message(
        id="switch-test",
        type="req",
        channel_id=channel_id,
        session_id=None,
        params={"query": query},
        timestamp=0.0,
        ok=True,
        provider=channel_id,
    )


@pytest.mark.parametrize(
    ("start_mode", "switch_subcommand", "expected"),
    _SWITCH_OK_CASES,
)
def test_switch_ok_dispatches_to_target_canonical(
    start_mode: str,
    switch_subcommand: str,
    expected: str | None,
) -> None:
    """/switch 各子指令按当前 canonical 落到目标新 canonical（或非法指令不写 state）。"""
    handler = _TestMessageHandler.create()
    state = ChannelControlState(mode=ChannelMode(start_mode))
    handler._channel_states["feishu"] = state

    async def _drive() -> bool:
        processed = await handler._handle_channel_control(
            _control_message(f"/switch {switch_subcommand}")
        )
        # 让 SWITCH_OK 分支的 create_task 通知协程跑完，避免残留 pending task
        await asyncio.sleep(0)
        return processed

    processed = asyncio.run(_drive())

    assert processed is True
    assert isinstance(state.mode, ChannelMode)
    assert state.mode.value == (expected if expected is not None else start_mode)


@pytest.mark.asyncio
async def test_persist_creates_locked_session_and_forwards_first_task(monkeypatch) -> None:
    handler = _TestMessageHandler.create()
    state = ChannelControlState(
        session_id="old-session",
        mode=ChannelMode.AGENT_WORK_NORMAL,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(handler, "get_or_create_channel_state", lambda _msg: state)
    monkeypatch.setattr(handler._join_exit, "sender_has_joined", lambda _msg: False)

    async def _allocate(_msg, target_state, *, persist_session=False):
        captured["persist_session"] = persist_session
        target_state.session_id = "persist-session"
        return "persist-session"

    async def _cancel_and_notice(params, _msg):
        captured["old_sid"] = params.old_sid
        captured["new_sid"] = params.new_sid

    monkeypatch.setattr(handler, "_allocate_channel_session", _allocate)
    monkeypatch.setattr(handler, "_new_session_cancel_and_notice", _cancel_and_notice)
    handler._gateway_hook_handler = None

    msg = _control_message("/persist 跟进发布\n重点关注回滚方案")
    msg.params["content"] = msg.params["query"]
    processed = await handler._handle_channel_control(msg)
    await asyncio.sleep(0)

    assert processed is False
    assert captured == {
        "persist_session": True,
        "old_sid": "old-session",
        "new_sid": "persist-session",
    }
    assert state.session_id == "persist-session"
    assert msg.session_id == "persist-session"
    assert msg.params["query"] == "跟进发布\n重点关注回滚方案"
    assert msg.params["content"] == msg.params["query"]
    assert msg.metadata["persist_session_first_task"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_error"),
    [
        ("/new_session", "创建新会话失败，请稍后重试"),
        ("/persist 跟进发布", "创建永续会话失败，请稍后重试"),
    ],
)
async def test_session_creation_failure_hides_internal_error(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
    command: str,
    expected_error: str,
) -> None:
    handler = _TestMessageHandler.create()
    state = ChannelControlState(
        session_id="old-session",
        mode=ChannelMode.AGENT_WORK_NORMAL,
    )
    notices: list[object] = []

    monkeypatch.setattr(handler, "get_or_create_channel_state", lambda _msg: state)
    monkeypatch.setattr(handler._join_exit, "sender_has_joined", lambda _msg: False)

    async def _allocate(*_args, **_kwargs):
        raise RuntimeError("secret-database-path")

    async def _notice(_user_infos, _channel, _session_id, content):
        notices.append(content)

    monkeypatch.setattr(handler, "_allocate_channel_session", _allocate)
    monkeypatch.setattr(handler, "send_channel_notice", _notice)
    target_logger = logging.getLogger(
        "jiuwenswarm.gateway.message_handler.message_handler"
    )
    target_logger.addHandler(caplog.handler)
    caplog.set_level(logging.ERROR, logger=target_logger.name)
    try:
        processed = await handler._handle_channel_control(_control_message(command))
        await asyncio.sleep(0)
    finally:
        target_logger.removeHandler(caplog.handler)

    assert processed is True
    assert state.session_id == "old-session"
    assert notices == [{"error": expected_error}]
    assert "secret-database-path" not in str(notices)
    assert "secret-database-path" in caplog.text
