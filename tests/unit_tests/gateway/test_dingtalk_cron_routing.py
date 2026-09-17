"""DingTalk cron originating-session binding (Issue #2449)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _CronToolsCronBackend,
    _extract_legacy_params,
)
from jiuwenswarm.gateway.cron.dingtalk_routing import (
    build_dingtalk_cron_session_id_from_context,
    encode_dingtalk_cron_session_id,
    is_usable_dingtalk_staff_id,
    parse_dingtalk_cron_session_id,
    resolve_dingtalk_push_metadata,
)
from jiuwenswarm.gateway.cron.models import CronRunState
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
from jiuwenswarm.gateway.cron.store import CronJobStore


def test_encode_parse_roundtrip_private() -> None:
    sid = encode_dingtalk_cron_session_id(
        sender_id="01686816272323644702",
        conversation_id="conv-a",
        conversation_type="1",
    )
    assert sid == "dingtalk::conv-a::01686816272323644702::1"
    assert parse_dingtalk_cron_session_id(sid) == {
        "dingtalk_sender_id": "01686816272323644702",
        "dingtalk_chat_id": "conv-a",
        "conversation_id": "conv-a",
        "conversation_type": "1",
    }


def test_encode_parse_roundtrip_group() -> None:
    sid = encode_dingtalk_cron_session_id(
        sender_id="01686816272323644702",
        conversation_id="group-1",
        conversation_type="2",
    )
    parsed = parse_dingtalk_cron_session_id(sid)
    assert parsed is not None
    assert parsed["conversation_type"] == "2"
    assert parsed["conversation_id"] == "group-1"


def test_internal_gateway_session_id_is_not_staff_id() -> None:
    assert not is_usable_dingtalk_staff_id("dingtalk_19fb740bf54_5d52cf")
    assert parse_dingtalk_cron_session_id("dingtalk_19fb740bf54_5d52cf") is None
    assert resolve_dingtalk_push_metadata("dingtalk_19fb740bf54_5d52cf") is None


def test_plain_staff_id_resolves_to_private_chat() -> None:
    assert resolve_dingtalk_push_metadata("01686816272323644702") == {
        "dingtalk_sender_id": "01686816272323644702",
        "conversation_type": "1",
    }


def test_build_from_context_ignores_internal_session_fallback() -> None:
    assert (
        build_dingtalk_cron_session_id_from_context(
            session_id="dingtalk_19fb740bf54_5d52cf",
            metadata={"conversation_id": "conv-a", "conversation_type": "1"},
        )
        is None
    )


def test_build_from_context_prefers_metadata_sender() -> None:
    sid = build_dingtalk_cron_session_id_from_context(
        session_id="dingtalk_19fb740bf54_5d52cf",
        metadata={
            "dingtalk_sender_id": "01686816272323644702",
            "conversation_id": "conv-a",
            "conversation_type": "1",
        },
    )
    assert sid == "dingtalk::conv-a::01686816272323644702::1"


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


@pytest.mark.asyncio
async def test_push_prefers_bound_dingtalk_session_over_last_star(tmp_path) -> None:
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    job = await store.create_job(
        name="t1",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets="dingtalk",
        session_id="dingtalk::conv-a::01686816272323644702::1",
    )
    handler = FakeMessageHandler()
    svc = CronSchedulerService(
        store=store,
        agent_client=object(),
        message_handler=handler,
    )
    state = CronRunState(
        job_id=job.id,
        run_id=f"{job.id}:1",
        wake_at_iso="t",
        push_at_iso="t",
        targets="dingtalk",
        session_id=job.session_id,
        status="succeeded",
        result_text="hello from T1",
    )

    fake_cfg = {
        "channels": {
            "dingtalk": {
                "last_sender_id": "user-b",
                "last_conversation_id": "conv-b-group",
                "last_conversation_type": "2",
            }
        }
    }
    with patch("jiuwenswarm.common.config.get_config_raw", return_value=fake_cfg):
        await svc._push_to_targets(job, state, text="hello from T1", is_placeholder=False)

    assert len(handler.published) == 1
    msg = handler.published[0]
    assert msg.metadata["dingtalk_sender_id"] == "01686816272323644702"
    assert msg.metadata["conversation_id"] == "conv-a"
    assert msg.metadata["conversation_type"] == "1"


@pytest.mark.asyncio
async def test_push_internal_session_id_falls_back_to_last_star(tmp_path) -> None:
    """Regression: dingtalk_… must not become userIds (staffId.notExisted)."""
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    job = await store.create_job(
        name="t1",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets="dingtalk",
        session_id="dingtalk_19fb740bf54_5d52cf",
    )
    handler = FakeMessageHandler()
    svc = CronSchedulerService(
        store=store,
        agent_client=object(),
        message_handler=handler,
    )
    state = CronRunState(
        job_id=job.id,
        run_id=f"{job.id}:1",
        wake_at_iso="t",
        push_at_iso="t",
        targets="dingtalk",
        session_id=job.session_id,
        status="succeeded",
        result_text="hello",
    )
    fake_cfg = {
        "channels": {
            "dingtalk": {
                "last_sender_id": "01686816272323644702",
                "last_conversation_id": "cid-group",
                "last_conversation_type": "2",
            }
        }
    }
    with patch("jiuwenswarm.common.config.get_config_raw", return_value=fake_cfg):
        await svc._push_to_targets(job, state, text="hello", is_placeholder=False)

    msg = handler.published[0]
    assert msg.metadata["dingtalk_sender_id"] == "01686816272323644702"
    assert msg.metadata["conversation_id"] == "cid-group"
    assert msg.metadata["conversation_type"] == "2"
    assert msg.metadata["dingtalk_sender_id"] != "dingtalk_19fb740bf54_5d52cf"
