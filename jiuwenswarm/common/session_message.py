# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared constants for Host-authenticated cross-Session messages."""

SESSION_MESSAGE_INTERNAL_KEY = "_jiuwenswarm_cross_session"
SESSION_MESSAGE_ORIGIN = "cross_session_agent"
SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY = "_jiuwenswarm_session_message_owner_scope_id"
SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY = "_jiuwenswarm_session_message_anonymous_owner"

__all__ = [
    "SESSION_MESSAGE_INTERNAL_KEY",
    "SESSION_MESSAGE_ORIGIN",
    "SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY",
    "SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY",
]
