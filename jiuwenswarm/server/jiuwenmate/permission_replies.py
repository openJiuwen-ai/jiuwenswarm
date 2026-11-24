"""Consume authenticated IM permission replies before business request admission."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import (
    PermissionApproval,
    PermissionApprovalRepository,
)

logger = logging.getLogger(__name__)
_COMMAND = re.compile(
    r"\s*(approve|批准|同意|reject|拒绝)(?:\s+([a-f0-9]{12})(?:\s+(\S+))?)?\s*",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"\s*(approve\b|reject\b|(?:批准|拒绝)(?=\s|$|了))", re.IGNORECASE)
_REFERENCE = re.compile(r"\s*([a-f0-9]{12})(?:\s+(\S+))?\s*", re.IGNORECASE)
_TOOL_REFERENCE = re.compile(r"\s*(call[_-][A-Za-z0-9_-]+)\s*")
_ACTION_TOOL = re.compile(r"\s*(approve|批准|reject|拒绝)\s+(call[_-][A-Za-z0-9_-]+)\s*", re.IGNORECASE)
_LEADING_MENTION = re.compile(r"^\s*[@＠][^\s@＠]+[\s\u200b]+")


def permission_reply_text(text: str, *, receiver_bot_mentioned: bool = False) -> str:
    """Remove a leading mention only when the platform authenticated its recipient."""
    return _LEADING_MENTION.sub("", text, count=1) if receiver_bot_mentioned else text


def permission_command(text: str) -> tuple[str, str | None, str | None] | None:
    """Parse complete control messages; references never authorize an ambiguous batch."""
    match = _COMMAND.fullmatch(text)
    if match is not None:
        if match.group(1) == "同意" and match.group(2) is None:
            return None
        decision = "approve" if match.group(1).lower() in {"approve", "批准", "同意"} else "reject"
        return decision, match.group(2).lower() if match.group(2) is not None else None, match.group(3)
    match = _REFERENCE.fullmatch(text)
    if match is not None:
        return "approve", match.group(1).lower(), match.group(2)
    match = _ACTION_TOOL.fullmatch(text)
    if match is not None:
        return ("approve" if match.group(1).lower() in {"approve", "批准"} else "reject"), None, match.group(2)
    match = _TOOL_REFERENCE.fullmatch(text)
    return ("approve", None, match.group(1)) if match is not None else None


def approval_instructions(approval: PermissionApproval) -> str:
    """Render real, copyable commands without changing batch confirmation semantics."""
    commands = "\n".join(
        f"批准 {approval.approval_id} {item['tool_call_id']}" for item in approval.intercepted
    )
    if len(approval.intercepted) == 1:
        hint = "本会话只有这一条待审批操作时，可直接回复：批准 或 拒绝。"
    else:
        hint = "请选一条命令回复；仅批准选中的调用，其余调用会被拒绝。"
    return f"{hint}\n{commands}\n拒绝全部调用：拒绝 {approval.approval_id}"


def _decision_feedback(approval: PermissionApproval, *, duplicate: bool = False) -> str:
    if approval.decision is None and (duplicate or approval.status != "pending"):
        return "原操作已结束或审批已失效。"
    responses = (approval.reply or {}).get("responses", ())
    if approval.status == "expired" or any(
        (item.get("feedback") or "").startswith("Permission approval expired") for item in responses
    ):
        return "审批已过期，该操作未获批准；原任务将按拒绝结果继续处理。"
    if duplicate:
        return "该操作已批准，无需重复确认。" if approval.decision == "approve" else "该操作已拒绝，无需重复确认。"
    return "已批准，正在继续原任务。" if approval.decision == "approve" else "已拒绝，原任务将按拒绝结果继续处理。"


class IMPermissionReplies:
    """Share parsing and feedback across channels while retaining exact route checks."""

    def __init__(
        self, repository: PermissionApprovalRepository, *, decide: Callable[..., Awaitable[Any]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._repository = repository
        self._decide = decide
        self._send = send

    async def handle(
        self, text: str, *, namespace_id: str, agent_id: str, access: AccessSnapshot,
        channel: str, target_id: str, app_id: str, source_message_id: str,
        is_direct_message: bool = True,
        receiver_bot_mentioned: bool = False,
        mentions_other: bool = False,
    ) -> bool:
        """Return True only for control messages; business messages keep their normal queue."""
        if mentions_other:
            return False
        text = permission_reply_text(text, receiver_bot_mentioned=receiver_bot_mentioned)
        addressed = is_direct_message or receiver_bot_mentioned
        identity = dict(namespace_id=namespace_id, agent_id=agent_id, channel=channel,
                        app_id=app_id, target_id=target_id, principal_ref=access.principal_ref)

        async def feedback(message: str) -> None:
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"permission-reply:{identity}:{source_message_id}:{message}"))
            try:
                await self._send({
                    "channel": channel, "app_id": app_id, "chat_id": target_id,
                    "source_message_id": source_message_id,
                    "idempotency_key": key, "text": message,
                    **({"is_direct_message": is_direct_message} if channel == "dingtalk" else {}),
                })
            except Exception as exc:
                logger.warning("[JiuwenMate] permission feedback failed: error_type=%s", type(exc).__name__)

        def belongs(approval: PermissionApproval) -> bool:
            return (
                approval.namespace_id == namespace_id and approval.approver_agent_id == agent_id
                and approval.principal_ref == access.principal_ref and approval.route is not None
                and (approval.route.channel, approval.route.app_id, approval.route.target_id)
                == (channel, app_id, target_id)
            )

        command = permission_command(text)
        pending = await asyncio.to_thread(self._repository.for_route, **identity)
        replay = await asyncio.to_thread(
            self._repository.reply_approval, **identity, message_id=source_message_id,
        ) if command is not None else None
        reference_only = _REFERENCE.fullmatch(text) is not None or _TOOL_REFERENCE.fullmatch(text) is not None
        if reference_only and not (pending or replay is not None or receiver_bot_mentioned):
            # Reference-shaped business text needs an authenticated control context.
            command = None
        if command is None:
            if pending:
                approval = pending[0]
                if _PREFIX.match(text):
                    await feedback("未识别审批指令，请按下面的完整命令回复：\n" + approval_instructions(approval))
                    return True
                await feedback("当前任务仍在等待确认，后续消息将排队；请先批准或拒绝原操作。\n"
                               + approval_instructions(approval))
            elif _PREFIX.match(text):
                if addressed:
                    await feedback("未识别审批指令，请使用审批通知中给出的完整命令。")
                return True
            else:
                waiting = await asyncio.to_thread(
                    self._repository.for_route, **{**identity, "principal_ref": None},
                )
                if waiting:
                    await feedback("当前会话有任务等待原提问者确认，后续消息将排队。")
            return False

        if replay == "":
            if addressed or pending:
                await feedback("这条审批消息此前未对应可处理的操作，请重新发送完整审批命令。")
            return True
        decision, approval_id, tool_call_id = command
        approval_id = replay or approval_id
        if approval_id is None:
            ambiguous_group = False
            if not addressed and len(pending) == 1:
                conversations = await asyncio.to_thread(
                    self._repository.for_conversation, namespace_id, channel=channel, target_id=target_id,
                )
                ambiguous_group = len(conversations) != 1
            if len(pending) != 1 or ambiguous_group:
                # Even an unaccepted bare command must not authorize a future interruption.
                await asyncio.to_thread(
                    self._repository.reply_approval, **identity, message_id=source_message_id, approval_id="",
                )
                if addressed or pending:
                    await feedback("当前没有待审批操作。" if not pending else
                                   "有多条待审批记录，请 @ 对应员工或使用对应通知中的完整命令。")
                return True
            approval = pending[0]
        else:
            approval = await asyncio.to_thread(self._repository.get, approval_id)
            if approval is None or not belongs(approval):
                if addressed:
                    await feedback("未找到你在当前会话中可处理的审批，请核对原通知。")
                return True

        bound = await asyncio.to_thread(
            self._repository.reply_approval, **identity, message_id=source_message_id,
            approval_id=approval.approval_id,
        )
        if bound != approval.approval_id:
            await feedback("该消息已用于其他审批，请重新发送审批指令。")
            return True
        if approval.status != "pending":
            await feedback(_decision_feedback(approval, duplicate=True))
            return True
        if decision == "approve" and tool_call_id is None and len(approval.intercepted) == 1:
            tool_call_id = approval.intercepted[0]["tool_call_id"]
        invalid_approval = decision == "approve" and tool_call_id not in {
            item["tool_call_id"] for item in approval.intercepted
        }
        if invalid_approval or decision == "reject" and tool_call_id is not None:
            await feedback("请选择具体工具调用，回复以下完整命令：\n" + approval_instructions(approval))
            return True

        try:
            await self._decide(
                approval.approval_id, decision=decision, access=access, channel=channel,
                target_id=target_id, app_id=app_id, tool_call_id=tool_call_id,
                approver_agent_id=agent_id,
            )
        except PersistentAgentError as exc:
            if exc.code == "CONFLICT":
                current = await asyncio.to_thread(self._repository.get, approval.approval_id)
                await feedback(_decision_feedback(current, duplicate=True))
            else:
                await feedback("审批未被接受，请核对发起人、会话及原审批通知后重试。")
            return True
        except Exception as exc:
            logger.warning("[JiuwenMate] permission decision failed: error_type=%s", type(exc).__name__)
            await feedback("审批处理遇到异常，请重新发送原审批命令；无需重新提交任务。")
            return True
        current = await asyncio.to_thread(self._repository.get, approval.approval_id)
        await feedback(_decision_feedback(current))
        return True
