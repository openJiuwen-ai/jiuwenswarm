"""Consume authenticated IM permission replies before business request admission."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
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
    ) -> bool:
        """Return True only for control messages; business messages keep their normal queue."""
        identity = dict(namespace_id=namespace_id, agent_id=agent_id, channel=channel,
                        app_id=app_id, target_id=target_id, principal_ref=access.principal_ref)

        async def feedback(message: str) -> None:
            digest = hashlib.sha256(f"{identity}:{source_message_id}:{message}".encode()).hexdigest()
            try:
                await self._send({
                    "channel": channel, "app_id": app_id, "chat_id": target_id,
                    "source_message_id": source_message_id,
                    "idempotency_key": f"permission-reply:{digest}", "text": message,
                    **({"is_direct_message": is_direct_message} if channel == "dingtalk" else {}),
                })
            except Exception as exc:
                logger.warning("[JiuwenMate] permission feedback failed: error_type=%s", type(exc).__name__)

        def belongs(approval: PermissionApproval) -> bool:
            return (
                approval.namespace_id == namespace_id and approval.agent_id == agent_id
                and approval.principal_ref == access.principal_ref and approval.route is not None
                and (approval.route.channel, approval.route.app_id, approval.route.target_id)
                == (channel, app_id, target_id)
            )

        match = _COMMAND.fullmatch(text)
        if match is not None and match.group(1) == "同意" and match.group(2) is None:
            # Ordinary agreement is business input; the alias needs an explicit approval ID.
            match = None
        pending = await asyncio.to_thread(self._repository.for_route, **identity)
        replay = await asyncio.to_thread(
            self._repository.reply_approval, **identity, message_id=source_message_id,
        ) if match is not None else None
        if match is None:
            if pending:
                approval = pending[0]
                if _PREFIX.match(text):
                    await feedback("未识别审批指令，请按下面的完整命令回复：\n" + approval_instructions(approval))
                    return True
                await feedback("当前任务仍在等待确认，后续消息将排队；请先批准或拒绝原操作。\n"
                               + approval_instructions(approval))
            elif _PREFIX.match(text):
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
            await feedback("这条审批消息此前未对应可处理的操作，请重新发送完整审批命令。")
            return True
        decision = "approve" if match.group(1).lower() in {"approve", "批准", "同意"} else "reject"
        approval_id = replay or match.group(2)
        tool_call_id = match.group(3)
        if approval_id is None:
            if len(pending) != 1:
                # Even an unaccepted bare command must not authorize a future interruption.
                await asyncio.to_thread(
                    self._repository.reply_approval, **identity, message_id=source_message_id, approval_id="",
                )
                await feedback("当前没有待审批操作。" if not pending else
                               "有多条待审批记录，请使用对应通知中的完整命令。")
                return True
            approval = pending[0]
        else:
            approval = await asyncio.to_thread(self._repository.get, approval_id)
            if approval is None or not belongs(approval):
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
        if (
            decision == "approve" and tool_call_id not in {item["tool_call_id"] for item in approval.intercepted}
            or decision == "reject" and tool_call_id is not None
        ):
            await feedback("请选择具体工具调用，回复以下完整命令：\n" + approval_instructions(approval))
            return True

        try:
            await self._decide(
                approval.approval_id, decision=decision, access=access, channel=channel,
                target_id=target_id, app_id=app_id, tool_call_id=tool_call_id,
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
