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
    - ``user_id``: 权威用户身份，后续会话路由 / 注册中心 / 实例创建应使用此值。
      日志里会被 ``SensitiveDataFilter`` 脱敏。
    - ``user_name``: 可读用户名，写入 AgentOS 日志，不受 ``user_id`` 掩码规则影响
    - ``extensions``: 可选扩展（如 username、role、auth_method）
    - ``error``: 失败原因
    """
    success: bool
    user_id: str = ""
    error: str = ""
    user_name: str = ""
    extensions: dict = field(default_factory=dict)


def resolve_user_name(result: AuthResult | None) -> str:
    """Readable display name for logs. ``user_id`` is masked by the sanitizer."""
    if result is None:
        return ""
    name = str(getattr(result, "user_name", "") or "").strip()
    if name:
        return name
    ext = result.extensions if isinstance(result.extensions, dict) else {}
    return str(ext.get("username") or ext.get("user_name") or "").strip()


# user_id 会被 SensitiveDataFilter 掩码；记下可读用户名，供 log_agentos 补打 user_name。
_USER_NAMES: dict[str, str] = {}


def remember_user_name(user_id: str, user_name: str) -> None:
    """Remember a display name so later AgentOS lines can print ``user_name``."""
    uid = str(user_id or "").strip()
    name = str(user_name or "").strip()
    if uid and name:
        _USER_NAMES[uid] = name


def lookup_user_name(user_id: str) -> str:
    return _USER_NAMES.get(str(user_id or "").strip(), "")


def clear_user_names() -> None:
    """Drop the in-process name cache. Tests use this to avoid cross-case leakage."""
    _USER_NAMES.clear()


# 抽象接口：统一认证和凭证管理
class CredentialAuthenticator(ABC):
    @abstractmethod
    async def authenticate(self, context: AuthContext) -> AuthResult:
        """认证用户身份，返回 AuthResult（含可贯通的 user_id）。"""
        pass