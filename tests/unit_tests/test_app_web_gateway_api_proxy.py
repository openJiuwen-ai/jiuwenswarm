from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from jiuwenswarm.channels.web.app_web import (
    _SpaStaticHandler,
    _inject_user_web_runtime_config,
    _parse_login_auth_simulate,
)


def _handler(path: str) -> _SpaStaticHandler:
    handler = object.__new__(_SpaStaticHandler)
    handler.path = path
    handler.web_http_target = "http://gateway:19002"
    return handler


def _document_handler(path: str, accept: str, directory: Path) -> _SpaStaticHandler:
    handler = object.__new__(_SpaStaticHandler)
    handler.path = path
    handler.directory = str(directory)
    headers = EmailMessage()
    if accept:
        headers["Accept"] = accept
    handler.headers = headers
    return handler


def test_gateway_api_is_classified_as_gateway_web_http_route() -> None:
    assert _handler("/gateway-api/v1/chat/completions")._is_web_http_route()


def test_gateway_api_prefix_is_rewritten_for_upstream(monkeypatch) -> None:
    handler = _handler("/gateway-api/v1/chat/completions?mode=work")
    captured: dict[str, str] = {}

    def proxy_http() -> None:
        captured["path"] = handler.path
        captured["target"] = handler.api_target

    monkeypatch.setattr(handler, "_proxy_http", proxy_http)
    handler._proxy_web_http()

    assert captured == {
        "path": "/api/v1/chat/completions?mode=work",
        "target": "http://gateway:19002",
    }
    assert handler.path == "/gateway-api/v1/chat/completions?mode=work"


@pytest.mark.parametrize(
    ("edition",),
    [("personal",), ("enterprise",)],
)
def test_user_web_runtime_mode_injection_preserves_property_names(
    edition: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_index = (
        Path(__file__).resolve().parents[2]
        / "jiuwenswarm"
        / "channels"
        / "web"
        / "frontend"
        / "index.html"
    ).read_text(encoding="utf-8")

    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    rendered = _inject_user_web_runtime_config(frontend_index)

    assert f"window.__JIUWENSWARM_EDITION__ = '{edition}'" in rendered
    assert "__JIUWENSWARM_EDITION_VALUE__" not in rendered
    assert "window.__JIUWEN_LOGIN_AUTH_SIMULATE__ = 'true'" in rendered
    assert "__JIUWEN_LOGIN_AUTH_SIMULATE_VALUE__" not in rendered


def test_login_auth_simulate_config_is_strict_and_defaults_to_true() -> None:
    assert _parse_login_auth_simulate(None) is True
    assert _parse_login_auth_simulate("") is True
    assert _parse_login_auth_simulate(" TRUE ") is True
    assert _parse_login_auth_simulate("false") is False
    with pytest.raises(ValueError, match="期望 true 或 false"):
        _parse_login_auth_simulate("yes")

def test_document_request_for_root_and_index(tmp_path: Path) -> None:
    assert _document_handler("/", "text/html", tmp_path)._is_document_request()
    assert _document_handler("/index.html", "text/html", tmp_path)._is_document_request()


def test_document_request_skips_when_accept_header_lacks_html(tmp_path: Path) -> None:
    assert not _document_handler("/", "*/*", tmp_path)._is_document_request()
    assert not _document_handler("/", "", tmp_path)._is_document_request()


def test_document_request_treats_spa_routes_as_document(tmp_path: Path) -> None:
    for path in ("/chat", "/agents", "/sessions/123", "/teams"):
        assert _document_handler(path, "text/html", tmp_path)._is_document_request(), path


def test_document_request_serves_existing_static_files_as_is(tmp_path: Path) -> None:
    (tmp_path / "logo.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    assert not _document_handler("/logo.svg", "text/html", tmp_path)._is_document_request()
    assert not _document_handler(
        "/assets/index-abc.js", "text/html", tmp_path
    )._is_document_request()


def test_document_request_rejects_path_traversal_as_document(tmp_path: Path) -> None:
    assert not _document_handler(
        "/../etc/passwd", "text/html", tmp_path
    )._is_document_request()
