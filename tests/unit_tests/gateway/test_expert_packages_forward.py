# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包随出站请求下发：名单来自当前 bot 的 Agent 模板槽，损坏头按空名单。"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from e2a.models import E2AEnvelope
from agent_gateway.enterprise.capabilities.experts.bound_packages import (
    packages_for_bot,
    packages_from_template_rows,
)
from agent_gateway.routing.agent_client import _stamp_expert_packages
from agent_gateway.enterprise.capabilities.experts.allowlist import bind_outbound_allowlist

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import (
    _assemble_with_expert_packages,
)
from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
    identity_headers,
)
from jiuwenswarm.server.agent_http_routes import _rebuild_identity_from_headers
from jiuwenswarm.common.schema.expert_packages import (
    HEADER_NAME,
    METADATA_KEY,
    decode_expert_packages_header,
    encode_expert_packages_header,
)

_KEEP = [{"kind": "agent_template", "id": "keep"}]
_EVIL = [{"kind": "agent_template", "id": "evil"}]
_PACKAGES = "agent_gateway.enterprise.capabilities.experts.bound_packages.packages_for_bot"


def _envelope() -> E2AEnvelope:
    return E2AEnvelope(
        request_id="req-1",
        channel="web",
        method=ReqMethod.CHAT_SEND.value,
        session_id="sess-1",
        params={"session_id": "sess-1"},
        user_id="alice",
        channel_context={
            METADATA_KEY: _EVIL,
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


def test_personal_identity_headers_omit_expert_packages(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert decode_expert_packages_header(headers[HEADER_NAME]) == _KEEP


def test_enterprise_unbound_header_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    headers = identity_headers(_envelope(), accept="application/json")
    assert decode_expert_packages_header(headers[HEADER_NAME]) == []


@pytest.mark.asyncio
async def test_http_assemble_reads_db_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _read(bot_id: str) -> list[dict]:
        assert bot_id == "bot-1"
        return list(_KEEP)

    monkeypatch.setattr(_PACKAGES, _read)
    assembled = await _assemble_with_expert_packages(_envelope(), "http://10.0.0.8:8766")
    assert decode_expert_packages_header(assembled.headers[HEADER_NAME]) == _KEEP


@pytest.mark.asyncio
async def test_personal_http_assemble_does_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    async def _read() -> list[dict]:
        raise AssertionError("personal edition read the expert packages")

    monkeypatch.setattr(_PACKAGES, _read)
    assembled = await _assemble_with_expert_packages(_envelope(), "http://10.0.0.8:8766")
    assert HEADER_NAME not in assembled.headers


@pytest.mark.asyncio
async def test_websocket_stamp_overwrites_client_expert_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _read(_bot_id: str) -> list[dict]:
        assert _bot_id == "bot-1"
        return list(_KEEP)

    monkeypatch.setattr(_PACKAGES, _read)
    envelope = _envelope()
    await _stamp_expert_packages(envelope)
    assert envelope.channel_context[METADATA_KEY] == _KEEP
    assert envelope.channel_context["routing"]["group_id"] == "finance"
    request = e2a_to_agent_request(envelope)
    assert request.metadata[METADATA_KEY] == _KEEP


@pytest.mark.asyncio
async def test_personal_websocket_stamp_skips_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    async def _read() -> list[dict]:
        raise AssertionError("personal edition read the expert packages")

    monkeypatch.setattr(_PACKAGES, _read)
    envelope = _envelope()
    await _stamp_expert_packages(envelope)
    assert envelope.channel_context[METADATA_KEY] == _EVIL


def test_corrupt_expert_packages_header_becomes_empty_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request({HEADER_NAME: "%%%", "X-User-Id": "alice"})
    )
    assert metadata[METADATA_KEY] == []


def test_missing_expert_packages_header_becomes_empty_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request({"X-User-Id": "alice"})
    )
    assert metadata[METADATA_KEY] == []


def test_expert_packages_header_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    encoded = encode_expert_packages_header(_KEEP)
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request({HEADER_NAME: encoded, "X-User-Id": "alice"})
    )
    assert metadata[METADATA_KEY] == _KEEP


def test_template_rows_become_packages_without_match_expr() -> None:
    packages = packages_from_template_rows(
        ["tpl-keep", "tpl-keep", "tpl-gone", "tpl-off"],
        [
            {
                "template_id": "tpl-keep",
                "enabled": True,
                "packages": [
                    {"kind": "agent_template", "asset_id": "keep", "version": "1"},
                    {"kind": "agent_group", "asset_id": "team-1", "version": ""},
                    {"kind": "agent_template", "asset_id": "keep", "version": "2"},
                    {"kind": "other", "asset_id": "skip-me"},
                ],
            },
            {
                "template_id": "tpl-off",
                "enabled": False,
                "packages": [{"kind": "agent_template", "asset_id": "off"}],
            },
        ],
    )
    assert packages == [
        {"kind": "agent_template", "id": "keep"},
        {"kind": "agent_group", "id": "team-1"},
    ]
    assert "match_expr" not in packages[0]


@pytest.mark.asyncio
async def test_packages_for_bot_reads_template_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _list(table: str, *, filters=None, order_by: str = ""):
        del order_by
        if table == "instance_agent_resource" and filters["resource_id"] == "bot-1":
            return [{"ref_template_id": "tpl-1", "enabled": True}]
        if table == "agent_template" and filters["template_id"] == "tpl-1":
            return [{
                "template_ref": {
                    "expert_package": ["tpl-keep"],
                },
            }]
        if table == "expert_package_template":
            return [
                {
                    "template_id": "tpl-keep",
                    "enabled": True,
                    "packages": [
                        {"kind": "agent_template", "asset_id": "keep"},
                        {"kind": "agent_group", "asset_id": "team-1"},
                    ],
                },
            ]
        return []

    monkeypatch.setattr(
        "agent_gateway.enterprise.config.effective.db_queries.list_records",
        _list,
    )
    assert await packages_for_bot("bot-1") == [
        {"kind": "agent_template", "id": "keep"},
        {"kind": "agent_group", "id": "team-1"},
    ]


@pytest.mark.asyncio
async def test_packages_for_missing_bot_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    async def _list(table: str, *, filters=None, order_by: str = ""):
        del table, filters, order_by
        return []

    monkeypatch.setattr(
        "agent_gateway.enterprise.config.effective.db_queries.list_records",
        _list,
    )
    assert await packages_for_bot("missing") == []
    assert await packages_for_bot("") == []


@pytest.mark.asyncio
async def test_personal_packages_for_bot_skips_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    def _missing(*_args, **_kwargs):
        raise AssertionError("personal edition read the gateway database")

    monkeypatch.setattr(
        "agent_gateway.enterprise.config.effective.db_queries.list_records",
        _missing,
    )
    assert await packages_for_bot("bot-1") == []
