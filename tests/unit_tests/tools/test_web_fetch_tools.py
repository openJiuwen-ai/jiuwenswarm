from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests


_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = (
    _REPO_ROOT
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "tools"
    / "web_fetch_tools.py"
)


def _load_module(*, reload: bool = False):
    if not reload and "web_fetch_tools_mod" in sys.modules:
        return sys.modules["web_fetch_tools_mod"]

    tools_pkg = sys.modules.get("jiuwenswarm.agents.harness.common.tools")
    if tools_pkg is None:
        tools_pkg = types.ModuleType("jiuwenswarm.agents.harness.common.tools")
        tools_pkg.__path__ = [str(_MODULE_PATH.parent)]
        tools_pkg.__package__ = "jiuwenswarm.agents.harness.common.tools"
        sys.modules["jiuwenswarm.agents.harness.common.tools"] = tools_pkg

    # Drop cached module so source edits are visible under pytest re-runs.
    sys.modules.pop("web_fetch_tools_mod", None)
    spec = importlib.util.spec_from_file_location("web_fetch_tools_mod", str(_MODULE_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["web_fetch_tools_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module(reload=True)


def _response(status_code: int, *, url: str = "https://example.com") -> Mock:
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.url = url
    response.headers = {"Content-Type": "text/html; charset=utf-8"}
    response.content = b"<html><title>Example</title><body>content</body></html>"
    response.encoding = "utf-8"
    response.apparent_encoding = "utf-8"
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} response",
            response=response,
        )
    return response


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_jina_fallback_is_disabled_by_default(monkeypatch, status_code):
    monkeypatch.delenv("JIUWENSWARM_ENABLE_JINA_FETCH", raising=False)
    response = _response(status_code)

    with (
        patch.object(_mod, "_http_get", return_value=response),
        patch.object(_mod, "_fetch_via_jina_reader_sync") as jina_fetch,
        pytest.raises(requests.HTTPError),
    ):
        _mod._fetch_webpage_sync("https://example.com", 30)

    jina_fetch.assert_not_called()


@pytest.mark.parametrize("enabled_value", ["1", "true", "yes", "on", "enabled"])
def test_jina_fallback_can_be_enabled(monkeypatch, enabled_value):
    monkeypatch.setenv("JIUWENSWARM_ENABLE_JINA_FETCH", enabled_value)
    response = _response(403)
    fallback_result = {
        "url": "https://example.com",
        "status_code": 200,
        "title": "",
        "content": "from jina",
    }

    with (
        patch.object(_mod, "_http_get", return_value=response),
        patch.object(
            _mod,
            "_fetch_via_jina_reader_sync",
            return_value=fallback_result,
        ) as jina_fetch,
    ):
        result = _mod._fetch_webpage_sync("https://example.com", 30)

    assert result == fallback_result
    jina_fetch.assert_called_once_with("https://example.com", 30)


def test_successful_direct_fetch_does_not_use_jina(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_ENABLE_JINA_FETCH", "true")
    response = _response(200)

    with (
        patch.object(_mod, "_http_get", return_value=response),
        patch.object(_mod, "_fetch_via_jina_reader_sync") as jina_fetch,
    ):
        result = _mod._fetch_webpage_sync("https://example.com", 30)

    assert result["status_code"] == 200
    assert result["title"] == "Example"
    jina_fetch.assert_not_called()


def test_fetch_rejects_doc_url_without_http():
    doc_url = "https://www.stats.gov.cn/xw/tjxw/tzgg/202302/P020230227339331821024.doc"
    with patch.object(_mod, "_http_get") as http_get, pytest.raises(ValueError, match="unsupported binary content"):
        _mod._fetch_webpage_sync(doc_url, 8)
    http_get.assert_not_called()


def test_fetch_rejects_ole_body_even_with_html_url():
    ole = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]) + b"\x00" * 32
    response = _response(200, url="https://example.com/article")
    response.headers = {"Content-Type": "application/msword"}
    response.content = ole
    response.encoding = "ISO-8859-1"
    response.apparent_encoding = "ISO-8859-1"

    with (
        patch.object(_mod, "_http_get", return_value=response),
        pytest.raises(ValueError, match="unsupported binary content"),
    ):
        _mod._fetch_webpage_sync("https://example.com/article", 8)


def test_fetch_rejects_pdf_magic_with_octet_stream():
    response = _response(200, url="https://example.com/download")
    response.headers = {"Content-Type": "application/octet-stream"}
    response.content = b"%PDF-1.7 binary-junk"

    with (
        patch.object(_mod, "_http_get", return_value=response),
        pytest.raises(ValueError, match="unsupported binary content"),
    ):
        _mod._fetch_webpage_sync("https://example.com/download", 8)


def test_fetch_allows_octet_stream_html():
    response = _response(200, url="https://example.com/page")
    response.headers = {"Content-Type": "application/octet-stream"}
    response.content = b"<!DOCTYPE html><html><title>Ok</title><body>hello</body></html>"
    response.encoding = "utf-8"
    response.apparent_encoding = "utf-8"

    with patch.object(_mod, "_http_get", return_value=response):
        result = _mod._fetch_webpage_sync("https://example.com/page", 8)

    assert result["status_code"] == 200
    assert "hello" in result["content"] or "Ok" in result["title"]


@pytest.mark.asyncio
async def test_fetch_single_url_returns_error_for_doc_extension():
    doc_url = "https://www.stats.gov.cn/file/report.doc"
    result = await _mod._fetch_single_url(
        doc_url,
        max_chars=8000,
        timeout_seconds=8,
        use_cache=True,
        cache=None,
    )
    assert result["error"]
    assert "unsupported binary content" in result["error"]
    assert result["content"] == ""


def test_plain_text_json_still_allowed():
    response = _response(200, url="https://example.com/data.json")
    response.headers = {"Content-Type": "application/json; charset=utf-8"}
    response.content = b'{"hello":"world"}'
    response.encoding = "utf-8"
    response.apparent_encoding = "utf-8"

    with patch.object(_mod, "_http_get", return_value=response):
        result = _mod._fetch_webpage_sync("https://example.com/data.json", 8)

    assert result["status_code"] == 200
    assert "hello" in result["content"]
