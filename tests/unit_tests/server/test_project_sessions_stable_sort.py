# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression tests for #8080: tied timestamps must paginate deterministically."""

from __future__ import annotations

import random

import pytest

from jiuwenswarm.server.control.store import project_queries as pq

_TS = 1_760_000_000.0


def _patch(monkeypatch: pytest.MonkeyPatch, sessions: list[dict]) -> None:
    monkeypatch.setattr(pq.project_store, "list_projects", lambda **_: [])
    monkeypatch.setattr(pq, "split_project_ids", lambda _projects: (set(), set()))
    monkeypatch.setattr(pq, "is_default_project_id", lambda _pid: True)
    monkeypatch.setattr(pq, "attribute_session_project", lambda *_a, **_k: "p1")
    monkeypatch.setattr(pq, "to_session_info", lambda s: {"session_id": s["session_id"]})
    monkeypatch.setattr(pq, "collect_all_sessions_metadata", lambda: list(sessions))


def _ids(payload: dict) -> list[str]:
    return [s["session_id"] for s in payload["sessions"]]


def _web_sessions() -> list[dict]:
    tied = [{"session_id": f"s_{i}", "channel_id": "web", "last_user_message_at": _TS} for i in "abc"]
    return tied + [
        {"session_id": "s_new", "channel_id": "web", "last_user_message_at": _TS + 5},
        {"session_id": "s_old", "channel_id": "web", "last_user_message_at": _TS - 5},
    ]


def test_project_sessions_tied_timestamps_use_session_id_desc(monkeypatch):
    sessions = _web_sessions()
    expected = ["s_new", "s_c", "s_b", "s_a", "s_old"]
    for seed in range(5):
        random.Random(seed).shuffle(sessions)
        _patch(monkeypatch, sessions)
        payload, err, _ = pq.load_project_sessions({"project_id": "p1"}, "u")
        assert err is None
        assert _ids(payload) == expected


def test_project_sessions_limited_page_is_prefix_of_full_list(monkeypatch):
    sessions = _web_sessions()
    _patch(monkeypatch, sessions)
    full = _ids(pq.load_project_sessions({"project_id": "p1"}, "u")[0])
    random.Random(42).shuffle(sessions)
    _patch(monkeypatch, sessions)
    page = _ids(pq.load_project_sessions({"project_id": "p1", "limit": 3}, "u")[0])
    assert page == full[:3]


def test_project_cron_sessions_tied_timestamps_are_stable(monkeypatch):
    sessions = [
        {"session_id": f"__cron___{i}", "channel_id": "__cron__", "cron_id": "job1",
         "last_user_message_at": _TS}
        for i in ("1a12e13faa0", "1a12e142134", "1a12e13cc9a")
    ]
    expected = ["__cron___1a12e142134", "__cron___1a12e13faa0", "__cron___1a12e13cc9a"]
    for seed in range(5):
        random.Random(seed).shuffle(sessions)
        _patch(monkeypatch, sessions)
        payload, err, _ = pq.load_project_cron_sessions({"project_id": "p1"}, "u")
        assert err is None
        assert _ids(payload) == expected
