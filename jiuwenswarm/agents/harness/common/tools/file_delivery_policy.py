"""File delivery capability for public channels and their internal delegates."""
from typing import Any


def is_send_file_enabled(config: dict[str, Any] | None, channel_id: str) -> bool:
    channels = config.get("channels", {}) if isinstance(config, dict) else {}
    allowed = channels.get(channel_id, {}).get("send_file_allowed")
    # Full-duplex delegates through a private stream, but delivers files to the Web conversation.
    policy_channel = "web" if channel_id == "video_tool" else channel_id
    if allowed is None:
        allowed = channels.get(policy_channel, {}).get("send_file_allowed")
    return policy_channel == "web" if allowed is None else bool(allowed)
