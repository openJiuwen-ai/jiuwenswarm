from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    format_ws_diagnostics,
)


def test_format_ws_exception_diagnostics_includes_close_fields():
    exc = ConnectionClosedError(None, None)

    text = format_ws_diagnostics(describe_ws_exception(exc))

    assert "exc_type='ConnectionClosedError'" in text
    assert "message='no close frame received or sent'" in text
    assert "close_code=1006" in text
    assert "close_reason=''" in text
    assert "rcvd=None" in text
    assert "sent=None" in text


def test_web_lazy_diagnostics_wrapper_preserves_positional_parts():
    text = app_web._format_ws_diagnostics(
        {"client": ("127.0.0.1", 57106)},
        {"exc_type": "TimeoutError", "message": "timed out"},
        upstream_port=19001,
    )

    assert "client=('127.0.0.1', 57106)" in text
    assert "exc_type='TimeoutError'" in text
    assert "message='timed out'" in text
    assert "upstream_port=19001" in text


def test_websocket_proxy_restores_timeout_and_handles_slow_handshake(monkeypatch):
    class TimeoutSocket:
        def __init__(self):
            self.timeout = None
            self.closed = False

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, _data):
            return None

        def recv(self, _size):
            raise TimeoutError("timed out")

        def close(self):
            self.closed = True

    upstream = TimeoutSocket()
    connect_timeouts = []
    monkeypatch.setattr(
        app_web.socket,
        "create_connection",
        lambda _address, timeout: connect_timeouts.append(timeout) or upstream,
    )

    handler = object.__new__(_SpaStaticHandler)
    handler.ws_target = "ws://127.0.0.1:19001"
    handler.path = "/ws"
    handler.command = "GET"
    handler.headers = {}
    handler.ws_disable_compress = False
    handler.client_address = ("127.0.0.1", 57106)
    handler._get_auth_cookie = lambda: None
    logged_errors = []
    sent_errors = []
    handler.log_error = lambda *args: logged_errors.append(args)
    handler.send_error = lambda *args: sent_errors.append(args)

    handler._proxy_websocket_tunnel()

    assert connect_timeouts == [0.25]
    assert upstream.timeout == handler._WS_CONNECT_TIMEOUT
    assert upstream.closed is True
    assert sent_errors == [(502, "proxy ws error")]
    assert "exc_type='TimeoutError'" in logged_errors[0][1]
