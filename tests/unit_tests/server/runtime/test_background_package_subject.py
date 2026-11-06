# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Background package authority belongs to the session, with current grants."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.enterprise.capabilities.experts import allowlist
from jiuwenswarm.enterprise.config.effective import db_queries
from jiuwenswarm.server.runtime.session import session_metadata


@pytest.mark.asyncio
async def test_background_subjects_isolate_identity_and_restore_on_cancellation(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    grants = [{"kind": "agent_group", "id": "shared", "match_expr": "user_id == 'alice'"}]
    monkeypatch.setattr(db_queries, "list_records", AsyncMock(return_value=[{"packages": grants}]))
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda sid, **kwargs: {
        "channel_metadata": {"user_id": sid},
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
        assert allowlist._current_identity()["user_id"] == "foreign"
        assert not allowlist.allows("agent_group", "shared")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["personal", "direct_request", "missing_session", "malformed_grants"])
async def test_background_subject_handles_missing_authority_without_bypassing_requests(monkeypatch, mode):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal" if mode == "personal" else "enterprise")
    read_metadata = lambda *args, **kwargs: None
    if mode == "malformed_grants":
        read_metadata = lambda *args, **kwargs: {"channel_metadata": {"user_id": "alice"}}
    monkeypatch.setattr(session_metadata, "get_session_metadata", read_metadata)
    read_grants = AsyncMock(return_value=[{"packages": [{"kind": "agent_group", "id": "shared", "match_expr": "user_id =="}]}])
    monkeypatch.setattr(db_queries, "list_records", read_grants)
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
