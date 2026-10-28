"""Scheduled task capability for public channels."""

from __future__ import annotations

SCHEDULED_TASK_UNSUPPORTED_CHANNELS: frozenset[str] = frozenset({"xiaoyi"})


def is_scheduled_task_supported(channel_id: str | None) -> bool:
    """Return whether a channel supports creating and delivering scheduled tasks."""
    channel = str(channel_id or "").strip().lower()
    if not channel:
        return True
    return channel.split(":", 1)[0] not in SCHEDULED_TASK_UNSUPPORTED_CHANNELS
