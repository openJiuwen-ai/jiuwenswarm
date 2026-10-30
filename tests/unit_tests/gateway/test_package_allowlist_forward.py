# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包允许名单随出站请求下发：头来自库行，损坏头按空名单。"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import (
    _assemble_with_allowlist,
)
from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
    identity_headers,
)
from gateway.enterprise.config.manager_config.core.application_config.package_allowlist import (
    PackageAllowlistService,
    read_package_allowlist_packages,
)
from gateway.routing.agent_client import _stamp_package_allowlist
from jiuwenswarm.server.agent_http_routes import _rebuild_identity_from_headers
from jiuwenswarm.server.runtime.package_allowlist import (
    HEADER_NAME,
    bind_outbound_allowlist,
    decode_package_allowlist_header,
    encode_package_allowlist_header,
)

_KEEP = [{"kind": "agent_template", "id": "keep"}]
_EVIL = [{"kind": "agent_template", "id": "evil"}]
_REPO = (
    "gateway.enterprise.config.manager_config.core.application_config."
    "package_allowlist.require_enterprise_repository"
)
_READ = (
    "gateway.enterprise.config.manager_config.core.application_config."
    "package_allowlist.read_package_allowlist_packages"
)


def _envelope() -> E2AEnvelope:
    return E2AEnvelope(
        request_id="req-1",
        channel="web",
        method=ReqMethod.CHAT_SEND.value,
        session_id="sess-1",
        params={"session_id": "sess-1"},
        user_id="alice",
        channel_context={
            "package_allowlist": _EVIL,
            "routing": {"group_id": "finance", "bot_id": "bot-1"},
        },
    )


def _http_request(headers: dict[str, str]) -> Request:
    raw = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in headers.items()
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw,
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
        }
    )


class _Repo:
    def __init__(self, row):
        self.row = row
        self.created = False

    async def get(self):
        return self.row

    async def create(self, _row):
        self.created = True
        return _row


def test_personal_identity_headers_omit_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    with bind_outbound_allowlist(_KEEP):
        headers = identity_headers(_envelope(), accept="application/json")
    assert HEADER_NAME not in headers


def test_enterprise_header_uses_bound_row_not_client_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    with bind_outbound_allowlist(_KEEP):
        headers = identity_headers(_envelope(), accept="application/json")
    assert decode_package_allowlist_header(headers[HEADER_NAME]) == _KEEP


def test_enterprise_unbound_header_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    headers = identity_headers(_envelope(), accept="application/json")
    assert decode_package_allowlist_header(headers[HEADER_NAME]) == []


@pytest.mark.asyncio
async def test_http_assemble_reads_db_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _read() -> list[dict]:
        return list(_KEEP)

    monkeypatch.setattr(_READ, _read)
    assembled = await _assemble_with_allowlist(_envelope(), "http://10.0.0.8:8766")
    assert decode_package_allowlist_header(assembled.headers[HEADER_NAME]) == _KEEP


@pytest.mark.asyncio
async def test_personal_http_assemble_does_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    async def _read() -> list[dict]:
        raise AssertionError("personal edition read the allowlist")

    monkeypatch.setattr(_READ, _read)
    assembled = await _assemble_with_allowlist(_envelope(), "http://10.0.0.8:8766")
    assert HEADER_NAME not in assembled.headers


@pytest.mark.asyncio
async def test_websocket_stamp_overwrites_client_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _read() -> list[dict]:
        return list(_KEEP)

    monkeypatch.setattr(_READ, _read)
    envelope = _envelope()
    await _stamp_package_allowlist(envelope)
    assert envelope.channel_context["package_allowlist"] == _KEEP
    assert envelope.channel_context["routing"]["group_id"] == "finance"
    request = e2a_to_agent_request(envelope)
    assert request.metadata["package_allowlist"] == _KEEP


@pytest.mark.asyncio
async def test_personal_websocket_stamp_skips_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    async def _read() -> list[dict]:
        raise AssertionError("personal edition read the allowlist")

    monkeypatch.setattr(_READ, _read)
    envelope = _envelope()
    await _stamp_package_allowlist(envelope)
    assert envelope.channel_context["package_allowlist"] == _EVIL


def test_corrupt_allowlist_header_becomes_empty_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request({HEADER_NAME: "%%%", "X-User-Id": "alice"})
    )
    assert metadata["package_allowlist"] == []


def test_missing_allowlist_header_becomes_empty_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request({"X-User-Id": "alice"})
    )
    assert metadata["package_allowlist"] == []


def test_allowlist_header_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    encoded = encode_package_allowlist_header(_KEEP)
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request({HEADER_NAME: encoded, "X-User-Id": "alice"})
    )
    assert metadata["package_allowlist"] == _KEEP


@pytest.mark.asyncio
async def test_read_returns_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(_REPO, lambda _name: _Repo({"packages": _KEEP}))
    assert await read_package_allowlist_packages() == _KEEP


@pytest.mark.asyncio
async def test_read_failure_sends_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    def _missing(_name: str):
        raise RuntimeError("not wired")

    monkeypatch.setattr(_REPO, _missing)
    assert await read_package_allowlist_packages() == []


@pytest.mark.asyncio
async def test_personal_read_skips_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    def _missing(_name: str):
        raise AssertionError("personal edition read the repository")

    monkeypatch.setattr(_REPO, _missing)
    assert await read_package_allowlist_packages() == []


@pytest.mark.asyncio
async def test_upsert_rejects_oversized_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    repo = _Repo(None)
    monkeypatch.setattr(_REPO, lambda _name: repo)
    huge = [
        {"kind": "agent_template", "id": f"{index:04d}{'x' * 124}"}
        for index in range(200)
    ]
    with pytest.raises(ValueError, match="16384"):
        await PackageAllowlistService().upsert({"packages": huge})
    assert repo.created is False
