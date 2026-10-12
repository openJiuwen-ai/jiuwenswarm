# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway fetch: transient failures are retried, a bad request is not. Offline."""

import http.client
import io
import urllib.error

import pytest

from jiuwenswarm.agents.harness.common.research import gateway as gw


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(outcomes, calls):
    def urlopen(req, timeout):
        calls.append(req.full_url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)
    return urlopen


def test_rate_limits_and_dropped_connections_are_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    monkeypatch.setattr(gw.urllib.request, "urlopen", _urlopen([
        urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
        http.client.RemoteDisconnected("closed"),
        b"ok"], calls))
    assert gw.fetch("https://x.test/a") == b"ok" and len(calls) == 3


def test_a_bad_request_is_raised_at_once(monkeypatch):
    calls = []
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    monkeypatch.setattr(gw.urllib.request, "urlopen", _urlopen([
        urllib.error.HTTPError("u", 400, "Bad Request", {}, None), b"never"], calls))
    with pytest.raises(urllib.error.HTTPError):
        gw.fetch("https://x.test/a")
    assert len(calls) == 1
