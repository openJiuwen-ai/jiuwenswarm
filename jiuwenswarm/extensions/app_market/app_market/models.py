from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

MAX_APP_VERSION_CHARS = 32
_APP_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.-]{1,16})?$")


def normalize_app_version(value: Any) -> str | None:
    """Validate one display version; ``None`` keeps any previously published one.

    An empty string means "unspecified" so a client cannot reset a version by
    omitting the value.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("version must be a string")
    version = value.strip()
    if not version:
        return None
    if len(version) > MAX_APP_VERSION_CHARS or not _APP_VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "version must start with digits, use at most three dots and an optional suffix, "
            f"and be at most {MAX_APP_VERSION_CHARS} characters"
        )
    return version


@dataclass
class AppManifest:
    """应用清单 — 应用元信息"""

    app_id: str
    name: str
    version: str
    run_type: Literal["static", "managed"]  # 静态 Web 应用或受管本地服务
    page_entry_path: str | None = None  # 页面入口相对路径
    start_script: dict[str, str] = field(default_factory=dict)  # {"windows": "start.ps1", "darwin": "start.sh"}
    stop_script: dict[str, str] = field(default_factory=dict)
    interpreter: str | None = None  # 解释器，如 "python", "node"
    args: list[str] = field(default_factory=list)
    work_dir: str = ""  # 工作目录
    default_port: int = 0  # 默认端口
    status_mode: Literal["push", "pull"] = "pull"
    permissions: list[str] = field(default_factory=list)
    source_session: str | None = None  # 来源会话 ID
    created_at: str = ""
    pinned: bool = False
    description: str = ""
    icon_url: str = ""
    owner_id: str = ""
    editor_session_id: str = ""
    editor_create_token: str = ""
    editor_create_pending: bool = False
    editor_session_ids: list[str] = field(default_factory=list)
    startup_timeout_seconds: int = 30
    # Distribution provenance. Local registrations keep the defaults so old
    # manifests load unchanged.
    source: str = "local"  # "local" | "hub"
    hub_asset_id: str = ""
    hub_url: str = ""
    artifact_sha256: str = ""
    requires_project: bool = False
    project_id: str = ""
    project_dir: str = ""
    project_work_mode: str = ""
    local_release_id: str = ""  # Directory ownership retained after Hub conversion.

    def __post_init__(self) -> None:
        timeout = self.startup_timeout_seconds
        self.startup_timeout_seconds = min(timeout, 300) if type(timeout) is int and timeout > 0 else 30

    def iter_editor_session_ids(self) -> Iterator[str]:
        """Include the singular binding from older app manifests."""
        yield from self.editor_session_ids
        if self.editor_session_id and self.editor_session_id not in self.editor_session_ids:
            yield self.editor_session_id


@dataclass
class AppInstance:
    """应用实例 — 每次启动生成"""

    instance_id: str
    app_id: str
    port: int = 0
    pid: int | None = None
    state: Literal["stopped", "starting", "running", "stopping"] = "stopped"
    seq: int = 0  # 状态序号，实例内递增
    detail: str = ""  # 错误摘要和日志位置
    work_dir: str = ""
    log_path: str = ""
    sessions: list[str] = field(default_factory=list)  # WorkSwarm 会话 ID 列表
    status_token: str = ""  # 状态请求鉴权令牌
    created_at: str = ""
    started_at: str | None = None
    owner_id: str = ""


@dataclass
class InstanceStatus:
    """状态协议数据结构 — push 和 pull 共用"""

    app_id: str
    instance_id: str
    seq: int
    state: Literal["running", "stopped"]
    detail: str = ""


def platform_key() -> str:
    """返回当前平台键名: "windows" 或 "darwin" 或 "linux" """
    import platform

    system = platform.system().lower()
    if system == "windows":
        return "windows"
    elif system == "darwin":
        return "darwin"
    else:
        return "linux"
