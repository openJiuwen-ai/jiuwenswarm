"""Unit tests: HTTP surface (CORS, static, snapshot, long poll, /ui-state)."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from team_collab_viz.http_server import ServiceContext  # noqa: E402
from team_collab_viz.lifecycle import LocalService  # noqa: E402
from team_collab_viz.snapshot import build_snapshot  # noqa: E402

PACKAGE_ID = "webui.team-collab-viz"

#: Contract §5.3 defaults; ``GET /ui-state`` must always return all nine keys.
_DEFAULT_STATE = {
    "active_tab": "team-info",
    "expanded_member_ids": [],
    "member_page": 1,
    "selected_task_id": None,
    "detail_open": False,
    "search": {"team_info": "", "task_graph": ""},
    "status_filter": [],
    "canvas": {"layout_mode": "horizontal", "pan": {"x": 0.0, "y": 0.0}, "zoom": 1.0},
    "theme": "system",
}



class _Owner:
    """Lifetime anchor; the listener keeps only a weak reference to it."""


def _request(port: int, path: str, method: str = "GET", body=None, headers=None):
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def _json_body(raw: bytes):
    return json.loads(raw.decode("utf-8"))


@pytest.fixture
def server(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "team-info.html").write_text("<!doctype html><p>info</p>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    context = ServiceContext(PACKAGE_ID, web_root=web)
    owner = _Owner()
    service = LocalService(context, owner).start()
    try:
        yield context, service
    finally:
        service.close("test")


def test_health_reports_listener(server):
    context, service = server
    status, headers, body = _request(service.port, "/health")
    payload = _json_body(body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["port"] == service.port
    assert payload["bind"] == "127.0.0.1"
    assert payload["draining_ports"] == []
    assert "snapshot" in payload["capabilities"]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert headers["Cache-Control"] == "no-store"


def test_snapshot_placeholder_is_200_empty(server):
    _, service = server
    status, headers, body = _request(service.port, "/snapshot?sid=s1")
    payload = _json_body(body)
    assert status == 200
    assert payload["schema_version"] == "1.1"
    assert payload["scope"] == {"kind": "session", "key": "s1"}
    assert payload["team"] is None
    assert payload["stale"] is True
    assert payload["stale_reason"] == "no_session_map"
    assert payload["revision"] == 1
    assert headers["ETag"] == '"1"'


def test_snapshot_without_sid_uses_global_scope(server):
    _, service = server
    status, _, body = _request(service.port, "/snapshot")
    payload = _json_body(body)
    assert status == 200
    assert payload["scope"] == {"kind": "global", "key": "global"}


def test_snapshot_full_and_since_semantics(server):
    context, service = server
    snapshot = build_snapshot(
        scope_kind="session",
        scope_key="s1",
        team={"team_name": "t1", "display_name": "团队一", "leader_member_name": "leader", "desc": None,
              "created": 1},
        members=[],
        tasks=[],
        dependencies={},
        session_map_source="event",
        last_event_at=10,
        last_success_at=20,
        stale=False,
        stale_reason=None,
        lifecycle_event="team_created",
        has_lifecycle_fact=True,
    )
    assert context.snapshots.publish("s1", snapshot) == 1

    status, _, body = _request(service.port, "/snapshot?sid=s1")
    assert status == 200
    assert _json_body(body)["revision"] == 1

    context.snapshots.publish("s1", dict(snapshot))
    status, _, body = _request(service.port, "/snapshot?sid=s1&since=1")
    payload = _json_body(body)
    assert status == 200
    assert payload["revision"] == 2
    assert payload["team"]["display_name"] == "团队一"

    status, _, body = _request(service.port, "/snapshot?sid=s1&since=2&wait=0")
    assert status == 200
    unchanged = _json_body(body)
    assert unchanged["unchanged"] is True
    assert unchanged["revision"] == 2
    assert unchanged["generated_at"] == payload["generated_at"]
    assert isinstance(unchanged["served_at"], int)
    assert unchanged["stale"] is False

    status, _, body = _request(service.port, "/snapshot?sid=s1&since=99&wait=0")
    payload = _json_body(body)
    assert payload["revision_reset"] is True
    assert payload["revision"] == 2


def test_degrade_over_http_keeps_data_and_bumps_once(server):
    context, service = server
    good = _placeholder("s1")
    good["team"] = {"team_name": "t1", "display_name": "团队一", "leader_member_name": "leader",
                    "desc": None, "created": 1, "status": "running", "status_label": "进行中",
                    "status_source": "lifecycle", "progress": {"total": 0, "done": 0, "ratio": 0.0}}
    good["stale"] = False
    good["stale_reason"] = None
    good["generated_at"] = 1000
    assert context.snapshots.publish("s1", good) == 1

    # D5: first false->true bumps the revision so a long poll can push it.
    assert context.snapshots.mark_stale("s1", "ops_error") == 2
    status, _, body = _request(service.port, "/snapshot?sid=s1")
    degraded = _json_body(body)
    assert status == 200
    assert degraded["team"] == good["team"]
    assert degraded["generated_at"] == 1000
    assert degraded["stale"] is True
    assert degraded["stale_reason"] == "ops_error"
    assert degraded["served_at"] > 1000

    # D5: repeating the failure keeps the revision stable.
    assert context.snapshots.mark_stale("s1", "ops_error") == 2
    status, _, body = _request(service.port, "/snapshot?sid=s1&since=2&wait=0")
    unchanged = _json_body(body)
    assert unchanged["unchanged"] is True
    assert unchanged["stale"] is True  # the badge still flips without a new revision

    # D7: /health mirrors the snapshot flag.
    _, _, body = _request(service.port, "/health")
    assert _json_body(body)["snapshot"]["stale"] is True

    # D5: recovery bumps once and clears the flag.
    good_again = _placeholder("s1")
    good_again["team"] = good["team"]
    good_again["stale"] = False
    good_again["stale_reason"] = None
    assert context.snapshots.publish("s1", good_again) == 3
    status, _, body = _request(service.port, "/snapshot?sid=s1")
    recovered = _json_body(body)
    assert recovered["stale"] is False
    assert recovered["stale_reason"] is None


def test_snapshot_rejects_bad_query_with_cors(server):
    _, service = server
    for query in ("since=abc", "wait=-1", "wait=99"):
        status, headers, body = _request(service.port, f"/snapshot?sid=s1&{query}")
        assert status == 400
        assert _json_body(body)["code"] == "BAD_QUERY"
        assert headers["Access-Control-Allow-Origin"] == "*"


def test_long_poll_wakes_on_publish(server):
    context, service = server
    context.snapshots.ensure("s1", lambda: _placeholder("s1"))
    result: dict = {}

    def poll():
        result["response"] = _request(service.port, "/snapshot?sid=s1&since=1&wait=10")

    thread = threading.Thread(target=poll, daemon=True)
    started = time.monotonic()
    thread.start()
    time.sleep(0.4)
    snapshot = build_snapshot(
        scope_kind="session",
        scope_key="s1",
        team=None,
        members=[],
        tasks=[],
        dependencies={},
        session_map_source="none",
        last_event_at=None,
        last_success_at=None,
        stale=True,
        stale_reason="no_session_map",
        lifecycle_event=None,
        has_lifecycle_fact=False,
    )
    context.snapshots.publish("s1", snapshot)
    thread.join(timeout=10)
    elapsed = time.monotonic() - started
    assert not thread.is_alive()
    status, _, body = result["response"]
    assert status == 200
    assert _json_body(body)["revision"] == 2
    assert elapsed < 5.0


def _placeholder(key: str):
    return build_snapshot(
        scope_kind="session",
        scope_key=key,
        team=None,
        members=[],
        tasks=[],
        dependencies={},
        session_map_source="none",
        last_event_at=None,
        last_success_at=None,
        stale=True,
        stale_reason="no_session_map",
        lifecycle_event=None,
        has_lifecycle_fact=False,
    )


def test_preflight_allows_patch(server):
    _, service = server
    status, headers, _ = _request(service.port, "/ui-state", method="OPTIONS")
    assert status == 204
    assert "PATCH" in headers["Access-Control-Allow-Methods"]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in headers


def test_ui_state_get_empty_then_put_and_patch(server):
    _, service = server
    status, _, body = _request(service.port, "/ui-state?sid=s1")
    payload = _json_body(body)
    assert status == 200
    assert payload["empty"] is True
    # U2/U3: the state is never null — defaults are filled in even when unwritten.
    assert payload["state"] == _DEFAULT_STATE
    assert payload["updated_at"] is None

    status, _, body = _request(service.port, "/ui-state", method="PUT",
                               body={"schema_version": "1.1", "sid": "s1", "state": {"theme": "dark"}})
    payload = _json_body(body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["state"]["theme"] == "dark"
    assert payload["bytes"] > 0

    status, _, body = _request(service.port, "/ui-state", method="PATCH",
                               body={"sid": "s1", "state": {"member_page": 3}})
    payload = _json_body(body)
    assert status == 200
    assert payload["state"]["member_page"] == 3
    assert payload["state"]["theme"] == "dark"

    status, _, body = _request(service.port, "/ui-state?sid=s1")
    payload = _json_body(body)
    assert payload["empty"] is False
    assert payload["state"]["member_page"] == 3
    assert payload["state"]["theme"] == "dark"


def test_ui_state_patch_from_two_clients_keeps_both(server):
    _, service = server
    _request(service.port, "/ui-state", method="PUT", body={"sid": "s2", "state": {}})
    _request(service.port, "/ui-state", method="PATCH", body={"sid": "s2", "state": {"member_page": 5}})
    _request(service.port, "/ui-state", method="PATCH",
             body={"sid": "s2", "state": {"canvas": {"zoom": 2.0}}})
    _, _, body = _request(service.port, "/ui-state?sid=s2")
    state = _json_body(body)["state"]
    assert state["member_page"] == 5
    assert state["canvas"]["zoom"] == 2.0
    assert state["canvas"]["layout_mode"] == "horizontal"


def test_ui_state_contract_u1_to_u4(server):
    """§5.3.1 U1–U4: complete nine-key state, null resets, canvas sub-merge."""
    _, service = server
    sid = "u1"

    # (1) never written -> empty=true AND all nine keys at their defaults
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    first = _json_body(body)
    assert first["empty"] is True
    assert first["state"] == _DEFAULT_STATE

    # (2) PATCH a scalar -> GET returns it
    _, _, body = _request(service.port, "/ui-state", method="PATCH",
                          body={"sid": sid, "state": {"selected_task_id": "t1"}})
    assert _json_body(body)["state"]["selected_task_id"] == "t1"
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    assert _json_body(body)["state"]["selected_task_id"] == "t1"

    # (3) explicit null resets to the default but the key must remain (U1)
    _, _, body = _request(service.port, "/ui-state", method="PATCH",
                          body={"sid": sid, "state": {"selected_task_id": None}})
    patch_state = _json_body(body)["state"]
    assert "selected_task_id" in patch_state
    assert patch_state["selected_task_id"] is None
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    assert _json_body(body)["state"]["selected_task_id"] is None

    # (4) canvas sub-merge keeps the other defaults (U2/U4)
    _, _, body = _request(service.port, "/ui-state", method="PATCH",
                          body={"sid": sid, "state": {"canvas": {"zoom": 2.0}}})
    canvas = _json_body(body)["state"]["canvas"]
    assert canvas == {"layout_mode": "horizontal", "pan": {"x": 0.0, "y": 0.0}, "zoom": 2.0}
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    assert _json_body(body)["state"]["canvas"]["layout_mode"] == "horizontal"

    # (5) every response carries all nine keys, none missing
    for method, kwargs in (
        ("GET", {"path": f"/ui-state?sid={sid}"}),
        ("PUT", {"path": "/ui-state", "method": "PUT", "body": {"sid": sid, "state": {"theme": "dark"}}}),
        ("PATCH", {"path": "/ui-state", "method": "PATCH", "body": {"sid": sid, "state": {"member_page": 2}}}),
    ):
        path = kwargs.pop("path")
        _, _, body = _request(service.port, path, **kwargs)
        state = _json_body(body)["state"]
        assert set(state) == set(_DEFAULT_STATE), method
        assert _json_body(body)["empty"] is False, method


def test_ui_state_get_put_round_trip(server):
    """§5.3.1 U5: ``null`` in PUT means "use the default"; GET→PUT round trips."""
    _, service = server
    sid = "u5"
    custom = {
        "active_tab": "task-graph",
        "expanded_member_ids": ["m1", "m2"],
        "member_page": 3,
        "selected_task_id": None,  # default is null — must survive the round trip
        "detail_open": True,
        "search": {"team_info": "abc", "task_graph": ""},
        "status_filter": ["in_progress"],
        "canvas": {"layout_mode": "vertical", "pan": {"x": 12.0, "y": -4.5}, "zoom": 2.0},
        "theme": "dark",
    }
    status, _, body = _request(service.port, "/ui-state", method="PUT", body={"sid": sid, "state": custom})
    assert status == 200
    assert _json_body(body)["state"] == custom

    # GET the complete nine-key state and PUT it straight back (front-end style)
    status, _, body = _request(service.port, f"/ui-state?sid={sid}")
    fetched = _json_body(body)["state"]
    status, _, body = _request(service.port, "/ui-state", method="PUT", body={"sid": sid, "state": fetched})
    assert status == 200
    status, _, body = _request(service.port, f"/ui-state?sid={sid}")
    assert _json_body(body)["state"] == custom

    # every key set to null -> all defaults, no 400
    status, _, body = _request(service.port, "/ui-state", method="PUT",
                               body={"sid": "u5-null", "state": {key: None for key in _DEFAULT_STATE}})
    assert status == 200
    assert _json_body(body)["state"] == _DEFAULT_STATE


def test_ui_state_subkey_whitelist(server):
    """§5.3.1 U7: nested whitelists, all-or-nothing, no partial write."""
    _, service = server
    sid = "u7"

    # (1) known sub-key -> 200 and effective
    status, _, body = _request(service.port, "/ui-state", method="PATCH",
                               body={"sid": sid, "state": {"search": {"team_info": "x"}}})
    assert status == 200
    assert _json_body(body)["state"]["search"]["team_info"] == "x"

    # (2) unknown sub-key -> 400 and nothing written
    status, _, body = _request(service.port, "/ui-state", method="PATCH",
                               body={"sid": sid, "state": {"search": {"evil": "团队数据"}}})
    assert status == 400
    assert _json_body(body)["code"] == "STATE_KEY_NOT_ALLOWED"
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    state = _json_body(body)["state"]
    assert set(state["search"]) == {"team_info", "task_graph"}
    assert state["search"]["team_info"] == "x"          # untouched by the rejected write
    assert "evil" not in state["search"]

    # (3) valid pan -> 200
    status, _, body = _request(service.port, "/ui-state", method="PATCH",
                               body={"sid": sid, "state": {"canvas": {"pan": {"x": 1, "y": 2}}}})
    assert status == 200
    assert _json_body(body)["state"]["canvas"]["pan"] == {"x": 1.0, "y": 2.0}

    # (4) unknown pan sub-key / unknown canvas sub-key -> 400, no partial write
    for payload in ({"canvas": {"pan": {"z": 1}}}, {"canvas": {"nodes": []}},
                    {"search": {"team": "secret"}}):
        for method in ("PATCH", "PUT"):
            status, _, body = _request(service.port, "/ui-state", method=method,
                                       body={"sid": sid, "state": payload})
            assert status == 400, (method, payload)
            assert _json_body(body)["code"] == "STATE_KEY_NOT_ALLOWED", (method, payload)
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    state = _json_body(body)["state"]
    assert state["canvas"]["pan"] == {"x": 1.0, "y": 2.0}
    assert set(state["canvas"]) == {"layout_mode", "pan", "zoom"}

    # (5) PUT round trip with the same shapes still 200
    status, _, body = _request(service.port, "/ui-state", method="PUT", body={"sid": sid, "state": state})
    assert status == 200
    _, _, body = _request(service.port, f"/ui-state?sid={sid}")
    assert _json_body(body)["state"] == state


def test_ui_state_rejects_team_data_and_keeps_previous_state(server):
    _, service = server
    _request(service.port, "/ui-state", method="PUT", body={"sid": "s3", "state": {"theme": "light"}})
    for method in ("PUT", "PATCH"):
        status, headers, body = _request(service.port, "/ui-state", method=method,
                                         body={"sid": "s3", "state": {"tasks": []}})
        assert status == 400
        assert _json_body(body)["code"] == "STATE_KEY_NOT_ALLOWED"
        assert headers["Access-Control-Allow-Origin"] == "*"
    _, _, body = _request(service.port, "/ui-state?sid=s3")
    assert _json_body(body)["state"]["theme"] == "light"


def test_ui_state_rejects_bad_json(server):
    _, service = server
    status, _, body = _request(service.port, "/ui-state", method="PUT", body=None,
                               headers={"Content-Type": "application/json", "Content-Length": "0"})
    assert status == 400
    assert _json_body(body)["code"] in ("BAD_BODY", "BAD_JSON")


def test_static_files_use_real_content_types(server):
    _, service = server
    status, headers, body = _request(service.port, "/web/team-info.html")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"info" in body

    status, headers, _ = _request(service.port, "/web/assets/app.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"

    status, headers, _ = _request(service.port, "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"

    status, _, _ = _request(service.port, "/favicon.ico")
    assert status == 204


def test_static_rejects_traversal(server):
    _, service = server
    for path in ("/web/../manifest.yaml", "/web/..%2Fmanifest.yaml", "/web/assets/../../manifest.yaml",
                 "/web/%2e%2e/manifest.yaml", "/web//etc/passwd"):
        status, _, body = _request(service.port, path)
        assert status == 404, path
        assert _json_body(body)["code"] == "NOT_FOUND"


def test_unsupported_methods_still_answer_json_with_cors(server):
    """§5.0: CORS on **every** response, including verbs we do not implement."""
    _, service = server
    for path in ("/snapshot?sid=s1", "/health", "/ui-state?sid=s1", "/web/team-info.html", "/"):
        for method in ("POST", "DELETE", "TRACE"):
            status, headers, body = _request(service.port, path, method=method)
            assert status == 405, (method, path, status)
            assert headers.get("Access-Control-Allow-Origin") == "*", (method, path)
            assert "PATCH" in headers.get("Access-Control-Allow-Methods", ""), (method, path)
            assert "Access-Control-Allow-Credentials" not in headers, (method, path)
            assert headers.get("Content-Type", "").startswith("application/json"), (method, path)
            assert headers.get("Cache-Control") == "no-store", (method, path)
            assert _json_body(body)["code"] == "METHOD_NOT_ALLOWED", (method, path)

    # HEAD keeps the CORS headers even though it carries no entity body.
    status, headers, body = _request(service.port, "/health", method="HEAD")
    assert status == 405
    assert headers.get("Access-Control-Allow-Origin") == "*"
    assert "PATCH" in headers.get("Access-Control-Allow-Methods", "")
    assert body == b""


def test_every_response_states_connection_close(server):
    """O1: no response may imply keep-alive on a socket the server closes."""
    _, service = server
    cases = (
        ("GET", "/health"),
        ("GET", "/snapshot?sid=s1&wait=0"),
        ("GET", "/ui-state?sid=s1"),
        ("GET", "/nope"),
        ("OPTIONS", "/snapshot"),
        ("POST", "/snapshot"),
        ("DELETE", "/ui-state"),
        ("TRACE", "/health"),
        ("HEAD", "/health"),
    )
    for method, path in cases:
        status, headers, _ = _request(service.port, path, method=method)
        assert headers.get("Connection", "").lower() == "close", (method, path, status, headers)
        assert headers.get("Access-Control-Allow-Origin") == "*", (method, path)


def _raw_request(port: int, path: str) -> "socket.socket":
    """Open one raw socket and fire a request without reading the response."""
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.sendall(
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: */*\r\n\r\n".encode("ascii")
    )
    return connection


def _read_until_eof(connection: "socket.socket", timeout: float = 3.0) -> tuple[bytes, bool]:
    """Drain ``connection`` until the peer closes; returns ``(received, saw_eof)``."""
    chunks: list[bytes] = []
    connection.settimeout(timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = connection.recv(4096)
        except socket.timeout:  # noqa: UP041 - py3.11 alias
            return b"".join(chunks), False
        except OSError:  # ConnectionResetError / ConnectionAbortedError
            return b"".join(chunks), True
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


def test_drain_closes_in_flight_long_poll(server):
    """F2: closing the service must cut a parked long poll instead of waiting 25s.

    Invariants only — deliberately *not* "the peer reads zero bytes": ``closing``
    also unblocks the parked handler, which may legally flush its final
    ``unchanged`` 200 before the socket is cut. What F2 owns is that the server
    ends the connection within seconds, leaves no tracked socket, frees the port
    and raises nothing on the way out.
    """
    context, service = server
    # Revision 1 exists, so ``since=1`` really parks until a change or the timeout.
    context.snapshots.ensure("s1", lambda: _placeholder("s1"))
    port = service.port

    errors: list[str] = []
    original_handle_error = service.httpd.handle_error

    def _record_error(request, client_address):  # type: ignore[no-untyped-def]
        errors.append("handle_error")
        original_handle_error(request, client_address)

    service.httpd.handle_error = _record_error  # type: ignore[method-assign]
    thread_errors: list[BaseException] = []
    previous_hook = threading.excepthook

    def _record_thread_error(args):  # type: ignore[no-untyped-def]
        thread_errors.append(args.exc_value or RuntimeError(args.exc_type))

    threading.excepthook = _record_thread_error  # type: ignore[assignment]
    try:
        parked = _raw_request(port, "/snapshot?sid=s1&since=1&wait=25")
        deadline = time.monotonic() + 5.0
        while service.httpd.active_connection_count() != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.httpd.active_connection_count() == 1

        # ① the drain returns far below the 25s poll window
        started = time.monotonic()
        service.close("test-drain")
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, elapsed

        # ② the peer is unblocked within seconds: EOF, optionally after a valid response
        body, saw_eof = _read_until_eof(parked)
        assert saw_eof, f"server did not close the parked connection: {body[:80]!r}"
        if body:
            assert body.startswith(b"HTTP/1.1 200"), body[:80]
        # ③ the connection really is finished, not merely idle
        assert parked.recv(16) == b""
        parked.close()

        # ④ no socket left behind
        assert service.httpd.active_connection_count() == 0

        # ⑤ the port can be bound again (kernel-level release)
        rebind = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rebind.bind(("127.0.0.1", port))
        finally:
            rebind.close()

        # ⑥ nothing escaped: no handle_error, no thread traceback
        time.sleep(0.1)
        assert errors == [], errors
        assert thread_errors == [], thread_errors
    finally:
        threading.excepthook = previous_hook


def test_unknown_path_and_method_are_json_errors(server):
    _, service = server
    status, _, body = _request(service.port, "/nope")
    assert status == 404
    assert _json_body(body)["code"] == "NOT_FOUND"
    status, _, _ = _request(service.port, "/health", method="HEAD")
    assert status == 405
