# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Background package authority belongs to the session, with current grants."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.enterprise.capabilities.experts import request_packages as allowlist
from agent_gateway.enterprise.capabilities.experts import bound_packages
from agent_gateway.enterprise.config.effective import db_queries
from jiuwenswarm.server.runtime.session import session_metadata


@pytest.mark.asyncio
async def test_background_subjects_isolate_bots_and_restore_on_cancellation(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    grants = [{"kind": "agent_group", "id": "shared"}]

    async def packages_for_bot(bot_id):
        return grants if bot_id == "alice" else []

    monkeypatch.setattr(bound_packages, "packages_for_bot", packages_for_bot)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda sid, **kwargs: {
        "channel_metadata": {"routing": {"bot_id": sid}},
    })
    monkeypatch.setattr(allowlist, "_is_user_owned", lambda *_: False)
    monkeypatch.setattr(allowlist, "_candidate_ids", lambda kind, name: {name})

    async def check(session_id):
        async with allowlist.bind_background_subject(session_id):
            await asyncio.sleep(0)
            return allowlist.allows("agent_group", "shared")

    with allowlist.bind_request_subject({"user_id": "foreign", allowlist.METADATA_KEY: []}):
        assert await asyncio.gather(check("alice"), check("bob")) == [True, False]
        with pytest.raises(asyncio.CancelledError):
            async with allowlist.bind_background_subject("alice"):
                assert allowlist.allows("agent_group", "shared")
                raise asyncio.CancelledError
        assert allowlist._REQUEST_PACKAGES.get() == []
        assert not allowlist.allows("agent_group", "shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["personal", "direct_request", "missing_session", "malformed_grants"])
async def test_background_subject_handles_missing_authority_without_bypassing_requests(monkeypatch, mode):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal" if mode == "personal" else "enterprise")
    read_metadata = lambda *args, **kwargs: None
    if mode == "malformed_grants":
        read_metadata = lambda *args, **kwargs: {"channel_metadata": {"routing": {"bot_id": "alice"}}}
    monkeypatch.setattr(session_metadata, "get_session_metadata", read_metadata)
    read_grants = AsyncMock(return_value=[{"kind": "unknown", "id": "shared"}])
    monkeypatch.setattr(bound_packages, "packages_for_bot", read_grants)
    monkeypatch.setattr(allowlist, "_is_user_owned", lambda *_: False)
    monkeypatch.setattr(allowlist, "_candidate_ids", lambda kind, name: {name})

    with allowlist.bind_request_subject({
        "user_id": "request-user", allowlist.METADATA_KEY: [{"kind": "agent_group", "id": "shared"}],
    }):
        async with allowlist.bind_background_subject("session", enabled=mode != "direct_request"):
            assert allowlist.allows("agent_group", "shared") is (mode in {"personal", "direct_request"})
        assert allowlist.allows("agent_group", "shared")
    if mode != "malformed_grants":
        read_grants.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["allowed", "revoked", "missing_bot", "read_error"])
async def test_current_packages_reads_current_bot_template_bindings(monkeypatch, state):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    rows = [{
        "template_id": "experts", "enabled": state != "revoked",
        "packages": [{"kind": "agent_group", "asset_id": "shared"}],
    }]

    async def read(table, *, filters=None, order_by=""):
        if state == "read_error":
            raise RuntimeError("database unavailable")
        if table == "instance_agent_resource":
            assert filters == {"enabled": True, "resource_id": "bot-alice"}
            return [{"ref_template_id": "agent"}]
        if table == "agent_template":
            assert filters == {"enabled": True, "template_id": "agent"}
            return [{"template_ref": {"expert_package": ["experts"]}}]
        assert table == "expert_package_template"
        assert filters == {"enabled": True}
        return rows

    read_grants = AsyncMock(side_effect=read)
    monkeypatch.setattr(db_queries, "list_records", read_grants)
    bot_id = "" if state == "missing_bot" else "bot-alice"
    assert await allowlist._current_packages(bot_id) == (
        [{"kind": "agent_group", "id": "shared"}] if state == "allowed" else []
    )
    if state == "missing_bot":
        read_grants.assert_not_awaited()
