import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthContext, AuthResult
from jiuwenswarm.extensions.agentos.auth.common import (
    extract_token,
    extract_headers,
    get_remote_addr
)


class FakeWs:
    def __init__(self, path="", headers=None, remote_address=None, request_headers=None):
        self.path = path
        self._request = MagicMock()
        self._request.headers = headers
        if request_headers is not None:
            self.request_headers = request_headers
        else:
            self.request_headers = None
        self.remote_address = remote_address
        self.closed = False
        self.user_id = None

    @property
    def request(self):
        return self._request if self._request.headers is not None else None

    def close(self):
        self.closed = True


class TestExtractToken:

    def test_from_query_param(self):
        ws = FakeWs(path="/ws?token=abc123")
        assert extract_token(ws) == "abc123"

    def test_from_authorization_header(self):
        ws = FakeWs(headers={"Authorization": "Bearer mytoken"})
        assert extract_token(ws) == "mytoken"

    def test_from_x_token_header(self):
        ws = FakeWs(headers={"X-Token": "xtoken999"})
        assert extract_token(ws) == "xtoken999"

    def test_query_param_priority_over_header(self):
        ws = FakeWs(path="/ws?token=query-token", headers={"Authorization": "Bearer header-token"})
        assert extract_token(ws) == "query-token"

    def test_no_token(self):
        ws = FakeWs(path="/ws")
        assert extract_token(ws) is None

    def test_empty_path(self):
        ws = FakeWs()
        assert extract_token(ws) is None

    def test_bearer_without_prefix(self):
        ws = FakeWs(headers={"Authorization": "token-without-bearer"})
        assert extract_token(ws) is None


class TestExtractHeaders:

    def test_from_request_headers(self):
        ws = FakeWs(headers={"Authorization": "Bearer x", "X-Custom": "val"})
        result = extract_headers(ws)
        assert result["Authorization"] == "Bearer x"
        assert result["X-Custom"] == "val"

    def test_from_request_headers_attr(self):
        ws = FakeWs(request_headers={"X-Alt": "alt-val"})
        result = extract_headers(ws)
        assert result["X-Alt"] == "alt-val"

    def test_request_headers_fallback(self):
        ws = FakeWs()
        ws._request.headers = None
        ws.request_headers = {"X-Fallback": "fb"}
        result = extract_headers(ws)
        assert result["X-Fallback"] == "fb"

    def test_no_headers(self):
        ws = FakeWs()
        ws._request.headers = None
        assert extract_headers(ws) == {}


class TestGetRemoteAddr:

    def test_tuple_address(self):
        ws = FakeWs(remote_address=("127.0.0.1", 9000))
        assert get_remote_addr(ws) == "127.0.0.1:9000"

    def test_list_address(self):
        ws = FakeWs(remote_address=["10.0.0.1", 8080])
        assert get_remote_addr(ws) == "10.0.0.1:8080"

    def test_string_address(self):
        ws = FakeWs(remote_address="192.168.1.1")
        assert get_remote_addr(ws) == "192.168.1.1"

    def test_no_address(self):
        ws = FakeWs()
        assert get_remote_addr(ws) == ""