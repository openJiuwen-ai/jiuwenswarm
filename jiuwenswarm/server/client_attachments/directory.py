# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Which resource is attached to which conversation, held by a lease its client renews.

An attachment lasts ``LEASE_SECONDS`` from its attach or its last renew
(``client.renew``, sent every ``RENEW_EVERY_SECONDS``); the sweep (``watch``)
drops the ones that lapsed. The directory lives in AgentServer memory and is
the only record of who is attached: nothing reports a closed socket, a Gateway
link change or a Worker restart. A client whose renew finds no attachment
attaches again, unless another socket's attach took it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace

from jiuwenswarm.server.client_attachments.errors import ClientAttachError, ClientAttachErrorCode

#: How long an attachment lasts without a renew.
LEASE_SECONDS = 120.0
#: How often a client renews: well inside the lease, so a late or lost renew does not end it.
RENEW_EVERY_SECONDS = 30.0


def clock() -> float:
    return time.monotonic()


def owner_scope(owner_id: str) -> str:
    return (owner_id or "").strip() or "local"


def attachment_id_for(owner_id: str, client_app: str, key: str) -> str:
    identity = json.dumps([owner_scope(owner_id), client_app, key], ensure_ascii=False, separators=(",", ":"))
    return "att_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Attachment:
    """One resource attached by one client WebSocket, until ``expires_at`` (on ``clock``).

    ``client_app`` is the app that knows the resource (``office-powerpoint``);
    ``kind`` is what the resource is (``presentation``).
    """

    channel_id: str
    ws_id: str
    owner_id: str
    client_app: str
    attachment_id: str
    display_name: str
    kind: str
    host_vendor: str
    session_id: str
    expires_at: float

    @property
    def key(self) -> tuple[str, str]:
        return self.channel_id, self.ws_id


class AttachmentDirectory:
    def __init__(self) -> None:
        self._connections: dict[tuple[str, str], Attachment] = {}
        # The sockets another socket's attach took their attachment from, and that attachment's id.
        self._displaced: dict[tuple[str, str], str] = {}

    def connections(self) -> tuple[Attachment, ...]:
        return tuple(self._connections.values())

    def connection_for_session(self, session_id: str) -> Attachment | None:
        return next((c for c in self._connections.values() if c.session_id == session_id), None)

    def attachments_for(self, session_id: str) -> list[Attachment]:
        """Attachments of the user whose client is attached to ``session_id``, by name."""
        caller = self.connection_for_session(session_id)
        if caller is None:
            return []
        owner = owner_scope(caller.owner_id)
        return sorted((c for c in self._connections.values() if owner_scope(c.owner_id) == owner),
                      key=lambda c: c.display_name.casefold())

    def attach(self, *, channel_id: str, ws_id: str, owner_id: str, client_app: str,
               resource: dict[str, str], session_id: str) -> Attachment:
        """Record that this socket's client attaches its resource to ``session_id``, with a fresh lease.

        The same resource attached from a new socket (a reloaded task pane), or
        another resource attached from this socket, replaces the earlier
        attachment. A conversation attached to another resource is refused.
        """
        attachment_id = attachment_id_for(owner_id, client_app, resource["key"])
        holder = self.connection_for_session(session_id)
        if holder is not None and holder.key != (channel_id, ws_id) and holder.attachment_id != attachment_id:
            raise ClientAttachError(ClientAttachErrorCode.CONFLICT, "this conversation is attached to another resource")
        connection = Attachment(
            channel_id=channel_id, ws_id=ws_id, owner_id=owner_id, client_app=client_app,
            attachment_id=attachment_id,
            display_name=resource["display_name"], kind=resource["kind"],
            host_vendor=resource["host_vendor"], session_id=session_id,
            expires_at=clock() + LEASE_SECONDS,
        )
        for other in self.displaced_by(channel_id=channel_id, ws_id=ws_id, attachment_id=connection.attachment_id):
            del self._connections[other.key]
            self._displaced[other.key] = other.attachment_id
        self._displaced.pop(connection.key, None)
        self._connections[connection.key] = connection
        return connection

    def renew(self, *, channel_id: str, ws_id: str, attachment_id: str, session_id: str) -> Attachment:
        """Extend the lease of this socket's attachment, if it is still the one its client names.

        Refused with ``DISPLACED`` when another socket's attach took this
        socket's attachment and still holds it (the same resource open twice,
        as in a copied file), otherwise with ``NOT_ATTACHED``: a reconnected
        socket, whose earlier socket's attachment may still be listed, attaches
        again.
        """
        current = self._connections.get((channel_id, ws_id))
        if current is None and self._displaced.get((channel_id, ws_id)) == attachment_id and any(
                c.attachment_id == attachment_id for c in self._connections.values()):
            raise ClientAttachError(ClientAttachErrorCode.DISPLACED, "another client holds this attachment")
        if current is None or current.attachment_id != attachment_id or current.session_id != session_id:
            raise ClientAttachError(ClientAttachErrorCode.NOT_ATTACHED, "no such attachment: attach again")
        renewed = replace(current, expires_at=clock() + LEASE_SECONDS)
        self._connections[current.key] = renewed
        return renewed

    def expire(self, now: float) -> list[Attachment]:
        """Drop the attachments whose lease lapsed by ``now``; returns them."""
        lapsed = [c for c in self._connections.values() if c.expires_at <= now]
        for connection in lapsed:
            del self._connections[connection.key]
        held = {c.attachment_id for c in self._connections.values()}
        self._displaced = {key: taken for key, taken in self._displaced.items() if taken in held}
        return lapsed

    def displaced_by(self, *, channel_id: str, ws_id: str, attachment_id: str) -> list[Attachment]:
        """The attachments ``attach`` of this socket and resource would replace."""
        return [c for c in self._connections.values()
                if c.key == (channel_id, ws_id) or c.attachment_id == attachment_id]

    def withdraw(self, connection: Attachment, restore: Iterable[Attachment] = ()) -> None:
        """Take back an attachment whose attach failed after it was recorded.

        ``restore`` are the attachments it replaced: they are listed again, as
        the client of a refused attach keeps what it had, unless another socket
        took their resource or conversation since.
        """
        if self._connections.get(connection.key) is connection:
            del self._connections[connection.key]
        for earlier in restore:
            if any(c.key == earlier.key or c.attachment_id == earlier.attachment_id
                   or c.session_id == earlier.session_id for c in self._connections.values()):
                continue
            self._displaced.pop(earlier.key, None)
            self._connections[earlier.key] = earlier

    def close(self) -> None:
        """The server stops: forget every attachment."""
        self._connections.clear()
        self._displaced.clear()


_DIRECTORY = AttachmentDirectory()


def get_attachment_directory() -> AttachmentDirectory:
    return _DIRECTORY
