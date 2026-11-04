# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Why the Gateway cancels a Session's work, beyond the sources the E2A protocol defines.

The value travels in the ``E2A_INTERNAL_CANCEL_SOURCE_KEY`` channel context of a
``chat.interrupt``, next to the protocol's ``E2A_CANCEL_SOURCE_CLIENT_DISCONNECT``.
"""

from __future__ import annotations

# A new chat message replaced the turn still streaming. The user is still in the
# conversation, so work that outlives a turn (detached client tool calls) keeps running.
CANCEL_SOURCE_NEW_MESSAGE = "new_message"

__all__ = ["CANCEL_SOURCE_NEW_MESSAGE"]
