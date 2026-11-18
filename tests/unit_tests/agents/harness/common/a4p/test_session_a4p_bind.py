# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-scoped a4p bind for enterprise config_base (process yaml stays off)."""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.a4p.runtime import (
    bind_session_a4p_config,
    get_a4p_config,
    is_a4p_enabled,
    is_a4p_preauthorization_active,
    unbind_session_a4p_config,
)


def test_session_bind_overrides_missing_process_a4p() -> None:
    sid = "sess-a4p-bind-1"
    unbind_session_a4p_config(sid)
    try:
        bind_session_a4p_config(
            sid,
            {"enabled": True, "source": "session"},
            permissions={"enabled": True, "mode": "manual"},
        )
        assert get_a4p_config(session_id=sid) == {
            "enabled": True,
            "source": "session",
        }
        assert is_a4p_enabled(session_id=sid) is True
        assert is_a4p_preauthorization_active(session_id=sid) is True
        assert is_a4p_preauthorization_active(
            {
                "a4p": {"enabled": True},
                "permissions": {"enabled": True, "mode": "manual"},
            }
        )
    finally:
        unbind_session_a4p_config(sid)
        # After unbind, session override is gone (may fall back to process yaml).
        assert get_a4p_config(session_id=sid).get("source") != "session"


def test_unbind_request_id_guard_keeps_other_owner() -> None:
    sid = "sess-a4p-bind-guard"
    unbind_session_a4p_config(sid)
    try:
        bind_session_a4p_config(
            sid,
            {"enabled": True, "source": "owner-a"},
            permissions={"enabled": True, "mode": "manual"},
            request_id="req-a",
        )
        # A newer stream takes ownership (last writer wins).
        bind_session_a4p_config(
            sid,
            {"enabled": True, "source": "owner-b"},
            permissions={"enabled": True, "mode": "manual"},
            request_id="req-b",
        )
        assert get_a4p_config(session_id=sid).get("source") == "owner-b"

        # Stale finally from req-a must not wipe req-b's gate.
        unbind_session_a4p_config(sid, "req-a")
        assert get_a4p_config(session_id=sid).get("source") == "owner-b"

        unbind_session_a4p_config(sid, "req-b")
        assert get_a4p_config(session_id=sid).get("source") != "owner-b"
    finally:
        unbind_session_a4p_config(sid)


def test_rebind_without_request_id_preserves_owner() -> None:
    sid = "sess-a4p-bind-preserve"
    unbind_session_a4p_config(sid)
    try:
        bind_session_a4p_config(
            sid,
            {"enabled": True, "source": "v1"},
            request_id="req-keep",
        )
        bind_session_a4p_config(
            sid,
            {"enabled": True, "source": "v2"},
        )
        assert get_a4p_config(session_id=sid).get("source") == "v2"
        unbind_session_a4p_config(sid, "req-other")
        assert get_a4p_config(session_id=sid).get("source") == "v2"
        unbind_session_a4p_config(sid, "req-keep")
        assert get_a4p_config(session_id=sid).get("source") != "v2"
    finally:
        unbind_session_a4p_config(sid)
