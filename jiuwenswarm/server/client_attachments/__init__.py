# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clients attached to a conversation, each running tools for it on the resource it edits.

AgentServer answers the client attach requests (``attach``: ``client.attach``
and the ``client.renew`` that keeps an attachment's lease), knows which
resource is attached to which conversation while its lease lasts
(``directory``), installs the app plugin an attached conversation loads
(``registry``, ``plugins``), and sends a client what its conversation's pause
asks. A periodic sweep (``watch``) drops lapsed attachments and answers
``client_gone`` the client tool calls of a conversation that no live attachment
showed for a grace period, reading which conversations wait from the runtime;
presence is derived, so nothing reports closed sockets or Gateway link changes.
"""

from jiuwenswarm.server.client_attachments.attach import (
    CLIENT_ATTACH_REQ_METHODS,
    handle_client_attach_request,
    load_attached_resource,
    load_client_capabilities,
)
from jiuwenswarm.server.client_attachments.directory import get_attachment_directory
from jiuwenswarm.server.client_attachments.watch import (
    close_client_attachments,
    set_conversation_access,
    start_client_attachment_sweep,
    sweep_once,
)

__all__ = [
    "CLIENT_ATTACH_REQ_METHODS",
    "close_client_attachments",
    "get_attachment_directory",
    "handle_client_attach_request",
    "load_attached_resource",
    "load_client_capabilities",
    "set_conversation_access",
    "start_client_attachment_sweep",
    "sweep_once",
]
