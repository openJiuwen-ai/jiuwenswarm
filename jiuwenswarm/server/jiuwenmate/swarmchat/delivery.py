"""Idempotent colleague replies through the existing public group message API."""

import logging

from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.output_files import WorkspaceOutputFiles
from jiuwenswarm.server.jiuwenmate.swarmchat.membership import require_membership
from jiuwenswarm.server.runtime.group_service import resolve_group_backend

logger = logging.getLogger(__name__)


async def send_delivery(job: dict, *, service) -> dict | None:
    acknowledgement = {"delivery_id": job["delivery_id"], "stage": "send"}
    try:
        group, member = require_membership(str(job["target_id"]), str(job["agent_id"]))
        if not service.get_colleague(str(job["namespace_id"]), str(job["agent_id"])).enabled:
            raise PersistentAgentError("Colleague is disabled", code="FORBIDDEN")
    except PersistentAgentError:
        return {**acknowledgement, "outcome": "failed", "error_code": "GROUP_ACCESS_LOST"}
    backend = await resolve_group_backend(group)
    if backend is None:
        return None
    token = set_session_id(group["session_id"])
    try:
        mentions = []
        roster = {item["member_name"] for item in group["members"]}
        text = str(job.get("text") or "")
        for item in job.get("mention_targets", []):
            name = item.get("agent_id")
            marker = f"[[agent:{name}]]"
            if name in roster and name != member["member_name"]:
                mentions.append(name)
                text = text.replace(marker, f"@{item['name']}")
            else:
                text = text.replace(marker, "")
        attachments = []
        if isinstance(job.get("file"), dict):
            file = job["file"]
            path = WorkspaceOutputFiles(service.workspaces).resolve_key(file["key"], file["file_ref"], file["size"])
            if path is None:
                return {**acknowledgement, "outcome": "failed", "error_code": "OUTPUT_FILE_UNAVAILABLE"}
            attachments.append({"path": str(path), "name": file["name"], "size": file["size"]})
        posted = await backend.append_group_message(
            member["member_name"], text, client_message_id=str(job["idempotency_key"]),
            mentions=mentions, attachments=attachments,
        )
        from jiuwenswarm.server.runtime.group_execution import project_group_stream_event
        from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

        projected = await project_group_stream_event(group["group_id"], {
            "event_type": "team.message", "event": {"message_id": posted.message.message_id},
        })
        if projected is not None:
            try:
                await WebSocketGatewayPushTransport().send_push({
                    "request_id": str(job["request_id"]), "session_id": group["group_id"],
                    "channel_id": group["channel_id"], "is_complete": False, "payload": projected,
                })
            except Exception:
                logger.warning("Group reply push failed; history remains available", exc_info=True)
        return {**acknowledgement, "outcome": "delivered", "external_message_id": posted.message.message_id}
    except Exception:
        # Retrying the same core client_message_id is safe even if the broadcast already committed.
        logger.exception("SwarmChat delivery will retry with the same public message identity")
        return None
    finally:
        reset_session_id(token)


async def post_unavailable_notice(group: dict, member: dict, source_id: str) -> None:
    """Report an unavailable colleague without starting an execution or an automatic chain."""
    from jiuwenswarm.common.utils import prompt_preferred_language

    backend = await resolve_group_backend(group)
    if backend is None:
        return
    token = set_session_id(group["session_id"])
    try:
        text = (
            "This AI colleague is unavailable. Check its enabled state and published model configuration."
            if prompt_preferred_language() == "en"
            else "该 AI 同事暂不可用，请检查启用状态和已发布的模型配置。"
        )
        await backend.append_group_message(member["member_name"], text,
            client_message_id=f"colleague-unavailable:{source_id}:{member['id']}", mentions=[])
    finally:
        reset_session_id(token)
