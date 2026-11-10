"""Host policy for meeting admission and explicit mailbox publication."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from openjiuwen.agent_teams.group_chat.handler import group_metadata, post_message, sync_history
from openjiuwen.agent_teams.meeting import MeetingCoordinator, MeetingRoute, SpeechRequest
from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.agent_teams.tools.tool_message import MAX_CONTENT_CHARS
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.common.utils import mask_sensitive

from .prompts import leader_speech_guidance, teammate_speech_guidance

logger = logging.getLogger(__name__)


async def publish_message(
    coordinator: MeetingCoordinator, manager: TeamMessageManager | None, conversation: GroupConversationLog | None,
    request_id: str, lease_token: str | None,
    playback_ready: bool, recover: bool = False,
) -> SpeechRequest | None:
    """Publish the persisted current grant; callers cannot supply message fields."""
    request = await coordinator.get_speaker()
    if request is None or request.request_id != request_id or not lease_token or request.lease_token != lease_token:
        return None
    try:
        ids = request.source_message_ids or ((request.source_message_id,) if request.source_message_id else ())
        if request.content and request.source_message_id is None:
            ids = await _publish_route(
                coordinator, manager, conversation,
                request.member_name, request.content, request.route, request.mentions,
                request.request_id, recover=recover,
            )
            request = replace(request, source_message_id=ids[0], source_message_ids=ids)
        # An earlier send can succeed while recording fails. Retrying uses
        # deterministic IDs so it links the same messages, never duplicates.
        # Empty content has no mailbox publication to record; it still follows
        # the same queue lease and playback acknowledgement path.
        return await coordinator.finish_publication(
            request.request_id, lease_token, source_message_ids=ids, playback_ready=playback_ready,
        )
    except Exception as exc:

        message = mask_sensitive(str(exc).strip() or type(exc).__name__)
        logger.exception("Meeting message publication failed: session_id=%s request_id=%s error=%s",
                         request.scope.session_id, request.request_id, message)
        current = await coordinator.get_speaker()
        if current is None or current.request_id != request_id or current.lease_token != lease_token:
            return None
        return replace(request, delivery_error=message)


async def _read_message(manager: TeamMessageManager, session_id: str, message_id: str):
    token = set_session_id(session_id)
    try:
        return await manager.db.message.get_message(message_id)
    finally:
        reset_session_id(token)


async def _publish_route(
    coordinator: MeetingCoordinator, manager: TeamMessageManager | None, conversation: GroupConversationLog | None,
    member_name: str, content: str,
    route: MeetingRoute, mentions: tuple[str, ...], request_id: str, *, recover: bool,
) -> tuple[str, ...]:
    if manager is None:
        raise ValueError("Publication requires the Team message manager")
    if route.kind == "group_chat":
        if conversation is None:
            raise ValueError("Group publication requires the Team conversation")
        if recover:
            # Use the existing group submission identity only on recovery;
            # an already committed message may outlive its members.
            identity = str(
                uuid5(
                    NAMESPACE_URL,
                    json.dumps(
                        [coordinator.scope.team_name, coordinator.scope.session_id, request_id], ensure_ascii=False
                    ),
                )
            )
            source = await _read_message(manager, coordinator.scope.session_id, identity)
            if source is not None:
                if (source.team_name != coordinator.scope.team_name or not source.broadcast
                        or not group_metadata(source)):
                    raise ValueError("Stored publication does not belong to this group")
                if (source.from_member_name, source.content, tuple(group_metadata(source)["mentions"])) != (
                    member_name,
                    content,
                    mentions,
                ):
                    raise ValueError("Stored publication does not match the queued utterance")
                token = set_session_id(coordinator.scope.session_id)
                try:
                    await sync_history(conversation, manager)
                    await manager.publish_broadcast(identity, member_name)
                finally:
                    reset_session_id(token)
                return (source.message_id,)
        posted = await post_message(
            conversation, manager, member_name, content,
            client_message_id=request_id, mentions=mentions,
        )
        return (posted.message.message_id,)
    recipients = route.recipients if route.kind in ("direct", "multicast") else (None,)
    ids = tuple(str(uuid5(NAMESPACE_URL, json.dumps(
        [coordinator.scope.team_name, coordinator.scope.session_id, coordinator.scope.meeting_id, request_id, route.kind, recipient],
        ensure_ascii=False,
    ))) for recipient in recipients)
    token = set_session_id(coordinator.scope.session_id)
    try:
        if route.kind == "direct":
            result = await manager.send_message(
                content, route.recipients[0], member_name, route.protocol, message_id=ids[0],
            )
            sent = (result,) if result else ()
        elif route.kind == "multicast":
            sent = tuple(await manager.multicast_message(
                content, list(route.recipients), member_name, route.protocol, message_ids=list(ids),
            ))
        elif route.kind == "broadcast":
            result = await manager.broadcast_message(
                content, member_name, message_id=ids[0], protocol=route.protocol,
            )
            sent = (result,) if result else ()
        else:
            raise ValueError("Unknown queued publication route")
        if sent != ids:
            raise RuntimeError("Team publication did not commit all messages")
        return ids
    finally:
        reset_session_id(token)


class MeetingSendMessageTool(TeamTool):
    """Queue speech using the member's ordinary recipient contract."""

    def __init__(
        self, backend: TeamBackend, coordinator: MeetingCoordinator, source: ToolCard, *, language: str,
    ) -> None:
        card = deepcopy(source)
        card.id = "team.meeting_send_message"
        card.name = "meeting_send_message"
        card.description = (
            "Queue a message for meeting speech using the declared recipient fields. "
            "Provide full content and a nonempty speech_text. The host publishes content when granted, "
            "then plays speech_text. A queue receipt is not delivery or playback completion. "
            "Use to='user' to reply to the human."
        )
        schema = card.input_params
        if backend.is_leader:
            card.description = (
                "Queue a spoken reply to the human without publishing a Team message. "
                "Use to='user', empty strings for content and summary, and the actual spoken reply in speech_text. "
                "Continue using ordinary assistant output for the text reply and send_message for Team messages. "
                "A queue receipt is not playback completion."
            )
            schema["properties"] = {
                "to": {"type": "string", "enum": ["user"]},
                "content": {"type": "string", "enum": [""], "description": "Empty; this call queues only audio."},
                "summary": {"type": "string", "enum": [""], "description": "Empty; the spoken reply is in speech_text."},
            }
            schema["required"] = ["to", "content", "summary"]
        schema.setdefault("properties", {})["speech_text"] = {
            "type": "string", "minLength": 1,
            "description": leader_speech_guidance(language) if backend.is_leader else teammate_speech_guidance(language),
        }
        required = schema.setdefault("required", [])
        if "speech_text" not in required:
            required.append("speech_text")
        schema["additionalProperties"] = False
        super().__init__(card)
        self.coordinator = coordinator
        self.backend = backend

    async def invoke(self, inputs: dict, **kwargs) -> ToolOutput:
        try:
            return await self._enqueue(inputs)
        except Exception as exc:

            message = mask_sensitive(str(exc).strip() or type(exc).__name__)
            logger.exception("Meeting message admission failed: session_id=%s member=%s error=%s",
                             self.coordinator.scope.session_id, self.backend.member_name, message)
            return ToolOutput(success=False, error=message)

    async def _enqueue(self, inputs: dict) -> ToolOutput:
        """Validate the exposed send contract, then queue without mailbox delivery."""
        schema = self.card.input_params
        if not isinstance(inputs, dict) or set(inputs) - set(schema.get("properties", {})):
            raise ValueError("Only declared message arguments are supported")
        if set(schema.get("required", ())) - set(inputs):
            raise ValueError("Required message arguments are missing")
        content, speech = inputs.get("content"), inputs.get("speech_text")
        if self.backend.is_leader:
            if content != "" or inputs.get("summary") != "":
                raise ValueError("Leader speech requires empty strings for content and summary")
        else:
            if not isinstance(content, str) or not content.strip():
                raise ValueError("'content' must be a nonempty string")
            content = content.strip()
            if len(content) > MAX_CONTENT_CHARS:
                raise ValueError(f"'content' is too long (limit {MAX_CONTENT_CHARS} characters); send a file and summary")
            if not isinstance(inputs.get("summary", ""), str):
                raise ValueError("'summary' must be a string")
        if not isinstance(speech, str) or not speech.strip():
            raise ValueError("'speech_text' must be a nonempty string")
        sender = self.backend.member_name
        route, failed = await self._route(inputs, schema)
        if not route:
            return ToolOutput(success=False, error="No valid recipients", data={"failed": failed})
        # The SDK checks meeting/member state at admission. Teammates do not
        # own member startup/recovery; the Leader's call only queues audio.
        request = await self.coordinator.enqueue_message(sender, content, speech_text=speech.strip(), route=route)
        return ToolOutput(
            success=not failed, error="Some recipients were not found" if failed else None,
            data={"type": "queued", "request_id": request.request_id, "from": sender,
                  "to": list(route.recipients), "failed": failed},
        )

    async def _route(self, inputs: dict, schema: dict) -> tuple[MeetingRoute | None, list[dict[str, str]]]:
        to, targets = inputs.get("to"), inputs.get("targets")
        properties = schema.get("properties", {})
        if targets is not None:
            if to is not None:
                raise ValueError("Use exactly one of 'to' or 'targets'")
            if "targets" not in properties or not isinstance(targets, list):
                raise ValueError("'targets' is not supported or is not an array")
            if any(not isinstance(item, str) or not item.strip() for item in targets):
                raise ValueError("'targets' must contain nonempty member names")
            recipients = tuple(dict.fromkeys(item.strip() for item in targets))
            if not recipients or "*" in recipients or "user" in recipients:
                raise ValueError("Use nonempty member targets; address '*' or 'user' separately with 'to'")
            roster = {member.member_name for member in await self.backend.list_member_roster()}
            if roster and set(recipients) == roster:
                raise ValueError("Multicast targets cover every other member; use to='*' instead")
            valid: list[str] = []
            failed: list[dict[str, str]] = []
            for recipient in recipients:
                if await self.backend.member_exists(recipient):
                    valid.append(recipient)
                else:
                    failed.append({"to": recipient, "reason": f"Member '{recipient}' not found"})
            return (MeetingRoute(kind="multicast", recipients=tuple(valid)) if valid else None), failed
        if not isinstance(to, str) or not to.strip():
            raise ValueError("'to' must be a nonempty string")
        to = to.strip()
        allowed = properties.get("to", {}).get("enum")
        if allowed is not None:
            if to not in allowed:
                raise ValueError(f"'to' must be one of {allowed}")
            if to == "leader":
                to = (await self.backend.resolve_leader_member_name() or "").strip()
                if not to:
                    raise ValueError("Cannot resolve the Team leader")
        if to == "*":
            return MeetingRoute(kind="broadcast"), []
        if to != "user" and not await self.backend.member_exists(to):
            raise ValueError(f"Member '{to}' not found")
        return MeetingRoute(kind="direct", recipients=(to,)), []

    def render_for_llm(self, output: ToolOutput) -> str:
        data = output.data
        if not isinstance(data, dict) or not data.get("request_id"):
            return output.error or "Meeting message was not queued"
        text = f"Message queued for meeting speech: {data['request_id']}"
        if output.error:
            text += f"; {output.error}: {data.get('failed', [])}"
        return text


class MeetingSpeakTool(TeamTool):
    """Leader audio entry point with no caller-controlled message fields."""

    def __init__(self, send_tool: TeamTool, *, language: str) -> None:
        super().__init__(ToolCard(
            id="team.meeting_speak", name="meeting_speak",
            description=(
                "Before EVERY reply to the human, you MUST call meeting_speak "
                "with the spoken summary in speech_text as the FIRST action of your turn. "
                "Only then produce ordinary assistant output. "
                "Correct: meeting_speak → text reply. Never: text reply → meeting_speak."
            ),
            input_params={
                "type": "object",
                "properties": {"speech_text": {
                    "type": "string", "minLength": 1, "description": leader_speech_guidance(language),
                }},
                "required": ["speech_text"], "additionalProperties": False,
            },
        ))
        self._send_tool = send_tool

    async def invoke(self, inputs: dict, **kwargs) -> ToolOutput:
        if (
            not isinstance(inputs, dict) or set(inputs) != {"speech_text"}
            or not isinstance(inputs["speech_text"], str) or not inputs["speech_text"].strip()
        ):
            return ToolOutput(success=False, error="Provide only nonempty speech_text")
        return await self._send_tool.invoke({
            "to": "user", "content": "", "summary": "", "speech_text": inputs["speech_text"],
        }, **kwargs)

    def render_for_llm(self, output: ToolOutput) -> str:
        return self._send_tool.render_for_llm(output)


class GroupMeetingSendMessageTool(TeamTool):
    """Queue a public group message and its speech without immediate delivery."""

    def __init__(
        self, backend: TeamBackend, coordinator: MeetingCoordinator, source: ToolCard, *, language: str,
    ) -> None:
        card = deepcopy(source)
        card.id = "team.group_meeting_send_message"
        card.name = "group_meeting_send_message"
        card.description = (
            "Queue a public group conversation message and its speech. Provide content, client_message_id, "
            "nonempty speech_text, and optional mentions. Reuse client_message_id only to retry the same message. "
            "The host publishes content to group history with its mentions when granted, then plays speech_text. "
            "A queue receipt is not delivery or playback completion. Do not also send the same message "
            "through meeting_send_message or group_send_message."
        )
        card.input_params.setdefault("properties", {})["speech_text"] = {
            "type": "string", "minLength": 1, "description": teammate_speech_guidance(language),
        }
        required = card.input_params.setdefault("required", [])
        if "speech_text" not in required:
            required.append("speech_text")
        card.input_params["additionalProperties"] = False
        super().__init__(card)
        self.backend = backend
        self.coordinator = coordinator

    async def invoke(self, inputs: dict, **kwargs) -> ToolOutput:
        try:
            if self.backend.conversation_mode != "group_chat":
                raise ValueError("group_meeting_send_message requires a group_chat runtime")
            schema = self.card.input_params
            if not isinstance(inputs, dict) or set(inputs) - set(schema.get("properties", {})):
                raise ValueError("Only declared group-message arguments are supported")
            if set(schema.get("required", ())) - set(inputs):
                raise ValueError("Required group-message arguments are missing")
            speech = inputs["speech_text"]
            if not isinstance(speech, str) or not speech.strip():
                raise ValueError("'speech_text' must be a nonempty string")
            conversation = await self.backend.group_conversation()
            if conversation is None:
                raise ValueError("Group publication requires the Team conversation")
            content, request_id = inputs["content"], inputs["client_message_id"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("A conversation message needs text")
            if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 255:
                raise ValueError("client_message_id must be a nonempty string of at most 255 characters")
            mentions = inputs.get("mentions", ())
            if not isinstance(mentions, (list, tuple)) or len(mentions) > 100:
                raise ValueError("mentions must be a list of at most 100 member names")
            if any(not isinstance(name, str) or not name.strip() or len(name) > 255 for name in mentions):
                raise ValueError("mentions must contain nonempty member names")
            request = await self.coordinator.enqueue_message(
                self.backend.member_name, content, speech_text=speech.strip(),
                route=MeetingRoute(kind="group_chat"), mentions=tuple(dict.fromkeys(mentions)),
                request_id=request_id,
            )
            return ToolOutput(success=True, data={"type": "queued", "request_id": request.request_id})
        except Exception as exc:

            message = mask_sensitive(str(exc).strip() or type(exc).__name__)
            logger.exception("Meeting group message admission failed: session_id=%s member=%s error=%s",
                             self.coordinator.scope.session_id, self.backend.member_name, message)
            return ToolOutput(success=False, error=message)

    def render_for_llm(self, output: ToolOutput) -> str:
        if isinstance(output.data, dict) and output.data.get("request_id"):
            return f"Group message queued for meeting speech: {output.data['request_id']}"
        return output.error or "Meeting group message was not queued"


def create_meeting_message_tool(
    backend: TeamBackend, coordinator: MeetingCoordinator, source: ToolCard, *, language: str,
) -> TeamTool:
    """Bind the meeting counterpart of an existing member message tool."""
    if source.name == "group_send_message":
        return GroupMeetingSendMessageTool(backend, coordinator, source, language=language)
    if source.name == "send_message":
        tool = MeetingSendMessageTool(backend, coordinator, source, language=language)
        return MeetingSpeakTool(tool, language=language) if backend.is_leader else tool
    raise ValueError(f"Unsupported meeting message tool: {source.name}")
