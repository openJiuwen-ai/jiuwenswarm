"""Heartbeat capability for public channels and their internal delegates."""

from __future__ import annotations

# 心跳任务要求渠道能把「非请求触发」的结果主动送回用户。小艺渠道目前没有可用的
# 主动投递路径（云端只认请求内的 taskId，心跳帧会被静默丢弃），所以小艺会话
# 不下发 heartbeat_* 工具：模型看不到该能力，也不会承诺"心跳已创建"。
HEARTBEAT_UNSUPPORTED_CHANNELS: frozenset[str] = frozenset({"xiaoyi"})


def is_heartbeat_supported(channel_id: str | None) -> bool:
    """渠道是否支持心跳任务；渠道未知时保持原有行为（支持）。"""
    channel = str(channel_id or "").strip().lower()
    if not channel:
        return True
    return channel.split(":", 1)[0] not in HEARTBEAT_UNSUPPORTED_CHANNELS
