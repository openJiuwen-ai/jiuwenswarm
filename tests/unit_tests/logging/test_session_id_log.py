# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""log_session_id ContextVar 与 SessionIdFilter。"""
import json
import logging

from jiuwenswarm.common.log_context import (
    NO_SESSION_ID,
    bind_log_session,
    current_log_session_id,
    reset_log_session_id,
    set_log_session_id,
    unbind_log_session,
)
from jiuwenswarm.common.utils import JsonUserVisibleFormatter, SessionIdFilter


def test_unbound_session_id_is_nosid():
    assert current_log_session_id() == NO_SESSION_ID


def test_set_and_reset_log_session_id():
    token = set_log_session_id("sess_abc")
    try:
        assert current_log_session_id() == "sess_abc"
    finally:
        reset_log_session_id(token)
    assert current_log_session_id() == NO_SESSION_ID


def test_empty_session_id_prints_nosid():
    token = set_log_session_id("")
    try:
        assert current_log_session_id() == NO_SESSION_ID
    finally:
        reset_log_session_id(token)


def test_filter_writes_record_field():
    token = set_log_session_id("sess_abc")
    try:
        record = logging.LogRecord("jiuwenswarm.gateway", logging.INFO, "", 1, "hi", (), None)
        assert SessionIdFilter().filter(record) is True
        assert record.session_id == "sess_abc"
    finally:
        reset_log_session_id(token)


def test_child_task_keeps_session_after_parent_unbind():
    import asyncio

    from openjiuwen.core.common.logging import get_session_id

    seen: dict[str, str] = {}

    async def _child() -> None:
        seen["log"] = current_log_session_id()
        seen["trace"] = get_session_id() or ""

    async def _parent() -> None:
        bound = bind_log_session("web_parent")
        task = asyncio.create_task(_child())
        unbind_log_session(*bound)
        await task

    asyncio.run(_parent())
    assert seen["log"] == "web_parent"
    assert seen["trace"] == "web_parent"
    assert current_log_session_id() == NO_SESSION_ID


def test_nested_bind_restores_outer_session():
    outer = bind_log_session("web_outer")
    inner = bind_log_session("web_inner")
    try:
        assert current_log_session_id() == "web_inner"
    finally:
        unbind_log_session(*inner)
    assert current_log_session_id() == "web_outer"
    unbind_log_session(*outer)
    assert current_log_session_id() == NO_SESSION_ID


def test_identity_text_formatter_falls_back_session_id():
    from jiuwenswarm.common.utils import IdentityTextFormatter

    fmt = IdentityTextFormatter(
        fmt="%(asctime)s [%(process)d] [%(session_id)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    record = logging.LogRecord("jiuwenswarm.gateway", logging.INFO, "", 1, "hi", (), None)
    out = fmt.format(record)
    assert f"[{NO_SESSION_ID}]" in out

    token = set_log_session_id("sess_abc")
    try:
        record = logging.LogRecord("jiuwenswarm.gateway", logging.INFO, "", 1, "hi", (), None)
        out = fmt.format(record)
        assert "[sess_abc]" in out
    finally:
        reset_log_session_id(token)


def test_json_includes_session_id():
    record = logging.LogRecord("jiuwenswarm.gateway", logging.INFO, "", 1, "hi", (), None)
    SessionIdFilter().filter(record)
    obj = json.loads(JsonUserVisibleFormatter().format(record))
    assert obj["session_id"] == NO_SESSION_ID

    token = set_log_session_id("sess_abc")
    try:
        record = logging.LogRecord("jiuwenswarm.gateway", logging.INFO, "", 1, "hi", (), None)
        SessionIdFilter().filter(record)
        obj = json.loads(JsonUserVisibleFormatter().format(record))
        assert obj["session_id"] == "sess_abc"
        positions = [
            obj and list(obj).index(k)
            for k in ("timestamp", "process", "session_id", "level")
        ]
        assert positions == sorted(positions)
    finally:
        reset_log_session_id(token)
