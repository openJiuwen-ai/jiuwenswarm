"""DingTalk cron originating-session binding (Issue #2449)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent_gateway.cron.dingtalk_routing import (
    build_dingtalk_cron_session_id_from_context,
    encode_dingtalk_cron_session_id,
    is_usable_dingtalk_staff_id,
    parse_dingtalk_cron_session_id,
    resolve_dingtalk_push_metadata,
)
from agent_gateway.cron.models import CronRunState
from agent_gateway.cron.scheduler import CronSchedulerService
from agent_gateway.cron.store import CronJobStore

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _CronToolsCronBackend,
    _extract_legacy_params,
)


def test_extract_legacy_params_binds_dingtalk_originating_session() -> None:
    context = SimpleNamespace(
        channel_id="dingtalk",
        session_id="dingtalk_19fb740bf54_5d52cf",
        metadata={
            "dingtalk_sender_id": "01686816272323644702",
            "dingtalk_chat_id": "conv-a",
            "conversation_id": "conv-a",
            "conversation_type": "1",
            "request_id": "req-1",
        },
    )
    payload = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "agentTurn", "message": "remind"},
        "delivery": {"channel": "dingtalk"},
        "name": "t1",
        "description": "dingtalk reminder",
    }

    out = _extract_legacy_params(payload, context=context, require_schedule=True)

    assert out["targets"] == "dingtalk"
    assert out["session_id"] == "dingtalk::conv-a::01686816272323644702::1"


def test_route_from_context_binds_delivery_session_not_internal_id() -> None:
    context = SimpleNamespace(
        channel_id="dingtalk",
        session_id="dingtalk_19fb740bf54_5d52cf",
        metadata={
            "dingtalk_sender_id": "01686816272323644702",
            "conversation_id": "conv-a",
            "conversation_type": "2",
            "request_id": "req-1",
        },
    )
    route = _CronToolsCronBackend._route_from_context(context)
    assert route.chat_type == "group"
    assert route.session_id == "dingtalk::conv-a::01686816272323644702::2"


class FakeMessageHandler:
    def __init__(self) -> None:
        self.published = []

    async def publish_robot_messages(self, msg):
        self.published.append(msg)


