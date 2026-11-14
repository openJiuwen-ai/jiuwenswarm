"""Verify the wire boundary between a standalone Gateway and the JiuwenMate host."""

from datetime import datetime, timezone

import pytest
from jiuwenswarm.common.schema.req_method import ReqMethod as WireMethod
from e2a.models import E2AEnvelope

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkInboundEvent as HostDingTalkEvent
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import FeishuInboundEvent as HostFeishuEvent


@pytest.mark.parametrize("channel", ["feishu", "dingtalk"])
def test_gateway_group_event_and_receipt_decode_on_host(channel):
    from agent_gateway.channel_manager.persistent_events import DingTalkInboundEvent, FeishuInboundEvent

    fields = dict(receiving_app_id="app", expected_agent_id="mate", message_id="source-msg",
                  sender_name="Alice", text="agenda", received_at=datetime.now(timezone.utc))
    if channel == "feishu":
        source = FeishuInboundEvent(**fields, chat_id="group", chat_type="group", sender_type="user",
                                    sender_id="alice", sender_app_id=None)
        parse = HostFeishuEvent.from_payload
    else:
        source = DingTalkInboundEvent(**fields, conversation_id="group", conversation_type="2",
                                      sender_staff_id="alice")
        parse = HostDingTalkEvent.from_payload

    def request(method, params):
        wire = E2AEnvelope(channel=channel, chat_id="group", request_id="request", method=method, params=params)
        return e2a_to_agent_request(E2AEnvelope.from_dict(wire.to_dict()))

    inbound = request(f"jiuwenmate.{channel}.inbound", {"event": source.to_payload()})
    assert inbound.req_method is WireMethod(f"jiuwenmate.{channel}.inbound")
    assert parse(inbound.params["event"]).to_payload() == source.to_payload()
    receipt = request("jiuwenmate.delivery.ack", {"delivery_id": "delivery", "stage": "send",
                                                 "outcome": "delivered", "external_message_id": "sent"})
    assert receipt.req_method is ReqMethod.JIUWENMATE_DELIVERY_ACK
    assert receipt.params["delivery_id"] == "delivery"
