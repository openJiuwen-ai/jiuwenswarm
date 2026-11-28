"""Route persisted SwarmChat mentions into the existing colleague scheduler."""

import asyncio
import logging
from contextlib import suppress
from typing import Any

from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.jiuwenmate.swarmchat.membership import colleague_members, require_membership
from jiuwenswarm.server.jiuwenmate.swarmchat.port import converted
from jiuwenswarm.server.runtime.group_service import GroupService, read_group, resolve_group_backend

logger = logging.getLogger(__name__)

# Callbacks deliver mentions immediately; the scan only recovers what a callback missed.
SCAN_INTERVAL_SECONDS = 5


def _position(message: ConversationMessage) -> tuple[int, str]:
    return int(message.timestamp), message.message_id


def _joined_ms(group: dict, member_name: str) -> int:
    joined = group.get("compiled", {}).get(member_name, {}).get("joined_at", group["created_at"])
    return int(joined * 1000)


async def public_rows(group: dict) -> list[ConversationMessage]:
    """Visible public messages, read without rewriting the history projection."""
    messages = await GroupService(None).public_messages(
        group["group_id"], user_id=group["user_id"], channel_id=group["channel_id"],
    )
    return sorted(messages, key=_position)


class ColleagueGroupBridge:
    """One host-owned receiver; persistent request identities make polling and callbacks idempotent."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._groups = GroupService(None)
        self._task: asyncio.Task[None] | None = None
        self._attached = {}
        self._pending: set[asyncio.Task[None]] = set()
        self._inflight: set[tuple[str, str, str]] = set()
        from jiuwenswarm.server.jiuwenmate.swarmchat.consumption import SwarmChatConsumption

        self._consumption = SwarmChatConsumption(manager.db_path)

    async def start(self) -> None:
        if self._task is None:
            from jiuwenswarm.server.runtime import group_auto_reply

            group_auto_reply.register_colleague_notifier(self.notify)
            self._task = asyncio.create_task(self._run(), name="swarmchat-colleagues")

    async def aclose(self) -> None:
        from jiuwenswarm.server.runtime import group_auto_reply

        group_auto_reply.register_colleague_notifier(None)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for pending in tuple(self._pending):
            pending.cancel()
        for backend, name in self._attached.values():
            with suppress(Exception):
                await backend.register_host_managed_inbound(name, None)
        self._attached.clear()

    async def _run(self) -> None:
        while True:
            try:
                await self.catch_up()
            except Exception:
                logger.exception("SwarmChat colleague recovery failed")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def _receive(self, group_id: str, member_name: str):
        async def receive(message: ConversationMessage) -> None:
            # Run on the event loop so the host can track the submission task.
            task = asyncio.create_task(self._submit_logged(group_id, member_name, message))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        return receive

    async def _submit_logged(self, group_id: str, member_name: str, message: ConversationMessage) -> None:
        try:
            await self.submit(group_id, member_name, message)
        except Exception:
            logger.exception("SwarmChat mention was not accepted; recovery will retry group=%s member=%s",
                             group_id, member_name)

    async def catch_up(self) -> None:
        try:
            await self._manager.reconcile_swarmchat_requests()
        except Exception:
            logger.exception("SwarmChat colleague request reconciliation failed")
        for path in get_agent_sessions_dir().glob("*/group.json"):
            try:
                await self._catch_up_group(path.parent.name)
            except Exception:
                # One broken group must not stop receivers and recovery for the others.
                logger.exception("SwarmChat colleague recovery failed for group=%s", path.parent.name)

    async def _catch_up_group(self, group_id: str) -> None:
        group = read_group(group_id)
        if group is None or group.get("status") == "deleting":
            return
        try:
            from jiuwenswarm.server.runtime import group_auto_reply

            # Notifications decided while the bridge was down are delivered by this scan.
            await group_auto_reply.recover_group(group_id)
        except Exception:
            logger.exception("SwarmChat auto-reply recovery failed for group=%s", group_id)
        members = colleague_members(group)
        if not members:
            return
        backend = await resolve_group_backend(group)
        if backend is None:
            return
        history = None
        for member in members:
            try:
                history = await self._catch_up_member(group, backend, member["member_name"], history)
            except Exception:
                logger.exception("SwarmChat colleague recovery failed for group=%s member=%s",
                                 group["group_id"], member["member_name"])

    async def _catch_up_member(
        self, group: dict, backend: Any, name: str, history: list[ConversationMessage] | None,
    ) -> list[ConversationMessage]:
        key = group["group_id"], name
        if self._attached.get(key, (None,))[0] is not backend:
            await backend.register_host_managed_inbound(name, self._receive(group["group_id"], name))
            self._attached[key] = backend, name
        if history is None:
            history = await public_rows(group)
        after = self._consumption.watermark(group["group_id"], name) or (_joined_ms(group, name), "")
        for message in history:
            position = _position(message)
            if position <= after:
                continue
            if name in message.mentions and message.sender != name:
                # A failure, or a mention a callback is still handling, stops this member
                # here so the position never moves past an unconfirmed mention.
                if not await self.submit(group["group_id"], name, message, history=history):
                    break
            self._consumption.advance(group["group_id"], name, *position)
        return history

    async def stop(self, group: dict) -> None:
        history = await self._groups.public_messages(
            group["group_id"], user_id=group["user_id"], channel_id=group["channel_id"],
        )
        roots = [item.message_id for item in history if item.sender == "user"]
        if roots:
            self._consumption.stop(group["group_id"], roots[-1])

    async def detach_member(self, group_id: str, member_name: str) -> None:
        """Detach host-managed inbound callback state after a colleague leaves a group."""
        entry = self._attached.pop((group_id, member_name), None)
        if entry is not None:
            backend, name = entry
            with suppress(Exception):
                await backend.register_host_managed_inbound(name, None)

    async def submit(
        self, group_id: str, member_name: str, message: ConversationMessage,
        *, history: list[ConversationMessage] | None = None, notified: bool = False,
    ) -> bool:
        """Handle one mention; False means another path is still handling it."""
        # The callback and the scan may see the same mention; one of them handles it.
        key = group_id, message.message_id, member_name
        if key in self._inflight:
            return False
        self._inflight.add(key)
        try:
            await self._submit(group_id, member_name, message, history, notified)
        finally:
            self._inflight.discard(key)
        return True

    async def notify(self, group_id: str, member_name: str, message: ConversationMessage) -> bool:
        """Ask a colleague to answer a message that does not mention it.

        Shares the request identity, reservation and stop handling of mentions,
        so a repeated notification never starts a second request.
        """
        return await self.submit(group_id, member_name, message, notified=True)

    async def _submit(
        self, group_id: str, member_name: str, message: ConversationMessage,
        history: list[ConversationMessage] | None, notified: bool = False,
    ) -> None:
        group = read_group(group_id)
        addressed = notified or member_name in message.mentions
        if group is None or not addressed or member_name == message.sender:
            return
        member = next((item for item in colleague_members(group) if item["member_name"] == member_name), None)
        if member is None or message.timestamp < _joined_ms(group, member_name):
            return
        from jiuwenswarm.server.runtime.group_service import _cleared, _seconds

        if _cleared(_seconds(message.timestamp), group.get("history_cleared_at")):
            return
        if message.sender == "user":
            root_id = message.message_id
        else:
            if history is None:
                history = await public_rows(group)
            root_id = None
            for previous in history:
                if _position(previous) <= _position(message) and previous.sender == "user":
                    root_id = previous.message_id
            if root_id is None:
                return
        reserved = self._consumption.reserve(group_id, message.message_id, member_name,
                                             root_id, message.sender != "user")
        if reserved in {"accepted", "stopped"}:
            return
        if reserved == "limited":
            backend = await resolve_group_backend(group)
            if backend is not None:
                from openjiuwen.agent_teams.context import set_session_id, reset_session_id

                token = set_session_id(group_id)
                try:
                    from jiuwenswarm.common.utils import prompt_preferred_language

                    notice = (
                        "AI colleague automatic reply limit reached. Send a new message to continue."
                        if prompt_preferred_language() == "en" else "AI 同事自动接力已达到上限，请发送新消息继续。"
                    )
                    notice_member = colleague_members(group)[0]["member_name"]
                    await backend.append_group_message(
                        notice_member, notice,
                        client_message_id=f"colleague-limit:{root_id}", mentions=[],
                    )
                finally:
                    reset_session_id(token)
            return
        source = converted(group, message)
        if notified and member["id"] not in source.mention_ids:
            # The group's judgement already chose this colleague. Without the mention its own
            # participation check may ignore the message while the group shows it was asked.
            # Only the colleague's input changes; the stored message keeps its real mentions.
            source = source.model_copy(update={"mention_ids": (*source.mention_ids, member["id"])})
        await self._manager.accept_swarmchat_message(group, member, source)
        self._consumption.accept(group_id, message.message_id, member_name)


def _approval_call_summary(call: dict, *, english: bool) -> str:
    """Build a bounded display summary without exposing raw tool arguments."""
    import json

    from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
        sanitize_permission_ui_payload,
    )

    raw = call.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            raw = None
    if not isinstance(raw, dict) or call.get("arguments_truncated"):
        return "Details unavailable" if english else "操作详情不可用"
    fields = ("description", "command", "group_name", "chat_id", "target_id", "path")
    values = {key: raw[key] for key in fields if isinstance(raw.get(key), str)}
    safe = sanitize_permission_ui_payload(values, max_string_length=180)
    if not isinstance(safe, dict):
        return "Details unavailable" if english else "操作详情不可用"
    parts = [" ".join(value.split()) for value in safe.values() if isinstance(value, str) and value.strip()]
    return " · ".join(parts) or ("No operation summary provided" if english else "工具未提供操作摘要")


async def publish_permission_approval(manager: Any, approval: Any) -> None:
    """Project a durable colleague approval into the group's existing question UI."""
    from jiuwenswarm.server.runtime.group_questions import GroupQuestions, register_question_target

    group, member = require_membership(approval.route.target_id, approval.agent_id)
    target = register_question_target(group["group_id"], approval.session_id, member["member_name"], manager)
    from jiuwenswarm.common.utils import prompt_preferred_language

    english = prompt_preferred_language() == "en"
    options = []
    for index, call in enumerate(approval.intercepted):
        label = "Approve" if english else "允许"
        if len(approval.intercepted) > 1:
            label = f"{label} {index + 1}: {call.get('tool_name', 'tool')}"
        summary = _approval_call_summary(call, english=english)
        options.append({"label": label, "tool_call_id": call["tool_call_id"],
                        "description": f"{call.get('tool_name', 'tool')} · {summary}"})
    options.append({"label": "Reject" if english else "拒绝"})
    scope = "Approve only the selected call" if english else "仅允许所选工具调用"
    question = f"{scope} · {group['name']}"
    await GroupQuestions(group["group_id"]).ask(
        {"request_id": approval.approval_id, "questions": [{
            "question": question,
            "header": "Permission" if english else "权限审批",
            "options": options,
        }]}, target, member["member_name"], None, source="colleague_permission",
    )
