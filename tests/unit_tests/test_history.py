# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json

import pytest

from jiuwenswarm.channels.web.history_store import (
    ChatHistoryStore,
    get_session_detail_sync,
    list_sessions_sync,
    make_history_callback,
    resolve_history_db_type,
    set_default_store,
)


def _mem_store() -> ChatHistoryStore:
    return ChatHistoryStore.memory()


@pytest.mark.asyncio
async def test_record_user_and_assistant_then_list_detail() -> None:
    store = _mem_store()
    await store.record_user(request_id="r1", session_id="s1", query="你好", ts=1000.0)
    await store.record_assistant(
        request_id="r1",
        session_id="s1",
        content="你好，有什么可以帮你？",
        event_type="chat.final",
        ts=1001.0,
    )
    sessions = await store.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "s1"
    assert s["title"] == "你好"
    assert s["message_count"] == 2
    detail = await store.get_session_detail("s1")
    assert detail is not None
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["role"] == "user"
    await store.close()


@pytest.mark.asyncio
async def test_record_idempotent_on_resend() -> None:
    store = _mem_store()
    inserted1 = await store.record_user(
        request_id="r1", session_id="s1", query="hello", ts=1000.0
    )
    inserted2 = await store.record_user(
        request_id="r1", session_id="s1", query="hello", ts=1000.0
    )
    assert inserted1 is True
    assert inserted2 is False
    sessions = await store.list_sessions()
    assert sessions[0]["message_count"] == 1
    await store.close()


@pytest.mark.asyncio
async def test_title_first_set_not_overwritten() -> None:
    store = _mem_store()
    await store.record_user(
        request_id="r1", session_id="s1", query="第一条用户消息", ts=1000.0
    )
    await store.record_assistant(
        request_id="r1",
        session_id="s1",
        content="回复内容",
        event_type="chat.final",
        ts=1001.0,
    )
    detail = await store.get_session_detail("s1")
    assert detail is not None
    assert detail["title"] == "第一条用户消息"
    await store.close()


@pytest.mark.asyncio
async def test_rename_session_blocking_query_set_clear() -> None:
    store = _mem_store()
    await store.record_user(
        request_id="r1", session_id="s1", query="第一条用户消息", ts=1000.0
    )
    # 查询（title=None）返回当前标题
    assert store.rename_session_blocking("s1", None, user="guest") == {
        "title": "第一条用户消息",
        "previous_title": "第一条用户消息",
    }
    # 设置：返回新标题与旧标题，列表读同源
    assert store.rename_session_blocking("s1", "新标题", user="guest") == {
        "title": "新标题",
        "previous_title": "第一条用户消息",
    }
    sessions = await store.list_sessions()
    assert sessions[0]["title"] == "新标题"
    # 清除（空串）：标题置空
    assert store.rename_session_blocking("s1", "", user="guest") == {
        "title": "",
        "previous_title": "新标题",
    }
    assert store.rename_session_blocking("s1", None, user="guest") == {
        "title": "",
        "previous_title": "",
    }
    # 不存在的会话 / 非本人会话返回 None
    assert store.rename_session_blocking("missing", "x", user="guest") is None
    assert store.rename_session_blocking("s1", "x", user="other") is None
    await store.close()


@pytest.mark.asyncio
async def test_callback_whitelist_ignores_non_chat() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "browser",
        json.dumps(
            {
                "type": "req",
                "id": "x1",
                "method": "skilldev.start",
                "params": {"query": "应被忽略"},
            }
        ),
    )
    await cb(
        "browser",
        json.dumps(
            {
                "type": "req",
                "id": "x2",
                "method": "chat.interrupt",
                "params": {"query": "应被忽略"},
            }
        ),
    )
    assert await store.list_sessions() == []
    await store.close()


@pytest.mark.asyncio
async def test_callback_user_with_session_id_records_directly() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "browser",
        json.dumps(
            {
                "type": "req",
                "id": "r1",
                "method": "chat.send",
                "params": {"session_id": "s1", "query": "直接落盘"},
            }
        ),
    )
    detail = await store.get_session_detail("s1")
    assert detail is not None
    assert len(detail["messages"]) == 1
    await store.close()


@pytest.mark.asyncio
async def test_callback_pending_backfill_on_final() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "browser",
        json.dumps(
            {
                "type": "req",
                "id": "r1",
                "method": "chat.send",
                "params": {"query": "在吗"},
            }
        ),
    )
    assert await store.list_sessions() == []
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.delta",
                "request_id": "r1",
                "payload": {"session_id": "s1", "content": "在"},
            }
        ),
    )
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.delta",
                "request_id": "r1",
                "payload": {"session_id": "s1", "content": "的"},
            }
        ),
    )
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.final",
                "request_id": "r1",
                "payload": {"session_id": "s1", "content": ""},
            }
        ),
    )
    detail = await store.get_session_detail("s1")
    assert detail is not None
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "在的"
    await store.close()


@pytest.mark.asyncio
async def test_callback_ignores_delta_events() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.tool_calls.delta",
                "request_id": "r1",
                "payload": {"session_id": "s1", "content": "增量"},
            }
        ),
    )
    assert await store.list_sessions() == []
    await store.close()


@pytest.mark.asyncio
async def test_callback_records_chat_error() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "browser",
        json.dumps(
            {
                "type": "req",
                "id": "r1",
                "method": "chat.send",
                "params": {"session_id": "s1", "query": "出错了"},
            }
        ),
    )
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.error",
                "request_id": "r1",
                "payload": {"session_id": "s1", "error": "内部错误"},
            }
        ),
    )
    detail = await store.get_session_detail("s1")
    assert detail is not None
    assistant_msgs = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["event_type"] == "chat.error"
    await store.close()


@pytest.mark.asyncio
async def test_callback_invalid_json_ignored() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb("browser", "不是JSON", "conn1")
    assert await store.list_sessions() == []
    await store.close()


@pytest.mark.asyncio
async def test_callback_final_without_session_id_dropped() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.final",
                "request_id": "r1",
                "payload": {"content": "缺 sid"},
            }
        ),
    )
    assert await store.list_sessions() == []
    await store.close()


@pytest.mark.asyncio
async def test_sync_read_after_write() -> None:
    store = _mem_store()
    set_default_store(store)
    await store.record_user(request_id="r1", session_id="s1", query="问题", ts=1000.0)
    await store.record_assistant(
        request_id="r1",
        session_id="s1",
        content="回答",
        event_type="chat.final",
        ts=1001.0,
    )

    sessions = list_sessions_sync(store)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"

    detail = get_session_detail_sync("s1", store)
    assert detail is not None
    assert len(detail["messages"]) == 2

    assert get_session_detail_sync("nope", store) is None
    await store.close()


def test_mysql_without_host_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_DB_HOST", raising=False)
    monkeypatch.setenv("WEB_DB_TYPE", "mysql")
    store = ChatHistoryStore.from_env()
    assert store.backend == "mysql"
    assert store.available is False


def test_resolve_history_db_type_prefers_web_db_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_DB_TYPE", "postgresql")
    monkeypatch.setenv("DB_TYPE", "mysql")
    monkeypatch.delenv("WEB_DB_HOST", raising=False)
    assert resolve_history_db_type() == "postgresql"


def test_resolve_history_db_type_host_implies_mysql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB_DB_TYPE", raising=False)
    monkeypatch.delenv("DB_TYPE", raising=False)
    monkeypatch.setenv("WEB_DB_HOST", "127.0.0.1")
    assert resolve_history_db_type() == "mysql"


def test_resolve_history_db_type_follows_deployment_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB_DB_TYPE", raising=False)
    monkeypatch.delenv("DB_TYPE", raising=False)
    monkeypatch.delenv("WEB_DB_HOST", raising=False)
    monkeypatch.setenv("DEPLOYMENT_MODE", "distributed")
    assert resolve_history_db_type() == "mysql"
    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    assert resolve_history_db_type() == "memory"

@pytest.mark.asyncio
async def test_callback_uplink_request_id_in_payload() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "browser",
        json.dumps({"type": "req", "id": "r1", "method": "chat.send", "params": {"query": "hi"}}),
    )
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.delta",
                "payload": {"session_id": "s1", "request_id": "r1", "content": "ok"},
            },
        ),
    )
    await cb(
        "uplink",
        json.dumps(
            {
                "type": "event",
                "event": "chat.final",
                "payload": {"session_id": "s1", "request_id": "r1", "content": ""},
            },
        ),
    )
    detail = await store.get_session_detail("s1")
    assert detail is not None
    assert detail["messages"][-1]["content"] == "ok"
    await store.close()


def test_sync_read_missing_sqlite_file(tmp_path) -> None:
    missing = tmp_path / "missing.db"
    assert list_sessions_sync(missing) == []
    assert get_session_detail_sync(missing, "x") is None



# ── 身份口径（group_id/bot_id 与 pod workspace 三元组对齐） ──────────


@pytest.mark.asyncio
async def test_identity_scope_filtering_on_list_and_detail() -> None:
    store = _mem_store()
    await store.record_user(
        request_id="r1", session_id="s_org_a", query="orgA 会话", ts=1000.0,
        user="u1", group_id="g1", bot_id="b1",
    )
    await store.record_user(
        request_id="r2", session_id="s_org_b", query="orgB 会话", ts=1001.0,
        user="u1", group_id="g2", bot_id="b1",
    )
    # 无身份行（存量语义）：通配，任何 scope 可见
    await store.record_user(
        request_id="r3", session_id="s_legacy", query="存量会话", ts=1002.0, user="u1",
    )

    # 命中 g1 scope：仅 s_org_a + 存量
    rows = store.list_sessions_blocking(limit=50, offset=0, user="u1", group_id="g1", bot_id="b1")
    assert {r["session_id"] for r in rows} == {"s_org_a", "s_legacy"}
    # 命中 g2 scope：仅 s_org_b + 存量
    rows = store.list_sessions_blocking(limit=50, offset=0, user="u1", group_id="g2", bot_id="b1")
    assert {r["session_id"] for r in rows} == {"s_org_b", "s_legacy"}
    # 不带身份查询：全量（退化 user-only，兼容旧行为）
    rows = store.list_sessions_blocking(limit=50, offset=0, user="u1")
    assert len(rows) == 3
    # 计数与列表同口径
    assert store.count_sessions_blocking(user="u1", group_id="g1", bot_id="b1") == 2
    # 详情读受身份约束：跨 scope 等同不存在
    assert store.get_session_detail_blocking("s_org_a", user="u1", group_id="g2", bot_id="b1") is None
    assert store.get_session_detail_blocking("s_org_a", user="u1", group_id="g1", bot_id="b1") is not None
    await store.close()


@pytest.mark.asyncio
async def test_identity_first_writer_wins_on_row() -> None:
    store = _mem_store()
    await store.record_user(
        request_id="r1", session_id="s1", query="第一条", ts=1000.0,
        user="u1", group_id="g1", bot_id="b1",
    )
    # 同一行的后续消息带不同身份——不改写已有行身份（first-writer-wins）
    await store.record_user(
        request_id="r2", session_id="s1", query="第二条", ts=1001.0,
        user="u1", group_id="gX", bot_id="bX",
    )
    detail = store.get_session_detail_blocking("s1", user="u1")
    assert detail is not None
    assert detail["group_id"] == "g1"
    assert detail["bot_id"] == "b1"
    await store.close()


@pytest.mark.asyncio
async def test_ensure_session_row_and_touch() -> None:
    store = _mem_store()
    # session.create 落行：零消息行，带完整身份与项目归属
    created = store.ensure_session_row_blocking(
        "s_new", user="u1", group_id="g1", bot_id="b1",
        project_id="p1", work_mode="work", title="定时任务", ts=2000.0,
    )
    assert created is True
    rows = store.list_sessions_blocking(limit=50, offset=0, user="u1", group_id="g1", bot_id="b1")
    assert [r["session_id"] for r in rows] == ["s_new"]
    assert rows[0]["message_count"] == 0
    assert rows[0]["title"] == "定时任务"
    assert rows[0]["project_id"] == "p1"
    # 已有行再次 ensure：不覆盖身份，不重复建行
    store.ensure_session_row_blocking(
        "s_new", user="u1", group_id="gX", bot_id="bX", project_id="pX", ts=2001.0,
    )
    detail = store.get_session_detail_blocking("s_new", user="u1")
    assert detail is not None
    assert detail["group_id"] == "g1"
    assert detail["project_id"] == "p1"
    # 首条消息计数补齐 + last_user_message_at 写入
    await store.record_user(
        request_id="r1", session_id="s_new", query="第一条", ts=2002.0,
        user="u1", group_id="g1", bot_id="b1",
    )
    detail = store.get_session_detail_blocking("s_new", user="u1")
    assert detail["message_count"] == 1
    assert detail["last_user_message_at"] == 2002.0
    # touch 仅刷活动时间
    assert store.touch_session_blocking("s_new", ts=2003.0) is True
    detail = store.get_session_detail_blocking("s_new", user="u1")
    assert detail["updated_at"] == 2003.0
    assert detail["last_user_message_at"] == 2003.0
    # 不存在的行 touch 为 no-op
    assert store.touch_session_blocking("missing", ts=2004.0) is False
    await store.close()


@pytest.mark.asyncio
async def test_pinned_renumber_scoped_by_identity() -> None:
    store = _mem_store()
    await store.record_user(
        request_id="r1", session_id="s_a1", query="a1", ts=1000.0,
        user="u1", group_id="g1", bot_id="b1",
    )
    await store.record_user(
        request_id="r2", session_id="s_a2", query="a2", ts=1001.0,
        user="u1", group_id="g1", bot_id="b1",
    )
    await store.record_user(
        request_id="r3", session_id="s_b1", query="b1", ts=1002.0,
        user="u1", group_id="g2", bot_id="b1",
    )
    # 新置顶 pin_order=0 排最前（与既有置顶语义一致），重编号后 s_a2=1、s_a1=2
    assert store.set_session_pinned_blocking("s_a1", True, user="u1", group_id="g1", bot_id="b1") == (True, 1)
    assert store.set_session_pinned_blocking("s_a2", True, user="u1", group_id="g1", bot_id="b1") == (True, 1)
    assert store.set_session_pinned_blocking("s_b1", True, user="u1", group_id="g2", bot_id="b1") == (True, 1)
    # 置顶列表按 scope 隔离：g1 scope 只见 a1/a2（最新置顶在前）
    pinned = store.list_pinned_sessions_blocking(user="u1", group_id="g1", bot_id="b1")
    assert [p["session_id"] for p in pinned] == ["s_a2", "s_a1"]
    pinned = store.list_pinned_sessions_blocking(user="u1", group_id="g2", bot_id="b1")
    assert [p["session_id"] for p in pinned] == ["s_b1"]
    await store.close()


@pytest.mark.asyncio
async def test_callback_carries_identity_from_frame_params() -> None:
    store = _mem_store()
    cb = make_history_callback(store)
    await cb(
        "browser",
        json.dumps({
            "type": "req", "id": "r1", "method": "chat.send",
            "params": {
                "session_id": "s1", "query": "带身份的消息",
                "user": "u1", "group_id": "g1", "bot_id": "b1",
                "project_id": "p1", "work_mode": "work",
            },
        }),
    )
    detail = store.get_session_detail_blocking("s1", user="u1")
    assert detail is not None
    assert detail["group_id"] == "g1"
    assert detail["bot_id"] == "b1"
    assert detail["project_id"] == "p1"
    assert detail["work_mode"] == "work"
    await store.close()


@pytest.mark.asyncio
async def test_rename_and_delete_reject_cross_scope_identity() -> None:
    store = _mem_store()
    await store.record_user(
        request_id="r1", session_id="s_org1", query="组织一会话", ts=1000.0,
        user="u1", group_id="g1", bot_id="b1",
    )
    await store.record_user(
        request_id="r2", session_id="s_legacy", query="存量会话", ts=1001.0,
        user="u1",
    )
    # 跨 scope 改名/查询：按不存在处理（与 session.list 可见口径一致）
    assert store.rename_session_blocking("s_org1", "越权改名", user="u1", group_id="g2", bot_id="b1") is None
    assert store.rename_session_blocking("s_org1", None, user="u1", group_id="g2", bot_id="b1") is None
    # 同 scope 改名成功；身份列为空的存量行通配不受阻
    assert store.rename_session_blocking("s_org1", "新标题", user="u1", group_id="g1", bot_id="b1") == {
        "title": "新标题", "previous_title": "组织一会话",
    }
    assert store.rename_session_blocking("s_legacy", "存量改名", user="u1", group_id="g9", bot_id="b9") == {
        "title": "存量改名", "previous_title": "存量会话",
    }
    # 跨 scope 删除拒绝；同 scope 与存量行删除成功
    assert store.delete_session_blocking("s_org1", user="u1", group_id="g2", bot_id="b1") is False
    assert store.delete_session_blocking("s_org1", user="u1", group_id="g1", bot_id="b1") is True
    assert store.delete_session_blocking("s_legacy", user="u1", group_id="g9", bot_id="b9") is True
    await store.close()


def test_db_actor_wrappers_accept_identity_kwargs() -> None:
    """回归锁：store 的 DB 分支向 actor 传 group_id/bot_id，公共包装必须收下。

    此前 set_session_pinned_sync 包装漏加身份形参（单测全走 memory 分支未覆盖），
    企业版真实置顶直接 TypeError——用签名检查锁住这一类漂移。``**kw`` 透传式
    包装须继续检查其委托的私有方法形参。
    """
    import inspect

    from jiuwenswarm.channels.web.history_store.db_actor import HistoryDbActor

    def _accepts(func, kw: str) -> bool:
        params = inspect.signature(func).parameters
        if kw in params:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    for name in (
        "set_session_pinned_sync",
        "rename_session_sync",
        "delete_session_sync",
        "list_pinned_sessions_sync",
        "list_all_sessions_sync",
    ):
        sig = inspect.signature(getattr(HistoryDbActor, name))
        assert not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ), f"HistoryDbActor.{name} 应显式声明形参（**kw 透传会绕过本检查）"
        for kw in ("group_id", "bot_id"):
            assert kw in sig.parameters, f"HistoryDbActor.{name} 缺少形参 {kw}（store 调用会 TypeError）"

    # **kw 透传式包装：私有方法必须显式携带身份形参
    for private in ("_list_sessions", "_get_session_detail", "_record_message"):
        for kw in ("group_id", "bot_id"):
            assert kw in inspect.signature(getattr(HistoryDbActor, private)).parameters, (
                f"HistoryDbActor.{private} 缺少形参 {kw}"
            )
