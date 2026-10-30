"""Enterprise HTTP transports customer identity independently of platform auth."""
from jiuwenswarm.common import request_ext
from gateway.channel_manager.web import web_http_dispatch


def test_enterprise_customer_token_needs_no_additional_environment(monkeypatch):
    monkeypatch.setattr(web_http_dispatch, "is_enterprise", lambda: True)
    monkeypatch.delenv(request_ext.ENV_FORWARD_HEADERS, raising=False)
    monkeypatch.delenv(request_ext.ENV_FORWARD_HEADERS_LEGACY, raising=False)
    request_ext.set_forward_headers(None)
    headers = {"Authorization": "Bearer platform", "x-user-token": "Bearer customer"}
    ext = web_http_dispatch._build_http_ext(headers, {})
    assert ext == {"X-User-Token": "Bearer customer"}
    assert headers["Authorization"] == "Bearer platform"
    encoded = request_ext.encode_internal_header(ext)
    assert request_ext.decode_internal_header(encoded) == ext


def test_personal_mode_does_not_add_customer_token(monkeypatch):
    monkeypatch.setattr(web_http_dispatch, "is_enterprise", lambda: False)
    monkeypatch.delenv(request_ext.ENV_FORWARD_HEADERS, raising=False)
    monkeypatch.delenv(request_ext.ENV_FORWARD_HEADERS_LEGACY, raising=False)
    request_ext.set_forward_headers(None)
    assert web_http_dispatch._build_http_ext({"X-User-Token": "Bearer customer"}, {}) is None


def test_existing_extension_fields_are_preserved(monkeypatch):
    monkeypatch.setattr(web_http_dispatch, "is_enterprise", lambda: True)
    request_ext.set_forward_headers(["X-Trace-Tag"])
    try:
        ext = web_http_dispatch._build_http_ext(
            {"X-User-Token": "Bearer customer", "X-Trace-Tag": "trace"}, {}
        )
        assert ext == {"X-User-Token": "Bearer customer", "X-Trace-Tag": "trace"}
    finally:
        request_ext.set_forward_headers(None)
