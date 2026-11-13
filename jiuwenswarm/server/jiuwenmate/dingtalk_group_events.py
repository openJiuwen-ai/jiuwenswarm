"""Typed, app-bound DingTalk Stream group facts; no chat task is created here."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

GROUP_EVENT_TYPES = frozenset({
    "chat_add_member", "chat_remove_member", "chat_quit", "chat_update_title", "chat_disband",
})
logger = logging.getLogger(__name__)


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"invalid DingTalk group {name}")
    return value.strip()


@dataclass(frozen=True, kw_only=True)
class DingTalkGroupEvent:
    receiving_app_id: str
    expected_agent_id: str
    event_type: str
    event_id: str
    conversation_id: str
    changed_at: datetime
    received_at: datetime
    corp_id: str
    title: str | None = None
    operator: str = "lifecycle"

    def __post_init__(self):
        for name in ("receiving_app_id", "expected_agent_id", "event_id", "conversation_id", "corp_id", "operator"):
            _text(getattr(self, name), name)
        if self.event_type not in GROUP_EVENT_TYPES:
            raise ValueError("unsupported DingTalk group event")
        for stamp in (self.changed_at, self.received_at):
            if not isinstance(stamp, datetime) or stamp.utcoffset() is None or stamp.year < 2000:
                raise ValueError("invalid DingTalk group event time")
        if self.changed_at > self.received_at + timedelta(minutes=5):
            raise ValueError("future DingTalk group event")
        if self.title is not None:
            _text(self.title, "title")
        if self.event_type == "chat_update_title" and self.title is None:
            raise ValueError("missing DingTalk group title")

    @property
    def sender_staff_id(self) -> str:
        """Transport metadata only; never a permission approver or user request."""
        return self.operator

    @classmethod
    def from_stream(cls, raw, *, app_id: str, agent_id: str, headers=None):
        if not isinstance(raw, Mapping):
            raise ValueError("invalid DingTalk event data")
        body = raw.get("data", raw)
        if not isinstance(body, Mapping):
            raise ValueError("invalid DingTalk group event body")
        header_type = getattr(headers, "event_type", None)
        event_type = raw.get("eventType") or header_type
        if header_type is not None and header_type != event_type:
            raise ValueError("conflicting DingTalk group event type")
        if body.get("eventType", event_type) != event_type:
            raise ValueError("conflicting DingTalk group event body")
        corporation_facts = (
            raw.get("eventCorpId"), getattr(headers, "event_corp_id", None), body.get("corpId"),
        )
        corp_ids = [value for value in corporation_facts if value is not None]
        if not corp_ids or any(value != corp_ids[0] for value in corp_ids):
            raise ValueError("conflicting DingTalk group event corporation")
        event_id = raw.get("eventId") or getattr(headers, "event_id", None)
        if getattr(headers, "event_id", None) not in (None, event_id):
            raise ValueError("conflicting DingTalk group event identity")
        stamp = body.get("timeStamp", raw.get("eventBornTime", getattr(headers, "event_born_time", None)))
        if isinstance(stamp, bool) or not str(stamp).isdecimal():
            raise ValueError("invalid DingTalk group event timestamp")
        try:
            changed_at = datetime.fromtimestamp(int(stamp) / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("invalid DingTalk group event timestamp") from exc
        return cls(
            receiving_app_id=app_id, expected_agent_id=agent_id, event_type=event_type,
            event_id=_text(event_id, "event_id"), conversation_id=_text(body.get("openConversationId"), "group"),
            changed_at=changed_at, received_at=datetime.now(timezone.utc), corp_id=_text(corp_ids[0], "corp"),
            title=body.get("title"), operator=body.get("operator") or "lifecycle",
        )

    def to_payload(self) -> dict:
        return {**asdict(self), "event_kind": "group_lifecycle", "changed_at": self.changed_at.isoformat(),
                "received_at": self.received_at.isoformat()}

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or payload.get("event_kind") != "group_lifecycle":
            raise ValueError("invalid DingTalk lifecycle payload")
        values = {key: value for key, value in payload.items() if key != "event_kind"}
        values["changed_at"] = datetime.fromisoformat(values["changed_at"])
        values["received_at"] = datetime.fromisoformat(values["received_at"])
        return cls(**values)


async def handle_group_change(handler, event: DingTalkGroupEvent):
    """Apply platform facts only after revalidating the current app/Agent binding."""
    from jiuwenswarm.common.im_binding_lock import im_binding_lock
    from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope
    from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import configured_dingtalk_bindings
    from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition

    async with im_binding_lock("dingtalk", event.receiving_app_id):
        loader = handler.config_loader
        if loader is None:
            from jiuwenswarm.common.config import get_config
            loader = get_config
        bindings = configured_dingtalk_bindings(await asyncio.to_thread(loader))
        identity = (event.receiving_app_id, event.expected_agent_id)
        matches = []
        for item in bindings:
            if (item.app_id, item.agent_id) == identity:
                matches.append(item)
        if len(matches) != 1:
            return InboundDisposition.REJECTED
        binding = matches[0]
        connection = await asyncio.to_thread(handler.service.connection_of, binding.namespace_id, binding.agent_id)
        if connection is None:
            await asyncio.to_thread(handler.service.get_config, binding.namespace_id, binding.agent_id)
        elif (connection.channel, connection.connection_key) != ("dingtalk", binding.app_id):
            return InboundDisposition.REJECTED
        groups = handler.manager.group_chats
        group = await asyncio.to_thread(groups.find, binding.namespace_id, "dingtalk", event.conversation_id)
        known = False
        if group is not None:
            known = await asyncio.to_thread(groups.has_binding, binding.namespace_id, binding.agent_id,
                                            group.group_chat_id, binding.app_id)
        if event.event_type == "chat_update_title":
            if known:
                await asyncio.to_thread(groups.set_name, binding.namespace_id, group.group_chat_id, event.title,
                                        observed_at=event.changed_at)
        elif event.event_type == "chat_disband":
            # Keep a tombstone even before the first message, without adding Agent access.
            if group is None:
                group = await asyncio.to_thread(groups.resolve, binding.namespace_id, "dingtalk",
                                                event.conversation_id, now=event.changed_at)
            await asyncio.to_thread(groups.mark_dissolved, binding.namespace_id, group.group_chat_id,
                                    observed_at=event.changed_at, source_ref=f"dingtalk:event:{event.event_id}")
        else:
            if not known:
                return InboundDisposition.HANDLED
            try:
                proof = await handler.manager.dingtalk_cli_runtime.inspect_group_membership(
                    DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id),
                    conversation_id=event.conversation_id,
                )
            except Exception as exc:
                logger.warning("DingTalk lifecycle membership unavailable (%s)", type(exc).__name__)
                proof = {"status": "unknown"}
            status = proof.get("status") if isinstance(proof, dict) else None
            if status not in {"active", "left"}:
                # No successful ACK for an unverified change: let Stream retry.
                return InboundDisposition.REJECTED
            if status == "left":
                await asyncio.to_thread(groups.update_membership, binding.namespace_id, binding.agent_id,
                                        group.group_chat_id, binding.app_id, status=status,
                                        # The live probe concerns current membership, not the old human event.
                                        observed_at=event.received_at, source_ref=f"dingtalk:event:{event.event_id}")
        return InboundDisposition.HANDLED
