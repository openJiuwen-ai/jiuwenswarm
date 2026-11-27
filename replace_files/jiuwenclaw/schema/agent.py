# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Agent 请求与响应模型."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jiuwenclaw.schema.message import ReqMethod


@dataclass
class PermissionContext:
    """权限上下文 - 统一承载权限判定所需的身份与场景信息.

    Attributes:
        principal_user_id: 权限 owner（channel config 的 my_user_id）
        triggering_user_id: 触发者（IM sender）
        channel_id: 渠道标识
        group_digital_avatar: 是否为数字分身场景
        web_user_id: 预留：第二期 web 端本人审批
    """

    principal_user_id: str = ""
    triggering_user_id: str = ""
    channel_id: str = ""
    group_digital_avatar: bool = False
    web_user_id: str = ""

    @property
    def scene(self) -> str:
        """从 channel_id + group_digital_avatar 派生，不要求外部显式赋值."""
        if self.channel_id == "web":
            return "web"
        if self.group_digital_avatar:
            return "group_digital_avatar"
        return "normal_im"

    @property
    def owner_scope_key(self) -> tuple[str, str]:
        """用于 owner_scopes 配置查找的 key: (channel_id, principal_user_id)."""
        return (self.channel_id, self.principal_user_id)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（供 Gateway→AgentServer WebSocket 传输）."""
        return {
            "principal_user_id": self.principal_user_id,
            "triggering_user_id": self.triggering_user_id,
            "channel_id": self.channel_id,
            "group_digital_avatar": self.group_digital_avatar,
            "web_user_id": self.web_user_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PermissionContext:
        """从 dict 反序列化."""
        return cls(
            principal_user_id=data.get("principal_user_id", ""),
            triggering_user_id=data.get("triggering_user_id", ""),
            channel_id=data.get("channel_id", ""),
            group_digital_avatar=data.get("group_digital_avatar", False),
            web_user_id=data.get("web_user_id", ""),
        )


@dataclass
class AgentRequest:
    """Agent 请求（Gateway → AgentServer）."""

    request_id: str
    channel_id: str = ""
    session_id: str | None = None
    chat_id: str | None = None
    service_id: str | None = None
    agent_id: str | None = None
    req_method: ReqMethod | None = None
    params: dict = field(default_factory=dict)
    is_stream: bool = False
    timestamp: float = 0.0
    metadata: dict[str, Any] | None = None
    enable_memory: bool | None = None
    permission_context: PermissionContext | None = None
    workspace_dir: str = None


@dataclass
class AgentResponse:
    """Agent 响应（AgentServer → Gateway，非流式完整响应）."""

    request_id: str
    channel_id: str
    ok: bool = True
    payload: dict | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class AgentResponseChunk:
    """Agent 响应片段（AgentServer → Gateway，流式）."""

    request_id: str
    channel_id: str
    payload: dict | None = None
    is_complete: bool = False
