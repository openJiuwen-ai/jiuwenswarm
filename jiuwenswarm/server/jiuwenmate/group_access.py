"""Platform-aware access checks shared by Web and IM request runtimes."""

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def configure_group_access(manager, verify_membership=None) -> None:
    """Wire platform access without starting a second manager or borrowing its stores."""
    groups = manager.group_chats

    async def verify(access, group):
        if getattr(group, "platform", "") == "dingtalk":
            from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import configured_dingtalk_bindings

            bindings = configured_dingtalk_bindings()
            if access.status == "active" and any(
                (item.namespace_id, item.agent_id, item.app_id)
                == (access.namespace_id, access.agent_id, access.app_id)
                for item in bindings
            ):
                # A group callback itself is DingTalk's available proof that this bound Bot
                # currently receives messages in the group; there is no membership-read API here.
                return "active"
            return "unknown"
        if getattr(group, "platform", "") == "welink":
            from jiuwenswarm.server.jiuwenmate.welink_inbound import welink_group_membership_status

            return welink_group_membership_status(access)
        from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import resolve_configured_feishu_bindings

        if group.platform != "feishu" or verify_membership is None:
            return "unknown"
        bindings = resolve_configured_feishu_bindings("feishu", access.app_id)
        if len(bindings) != 1 or (bindings[0].namespace_id, bindings[0].agent_id) != (
                access.namespace_id, access.agent_id):
            return "unknown"
        facts = await verify_membership(access.app_id, group.platform_chat_id)
        status = facts.get("status", "unknown")
        if status == "left":
            await asyncio.to_thread(groups.update_membership, access.namespace_id, access.agent_id,
                                   access.group_id, access.app_id, status="left",
                                   observed_at=datetime.now(timezone.utc), source_ref="membership:live")
            return "left"
        if status == "active" and facts.get("group_name"):
            await asyncio.to_thread(groups.set_name, access.namespace_id, access.group_id, facts["group_name"])
        if status == "active":
            return "active"
        # Feishu sometimes does not answer during a reconnect. That is not proof the Bot left.
        if access.status == "active":
            logger.warning(
                "[JiuwenMate] group membership check inconclusive, keeping the active binding: app_id=%s",
                access.app_id,
            )
            return "active"
        return "unknown"
    manager.verify_group_membership = verify

    async def refresh(access, group, messages):
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

        if getattr(group, "platform", "") in {"dingtalk", "welink"}:
            # These platforms use observed archive facts; do not call Feishu inspection.
            return tuple(messages)
        if group.platform != "feishu" or verify_membership is None:
            raise ValueError("platform message inspection is unavailable")
        facts = await verify_membership(access.app_id, group.platform_chat_id,
                                       tuple(item.message_id for item in messages))
        if facts.get("status") == "unknown":
            # A slow or dropped Feishu read is not proof the archived text changed.
            logger.warning(
                "[JiuwenMate] group message refresh got no Feishu answer, keeping the archived text: agent_id=%s",
                access.agent_id,
            )
            return tuple(messages)
        if facts.get("status") != "ok" or not isinstance(facts.get("messages"), dict):
            raise ValueError("current group message state unavailable")
        updated = []
        for message in messages:
            item = facts["messages"].get(message.message_id, {})
            status = item.get("status")
            if status == "recalled":
                await asyncio.to_thread(manager.group_archive.recall, access.namespace_id, access.group_id,
                    message.message_id, source_ref=f"refresh:{message.message_id}",
                    changed_at=datetime.now(timezone.utc))
                updated.append(message.model_copy(update={"status": "recalled", "text": None, "attachment_refs": ()}))
            elif status in {"normal", "edited"}:
                changed_at = datetime.fromisoformat(item["changed_at"])
                fresh = type(message).model_validate({**message.model_dump(), "status": status,
                    "text": item.get("text"), "attachment_refs": item.get("attachment_refs", ())})
                try:
                    await asyncio.to_thread(
                        manager.group_archive.append, access.namespace_id, access.group_id, fresh,
                        source_ref=f"refresh:{message.message_id}", changed_at=changed_at,
                        allow_resolved_mentions=True,
                    )
                except PersistentAgentError as exc:
                    if exc.code != "CONFLICT":
                        raise
                    # One unchanged revision must not fail the whole history read.
                    logger.warning(
                        "[JiuwenMate] group refresh kept the archived text: message_id=%s",
                        message.message_id,
                    )
                    updated.append(message)
                else:
                    updated.append(fresh)
            elif status == "unknown":
                # Feishu did not answer. The archived inbound text remains the trigger.
                updated.append(message)
            else:
                updated.append(message.model_copy(
                    update={"status": "unavailable", "text": None, "attachment_refs": ()},
                ))
        return tuple(updated)

    manager.refresh_group_messages = refresh
