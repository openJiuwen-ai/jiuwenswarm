# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clients attached to a conversation, each running tools for it on the resource it edits.

AgentServer answers the client attach requests (``attach``: ``client.attach``
and the ``client.renew`` that keeps an attachment's lease), knows which
resource is attached to which conversation while its lease lasts
(``directory``), knows the apps a client may attach with and the app package each
app's conversations load as their template (``registry``), and sends a client what its
conversation's pause asks. A periodic sweep (``watch``) drops lapsed attachments and answers
``client_gone`` the client tool calls of a conversation that no live attachment
showed for a grace period, reading which conversations wait from the runtime;
presence is derived, so nothing reports closed sockets or Gateway link changes.
"""

from importlib import import_module

_EXPORTS = {
    "CLIENT_ATTACH_REQ_METHODS": "attach",
    "handle_client_attach_request": "attach",
    "load_attached_resource": "attach",
    "load_client_capabilities": "attach",
    "get_attachment_directory": "directory",
    "close_client_attachments": "watch",
    "set_conversation_access": "watch",
    "start_client_attachment_sweep": "watch",
    "sweep_once": "watch",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> object:
    """Load a public export's module on first use: importing ``registry`` must not load the runtime."""
    try:
        module = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
