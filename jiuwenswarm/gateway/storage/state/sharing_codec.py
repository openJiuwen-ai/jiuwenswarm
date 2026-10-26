# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionSharing Subscription ↔ Ephemeral bytes。"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from typing import Any

from jiuwenswarm.gateway.routing.keys import (
    AcpDeliveryTarget,
    AgentRef,
    DeliveryTarget,
    DingTalkDeliveryTarget,
    DiscordDeliveryTarget,
    FeishuDeliveryTarget,
    RoutingKey,
    SlackDeliveryTarget,
    TelegramDeliveryTarget,
    TuiDeliveryTarget,
    WebDeliveryTarget,
    WechatDeliveryTarget,
    WecomDeliveryTarget,
    WhatsAppDeliveryTarget,
    XiaoyiDeliveryTarget,
)
from jiuwenswarm.gateway.routing.session_sharing import Subscription

_DELIVERY_TYPES: dict[str, type[DeliveryTarget]] = {
    "WebDeliveryTarget": WebDeliveryTarget,
    "TuiDeliveryTarget": TuiDeliveryTarget,
    "AcpDeliveryTarget": AcpDeliveryTarget,
    "XiaoyiDeliveryTarget": XiaoyiDeliveryTarget,
    "FeishuDeliveryTarget": FeishuDeliveryTarget,
    "WecomDeliveryTarget": WecomDeliveryTarget,
    "DingTalkDeliveryTarget": DingTalkDeliveryTarget,
    "TelegramDeliveryTarget": TelegramDeliveryTarget,
    "DiscordDeliveryTarget": DiscordDeliveryTarget,
    "SlackDeliveryTarget": SlackDeliveryTarget,
    "WhatsAppDeliveryTarget": WhatsAppDeliveryTarget,
    "WechatDeliveryTarget": WechatDeliveryTarget,
}

SUBSCRIPTIONS_HASH = "subscriptions"


def _delivery_to_dict(delivery: DeliveryTarget) -> dict[str, Any]:
    cls = type(delivery)
    payload = {f.name: getattr(delivery, f.name) for f in fields(delivery)}
    return {"_type": cls.__name__, "fields": payload}


def _delivery_from_dict(data: dict[str, Any]) -> DeliveryTarget:
    type_name = str(data.get("_type") or "")
    cls = _DELIVERY_TYPES.get(type_name)
    if cls is None:
        raise ValueError(f"unknown delivery target type: {type_name!r}")
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict):
        raise ValueError("delivery.fields must be an object")
    allowed = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in raw_fields.items() if k in allowed}
    return cls(**kwargs)


def subscription_to_bytes(sub: Subscription) -> bytes:
    payload = {
        "sub_id": sub.sub_id,
        "member_name": sub.member_name,
        "joined_at": sub.joined_at,
        "routing_key": sub.routing_key.to_dict(),
        "delivery": _delivery_to_dict(sub.delivery),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def subscription_from_bytes(raw: bytes) -> Subscription:
    data = json.loads(raw.decode("utf-8"))
    rk = data["routing_key"]
    agent_ref = rk["agent_ref"]
    routing_key = RoutingKey(
        user_id=str(rk["user_id"]),
        channel_id=str(rk["channel_id"]),
        app_id=str(rk["app_id"]),
        agent_ref=AgentRef(mode=str(agent_ref["mode"]), id=str(agent_ref["id"])),
        session_id=str(rk["session_id"]),
    )
    delivery = _delivery_from_dict(data["delivery"])
    return Subscription(
        sub_id=str(data["sub_id"]),
        member_name=str(data["member_name"]),
        routing_key=routing_key,
        delivery=delivery,
        joined_at=float(data.get("joined_at") or 0),
    )


def subscription_session_id(sub: Subscription) -> str:
    return sub.routing_key.session_id


__all__ = [
    "SUBSCRIPTIONS_HASH",
    "subscription_from_bytes",
    "subscription_session_id",
    "subscription_to_bytes",
]
