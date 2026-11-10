# coding: utf-8
"""方向二（team → 默认模式）净化与指纹 fallback 单测。"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service as sos


def _user(rid, text, ts):
    return {"role": "user", "request_id": rid, "content": text, "timestamp": ts}


def _final(rid, text, ts, **extra):
    record = {
        "role": "assistant",
        "request_id": rid,
        "event_type": "chat.final",
        "content": text,
        "timestamp": ts,
    }
    record.update(extra)
    return record


def _member_tool(rid, ts):
    return {
        "role": "assistant",
        "request_id": rid,
        "event_type": "chat.tool_call",
        "content": "",
        "tool_call": {"name": "write_file", "arguments": "{}", "tool_call_id": "tc1"},
        "timestamp": ts,
    }


def _member_tool_result(rid, ts):
    return {
        "role": "assistant",
        "request_id": rid,
        "event_type": "chat.tool_result",
        "content": "",
        "tool_call_id": "tc1",
        "result": "ok",
        "timestamp": ts,
    }


def test_converter_drop_member_internals():
    records = [
        _user("r1", "帮我写报告", 1.0),
        _member_tool("member-tool-bob-1", 2.0),
        _member_tool_result("member-tool-bob-1", 3.0),
        _final("member-final-bob-2", "成员产出正文" * 30, 4.0, member_name="bob"),
        _final("r1", "leader 汇总答复", 5.0),
    ]

    messages, skipped = sos._build_context_messages_from_history(
        records, drop_member_internals=True
    )

    kinds = [type(m).__name__ for m in messages]
    # 成员工具事件不成 ToolMessage/AssistantMessage(tool_calls)
    assert "ToolMessage" not in kinds
    # 成员 final 折叠为归属注记（截断 200 字）
    notes = [m for m in messages if type(m).__name__ == "AssistantMessage"
             and str(getattr(m, "content", "")).startswith("（团队成员 bob 的产出）")]
    assert len(notes) == 1
    assert len(notes[0].content) < 250
    # leader final 与用户轮次原样保留
    assert any(getattr(m, "content", "") == "leader 汇总答复" for m in messages)
    assert any(type(m).__name__ == "UserMessage" and m.content == "帮我写报告" for m in messages)
    assert skipped >= 2  # 成员 tool_call + tool_result
    # 团队期检出 → 末尾追加身份复位注记（AssistantMessage 且为序列末条）
    last = messages[-1]
    assert type(last).__name__ == "AssistantMessage"
    assert last.content == sos._TEAM_IDENTITY_RESET_NOTE


def test_identity_reset_note_via_mode_team_only():
    """无成员归因记录但记录带 mode=team（老口径团队期）→ 同样追加复位注记。"""
    records = [
        {**_user("r1", "团队期问题", 1.0), "mode": "team"},
        {**_final("r1", "作为主理人，我们团队已完成。", 2.0), "mode": "team"},
    ]

    messages, _ = sos._build_context_messages_from_history(
        records, drop_member_internals=True
    )

    assert messages[-1].content == sos._TEAM_IDENTITY_RESET_NOTE
    # leader 答复原文保留（复位注记治理口吻延续，不做内容改写）
    assert any(getattr(m, "content", "") == "作为主理人，我们团队已完成。" for m in messages)


def test_identity_reset_note_absent_without_team_era():
    """纯默认模式历史（无 mode=team、无成员记录）→ 不追加注记。"""
    records = [
        _user("r1", "你好", 1.0),
        _final("r1", "你好！", 2.0),
    ]

    messages, _ = sos._build_context_messages_from_history(
        records, drop_member_internals=True
    )

    assert all(
        getattr(m, "content", "") != sos._TEAM_IDENTITY_RESET_NOTE for m in messages
    )


def test_identity_reset_note_absent_when_fidelity_mode():
    """保真路径（drop_member_internals=False，如 rewind）即使含团队期记录也不加注记。"""
    records = [
        {**_user("r1", "团队期问题", 1.0), "mode": "team"},
        _member_tool("member-tool-bob-1", 2.0),
    ]

    messages, _ = sos._build_context_messages_from_history(records)

    assert all(
        getattr(m, "content", "") != sos._TEAM_IDENTITY_RESET_NOTE for m in messages
    )


def test_converter_default_keeps_member_records():
    """默认（rewind 等保真路径）行为不变：成员工具事件照常转换。"""
    records = [
        _member_tool("member-tool-bob-1", 1.0),
        _member_tool_result("member-tool-bob-1", 2.0),
    ]

    messages, _ = sos._build_context_messages_from_history(records)

    kinds = [type(m).__name__ for m in messages]
    assert "ToolMessage" in kinds
    assert "AssistantMessage" in kinds


class _StubContext:
    def __init__(self):
        self.messages = ["x"]


class _StubContextEngine:
    def __init__(self):
        self.cleared = False

    def get_context(self, *, session_id):
        return _StubContext()

    async def clear_context(self, *, session_id):
        self.cleared = True


class _StubReactAgent:
    def __init__(self, engine):
        self.context_engine = engine


class _StubDeepAgent:
    def __init__(self, engine):
        self.react_agent = _StubReactAgent(engine)


@pytest.mark.asyncio
async def test_refresh_fingerprint_missing_falls_back_to_rebuild(monkeypatch):
    """指纹失效（rewind/历史轮换）时全量重建而非静默停更。"""
    engine = _StubContextEngine()
    deep_agent = _StubDeepAgent(engine)
    calls: dict[str, object] = {}

    async def _fake_restore(**kwargs):
        calls["restore_kwargs"] = kwargs
        return True

    async def _fake_persist(**kwargs):
        calls["persisted"] = True
        return True

    monkeypatch.setattr(sos, "_restore_session_context_from_history", _fake_restore)
    monkeypatch.setattr(sos, "_persist_session_context", _fake_persist)
    monkeypatch.setattr(sos, "resolve_live_agent_session", lambda *_a, **_k: object())
    monkeypatch.setattr(sos, "history_exists", lambda _sid: True)
    monkeypatch.setattr(sos, "load_history_tail_request_id",
                        lambda _sid, exclude_request_id=None: "tail-r2")
    monkeypatch.setattr(
        sos,
        "load_history_records",
        lambda _sid: [
            _user("r1", "旧问题", 1.0),
            _final("r1", "旧回答", 2.0),
            _user("r2", "新问题", 3.0),
            _final("r2", "新回答", 4.0),
        ],
    )

    result = await sos.refresh_session_context_if_stale(
        deep_agent=deep_agent,
        session_id="s-rebuild",
        synced_tail_request_id="r-not-in-history",
    )

    # 指纹失效 → 清窗 + 全量重建 + 持久化 + 指纹推进到磁盘尾
    assert engine.cleared is True
    assert calls.get("restore_kwargs", {}).get("drop_member_internals") is True
    assert calls.get("persisted") is True
    assert result == "tail-r2"


@pytest.mark.asyncio
async def test_refresh_rebuild_preserves_in_flight_tail(monkeypatch):
    """重建保留在途轮次：磁盘重建后，活窗尾部"磁盘未覆盖"的在途消息并回。

    HITL 挂起运行的本轮 query/段1/工具调用都在内存未落盘——清窗重建只
    恢复磁盘已完成轮（上一轮问候），不并回在途尾部则模型上下文缺本轮，
    续跑输出退化为问候复述（2026-11-07 a.py 轮实测）。
    """
    cleared: list = []
    added: list = []

    class _CE:
        def __init__(self):
            self.msgs: list = []

        def get_context(self, session_id=None):
            return self

        def get_messages(self, size=None, with_history=True):
            return list(self.msgs)

        async def clear_context(self, session_id=None):
            cleared.append(session_id)
            self.msgs = []

        async def add_messages(self, message):
            msgs = message if isinstance(message, list) else [message]
            self.msgs.extend(msgs)
            added.extend(msgs)

    ce = _CE()

    class _RA:
        context_engine = ce

    class _DA:
        react_agent = _RA()

    def _msg(role, content):
        return type("M", (), {"role": role, "content": content})()

    # 活窗：磁盘已完成轮（你好/greeting）+ 在途轮（query/段1，未落盘）
    ce.msgs = [
        _msg("user", "hello"),
        _msg("assistant", "greeting reply"),
        _msg("user", "query files and delete a.py"),
        _msg("assistant", "seg1 listing"),
    ]

    monkeypatch.setattr(sos, "history_exists", lambda sid: True)
    monkeypatch.setattr(
        sos, "load_history_tail_request_id", lambda sid, exclude_request_id=None: "r0"
    )
    monkeypatch.setattr(
        sos,
        "load_history_records",
        lambda sid: [_user("r0", "hello", 1.0), _final("r0", "greeting reply", 1.1)],
    )

    async def _fake_restore(**kwargs):
        # 磁盘重建：只恢复已完成轮的 2 条消息
        ce.msgs = [_msg("user", "hello"), _msg("assistant", "greeting reply")]
        return True

    monkeypatch.setattr(sos, "_restore_session_context_from_history", _fake_restore)
    monkeypatch.setattr(sos, "resolve_live_agent_session", lambda deep_agent, session_id: object())
    monkeypatch.setattr(sos, "_persist_session_context", _fake_true)

    tail = await sos.refresh_session_context_if_stale(
        deep_agent=_DA(),
        session_id="s1",
        synced_tail_request_id="r9",  # 在途轮 id，不在磁盘历史 → delta=None
    )
    assert tail == "r0"
    assert cleared == ["s1"]
    # 在途尾部已并回：重建上下文 = 磁盘 2 条 + 在途 2 条，顺序不乱
    assert [m.content for m in ce.msgs] == [
        "hello",
        "greeting reply",
        "query files and delete a.py",
        "seg1 listing",
    ]
    assert [m.content for m in added] == [
        "query files and delete a.py",
        "seg1 listing",
    ]


async def _fake_true(**kwargs):
    return True


@pytest.mark.asyncio
async def test_refresh_rebuild_scans_pool_and_preserves_in_flight(monkeypatch):
    """Rebuild preserve: live window snapshot + pool scan + in-flight re-append.

    Scenario (2026-11-07 a.py round): the HITL resume rebuilds the context
    from disk (fingerprint missing), but the suspended run's in-flight turns
    (this round's query/seg1/tool calls) live only in the in-memory window.
    After the disk restore, the unmatched live tail must be re-appended so
    the model keeps the task context (otherwise it outputs a greeting).
    """
    cleared: list = []
    added: list = []

    class _Ctx:
        def __init__(self, msgs):
            self._msgs = list(msgs)

        def get_messages(self, size=None, with_history=True):
            return list(self._msgs)

        async def add_messages(self, message):
            msgs = message if isinstance(message, list) else [message]
            self._msgs.extend(msgs)
            return list(self._msgs)

    class _CE:
        def __init__(self):
            self.pool: dict = {}

        def get_context(self, context_id="default_context_id", session_id="default_session_id"):
            return self.pool.get("{}_{}".format(session_id, context_id))

        async def clear_context(self, session_id=None):
            cleared.append(session_id)
            for k in [k for k in self.pool if k.startswith("{}_".format(session_id))]:
                self.pool.pop(k, None)

    ce = _CE()

    class _RA:
        context_engine = ce

    class _DA:
        react_agent = _RA()

    def _msg(role, content):
        return type("M", (), {"role": role, "content": content})()

    # Live window under the DEFAULT key: 2 disk-covered turns + 2 in-flight
    ce.pool["s1_default_context_id"] = _Ctx([
        _msg("user", "hello"),
        _msg("assistant", "greeting reply"),
        _msg("user", "query files and delete a.py"),
        _msg("assistant", "seg1 listing"),
    ])

    monkeypatch.setattr(sos, "history_exists", lambda sid: True)
    monkeypatch.setattr(
        sos, "load_history_tail_request_id", lambda sid, exclude_request_id=None: "r0"
    )
    monkeypatch.setattr(
        sos,
        "load_history_records",
        lambda sid: [_user("r0", "hello", 1.0), _final("r0", "greeting reply", 1.1)],
    )

    async def _fake_restore(**kwargs):
        ce.pool["s1_default_context_id"] = _Ctx([
            _msg("user", "hello"),
            _msg("assistant", "greeting reply"),
        ])
        return True

    monkeypatch.setattr(sos, "_restore_session_context_from_history", _fake_restore)
    monkeypatch.setattr(sos, "resolve_live_agent_session", lambda deep_agent, session_id: object())
    monkeypatch.setattr(sos, "_persist_session_context", _fake_true)

    tail = await sos.refresh_session_context_if_stale(
        deep_agent=_DA(),
        session_id="s1",
        synced_tail_request_id="r9",  # in-flight round id, missing on disk
    )
    assert tail == "r0"
    # in-flight tail re-appended after the disk restore, order preserved
    msgs_after = ce.pool["s1_default_context_id"].get_messages()
    assert [m.content for m in msgs_after] == [
        "hello",
        "greeting reply",
        "query files and delete a.py",
        "seg1 listing",
    ]


@pytest.mark.asyncio
async def test_refresh_rebuild_scans_pool_when_default_empty(monkeypatch):
    """Pool-scan fallback: the live window hangs under a non-default
    context_id (empty default) - the scan must find it and re-append."""
    cleared: list = []
    added: list = []

    class _Ctx:
        def __init__(self, msgs):
            self._msgs = list(msgs)

        def get_messages(self, size=None, with_history=True):
            return list(self._msgs)

        async def add_messages(self, message):
            msgs = message if isinstance(message, list) else [message]
            self._msgs.extend(msgs)
            return list(self._msgs)

    class _CE:
        def __init__(self):
            self._context_pool: dict = {}

        def get_context(self, context_id="default_context_id", session_id="default_session_id"):
            return self._context_pool.get("{}_{}".format(session_id, context_id))

        async def clear_context(self, session_id=None):
            cleared.append(session_id)
            for k in [k for k in self._context_pool if k.startswith("{}_".format(session_id))]:
                self._context_pool.pop(k, None)

    ce = _CE()

    class _RA:
        context_engine = ce

    class _DA:
        react_agent = _RA()

    def _msg(role, content):
        return type("M", (), {"role": role, "content": content})()

    # Default key EMPTY; live window under the non-default "turn-1" key
    ce._context_pool["s1_default_context_id"] = _Ctx([])
    ce._context_pool["s1_turn-1"] = _Ctx([
        _msg("user", "query files and delete a.py"),
        _msg("assistant", "seg1 listing"),
    ])

    monkeypatch.setattr(sos, "history_exists", lambda sid: True)
    monkeypatch.setattr(
        sos, "load_history_tail_request_id", lambda sid, exclude_request_id=None: "r0"
    )
    monkeypatch.setattr(
        sos,
        "load_history_records",
        lambda sid: [_user("r0", "hello", 1.0), _final("r0", "greeting reply", 1.1)],
    )

    async def _fake_restore(**kwargs):
        ce._context_pool["s1_default_context_id"] = _Ctx([
            _msg("user", "hello"),
            _msg("assistant", "greeting reply"),
        ])
        return True

    monkeypatch.setattr(sos, "_restore_session_context_from_history", _fake_restore)
    monkeypatch.setattr(sos, "resolve_live_agent_session", lambda deep_agent, session_id: object())
    monkeypatch.setattr(sos, "_persist_session_context", _fake_true)

    tail = await sos.refresh_session_context_if_stale(
        deep_agent=_DA(),
        session_id="s1",
        synced_tail_request_id="r9",
    )
    assert tail == "r0"
    # zero overlap -> whole live window re-appended after the disk restore
    msgs_after = ce._context_pool["s1_default_context_id"].get_messages()
    assert [m.content for m in msgs_after] == [
        "hello",
        "greeting reply",
        "query files and delete a.py",
        "seg1 listing",
    ]




@pytest.mark.asyncio
async def test_refresh_uses_agent_session_id_not_channel_key(monkeypatch):
    """refresh 的历史读取键 = 运行实例的真实会话 id（非渠道会话键）。

    渠道会话键（conv id）与运行实例会话 id（session.create 分配）是两个
    键空间——用渠道键读历史会拿到陈旧/错位文件（a.py 轮：refresh 读到
    4 条陈旧记录重建，模型上下文只剩上一轮问候）。
    """
    seen_session_ids: list = []
    cleared: list = []

    class _CE:
        def __init__(self):
            self.msgs: list = []

        def get_context(self, session_id=None):
            seen_session_ids.append(session_id)
            if session_id == "internal-1":
                return self
            return None

        def get_messages(self, size=None, with_history=True):
            return list(self.msgs)

        async def save_contexts(self, session=None):
            return True
        async def clear_context(self, session_id=None):
            cleared.append(session_id)

        async def add_messages(self, message):
            self.msgs.extend(message if isinstance(message, list) else [message])

    ce = _CE()

    class _RA:
        context_engine = ce

    class _Session:
        def get_session_id(self):
            return "internal-1"

    class _Instance:
        _interaction_session = _Session()

    class _DA:
        react_agent = _RA()
        _instance = _Instance()
        _interaction_session = _Session()

    monkeypatch.setattr(sos, "history_exists", lambda sid: sid == "internal-1")
    monkeypatch.setattr(
        sos, "load_history_tail_request_id", lambda sid, exclude_request_id=None: "r0"
    )
    monkeypatch.setattr(
        sos, "load_history_records", lambda sid: [_user("r0", "hello", 1.0), _final("r0", "greeting", 1.1)]
    )

    async def _fake_restore(**kwargs):
        return True

    monkeypatch.setattr(sos, "_restore_session_context_from_history", _fake_restore)

    tail = await sos.refresh_session_context_if_stale(
        deep_agent=_DA(),
        session_id="internal-1",
        synced_tail_request_id="r9",
    )
    assert tail == "r0"
    # 历史读取键 = 运行实例会话 id（非渠道键）
    assert seen_session_ids and all(s == "internal-1" for s in seen_session_ids)
    assert cleared == ["internal-1"]  # 指纹缺失 → 全量重建兜底（原语义）
