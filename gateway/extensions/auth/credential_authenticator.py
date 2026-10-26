from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# 认证上下文
@dataclass
class AuthContext:
    channel_type: str = ""  # web / tui / ssh
    credentials: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    remote_addr: str = ""

    def __repr__(self) -> str:
        return (
            f"AuthContext(channel_type={self.channel_type!r}, "
            f"credentials=<redacted>, "
            f"headers=<redacted>, "
            f"remote_addr={self.remote_addr!r})"
        )


# 认证结果
@dataclass
class AuthResult:
    """鉴权结果。
    成功时字段约定（身份贯通预留，由调用方写入连接上下文）：
    - ``user_id``: 权威用户身份，后续会话路由 / 注册中心 / 实例创建应使用此值
    - ``extensions``: 可选扩展（如 username、role、auth_method）
    - ``error``: 失败原因
    """
    success: bool
    user_id: str = ""
    error: str = ""
    extensions: dict = field(default_factory=dict)


# 抽象接口：统一认证和凭证管理
class CredentialAuthenticator(ABC):
    @abstractmethod
    async def authenticate(self, context: AuthContext) -> AuthResult:
        """认证用户身份，返回 AuthResult（含可贯通的 user_id）。"""
        pass