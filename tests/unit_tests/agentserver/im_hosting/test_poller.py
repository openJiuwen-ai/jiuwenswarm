from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin
from jiuwenswarm.server.im.im_connector.types import (
    ChannelMeta,
    ChannelTarget,
    FetchOptions,
    Identity,
    ImMessage,
    MessagePage,
    SendOptions,
    SendResult,
    TestResult as ChannelTestResult,
)
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.im.im_hosting.poller import bootstrap_watermark_ms, run_poll_once
from jiuwenswarm.server.im.im_hosting.store import HostingStore


class _FakePlugin(ChannelPlugin):
    def __init__(self, messages: list[ImMessage]) -> None:
        self._messages = messages

    @property
    def meta(self) -> ChannelMeta:
        return ChannelMeta(id="feishu", label="飞书")

    async def fetch_messages(self, target: ChannelTarget, options: FetchOptions) -> MessagePage:
        del target, options
        return MessagePage(messages=list(self._messages))

    async def send_message(self, target, content, options: SendOptions | None = None) -> SendResult:
        del target, content, options
        return SendResult(ok=True)

    async def resolve_identity(self) -> Identity | None:
        return Identity(account="me", display_name="小蚊子")

    async def test_connection(self) -> TestResult:
        return ChannelTestResult(ok=True, message="ok")


@pytest.mark.asyncio
async def test_first_poll_bootstraps_watermark_and_preview(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    target = store.add_target(
        channel_id="feishu",
        target_kind="group",
        external_id="oc_test",
        title="测试群",
    )
    messages = [
        ImMessage(
            channel_id="feishu",
            msg_id="m1",
            conversation_external_id="oc_test",
            sender_name="许康",
            content_text="你好",
            sent_at=1_000,
        ),
        ImMessage(
            channel_id="feishu",
            msg_id="m2",
            conversation_external_id="oc_test",
            sender_name="小蚊子",
            content_text="在的",
            sent_at=2_000,
            is_self=True,
        ),
    ]
    summary = await run_poll_once(store, _FakePlugin(messages), target, fetch_count=50)
    assert summary["fetched"] == 2
    wm = store.get_watermark("feishu", "group", "oc_test")
    assert wm is not None
    expected = bootstrap_watermark_ms(messages, target["hosting_since_ms"])
    assert wm["last_processed_at_ms"] == expected
    refreshed = store.get_target(target["id"])
    assert refreshed is not None
    assert refreshed["last_preview"]
    assert refreshed["last_preview"][-1]["msg_id"] == "m2"
    assert refreshed["last_error"] is None
    assert summary["gated_in"] == 0

    later_base = int(wm["last_processed_at_ms"]) + 1
    later = [
        *messages,
        ImMessage(
            channel_id="feishu",
            msg_id="m3",
            conversation_external_id="oc_test",
            sender_name="许康",
            content_text="入职流程怎么走",
            sent_at=later_base,
        ),
        ImMessage(
            channel_id="feishu",
            msg_id="m4",
            conversation_external_id="oc_test",
            sender_name="许康",
            content_text="今晚吃饭吗",
            sent_at=later_base + 1,
        ),
    ]
    store.patch_target(
        target["id"],
        {"rule_override": {"match_mode": "keyword", "keywords": ["入职"]}},
    )
    target = store.get_target(target["id"])
    assert target is not None
    second = await run_poll_once(
        store,
        _FakePlugin(later),
        target,
        fetch_count=50,
        channel_policy={
            "default_group_rule": {"match_mode": "keyword", "keywords": []},
            "reply_enabled": False,
        },
    )
    assert second["gated_in"] == 1
    gated_ids = {item["msg_id"]: item.get("gated") for item in second["preview"]}
    assert gated_ids["m3"] is True
    assert gated_ids["m4"] is False


@pytest.mark.asyncio
async def test_auto_host_first_poll_replies_recent_trigger_messages(tmp_path: Path):
    store = HostingStore(tmp_path / "hosting.db")
    target = store.add_target(
        channel_id="dingtalk",
        target_kind="group",
        external_id="cid_no_ot",
        title="今天不加班",
        source="auto",
    )
    now = int(target["hosting_since_ms"])
    messages = [
        ImMessage(
            channel_id="dingtalk",
            msg_id="old",
            conversation_external_id="cid_no_ot",
            sender_name="许康",
            content_text="嗨 很久以前",
            sent_at=now - 6 * 60 * 1000,
        ),
        ImMessage(
            channel_id="dingtalk",
            msg_id="hi1",
            conversation_external_id="cid_no_ot",
            sender_name="许康",
            content_text="嗨",
            sent_at=now - 8_000,
        ),
        ImMessage(
            channel_id="dingtalk",
            msg_id="hi2",
            conversation_external_id="cid_no_ot",
            sender_name="许康",
            content_text="嗨",
            sent_at=now - 3_000,
        ),
    ]
    class _ReplyRuntime:
        async def process_message(self, request):
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"content": "你好 哈哈哈 我不回你"},
            )

    plugin = _FakePlugin(messages)
    summary = await run_poll_once(
        store,
        plugin,
        target,
        fetch_count=50,
        channel_policy={
            "default_group_rule": {"match_mode": "keyword", "keywords": ["嗨"]},
            "reply_enabled": True,
        },
        agent_manager=_ReplyRuntime(),
    )
    assert summary["gated_in"] == 2
    assert summary["replied"] == 2
    gated = {item["msg_id"]: item for item in summary["preview"]}
    assert gated["old"].get("gated") is None
    assert gated["hi1"].get("gated") is True
    assert gated["hi2"].get("gated") is True
