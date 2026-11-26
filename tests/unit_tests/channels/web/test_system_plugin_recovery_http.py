# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the emergency HTTP entry points without opening a listening socket."""

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.common import system_plugin_recovery
from jiuwenswarm.common.system_plugin_recovery_sidecar import _RecoveryHandler

_PORT = 18763
_AUTHORITY = f"127.0.0.1:{_PORT}"
_TOKEN = "test-only-token"


def _request(handler_type, server, method, target, headers, body=b"", client_address="127.0.0.1"):
    lines = [f"{method} {target} HTTP/1.0", *headers, f"Content-Length: {len(body)}", "", ""]
    raw = "\r\n".join(lines).encode("ascii") + body
    output = io.BytesIO()
    connection = SimpleNamespace(makefile=lambda *args: io.BytesIO(raw), sendall=output.write)
    handler_type(connection, (client_address, 49152), server)
    head, payload = output.getvalue().split(b"\r\n\r\n", 1)
    return int(head.split(b" ", 2)[1]), head, payload


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("headers", [
    [],
    [f"Host: attacker.example:{_PORT}"],
    ["Host: 127.0.0.1:1"],
    [f"Host: {_AUTHORITY}", f"Host: {_AUTHORITY}"],
    [f"Host: {_AUTHORITY}", "Origin: https://attacker.example"],
    [f"Host: {_AUTHORITY}", "Origin: null"],
    [f"Host: {_AUTHORITY}", f"Origin: http://{_AUTHORITY}", f"Origin: http://{_AUTHORITY}"],
])
def test_sidecar_rejects_foreign_authority_before_reading_token(method, headers):
    recovery = SimpleNamespace(records=Mock(), restore=Mock())
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT), recovery=recovery, token=object())
    suffix = "history" if method == "GET" else "restore"
    status, _, _ = _request(
        _RecoveryHandler, server, method, f"/system-plugin-recovery/api/{suffix}?token={_TOKEN}",
        [*headers, "Content-Type: application/json"], b'{"record_id":"saved"}',
    )
    assert status == 403
    recovery.records.assert_not_called()
    recovery.restore.assert_not_called()


@pytest.mark.parametrize("authority", [_AUTHORITY, f"localhost:{_PORT}", f"[::1]:{_PORT}"])
@pytest.mark.parametrize("include_origin", [False, True])
def test_sidecar_accepts_local_page_json_restore(authority, include_origin):
    restore = Mock(return_value={"id": "restored"})
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT),
                             recovery=SimpleNamespace(restore=restore), token=_TOKEN)
    headers = [f"Host: {authority}", "Content-Type: application/json; charset=utf-8"]
    if include_origin:
        headers.append(f"Origin: http://{authority}")
    status, _, payload = _request(
        _RecoveryHandler, server, "POST", f"/system-plugin-recovery/api/restore?token={_TOKEN}",
        headers, b'{"record_id":"saved"}',
    )
    assert status == 200
    assert json.loads(payload) == {"record": {"id": "restored"}, "restart_requested": True}
    restore.assert_called_once_with("saved")


@pytest.mark.parametrize("content_types", [[], ["Content-Type: text/plain"],
    ["Content-Type: application/x-www-form-urlencoded"],
    ["Content-Type: application/json", "Content-Type: application/json"]])
def test_sidecar_restore_requires_one_json_content_type(content_types):
    restore = Mock()
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT),
                             recovery=SimpleNamespace(restore=restore), token=_TOKEN)
    status, _, _ = _request(
        _RecoveryHandler, server, "POST", f"/system-plugin-recovery/api/restore?token={_TOKEN}",
        [f"Host: {_AUTHORITY}", *content_types], b'{"record_id":"saved"}',
    )
    assert status == 415
    restore.assert_not_called()


def test_sidecar_still_requires_the_private_token():
    restore = Mock()
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT),
                             recovery=SimpleNamespace(restore=restore), token=_TOKEN)
    status, _, _ = _request(
        _RecoveryHandler, server, "POST", "/system-plugin-recovery/api/restore?token=wrong",
        [f"Host: {_AUTHORITY}", "Content-Type: application/json"], b'{"record_id":"saved"}',
    )
    assert status == 403
    restore.assert_not_called()


def test_sidecar_serves_history_to_its_local_page():
    records = Mock(return_value=[{"id": "saved"}])
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT),
                             recovery=SimpleNamespace(records=records), token=_TOKEN)
    status, _, payload = _request(
        _RecoveryHandler, server, "GET", f"/system-plugin-recovery/api/history?token={_TOKEN}",
        [f"Host: {_AUTHORITY}"],
    )
    assert status == 200
    assert json.loads(payload) == {"records": [{"id": "saved"}]}
    records.assert_called_once_with()


def test_web_discovery_accepts_the_browser_default_port_origin(monkeypatch):
    endpoint = Mock(return_value="http://127.0.0.1:19634/system-plugin-recovery?token=test-only")
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: SimpleNamespace(endpoint=endpoint))
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: False)
    monkeypatch.setattr(_SpaStaticHandler, "log_message", lambda *args: None)
    server = SimpleNamespace(server_address=("127.0.0.1", 80))
    status, _, _ = _request(
        _SpaStaticHandler, server, "GET", "/system-plugin-recovery/endpoint",
        ["Host: localhost:80", "Origin: http://localhost"],
    )
    assert status == 200
    endpoint.assert_called_once_with()


@pytest.mark.parametrize("headers, expected", [
    ([], 403),
    ([f"Host: {_AUTHORITY}", f"Host: {_AUTHORITY}"], 403),
    ([f"Host: {_AUTHORITY}", "Origin: https://attacker.example"], 403),
    ([f"Host: {_AUTHORITY}", "Origin: null"], 403),
    ([f"Host: {_AUTHORITY}", "Sec-Fetch-Site: cross-site"], 403),
    ([f"Host: {_AUTHORITY}"], 200),
    ([f"Host: {_AUTHORITY}", f"Origin: http://{_AUTHORITY}"], 200),
])
def test_web_discovery_checks_authority_before_reading_private_endpoint(monkeypatch, headers, expected):
    endpoint = Mock(return_value="http://127.0.0.1:19634/system-plugin-recovery?token=test-only")
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: SimpleNamespace(endpoint=endpoint))
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: False)
    monkeypatch.setattr(_SpaStaticHandler, "log_message", lambda *args: None)
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT))
    status, head, payload = _request(
        _SpaStaticHandler, server, "GET", "/system-plugin-recovery/endpoint", headers,
    )
    assert status == expected
    assert b"Access-Control-Allow-Origin" not in head
    if expected == 200:
        endpoint.assert_called_once_with()
        assert json.loads(payload)["url"].endswith("token=test-only")
    else:
        endpoint.assert_not_called()
        assert b"test-only" not in payload


@pytest.mark.parametrize("prefix", ["", "/portal"])
def test_web_recovery_redirects_legacy_url_to_standalone_mount(monkeypatch, prefix):
    discover = Mock()
    monkeypatch.setattr(system_plugin_recovery.SystemPluginRecovery, "endpoint", discover)
    monkeypatch.setattr(_SpaStaticHandler, "log_message", lambda *args: None)
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT))
    status, head, body = _request(
        _SpaStaticHandler, server, "GET", f"{prefix}/system-plugin-recovery?locale=en&token=discarded",
        [f"Host: {_AUTHORITY}"],
    )

    assert status == 308
    assert b"Location: ./system-plugin-recovery/?locale=en" in head
    assert b"discarded" not in head
    assert not body
    discover.assert_not_called()


@pytest.mark.parametrize("authority,proxied_https", [
    (f"localhost:{_PORT}", False),
    ("203.0.113.10", False),
    ("203.0.113.10", True),
    ("[2001:db8::10]", False),
    ("app.example", False),
    ("app.example", True),
])
def test_web_restore_without_password_accepts_same_origin(monkeypatch, authority, proxied_https):
    endpoint = Mock(return_value="http://127.0.0.1:19634/system-plugin-recovery?token=test-only")
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: SimpleNamespace(endpoint=endpoint))
    monkeypatch.delenv("JIUWENSWARM_REMOTE_RECOVERY_PASSWORD", raising=False)
    monkeypatch.setattr(_SpaStaticHandler, "log_message", lambda *args: None)
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT))
    headers = [f"Host: {authority}"]
    if proxied_https:
        headers.extend([f"Origin: https://{authority}", "X-Forwarded-Proto: https"])
    else:
        headers.append(f"Origin: http://{authority}")
    # Authorized requests advance to JSON body validation (415 here).
    actual, head, payload = _request(
        _SpaStaticHandler, server, "POST", "/system-plugin-recovery/api/restore", headers,
    )

    assert actual == 415
    assert b"WWW-Authenticate" not in head
    assert b"test-only" not in payload
    endpoint.assert_not_called()


@pytest.mark.parametrize("route,scheme,peer,proto,authorized,status,challenge", [
    ("/", "https", "127.0.0.1", ["https"], False, 401, True),
    ("/endpoint", "https", "127.0.0.1", ["https"], False, 401, False),
    ("/endpoint", "http", "203.0.113.9", [], False, 403, False),
    ("/api/restore", "https", "127.0.0.1", ["https"], False, 401, False),
    ("/api/restore", "http", "127.0.0.1", [], True, 403, False),
    ("/api/restore", "http", "203.0.113.9", ["https"], True, 403, False),
    ("/api/restore", "http", "127.0.0.1", ["https", "https"], True, 403, False),
])
def test_web_remote_recovery_challenges_only_standalone_over_trusted_https(
    monkeypatch, route, scheme, peer, proto, authorized, status, challenge,
):
    endpoint = Mock(return_value="http://127.0.0.1:19634/system-plugin-recovery?token=test-only")
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: SimpleNamespace(endpoint=endpoint))
    monkeypatch.setenv("JIUWENSWARM_REMOTE_RECOVERY_PASSWORD", "test-password")
    monkeypatch.setattr(_SpaStaticHandler, "log_message", lambda *args: None)
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT))
    credentials = base64.b64encode(b"recovery:test-password" if authorized else b"recovery:wrong").decode("ascii")
    headers = [
        "Host: app.example", f"Origin: {scheme}://app.example",
        *(f"X-Forwarded-Proto: {value}" for value in proto),
        f"Authorization: Basic {credentials}",
    ]
    method = "POST" if route == "/api/restore" else "GET"
    actual, head, payload = _request(
        _SpaStaticHandler, server, method, f"/system-plugin-recovery{route}", headers, client_address=peer,
    )

    assert actual == status
    assert (b"WWW-Authenticate: Basic " in head) is challenge
    assert b"test-only" not in payload
    endpoint.assert_not_called()


def test_web_discovery_rejects_a_registered_non_loopback_target(monkeypatch):
    endpoint = Mock(return_value="http://attacker.example:19634/system-plugin-recovery?token=test-only")
    monkeypatch.setattr(system_plugin_recovery, "SystemPluginRecovery", lambda: SimpleNamespace(endpoint=endpoint))
    monkeypatch.setattr("jiuwenswarm.enterprise.is_enterprise", lambda: False)
    monkeypatch.setattr(_SpaStaticHandler, "log_message", lambda *args: None)
    server = SimpleNamespace(server_address=("127.0.0.1", _PORT))
    status, _, payload = _request(
        _SpaStaticHandler,
        server,
        "GET",
        "/system-plugin-recovery/endpoint",
        [f"Host: {_AUTHORITY}"],
    )

    assert status == 503
    assert json.loads(payload) == {"error": "recovery_unavailable"}
