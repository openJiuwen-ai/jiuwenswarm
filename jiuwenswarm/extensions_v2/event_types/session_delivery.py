# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared limits for text-only cross-session delivery hooks."""


def valid_delivery_text(value: object) -> bool:
    """Accept a nonempty body within the existing mailbox byte limit."""
    from jiuwenswarm.server.runtime.session.session_message_service import MAX_SESSION_MESSAGE_BYTES

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_SESSION_MESSAGE_BYTES
    except UnicodeEncodeError:
        return False


def valid_delivery_reason(value: object) -> bool:
    """Accept a bounded explicit business refusal, rather than an exception."""
    return isinstance(value, str) and 0 < len(value.strip()) <= 512
