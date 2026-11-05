from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MemoryHookContext:
    session_id: str
    request_id: str
    channel_id: str | None
    agent_name: str
    workspace_dir: str
    assistant_message: str | None = None
    # 输入扩展
    extra: dict[str, Any] = field(default_factory=dict)
    # 记忆内容（before_chat 扩展写入，宿主从本字段读取拼接结果）
    memory_blocks: list[str] = field(default_factory=list)
    # 输出扩展
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GatewayChatHookContext:
    request_id: str
    channel_id: str
    session_id: str | None
    req_method: str | None
    # 扩展可直接原地修改 params，Gateway 会将其继续传给 AgentRequest.params
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentServerChatHookContext:
    request_id: str
    channel_id: str
    session_id: str | None
    req_method: str | None
    # 扩展可直接原地修改 params，AgentServer 后续逻辑会继续使用 request.params
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SystemPromptHookContext:
    # 扩展可设置此目录，用于覆盖默认的 home_dir
    home_dir: str | None = None
    # 扩展可设置此目录，用于扩展默认的 skill_dir
    skill_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
