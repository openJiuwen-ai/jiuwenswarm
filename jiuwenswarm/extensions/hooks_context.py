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
class AgentWsServerStartHookContext:
    """AgentWebSocketServer.start 入口、create_instance 之前"""

    skills_dir: str

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


@dataclass
class ImageArtifactHookContext:
    """图像产物落盘后的扩展回调上下文（兼容旧版扩展）。

    新版产物检测对所有产物同时触发 IMAGE_ARTIFACT_POST_PROCESS 和
    ARTIFACT_POST_PROCESS，扩展在 handler 中按扩展名自行过滤。
    """

    session_id: str
    tool_name: str
    task_id: str | None = None
    artifact_paths: list[str] = field(default_factory=list)
    # 输出扩展
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactPostProcessHookContext:
    """产物落盘检测后、向前端发送 ``artifact.generated`` 之前的扩展回调上下文。

    扩展可在 handler 中按 ``artifact_paths`` 对文件做原地后处理（如水印、源码可读性转换）。
    """

    session_id: str
    tool_name: str
    task_id: str | None = None
    subagent_id: str | None = None
    artifact_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentReloadConfigHookContext:
    """Agent 配置重载事件的 hook context。

    在 AgentServer 接收到 agent.reload_config 请求时触发；
    扩展可原地修改 ``config`` / ``env``，宿主在 hook 返回后使用修改值执行 reload。
    """

    request_id: str
    channel_id: str
    config: dict[str, Any] | None = None
    env: dict[str, str] | None = None
    # 输出扩展
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
