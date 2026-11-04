# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Why AgentServer refuses a client attach request (``client.attach``, ``client.list``, ...).

The refusal goes back as ``{"error": <message>, "code": <code>}``.
"""

from __future__ import annotations

from enum import StrEnum


class ClientAttachErrorCode(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND_OR_FORBIDDEN = "NOT_FOUND_OR_FORBIDDEN"
    # The conversation is attached to another resource or belongs to another application.
    CONFLICT = "CONFLICT"
    # A renew found no attachment of that socket, id and conversation: the client attaches again.
    NOT_ATTACHED = "NOT_ATTACHED"
    PLUGIN_UNAVAILABLE = "PLUGIN_UNAVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ClientAttachError(Exception):
    """A refused request; ``code`` goes to the client."""

    def __init__(self, code: ClientAttachErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["ClientAttachError", "ClientAttachErrorCode"]
