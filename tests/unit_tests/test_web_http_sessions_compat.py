# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway ``GET /api/sessions*`` compat over ChatHistoryStore."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jiuwenswarm.channels.web.history_store import ChatHistoryStore, set_default_store
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_http_app import create_web_http_app
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


@pytest.fixture()
def history_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    store = ChatHistoryStore.memory()
    set_default_store(store)
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    client = TestClient(create_web_http_app(channel))
    return client, store


@pytest.mark.asyncio
async def test_api_sessions_list_and_detail(history_client):
    client, store = history_client
    await store.record_user(
        request_id="r1", session_id="s1", query="hello", ts=1.0, user="u1",
    )
    await store.record_assistant(
        request_id="r1",
        session_id="s1",
        content="hi",
        event_type="chat.final",
        ts=2.0,
    )

    r = client.get("/api/sessions", params={"limit": 10, "user": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "s1"

    r = client.get("/api/sessions/s1", params={"user": "u1"})
    assert r.status_code == 200
    detail = r.json()
    assert detail["session_id"] == "s1"
    assert isinstance(detail.get("messages"), list)
    assert len(detail["messages"]) >= 2

    r = client.get("/api/sessions/s1", params={"user": "other"})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"

    r = client.get("/api/sessions/missing")
    assert r.status_code == 404


def test_api_sessions_empty_when_no_store(monkeypatch: pytest.MonkeyPatch):
    # /api/sessions* is enterprise-only (history_store); personal edition doesn't
    # register the route. Use enterprise edition with an unavailable store so the
    # endpoint still returns 200 + empty list (graceful "no store" handling).
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    # Force unavailable default by pointing mysql without host via empty memory reset.
    store = ChatHistoryStore(settings=None, memory=False)
    set_default_store(store)
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    client = TestClient(create_web_http_app(channel))
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json()["sessions"] == []


def test_catalog_includes_sessions_compat(history_client):
    client, _store = history_client
    r = client.get("/api/v1/catalog")
    assert r.status_code == 200
    paths = {row["path"] for row in r.json()["data"]["routes"]}
    assert "/api/sessions" in paths
    assert "/api/sessions/{session_id}" in paths


def test_openapi_includes_sessions_compat(history_client):
    client, _store = history_client
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/sessions" in paths
    assert "/api/sessions/{session_id}" in paths
