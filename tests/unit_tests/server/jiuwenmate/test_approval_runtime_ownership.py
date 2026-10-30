"""Owner Web decisions must resume through the runtime serving the original channel."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from jiuwenswarm.server.jiuwenmate import web_adapter
from tests.unit_tests.server.jiuwenmate.test_permission_approvals import NOW, paused_request
from tests.unit_tests.server.jiuwenmate.test_session_scheduler import Host


@pytest.mark.parametrize("channels,channel,expected", [
    (frozenset({"web"}), "feishu", False),
    (frozenset({"web"}), "dingtalk", False),
    (frozenset({"web"}), "web", True),
    (frozenset({"feishu", "dingtalk", "welink"}), "web", False),
    (frozenset({"feishu", "dingtalk", "welink"}), "feishu", True),
    (frozenset({"feishu", "dingtalk", "welink"}), "dingtalk", True),
])
async def test_resume_is_owned_by_the_original_channel_runtime(tmp_path, channels, channel, expected):
    manager = Host(tmp_path).manager
    manager._approval_channels = channels
    manager._resume_permission_approval = AsyncMock()
    state, event = paused_request(channel="web" if channel == "web" else "feishu",
                                 app_id=None if channel == "web" else "app-1")
    state = replace(state, record=replace(state.record, route_snapshot=replace(state.record.route_snapshot, channel=channel)))
    manager._sessions.get_request = Mock(return_value=state)
    approval = PermissionApprovalRepository(tmp_path / "approval.sqlite3").create(state, event, now=NOW)
    manager._schedule_permission_resume(approval)
    await asyncio.gather(*manager._permission_resume_tasks.values())
    assert manager._resume_permission_approval.await_count == int(expected)


def test_production_web_factory_claims_only_web_approvals(monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr(web_adapter.AgentManager, "create_default", factory)
    observer = Mock()
    web_adapter._default_manager(object(), on_event=observer)
    assert factory.call_args.kwargs["approval_channels"] == frozenset({"web"})
