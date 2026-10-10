"""Repository account validation and cancellation regression coverage."""

import asyncio

import httpx
import pytest

from jiuwenswarm.server.personal_context import host_api


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["login", "name"])
@pytest.mark.parametrize("value", [True, "   ", "x" * 257, "bad\x00value"])
async def test_pat_rejects_invalid_account_fields(monkeypatch, field, value):
    payload = {"login": "account", "name": "Account"}
    payload[field] = value
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(
        host_api.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    with pytest.raises(
        host_api.PersonalContext.Error, match="account response is invalid"
    ):
        await host_api._validate_repository_pat("github", "test-secret")


@pytest.mark.asyncio
async def test_pat_rejects_invalid_json_without_exposing_body(monkeypatch):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"invalid-json-private-body")
    )
    monkeypatch.setattr(
        host_api.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    with pytest.raises(
        host_api.PersonalContext.Error, match="response is invalid"
    ) as caught:
        await host_api._validate_repository_pat("github", "test-secret")
    assert "private-body" not in str(caught.value)


@pytest.mark.asyncio
async def test_pat_write_preserves_cancellation(monkeypatch):
    cancelled = asyncio.CancelledError()

    async def cancel(provider, secret):
        raise cancelled

    monkeypatch.setattr(host_api, "_validate_repository_pat", cancel)
    with pytest.raises(asyncio.CancelledError) as caught:
        await host_api._validate_repository_pat_for_write("github", "test-secret")
    assert caught.value is cancelled
