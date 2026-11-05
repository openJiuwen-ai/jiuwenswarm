# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The client attach requests AgentServer answers, and what a conversation's client can run.

A client such as the PowerPoint task pane shows one conversation next to the
resource it edits (an open document) and runs that conversation's resource
tools itself. It attaches with ``client.attach``: the conversation, the app plugin that knows
the resource (``registry``), the resource, and what its host can run.
AgentServer then installs the plugin and equips the conversation with it
(``plugins``), keeps the capabilities and the resource with the conversation
(``client_capabilities.json``: the conversation's resource outlives any lease),
and lists the attachment (``directory``) for as long as the client
renews it (``client.renew``, every ``renew_every_seconds`` of the attach
response). A renew refused with ``NOT_ATTACHED`` (the lease lapsed, the
Worker restarted, the socket reconnected) makes the client attach again.

``resource.key`` is a stable identity the client keeps with the resource itself
(for a document, an id written into the file), so it survives save-as, renames
and moves; it must not be a file path or URL.

One resource is attached to one conversation. An attach is refused with
``CONFLICT`` when the conversation already belongs to another app or is attached
to another resource right now; the client then starts a new conversation.

A user's attached clients can find each other (``client.list``) and read each
other's latest messages (``client.conversation.messages``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
from jiuwenswarm.server.client_attachments.plugins import (
    check_client_plugin,
    check_declared_tools,
    ensure_plugin,
    equip,
)
from jiuwenswarm.server.client_attachments.registry import app_plugin
from jiuwenswarm.server.client_attachments.watch import Push, announce_change, pending_interactions
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)

CLIENT_ATTACH_REQ_METHODS = frozenset({
    ReqMethod.CLIENT_ATTACH,
    ReqMethod.CLIENT_RENEW,
    ReqMethod.CLIENT_LIST,
    ReqMethod.CLIENT_CONVERSATION_MESSAGES,
})

_CAPABILITIES_FILE = "client_capabilities.json"
_MAX_CAPABILITIES_BYTES = 64 * 1024
_PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
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
    """This socket's client attaches its resource to ``session_id``, through ``plugin_id``."""
    session_id, owner = await asyncio.to_thread(_owned_session, request, params.get("session_id"))
    plugin_id, resource, capabilities = params.get("plugin_id"), params.get("resource"), params.get("capabilities")
    bad_request = ClientAttachErrorCode.BAD_REQUEST
    if not isinstance(plugin_id, str) or not _PLUGIN_ID.fullmatch(plugin_id):
        raise ClientAttachError(bad_request, "client.attach needs a plugin_id")
    if not isinstance(resource, dict) or not all(
            isinstance(resource.get(k), str) and resource[k].strip() for k in _RESOURCE_FIELDS):
        raise ClientAttachError(bad_request, f"client.attach needs a resource with {', '.join(_RESOURCE_FIELDS)}")
    if not isinstance(capabilities, dict) or len(json.dumps(capabilities)) > _MAX_CAPABILITIES_BYTES:
        raise ClientAttachError(bad_request, "client.attach needs a capabilities object")
    ws_id = str((request.metadata or {}).get("ws_id") or "")
    if not request.channel_id or not ws_id:
        raise ClientAttachError(bad_request, "client.attach needs a client connection")
    plugin = await asyncio.to_thread(app_plugin, plugin_id)
    check_declared_tools(plugin, capabilities)
    directory = get_attachment_directory()
    resource = {k: " ".join(resource[k].split()) for k in _RESOURCE_FIELDS}
    attach = {"channel_id": request.channel_id, "ws_id": ws_id, "owner_id": owner, "plugin_id": plugin_id}
    directory.check_attach(**attach, resource_key=resource["key"], session_id=session_id)
    attached_plugin_id = await asyncio.to_thread(_attached_plugin_id, session_id)
    await asyncio.to_thread(check_client_plugin, session_id, plugin_id, attached_plugin_id)
    await ensure_plugin(plugin)
    await asyncio.to_thread(equip, session_id, plugin_id, attached_plugin_id)
    # Checked again: the requests of other sockets (another attach) ran
    # while this one awaited. Nothing awaits from here to attach.
    displaced = directory.displaced_by(channel_id=request.channel_id, ws_id=ws_id,
                                       attachment_id=attachment_id_for(owner, plugin_id, resource["key"]))
    connection = directory.attach(**attach, resource=resource, session_id=session_id)
    try:
        await asyncio.to_thread(save_client_capabilities, session_id, plugin_id, capabilities,
                                attachment_id=connection.attachment_id, resource=resource)
        pending = await pending_interactions(session_id)
        await asyncio.to_thread(_route_pushes_to_socket, session_id, request)
    except Exception as exc:
        # Refused: the socket keeps the attachment it had.
        directory.withdraw(connection, restore=displaced)
        raise ClientAttachError(
            ClientAttachErrorCode.UNAVAILABLE, f"the client could not attach: {exc}") from exc
    await announce_change(push, owner)
    return {"attachment_id": connection.attachment_id, "session_id": session_id, "plugin_id": plugin_id,
            "pending": pending, **_LEASE}


def _renew(request: AgentRequest, params: dict[str, Any]) -> dict[str, Any]:
    """Extend the lease of this socket's attachment; cheap, as every attached client sends it often."""
    renewed = get_attachment_directory().renew(
        channel_id=request.channel_id or "", ws_id=str((request.metadata or {}).get("ws_id") or ""),
        attachment_id=str(params.get("attachment_id") or ""), session_id=str(params.get("session_id") or ""),
    )
    if renewed is None:
        raise ClientAttachError(ClientAttachErrorCode.NOT_ATTACHED, "no such attachment: attach again")
    return dict(_LEASE)


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
    session_id: str, plugin_id: str, capabilities: dict[str, Any], *, attachment_id: str, resource: dict[str, str],
) -> None:
    saved = {"plugin_id": plugin_id, "capabilities": capabilities, "attachment_id": attachment_id,
             "resource": resource}
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


def _attached_plugin_id(session_id: str) -> str | None:
    """The app plugin whose client attached the conversation last, if any."""
    plugin_id = _saved_capabilities(session_id).get("plugin_id")
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None


def load_client_capabilities(session_id: str, plugin_id: str) -> dict[str, Any] | None:
    """What the client of ``plugin_id`` declared for this conversation at its last attach."""
    raw = _saved_capabilities(session_id)
    if raw.get("plugin_id") != plugin_id:
        return None
    capabilities = raw.get("capabilities")
    return capabilities if isinstance(capabilities, dict) else None


def load_attached_resource(session_id: str) -> dict[str, Any] | None:
    """``{"attachment_id", "resource"}`` of the resource last attached to this conversation, if any."""
    raw = _saved_capabilities(session_id)
    attachment_id, resource = raw.get("attachment_id"), raw.get("resource")
    if not isinstance(attachment_id, str) or not isinstance(resource, dict):
        return None
    return {"attachment_id": attachment_id, "resource": resource}
