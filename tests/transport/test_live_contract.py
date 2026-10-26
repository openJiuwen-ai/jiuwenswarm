# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import os
import uuid

import pytest

try:  # httpx 随 fastapi[testclient] 提供
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

BASE = os.getenv("JIUWENSWARM_LIVE_BASE", "http://127.0.0.1:8766/api/v1")
WRITE_ENABLED = os.getenv("JIUWENSWARM_LIVE_WRITE", "").strip().lower() in {"1", "true", "yes"}

pytestmark = pytest.mark.skipif(httpx is None, reason="httpx 不可用")


def _client() -> "httpx.Client":
    return httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)


@pytest.fixture(scope="module")
def live():
    if httpx is None:
        pytest.skip("httpx 不可用")
    client = _client()
    try:
        client.get("/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        client.close()
        hint = ""
        if "502" in str(exc) or "504" in str(exc):
            # 5xx 网关错误不可能来自本服务，必然是中间代理
            hint = "（502/504 来自代理而非本服务，请检查 HTTP_PROXY / NO_PROXY）"
        pytest.skip(f"AgentServer HTTP 未运行或不可达（{BASE}）: {exc}{hint}")
    try:
        yield client
    finally:
        client.close()


def _assert_ok(resp: "httpx.Response", label: str) -> dict:
    assert resp.status_code < 400, f"{label} → HTTP {resp.status_code}: {resp.text[:400]}"
    body = resp.json()
    assert body.get("ok") is True, f"{label} → ok=False: {body}"
    assert body.get("request_id"), f"{label} → 缺 request_id"
    return body


READONLY_CASES = [
    ("health", "GET", "/health"),
    ("session.list", "GET", "/sessions?limit=3"),
    ("agents.list", "GET", "/agents"),
    ("skills.installed", "GET", "/skills/installed"),
    ("skills.marketplace.list", "GET", "/skills/marketplace"),
    ("skills.retrieval.status", "GET", "/skills/retrieval/status"),
    ("extensions.list", "GET", "/extensions"),
    ("hooks.list", "GET", "/hooks"),
    ("plugins.list", "GET", "/plugins"),
    ("team.templates.list", "GET", "/teams/templates"),
    ("team.bindings.list", "GET", "/teams/bindings"),
    ("permissions.enabled.get", "GET", "/permissions/enabled"),
    ("permissions.tools.get", "GET", "/permissions/tools"),
    ("permissions.rules.get", "GET", "/permissions/rules"),
    ("harness.packages.get", "GET", "/harness/packages"),
    ("schedule.config", "GET", "/schedule/config"),
    ("schedule.list", "GET", "/schedule/tasks"),
    ("issue.state.list", "GET", "/issues/states"),
    ("updater.status", "GET", "/updater/status"),
    ("updater.config", "GET", "/updater/config"),
    ("heartbeat.config", "GET", "/heartbeat/config"),
]


@pytest.mark.parametrize(("label", "verb", "path"), READONLY_CASES, ids=[c[0] for c in READONLY_CASES])
def test_readonly_endpoint(live: "httpx.Client", label: str, verb: str, path: str) -> None:
    _assert_ok(live.request(verb, path), label)


def test_generic_rpc_readonly(live: "httpx.Client") -> None:
    _assert_ok(live.post("/rpc/session.list", json={"limit": 2}), "rpc session.list")


def test_request_id_echo(live: "httpx.Client") -> None:
    rid = f"live-{uuid.uuid4().hex[:8]}"
    resp = live.get("/health", headers={"X-Request-Id": rid})
    assert resp.headers.get("X-Request-Id") == rid
    assert resp.json()["request_id"] == rid


def test_unknown_method_is_404(live: "httpx.Client") -> None:
    resp = live.post("/rpc/definitely.not.a.method", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "UNKNOWN_METHOD"


write_only = pytest.mark.skipif(
    not WRITE_ENABLED, reason="需 JIUWENSWARM_LIVE_WRITE=1 才运行写操作契约测试"
)


def _session_id_from(body: dict) -> str | None:
    data = body.get("data") or {}
    inner = data.get("result") if isinstance(data.get("result"), dict) else data
    return (inner or {}).get("session_id")


@write_only
def test_session_create_rename_delete_roundtrip(live: "httpx.Client") -> None:
    created = _assert_ok(live.post("/sessions", json={"mode": "agent"}), "session.create")
    sid = _session_id_from(created)
    assert sid, f"未返回 session_id: {created}"

    try:
        renamed = _assert_ok(
            live.patch(f"/sessions/{sid}", json={"title": "契约测试会话"}), "session.rename"
        )
        assert renamed["ok"] is True

        listed = _assert_ok(live.get("/sessions?limit=50"), "session.list")
        text = str(listed)
        assert sid in text, "新建的会话未出现在列表中"
    finally:
        _assert_ok(live.delete(f"/sessions/{sid}"), "session.delete")


@write_only
def test_session_create_is_idempotent_per_request_id(live: "httpx.Client") -> None:
    rid = f"idem-{uuid.uuid4().hex[:8]}"
    first = _assert_ok(
        live.post("/sessions", json={"mode": "agent"}, headers={"X-Request-Id": rid}),
        "session.create #1",
    )
    second = _assert_ok(
        live.post("/sessions", json={"mode": "agent"}, headers={"X-Request-Id": rid}),
        "session.create #2",
    )
    sid1, sid2 = _session_id_from(first), _session_id_from(second)
    try:
        assert sid1, "首次创建未返回 session_id"
        assert sid1 == sid2, f"同一 request_id 却创建了两个会话: {sid1} != {sid2}"
    finally:
        for sid in {sid1, sid2} - {None}:
            live.delete(f"/sessions/{sid}")


@write_only
def test_missing_session_delete_is_404_not_500(live: "httpx.Client") -> None:
    resp = live.delete(f"/sessions/definitely_missing_{uuid.uuid4().hex[:8]}")
    assert resp.status_code == 404, f"应为 404，实际 {resp.status_code}: {resp.text[:300]}"
    assert resp.json()["ok"] is False


@write_only
def test_config_cache_clear(live: "httpx.Client") -> None:
    _assert_ok(live.post("/config/actions/cache-clear", json={}), "config.cache_clear")


@write_only
def test_permissions_enabled_roundtrip(live: "httpx.Client") -> None:
    body = _assert_ok(live.get("/permissions/enabled"), "permissions.enabled.get")
    data = body.get("data") or {}
    inner = data.get("result") if isinstance(data.get("result"), dict) else data
    current = (inner or {}).get("enabled")
    if current is None:
        pytest.skip(f"未能解析 enabled 当前值: {body}")
    _assert_ok(
        live.put("/permissions/enabled", json={"enabled": current}),
        "permissions.enabled.set(原值写回)",
    )
