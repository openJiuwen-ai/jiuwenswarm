# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assistant replies stay on the assistant gateway. The employee CLI never sends."""

from __future__ import annotations

import asyncio
import json

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_link import (
    AssistantLink,
    control_reply,
    parse_mention,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_assistant_send import (
    AssistantSendError,
    build_auth_protocol,
    build_group_frames,
    push_group_text,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink import welink_cli as welink_cli_module
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import (
    WelinkCli,
    WelinkPersonalSendForbidden,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_connect import WelinkChannel, WelinkConfig


class _Router:
    def __init__(self) -> None:
        return None


class _Socket:
    def __init__(self, reads: list[str]) -> None:
        self.sent: list[dict[str, object]] = []
        self._reads = list(reads)
        self.closed = False

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        if not self._reads:
            raise TimeoutError("idle")
        return self._reads.pop(0)

    def settimeout(self, value: float) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_auth_protocol_hides_the_secret_and_keeps_key_order() -> None:
    protocol = build_auth_protocol("ak-1", "secret-value", ts="10", nonce="n-1")
    assert protocol.startswith("auth.")
    assert "secret-value" not in protocol
    assert "ak-1" not in protocol


def test_group_frames_address_the_group_as_the_assistant() -> None:
    message_id, frames = build_group_frames("1023", "你好", suffix="abc", trace_id="trace-1")
    assert message_id == "msg_notify_abc"
    assert [frame["type"] for frame in frames] == ["tool_event", "tool_event", "tool_event", "tool_done"]
    assert frames[0]["businessType"] == "group"
    assert frames[0]["businessId"] == "1023"
    assert frames[0]["businessPushType"] == "active_push"
    assert frames[1]["event"]["properties"]["content"] == "你好"
    assert frames[-1]["businessPushType"] == "active_push"
    assert "send-to-group" not in json.dumps(frames)


def test_push_stops_when_the_assistant_slot_is_taken() -> None:
    socket = _Socket([json.dumps({"type": "register_rejected", "reason": "duplicate_connection"})])

    def connect(url: str, *, subprotocols: list[str], timeout: float) -> _Socket:
        assert url.startswith("ws://")
        assert subprotocols[0].startswith("auth.")
        return socket

    try:
        push_group_text("ak-1", "secret-value", "1023", "你好", connect=connect)
    except AssistantSendError as exc:
        assert exc.code == "duplicate_connection"
    else:
        raise AssertionError("occupied credential must not send")
    assert [item["type"] for item in socket.sent] == ["register"]
    assert socket.closed is True


def test_personal_cli_refuses_every_send_command(tmp_path) -> None:
    def fail_if_started(*_args, **_kwargs):
        raise AssertionError("personal welink-cli must not start")

    original = welink_cli_module.subprocess.run
    welink_cli_module.subprocess.run = fail_if_started
    cli = WelinkCli("welink-cli", tmp_path)
    try:
        for command in (
            ["im", "send-to-group", "--group-id", "g", "--text", "hi"],
            ["im", "send-to-user", "--receiver", "a001", "--text", "hi"],
        ):
            try:
                cli.run(command)
            except WelinkPersonalSendForbidden:
                continue
            raise AssertionError(command[1])
    finally:
        welink_cli_module.subprocess.run = original


def test_channel_refuses_to_send_with_the_personal_account(tmp_path) -> None:
    channel = WelinkChannel(
        WelinkConfig(enabled=True, account="h00557917", agent_id="mate", credential_dir=str(tmp_path)),
        _Router(),
    )
    channel._cli.send_to_group = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("personal send"))

    missing = asyncio.run(channel.send_group_text(chat_id="1023", text="你好", idempotency_key="k1"))
    assert missing["error_code"] == "WELINK_ASSISTANT_NOT_CONFIGURED"



class _QueuedSocket:
    def __init__(self) -> None:
        import queue

        self.sent: list[dict[str, object]] = []
        self._reads: queue.Queue[str | None] = queue.Queue()
        self._timeout = 0.2
        self.closed = False

    def push(self, payload: dict[str, object]) -> None:
        self._reads.put(json.dumps(payload))

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        import queue

        if self.closed:
            raise ConnectionError("closed")
        try:
            item = self._reads.get(timeout=self._timeout)
        except queue.Empty as exc:
            raise TimeoutError("idle") from exc
        if item is None:
            raise TimeoutError("idle")
        return item

    def settimeout(self, value: float) -> None:
        self._timeout = value

    def close(self) -> None:
        self.closed = True
        self._reads.put(None)


def _mention_frame() -> dict[str, object]:
    return {
        "type": "invoke",
        "action": "chat",
        "traceId": "trace-mention",
        "welinkSessionId": "ws-1",
        "payload": {
            "toolSessionId": "ses_mention",
            "text": "@小九 再给我写首绝句",
            "sendUserAccount": "h00557917",
            "imGroupId": "1023",
            "extParameters": {"businessExtParam": json.dumps({
                "businessMessageId": "m-9",
                "senderName": "黄宏运",
                "atAccountList": ["h001"],
            })},
        },
    }


def _private_frame() -> dict[str, object]:
    return {
        "type": "invoke",
        "action": "chat",
        "traceId": "trace-direct",
        "payload": {
            "toolSessionId": "ses_direct",
            "text": "介绍一下你自己",
            "sendUserAccount": "h00557917",
            "extParameters": {
                "businessExtParam": json.dumps({
                    "businessMessageId": "m-direct",
                    "senderId": "h00557917",
                    "senderName": "黄宏运",
                }),
                "platformExtParam": json.dumps({
                    "businessSessionType": "p2p",
                    "businessSessionId": "h00557917",
                }),
            },
        },
    }


def test_private_chat_frame_is_a_direct_mention() -> None:
    mention = parse_mention(_private_frame())
    assert mention is not None
    assert mention.direct is True
    assert mention.group_id == "p2p:h00557917"
    assert mention.message_id == "m-direct"
    assert mention.text == "介绍一下你自己"
    group = parse_mention(_mention_frame())
    assert group is not None
    assert group.direct is False
    assert group.group_id == "1023"


def test_mention_frame_keeps_the_group_sender_and_message_id() -> None:
    mention = parse_mention(_mention_frame())
    assert mention is not None
    assert mention.group_id == "1023"
    assert mention.sender_id == "h00557917"
    assert mention.sender_name == "黄宏运"
    assert mention.message_id == "m-9"
    assert mention.mention_accounts == ("h001",)
    assert mention.tool_session_id == "ses_mention"
    assert control_reply({"type": "status_query", "traceId": "t-1"}) == [
        {"type": "status_response", "traceId": "t-1", "opencodeOnline": True}
    ]


def test_one_connection_answers_a_mention_and_a_later_polled_message() -> None:
    socket = _QueuedSocket()
    socket.push({"type": "register_ok"})
    opened: list[_QueuedSocket] = []

    def connect(url: str, *, subprotocols: list[str], timeout: float) -> _QueuedSocket:
        assert url.startswith("ws://")
        assert subprotocols[0].startswith("auth.")
        opened.append(socket)
        return socket

    async def run() -> None:
        link = AssistantLink("ak-1", "secret-value", connect=connect, heartbeat_seconds=30, slot_ttl_seconds=5)
        seen: list[str] = []

        async def on_mention(mention) -> bool:
            seen.append(mention.message_id)
            return True

        link.start(asyncio.get_running_loop(), on_mention)
        try:
            for _ in range(40):
                if any(item.get("type") == "register" for item in socket.sent):
                    break
                await asyncio.sleep(0.05)
            socket.push(_mention_frame())
            for _ in range(40):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == ["m-9"]
            reply_id = link.send_text("1023", "绝句在此", "m-9")
            assert reply_id.startswith("msg_notify_")
            pushed = link.send_text("1023", "自主回复", "m-10")
            assert pushed.startswith("msg_notify_")
            assert link.claim_polled("1023", "m-9", "@小九 再给我写首绝句") is True
        finally:
            link.stop()

    asyncio.run(run())
    assert len(opened) == 1
    kinds = [item.get("type") for item in socket.sent]
    assert kinds[0] == "register"
    qa = [item for item in socket.sent if item.get("businessPushType") == "q_a"]
    active = [item for item in socket.sent if item.get("businessPushType") == "active_push"]
    assert qa
    assert all(item.get("toolSessionId") == "ses_mention" for item in qa)
    assert any(item.get("type") == "tool_event" and item.get("event", {}).get("properties", {}).get("content") == "绝句在此" for item in qa)
    assert active
    assert active[0]["businessId"] == "1023"
    assert "send-to-group" not in json.dumps(socket.sent)


def test_private_reply_finishes_the_open_task() -> None:
    socket = _QueuedSocket()
    socket.push({"type": "register_ok"})

    def connect(url: str, *, subprotocols: list[str], timeout: float) -> _QueuedSocket:
        return socket

    async def run() -> None:
        link = AssistantLink("ak-1", "secret-value", connect=connect, heartbeat_seconds=30, slot_ttl_seconds=5)
        seen: list[bool] = []

        async def on_mention(mention) -> bool:
            seen.append(mention.direct)
            return True

        link.start(asyncio.get_running_loop(), on_mention)
        try:
            for _ in range(40):
                if any(item.get("type") == "register" for item in socket.sent):
                    break
                await asyncio.sleep(0.05)
            socket.push(_private_frame())
            for _ in range(40):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == [True]
            reply_id = link.send_text("p2p:h00557917", "我是英语表达教练", "m-direct")
            assert reply_id.startswith("msg_notify_")
            try:
                link.send_text("p2p:h00557917", "再回一次", "m-direct")
            except AssistantSendError as exc:
                assert exc.code == "direct_session_closed"
            else:
                raise AssertionError("closed private session must not push as a group")
        finally:
            link.stop()

    asyncio.run(run())
    qa = [item for item in socket.sent if item.get("businessPushType") == "q_a"]
    assert any(
        item.get("type") == "tool_event" and item.get("event", {}).get("properties", {}).get("content") == "我是英语表达教练"
        for item in qa
    )
    assert not any(item.get("businessPushType") == "active_push" for item in socket.sent)
    assert "send-to-group" not in json.dumps(socket.sent)
