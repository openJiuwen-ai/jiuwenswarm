"""Authorized message search is direct; provenance, identity and file consent remain enforced."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.server.jiuwenmate.execution_models import RequestRouteSnapshot
from tests.unit_tests.server.jiuwenmate.test_group_sharing_service import sharing, NOW


@pytest.mark.parametrize("channel", ["web", "feishu", "dingtalk", "welink", "swarmchat"])
async def test_authorized_messages_return_without_a_share_approval(sharing, channel):
    sharing.record.route_snapshot = RequestRouteSnapshot(
        channel=channel, target_id="destination", source_message_id="trigger",
        app_id="app" if channel in {"feishu", "dingtalk", "welink"} else None,
    )
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.hits == sharing.result.hits
    assert result.query_complete
    assert not sharing.repo.pending_for_request("local", "request-1")
    assert sharing.repo.list_for_owner("local", "config-1")["approvals"] == []


async def test_changed_query_does_not_create_consent_or_freeze_old_messages(sharing):
    first = await sharing.port.search_messages(sharing.execution, sharing.query)
    message = first.hits[0].message.model_copy(update={"text": "updated plan"})
    hit = first.hits[0].model_copy(update={"message": message})
    sharing.delegate.search_messages.return_value = first.model_copy(update={"hits": (hit,)})
    sharing.archive.current_messages = lambda *args: (message,)
    result = await sharing.port.search_messages(sharing.execution, sharing.query.model_copy(update={"limit": 200}))
    assert result.hits[0].message.text == "updated plan"
    assert not sharing.repo.pending_for_request("local", "request-1")


async def test_no_consent_does_not_bypass_source_membership_verification(sharing):
    sharing.delegate.sharing_sources = AsyncMock(side_effect=RuntimeError("membership unknown"))
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "unavailable"
    assert not result.hits
    sharing.delegate.search_messages.assert_not_called()


async def test_source_change_during_search_does_not_release_messages(sharing):
    sharing.delegate.sharing_sources.side_effect = [
        ({"group_id": "source-group", "name": "Source", "visible_from": "2026-10-01T00:00:00+00:00"},),
        ({"group_id": "source-group", "name": "Source", "visible_from": "2026-10-20T00:00:00+00:00"},),
    ]
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "unavailable"
    assert not result.hits


async def test_recalled_message_is_not_released_by_no_consent_policy(sharing):
    message = sharing.result.hits[0].message.model_copy(update={"status": "recalled", "text": None})
    sharing.archive.current_messages = lambda *args: (message,)
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "unavailable"
    assert not result.hits


@pytest.mark.parametrize("field", ["agent_id", "session_id"])
async def test_no_consent_does_not_allow_another_request_identity(sharing, field):
    setattr(sharing.record, field, "other")
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status in {"failed", "unavailable", "forbidden"}
    assert not result.hits


async def test_unlisted_source_is_not_released(sharing):
    hit = sharing.result.hits[0].model_copy(update={"group_id": "another-group"})
    sharing.delegate.search_messages.return_value = sharing.result.model_copy(update={"hits": (hit,)})
    result = await sharing.port.search_messages(sharing.execution, sharing.query)
    assert result.status == "unavailable"
    assert not result.hits
