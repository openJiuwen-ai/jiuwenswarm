# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SessionMap ``Session`` rows + unified runtime invoke-id helpers."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.gateway.routing.session_map import (
    SessionMap,
    SessionMapScope,
    invoke_ids_from_identity,
    invoke_ids_from_session_id_string,
    invoke_service_id,
)


@pytest.fixture
def checkpoint_tmp(monkeypatch, tmp_path):
    import sys

    session_storage_mod = sys.modules["jiuwenswarm.gateway.routing.session_storage"]
    store_path = tmp_path / "session_map.json"

    def _resolve_storage() -> session_storage_mod.SessionStorage:
        return session_storage_mod.LocalSessionStorage(store_path=store_path)

    monkeypatch.setattr(SessionMap, "_resolve_storage", staticmethod(_resolve_storage))
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    return tmp_path


def test_invoke_ids_from_identity() -> None:
    sid = invoke_service_id("c1", "b1")
    s, a = invoke_ids_from_identity("c1", "b1", "u1", SessionMapScope.PER_CHAT_BOT)
    assert s == sid and a is None
    s2, a2 = invoke_ids_from_identity("c1", "b1", "u1", SessionMapScope.PER_CHAT_BOT_USER)
    assert s2 == sid and a2 == "u1"


def test_invoke_ids_from_session_id_string_shapes() -> None:
    p, c, b, u = "feishu", "chat1", "bot1", "user1"
    five = f"{p}::{c}::{b}::1a2b3c::deadbe"
    s5, a5 = invoke_ids_from_session_id_string(five)
    assert a5 is None
    assert s5 == invoke_service_id(c, b)
    six = f"{p}::{c}::{b}::{u}::1a2b3c::deadbe"
    s6, a6 = invoke_ids_from_session_id_string(six)
    assert a6 == u
    assert s6 == invoke_service_id(c, b)


def test_session_map_legacy_string_json(checkpoint_tmp) -> None:
    legacy_sid = "prov::chatA::botB::abc::def12"
    key = "prov::chatA::botB"
    path = checkpoint_tmp / "session_map.json"
    path.write_text(json.dumps({key: legacy_sid}, ensure_ascii=False), encoding="utf-8")

    m = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    s = m.get_session("prov", "chatA", "botB", "userIgnored")
    assert s.session_id == legacy_sid
    exp_svc, _ = invoke_ids_from_session_id_string(legacy_sid)
    assert s.service_id == exp_svc
    assert s.agent_id is None


def test_session_map_dict_record_json(checkpoint_tmp) -> None:
    legacy_sid = "prov::chatA::botB::abc::def12"
    key = "prov::chatA::botB"
    svc = invoke_service_id("chatA", "botB")
    path = checkpoint_tmp / "session_map.json"
    path.write_text(
        json.dumps(
            {key: {"session_id": legacy_sid, "service_id": svc, "agent_id": None}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    m = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    s = m.get_session("prov", "chatA", "botB", "u")
    assert s.session_id == legacy_sid
    assert s.service_id == svc
    assert s.agent_id is None


def test_session_map_set_and_find(checkpoint_tmp) -> None:
    """本地 SessionMap 不分配 ID：先 set 再 get/find。"""
    m = SessionMap(scope=SessionMapScope.PER_CHAT_BOT_USER)
    sid = "p::c::b::u::1a2b3c::deadbe"
    m.set_session_id("p", "c", "b", "u", sid)
    a = m.get_session("p", "c", "b", "u", rotate=False)
    b = m.find_session("p", "c", "b", "u")
    assert a.session_id == b.session_id == sid
    assert a.service_id == b.service_id == invoke_service_id("c", "b")
    assert a.agent_id == "u"


def test_session_map_get_session_without_bind_raises(checkpoint_tmp) -> None:
    m = SessionMap(scope=SessionMapScope.PER_CHAT_BOT)
    with pytest.raises(RuntimeError, match="cannot allocate"):
        m.get_session("p", "c", "b", "u")


def test_message_to_e2a_lifts_service_id(monkeypatch) -> None:
    from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a
    from jiuwenswarm.common.schema.message import Message, ReqMethod

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    msg = Message(
        id="req1",
        type="req",
        channel_id="feishu_enterprise",
        session_id="feishu::c::b::ts::sfx",
        params={"service_id": "svc-abc", "agent_id": "user1", "mode": "agent"},
        timestamp=0.0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=False,
    )
    env = message_to_e2a(msg)
    assert env.service_id == "svc-abc"
    assert env.agent_id == "user1"
