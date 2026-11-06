# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The client attach requests AgentServer answers, and what a conversation's client can run.

A client such as the PowerPoint task pane shows one conversation next to the
resource it edits (an open document) and runs that conversation's resource
tools itself. It attaches with ``client.attach``: the conversation, the resource,
and what its host can run. The app that knows the resource (``registry``) is
the conversation's ``client_app``, named once at ``session.create``.
AgentServer keeps the capabilities and the resource with the conversation
(``client_capabilities.json``: the conversation's resource outlives any lease),
and lists the attachment (``directory``) for as long as the client
renews it (``client.renew``, every ``renew_every_seconds`` of the attach
response). A renew refused with ``NOT_ATTACHED`` (the lease lapsed, the
Worker restarted, the socket reconnected) makes the client attach again; one
refused with ``DISPLACED`` (another client attached the same resource, as when
a copied file is open too) makes it stop renewing and leave the resource to the
other client until its user chooses otherwise.

``resource.key`` is a stable identity the client keeps with the resource itself
(for a document, an id written into the file), so it survives save-as, renames
and moves; it must not be a file path or URL. A client finds the conversations
its resource was attached to with ``client.conversations``, the most recently
active first, so the resource itself keeps nothing but its key.

One resource is attached to one conversation. An attach is refused with
``CONFLICT`` when the conversation was not created by a client app (it has no
registered ``client_app``) or is attached to another resource right now; the
client then starts a new conversation.

A user's attached clients can find each other (``client.list``) and read each
other's latest messages (``client.conversation.messages``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from agent_gateway.common.protocol_ids import validate_session_id

from jiuwenswarm.server.client_attachments.errors import ClientAttachError, ClientAttachErrorCode
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.client_attachments.directory import (
    LEASE_SECONDS,
    RENEW_EVERY_SECONDS,
    Attachment,
    attachment_id_for,
    get_attachment_directory,
)
from jiuwenswarm.server.client_attachments.registry import ClientApp, conversation_app, registered_app
from jiuwenswarm.server.client_attachments.watch import Push, announce_change, pending_interactions
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)

CLIENT_ATTACH_REQ_METHODS = frozenset({
    ReqMethod.CLIENT_ATTACH,
    ReqMethod.CLIENT_RENEW,
    ReqMethod.CLIENT_LIST,
    ReqMethod.CLIENT_CONVERSATION_MESSAGES,
    ReqMethod.CLIENT_CONVERSATIONS,
})

_CAPABILITIES_FILE = "client_capabilities.json"
_MAX_CAPABILITIES_BYTES = 64 * 1024
_RESOURCE_FIELDS = ("key", "display_name", "kind", "host_vendor")
_LEASE = {"lease_seconds": LEASE_SECONDS, "renew_every_seconds": RENEW_EVERY_SECONDS}


async def handle_client_attach_request(
    ws: Any, request: AgentRequest, send_lock: asyncio.Lock, push: Push,
) -> None:
    """Answer one of ``CLIENT_ATTACH_REQ_METHODS``; ``push`` sends a server push message."""
    try:
        payload, ok = await _answer(request, push), True
    except ClientAttachError as exc:
        payload, ok = {"error": str(exc), "code": exc.code.value}, False
    response = AgentResponse(
        request_id=request.request_id, channel_id=request.channel_id, ok=ok, payload=payload,
    )
    wire = encode_agent_response_for_wire(response, response_id=request.request_id)
    async with send_lock:
        await send_wire_payload(ws, wire)


async def _answer(request: AgentRequest, push: Push) -> dict[str, Any]:
    params = request.params if isinstance(request.params, dict) else {}
    method = request.req_method
    if method == ReqMethod.CLIENT_ATTACH:
        return await _attach(request, params, push)
    if method == ReqMethod.CLIENT_RENEW:
        return _renew(request, params)
    if method == ReqMethod.CLIENT_LIST:
        session_id, _owner = await asyncio.to_thread(
            _owned_session, request, params.get("session_id") or request.session_id)
        return {"attachments": [_attachment_view(c, current_for=session_id)
                              for c in get_attachment_directory().attachments_for(session_id)]}
    if method == ReqMethod.CLIENT_CONVERSATION_MESSAGES:
        target, _owner = await asyncio.to_thread(_owned_session, request, params.get("target_session_id"))
        return {"messages": await asyncio.to_thread(_latest_messages, target)}
    if method == ReqMethod.CLIENT_CONVERSATIONS:
        return {"conversations": await asyncio.to_thread(_conversations, request, params)}
    raise ClientAttachError(ClientAttachErrorCode.BAD_REQUEST, f"not a client attach method: {method}")


def _owned_session(request: AgentRequest, session_id: Any) -> tuple[str, str]:
    """``(session_id, owner)`` when the requester owns that existing conversation."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    try:
        session_id = validate_session_id(str(session_id or "").strip())
    except ValueError as exc:
        raise ClientAttachError(ClientAttachErrorCode.BAD_REQUEST, str(exc)) from exc
    session_path = Path(get_agent_sessions_dir()) / session_id
    owner = str(request.user_id or "").strip()
    metadata = get_session_metadata(session_id, enable_writeback=False) or {}
    if (session_path.is_symlink() or not session_path.is_dir()
            or str(metadata.get("user_id") or "").strip() != owner):
        raise ClientAttachError(ClientAttachErrorCode.NOT_FOUND_OR_FORBIDDEN, "session not found")
    return session_id, owner


async def _attach(request: AgentRequest, params: dict[str, Any], push: Push) -> dict[str, Any]:
    """This socket's client attaches its resource to ``session_id``, through the conversation's app."""
    session_id, owner = await asyncio.to_thread(_owned_session, request, params.get("session_id"))
    resource, capabilities = params.get("resource"), params.get("capabilities")
    bad_request = ClientAttachErrorCode.BAD_REQUEST
    if not isinstance(resource, dict) or not all(
            isinstance(resource.get(k), str) and resource[k].strip() for k in _RESOURCE_FIELDS):
        raise ClientAttachError(bad_request, f"client.attach needs a resource with {', '.join(_RESOURCE_FIELDS)}")
    if not isinstance(capabilities, dict) or len(json.dumps(capabilities)) > _MAX_CAPABILITIES_BYTES:
        raise ClientAttachError(bad_request, "client.attach needs a capabilities object")
    ws_id = str((request.metadata or {}).get("ws_id") or "")
    if not request.channel_id or not ws_id:
        raise ClientAttachError(bad_request, "client.attach needs a client connection")
    app = await asyncio.to_thread(_conversation_app, session_id)
    check_declared_tools(app, capabilities)
    directory = get_attachment_directory()
    resource = {k: " ".join(resource[k].split()) for k in _RESOURCE_FIELDS}
    attach = {"channel_id": request.channel_id, "ws_id": ws_id, "owner_id": owner, "client_app": app.id}
    displaced = directory.displaced_by(channel_id=request.channel_id, ws_id=ws_id,
                                       attachment_id=attachment_id_for(owner, app.id, resource["key"]))
    connection = directory.attach(**attach, resource=resource, session_id=session_id)
    try:
        await asyncio.to_thread(save_client_capabilities, session_id, capabilities,
                                attachment_id=connection.attachment_id, resource=resource)
        pending = await pending_interactions(session_id)
        await asyncio.to_thread(_route_pushes_to_socket, session_id, request)
    except Exception as exc:
        # Refused: the socket keeps the attachment it had.
        directory.withdraw(connection, restore=displaced)
        raise ClientAttachError(
            ClientAttachErrorCode.UNAVAILABLE, f"the client could not attach: {exc}") from exc
    await announce_change(push, owner)
    return {"attachment_id": connection.attachment_id, "session_id": session_id, "client_app": app.id,
            "pending": pending, **_LEASE}


def _conversation_app(session_id: str) -> ClientApp:
    """The app the conversation was created for (``session.create``'s ``client_app``)."""
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    app = conversation_app(get_session_metadata(session_id, enable_writeback=False))
    if app is None:
        raise ClientAttachError(ClientAttachErrorCode.CONFLICT, "this conversation was not created by a client app")
    return app


def check_declared_tools(app: ClientApp, capabilities: dict[str, Any]) -> None:
    """A client declares only tools its client app defines."""
    declared = capabilities.get("supported_tools")
    if not isinstance(declared, list) or not all(isinstance(name, str) for name in declared):
        raise ClientAttachError(
            ClientAttachErrorCode.INVALID_ARGUMENT, "supported_tools must be a list of tool names")
    unknown = sorted(set(declared) - app.tool_names)
    if unknown:
        raise ClientAttachError(
            ClientAttachErrorCode.INVALID_ARGUMENT,
            f"supported_tools names tools the {app.id} app does not have: {', '.join(unknown)}",
        )


def _renew(request: AgentRequest, params: dict[str, Any]) -> dict[str, Any]:
    """Extend the lease of this socket's attachment; cheap, as every attached client sends it often."""
    get_attachment_directory().renew(
        channel_id=request.channel_id or "", ws_id=str((request.metadata or {}).get("ws_id") or ""),
        attachment_id=str(params.get("attachment_id") or ""), session_id=str(params.get("session_id") or ""),
    )
    return dict(_LEASE)


def _conversations(request: AgentRequest, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The requester's conversations of the app ``client_app`` last attached to the resource ``resource_key``."""
    from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata

    client_app, key = params.get("client_app"), params.get("resource_key")
    if not isinstance(client_app, str) or not isinstance(key, str) or not key.strip():
        raise ClientAttachError(ClientAttachErrorCode.BAD_REQUEST,
                                "client.conversations needs a client_app and a resource_key")
    registered_app(client_app)
    key = " ".join(key.split())  # as attach saved it
    owner = str(request.user_id or "").strip()
    conversations = []
    for metadata in collect_all_sessions_metadata(enable_writeback=False):
        if str(metadata.get("user_id") or "").strip() != owner or metadata.get("client_app") != client_app:
            continue
        session_id = metadata["session_id"]
        resource = _saved_capabilities(session_id).get("resource")
        if not isinstance(resource, dict) or resource.get("key") != key:
            continue
        conversations.append({"session_id": session_id, "title": metadata.get("title") or "",
                              "last_message_at": float(metadata.get("last_message_at") or 0)})
    return sorted(conversations, key=lambda c: (-c["last_message_at"], c["session_id"]))


def _route_pushes_to_socket(session_id: str, request: AgentRequest) -> None:
    """The conversation's server pushes go to the attaching socket, as after a message sent from it.

    A turn no socket started (a cross-Session message, a Heartbeat) thereby
    reaches the attached client, also before the user ever wrote in it.
    """
    from jiuwenswarm.server.runtime.session.session_metadata import set_session_delivery_context

    set_session_delivery_context(
        session_id=session_id, channel_id=request.channel_id, source_request_id=request.request_id,
        route_metadata=request.metadata, record_activity=False,
    )


def _attachment_view(connection: Attachment, *, current_for: str) -> dict[str, Any]:
    return {
        "attachment_id": connection.attachment_id,
        "display_name": connection.display_name,
        "kind": connection.kind,
        "host_vendor": connection.host_vendor,
        # Lets the client name the attachment a cross-conversation message came from or went to.
        "session_id": connection.session_id,
        "is_current": connection.session_id == current_for,
    }


def _latest_messages(session_id: str) -> list[dict[str, Any]]:
    """The latest page of a conversation, as ``history.get`` restores it, without reasoning text."""
    from jiuwenswarm.server.control.history_service import load_history_page
    from jiuwenswarm.server.wire_truncate import _sanitize_history_record_for_wire

    page = load_history_page(session_id, 1) or {"messages": []}
    messages = []
    for row in reversed(page.get("messages") or []):
        public = {k: v for k, v in row.items() if not k.startswith("reasoning")}
        if row.get("event_type") == "chat.reasoning" or row.get("reasoning_content"):
            public["has_reasoning"] = True
            if row.get("event_type") == "chat.reasoning":
                public["content"] = ""
        messages.append(_sanitize_history_record_for_wire(public))
    return messages


# --- What a conversation's client can run, and the resource it belongs to -------------


def _capabilities_path(session_id: str) -> Path:
    return Path(get_agent_sessions_dir()) / validate_session_id(session_id) / _CAPABILITIES_FILE


def save_client_capabilities(
    session_id: str, capabilities: dict[str, Any], *, attachment_id: str, resource: dict[str, str],
) -> None:
    saved = {"capabilities": capabilities, "attachment_id": attachment_id, "resource": resource}
    path = _capabilities_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(saved, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _saved_capabilities(session_id: str) -> dict[str, Any]:
    try:
        raw = json.loads(_capabilities_path(session_id).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_client_capabilities(session_id: str) -> dict[str, Any] | None:
    """What the conversation's client declared at its last attach."""
    capabilities = _saved_capabilities(session_id).get("capabilities")
    return capabilities if isinstance(capabilities, dict) else None


def load_attached_resource(session_id: str) -> dict[str, Any] | None:
    """``{"attachment_id", "resource"}`` of the resource last attached to this conversation, if any."""
    raw = _saved_capabilities(session_id)
    attachment_id, resource = raw.get("attachment_id"), raw.get("resource")
    if not isinstance(attachment_id, str) or not isinstance(resource, dict):
        return None
    return {"attachment_id": attachment_id, "resource": resource}
