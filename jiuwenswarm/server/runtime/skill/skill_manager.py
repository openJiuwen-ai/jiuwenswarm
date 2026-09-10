# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SkillManager - 管理 skills 的加载、安装、卸载与 marketplace 操作."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import quote, urlparse

import httpx
import requests
import urllib3
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from openjiuwen.agent_evolving.checkpointing.evolution_store import (
    EvolutionLog as EvolutionFile,
    EvolutionRecord as EvolutionEntry,
)
from jiuwenswarm.common.local_env_config import get_local_config
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.utils import (
    get_agent_root_dir,
    get_agent_skills_dir,
    get_builtin_skills_dir,
    is_package_installation,
)
from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    DownloadPolicy,
    InstalledArtifact,
    ProviderInvocationContext,
    SkillRef,
    SkillSearchRequest,
    SkillSourceExtension,
    SkillSourceProvider,
    SourceConfig,
)
from jiuwenswarm.server.runtime.skill.artifact_security import (
    ArtifactVerificationError,
)
from jiuwenswarm.server.runtime.skill.source_registry import (
    SourceRegistry,
    SourceRegistryError,
)
from jiuwenswarm.server.runtime.skill.enterprise_state_lock import enterprise_skill_state_lock


def _get_ssl_verify() -> bool:
    """延迟导入以规避循环依赖：ssl_config 所在的 tools 包 __init__ 会回引本模块。"""
    from jiuwenswarm.agents.harness.common.tools.ssl_config import get_ssl_verify

    return get_ssl_verify()

logger = logging.getLogger(__name__)

# Cold-start / tip skills allowlist: pass raw comma-separated string to SkillUseRail.
ENABLED_SKILLS_ENV = "ENABLED_SKILLS"


def enabled_skills_from_environ() -> str | None:
    """Read ENABLED_SKILLS from tip/env (skills allowlist).

    Returns the raw comma-separated string for SkillUseRail normalization,
    or None when unset so the rail does not apply an empty allowlist.
    """
    from jiuwenswarm.common.local_env_config import read_env_if_set

    raw = read_env_if_set(ENABLED_SKILLS_ENV)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text


_SKILLNET_DOWNLOAD_TIMEOUT: int = int(os.environ.get("SKILLNET_DOWNLOAD_TIMEOUT", "60"))
_SKILLNET_MAX_RETRIES: int = int(os.environ.get("SKILLNET_MAX_RETRIES", "3"))
_MARKETPLACE_GIT_TIMEOUT_SECONDS: float = float(
    os.environ.get("MARKETPLACE_GIT_TIMEOUT", "30")
)
# SkillNet 异步安装 job 必须跨 SkillManager 实例共享：skills.* 无状态 RPC 在
# AgentManager 缓存未命中时会临时 new JiuWenSwarm()，install 与 install_status
# 可能落到不同实例；若 job 仅存实例内存会误报「安装会话已过期」。
_SKILLNET_INSTALL_JOBS: dict[str, dict[str, Any]] = {}
_FREE_SEARCH_PROXY_URL_ENV = "FREE_SEARCH_PROXY_URL"
_FREE_SEARCH_SSL_VERIFY_ENV = "FREE_SEARCH_SSL_VERIFY"
_SKILLNET_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_SKILLNET_NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")
_FREE_SEARCH_DEFAULT_NO_PROXY = "127.0.0.1,.huawei.com,localhost,local,.local,10.155.97.247,.myhuaweicloud.com"

# Team Skills Hub（仅 TEAM_SKILLS_HUB_* 环境变量）
_TEAM_SKILLS_HUB_MARKET_TIMEOUT: float = float(os.environ.get("TEAM_SKILLS_HUB_TIMEOUT", "60"))
_TEAM_SKILLS_HUB_BASE_URL_DEFAULT = "https://teamskills.openjiuwen.com"
_TEAM_SKILLS_HUB_DEFAULT_ALLOWED_DOWNLOAD_HOSTS: tuple[str, ...] = (
    "openjiuwen-market.obs.*.myhuaweicloud.com",
    "127.0.0.1",
    "localhost",
)
_IMPORT_LOCAL_REMOTE_TIMEOUT: float = float(os.environ.get("IMPORT_LOCAL_REMOTE_TIMEOUT", "60"))
_IMPORT_LOCAL_DEFAULT_ALLOWED_DOWNLOAD_HOSTS: tuple[str, ...] = ("*.obs.*.myhuaweicloud.com",)
_ONLINE_SEARCH_RRF_K = 60
_ONLINE_SEARCH_SOURCE_ORDER = {"skillnet": 0, "clawhub": 1}


def _maybe_disable_insecure_warning() -> None:
    """关闭证书校验时同步静默 urllib3 的 InsecureRequestWarning。

    历史实现为模块导入即全局 disable_warnings，导致即便开启证书校验也仍静默
    警告；改为按开关条件触发，开启校验时保留警告输出。
    """
    if not _get_ssl_verify():
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class _ImportLocalTLSAdapter(HTTPAdapter):
    """仅在 ssl_verify=false 时挂载，跳过证书/主机名校验。

    开启证书校验（get_ssl_verify() 为 True）时不应 mount 本 Adapter，
    而是走 requests 默认校验逻辑——调用方负责条件判断。
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ssl_version=ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ---------------------------------------------------------------------------
# 默认路径
# ---------------------------------------------------------------------------
_EVOLUTION_FILENAME = "evolutions.json"


def _get_agent_root_dir() -> "Path":
    return get_agent_root_dir()


def _get_marketplace_dir() -> "Path":
    return get_agent_skills_dir() / "_marketplace"


def _get_state_file() -> "Path":
    return get_state_file()


from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
    get_skill_enabled,
    get_state_file,
    list_disabled_skills,
    list_execution_disabled_skills,
    normalize_local_skills,
    normalize_skill_configs,
    remove_skill_config,
    set_skill_enabled,
)


class SkillNetEmptyDownloadError(Exception):
    """skillnet-ai ``download()`` returned None; 前端用 detail_key 做多语言。"""

    def __init__(self, *, github_context: str = "") -> None:
        self.github_context = (github_context or "").strip()
        self.detail_key = "skills.skillNet.errors.emptyDownloadResult"
        hint = f"\n{self.github_context[:800]}" if self.github_context else ""
        self.detail_params = {"hint": hint}
        super().__init__(self.github_context or "empty download path")


class SkillNetInstallError(Exception):
    """安装失败，携带 i18n detail_key 供前端本地化。"""

    def __init__(
        self,
        detail_key: str,
        *,
        detail: str = "",
        detail_params: dict[str, Any] | None = None,
    ) -> None:
        self.detail_key = detail_key
        self.detail = (detail or "").strip()
        self.detail_params = detail_params or {}
        super().__init__(self.detail or detail_key)


def _map_github_api_error(exc: Exception) -> SkillNetInstallError:
    """将 GitHubAPIError 按状态码映射为可本地化的 detail_key，原始报错留在 detail。"""
    status = getattr(exc, "status_code", None)
    raw = str(exc).strip()
    if status == 404:
        return SkillNetInstallError("skills.skillNet.errors.githubNotFound", detail=raw)
    if status == 403:
        return SkillNetInstallError("skills.skillNet.errors.githubRateLimited", detail=raw)
    if status == 401:
        return SkillNetInstallError("skills.skillNet.errors.githubUnauthorized", detail=raw)
    if status in (0, None):
        return SkillNetInstallError("skills.skillNet.errors.githubNetwork", detail=raw)
    message = getattr(exc, "message", "") or raw
    return SkillNetInstallError(
        "skills.skillNet.errors.githubApiError",
        detail=raw,
        detail_params={"status": str(status), "message": message},
    )


def _is_valid_http_mirror_url(url: str) -> bool:
    """Return True if url is a plausible http(s) mirror base (for SkillDownloader)."""
    s = url.strip()
    if not s or len(s) > 2048:
        return False
    parsed = urlparse(s)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(get_local_config(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _get_free_search_proxy_url() -> str:
    return str(get_local_config(_FREE_SEARCH_PROXY_URL_ENV, "") or "").strip()


def _free_search_ssl_verify() -> bool:
    return _env_bool(_FREE_SEARCH_SSL_VERIFY_ENV, default=False)


def _disable_insecure_request_warning() -> None:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _skillnet_proxy_mapping() -> dict[str, str]:
    proxy_url = _get_free_search_proxy_url()
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


def _configure_skillnet_requests_session(session: Any) -> None:
    proxies = _skillnet_proxy_mapping()
    if proxies:
        session.proxies.update(proxies)
    verify = _free_search_ssl_verify()
    session.verify = verify
    if verify is False:
        _disable_insecure_request_warning()


@contextmanager
def _skillnet_network_context():
    """Expose the configured proxy to third-party SkillNet clients during one call."""
    proxy_url = _get_free_search_proxy_url()
    env_keys = (*_SKILLNET_PROXY_ENV_KEYS, *_SKILLNET_NO_PROXY_ENV_KEYS)
    previous = {key: os.environ.get(key) for key in env_keys}
    try:
        if proxy_url:
            for key in _SKILLNET_PROXY_ENV_KEYS:
                os.environ[key] = proxy_url
            if not os.environ.get("NO_PROXY") and not os.environ.get("no_proxy"):
                for key in _SKILLNET_NO_PROXY_ENV_KEYS:
                    os.environ[key] = _FREE_SEARCH_DEFAULT_NO_PROXY
        if _free_search_ssl_verify() is False:
            _disable_insecure_request_warning()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class SkillNameConflictError(ValueError):
    """同名但不同来源的 Skill 安装冲突（映射为稳定错误码 skill_name_conflict）。"""


def _safe_path_name(value: Any, label: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"invalid {label} name")
    path_value = Path(raw)
    invalid_name_checks = (
        raw in (".", ".."),
        "/" in raw,
        "\\" in raw,
        path_value.is_absolute(),
        PureWindowsPath(raw).is_absolute(),
    )
    if any(invalid_name_checks):
        raise ValueError(f"invalid {label} name: {raw}")
    return raw


def _safe_child_path(base: Path, name: Any, label: str) -> Path:
    safe_name = _safe_path_name(name, label)
    base_resolved = base.resolve()
    candidate = (base / safe_name).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"invalid {label} path: {safe_name}") from exc
    return candidate


def _log_rejected_name(operation: str, label: str, value: Any, exc: ValueError) -> None:
    logger.warning(
        "rejected invalid %s name: operation=%s value=%r error=%s",
        label,
        operation,
        value,
        exc,
    )


def _sha256_matches(body: bytes, checksum_sha256: str) -> bool:
    """校验值非空时比对 SHA-256（大小写不敏感）；空校验值视为通过."""
    expected = str(checksum_sha256 or "").strip().lower()
    if not expected:
        return True
    return hashlib.sha256(body).hexdigest().lower() == expected


def _safe_rmtree(path: Path) -> bool:
    """安全地删除目录树，处理 Windows 上的 git 文件锁定问题."""
    if not path.exists():
        return True

    import time
    import stat

    max_retries = 3
    retry_delay = 0.2

    for attempt in range(max_retries):
        try:
            shutil.rmtree(path)
            return True
        except OSError as exc:
            logger.debug("删除目录失败（尝试 %d/%d）: %s", attempt + 1, max_retries, exc)

            # 最后一次尝试失败，直接返回 False
            if attempt == max_retries - 1:
                logger.warning("删除目录失败（已重试 %d 次）: %s", max_retries, path)
                return False

            # 检查是否是 Windows 上的权限问题
            # 尝试修改文件权限
            if os.name == "nt":
                try:
                    # 尝试递归修改权限
                    for root, dirs, files in os.walk(path):
                        for name in files + dirs:
                            filepath = Path(root) / name
                            try:
                                # 移除只读属性
                                if os.name == "nt":
                                    os.chmod(filepath, stat.S_IWRITE)
                                elif os.name == "posix":
                                    os.chmod(filepath, 0o777)
                                # 对目录，尝试删除其中的文件
                                if filepath.is_dir():
                                    try:
                                        shutil.rmtree(filepath)
                                    except OSError:
                                        pass  # 忽略子目录删除失败，外层会重试
                                elif filepath.is_file():
                                    try:
                                        os.unlink(filepath)
                                    except PermissionError:
                                        pass  # 忽略文件删除失败
                                # 小延迟
                                time.sleep(0.01)
                            except OSError:
                                pass  # 忽略权限修改失败
                except Exception:
                    pass  # 忽略其他异常

            # 等待后重试
            time.sleep(retry_delay)
            retry_delay *= 2

    return False


def _handle_copy_error(exc: OSError, dest: Path, logger_prefix: str, src: Path | None = None) -> dict[str, Any]:
    """处理文件/目录复制失败的统一错误处理函数.
    
    Args:
        exc: 捕获到的 OSError 异常（包括 shutil.Error）
        dest: 目标路径（会被清理）
        logger_prefix: 日志前缀（用于区分不同操作）
        src: 源路径（可选，用于日志记录）
    
    Returns:
        错误响应字典
    """
    # 记录错误日志
    if src:
        logger.error("[SkillManager] %s copy failed: src=%s dest=%s error=%s", logger_prefix, src, dest, exc)
    else:
        logger.error("[SkillManager] %s copy failed: dest=%s error=%s", logger_prefix, dest, exc)
    
    # 清理可能已部分创建的目录
    if dest.exists():
        _safe_rmtree(dest)
    
    # 获取错误消息字符串（支持普通 OSError 和 shutil.Error）
    error_msg = str(exc)    
    # 检查是否是路径太长错误 (WinError 206)
    if "WinError 206" in error_msg or "206" in error_msg:
        return {
            "success": False,
            "detail": (
                "文件名或路径过长：可能是skill 内部子文件路径过长"
                "或当前工作路径过长，请尝试缩短 skill 名称，"
                "使用更短的目录路径，或者开启windows长路径支持"
            ),
        }

    # 检查是否是路径不存在错误 (WinError 3)
    if "WinError 3" in error_msg or "系统找不到指定的路径" in error_msg:
        return {
            "success": False,
            "detail": (
                "路径不存在：可能是skill 内部子文件路径过长"
                "或当前工作路径过长，请尝试缩短 skill 名称，"
                "使用更短的目录路径，或者开启windows长路径支持"
            ),
        }

    # 返回通用错误信息
    return {
        "success": False,
        "detail": error_msg if error_msg else f"复制失败 ({type(exc).__name__})",
    }


def _state_transactional(method: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a synchronous SkillManager mutation in the workspace lock.

    The outer-most wrapper reloads the authoritative skills_state.json from
    disk before the body runs, so cross-pod AgentServer instances sharing one
    workspace never overwrite each other's writes (last-writer-wins bug for
    enterprise skill state). Nested wrappers reuse the same transaction and do
    not reload, preserving inner helper mutations. Async handlers must call a
    decorated synchronous helper through ``asyncio.to_thread`` so waiting for
    the cross-process file lock never blocks the event-loop thread.
    """
    if asyncio.iscoroutinefunction(method):
        raise TypeError("_state_transactional only supports synchronous methods")

    @wraps(method)
    def sync_wrapper(self: "SkillManager", *args: Any, **kwargs: Any) -> Any:
        with self.state_transaction():
            return method(self, *args, **kwargs)

    return sync_wrapper


class SkillManager:
    """Skill 管理器，对应 skills.* 请求方法."""

    def __init__(
        self,
        workspace_dir: str | None = None,
        *,
        persist_skills_state: bool = True,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        # 若传入 workspace_dir（harness adapter 使用），优先通过 Workspace/WorkspaceNode
        # 解析 skills 路径；否则使用全局默认路径（react adapter 或无参数时）。
        if workspace_dir is not None:
            try:
                from openjiuwen.harness.workspace.workspace import Workspace, WorkspaceNode

                workspace = Workspace(root_path=workspace_dir)
                skills_path = workspace.get_node_path(WorkspaceNode.SKILLS)
                self._skills_dir: Path = (
                    skills_path if skills_path is not None else Path(workspace_dir) / WorkspaceNode.SKILLS.value
                )
            except ImportError:
                self._skills_dir = Path(workspace_dir) / "skills"
            self._agent_root: Path = Path(workspace_dir)
            self._marketplace_dir: Path = self._skills_dir / "_marketplace"
            self._state_file: Path = self._skills_dir / "skills_state.json"
        else:
            self._agent_root = _get_agent_root_dir()
            self._skills_dir = get_agent_skills_dir()
            self._marketplace_dir = _get_marketplace_dir()
            self._state_file = _get_state_file()
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._persist_skills_state = bool(persist_skills_state)
        self._service_id = str(service_id or "").strip() or None
        self._agent_id = str(agent_id or "").strip() or None
        self._state: dict[str, Any] = self._load_state()
        self._source_registry = SourceRegistry()
        self._register_builtin_skill_sources()
        # 把手动拷入 skills 目录、未经任何安装流程登记的本地技能，自动补登记到
        # local_skills，使其与"导入本地技能"完全等价（可展示/卸载/查看详情/禁用）。
        self._register_unmanaged_local_skills()
        # 企业版：把仓库内置技能复制进 tenant workspace 并登记为 builtin（不可卸载），
        # 使"我的技能"与个人版一致地展示内置技能（个人版走 _scan_builtin_skills 可浏览）。
        self._register_builtin_skills()
        # SkillNet 异步安装：install 立即返回 install_id，后台下载；完成后调用 hook 重载 Agent
        self._skillnet_install_complete_hook: Callable[[], Awaitable[None]] | None = None

    @property
    def _skillnet_install_jobs(self) -> dict[str, dict[str, Any]]:
        """进程级共享的 SkillNet 安装任务表（见模块常量 ``_SKILLNET_INSTALL_JOBS``）."""
        return _SKILLNET_INSTALL_JOBS

    def set_skillnet_install_complete_hook(self, hook: Callable[[], Awaitable[None]] | None) -> None:
        """安装成功落盘后回调（通常为重载 Agent 实例）."""
        self._skillnet_install_complete_hook = hook

    def _register_builtin_skill_sources(self) -> None:
        """Register extension-offered default sources; clients are started lazily."""
        self._register_default_sources(self._source_registry)

    @staticmethod
    def _register_default_sources(
        registry: SourceRegistry, *, skip: set[str] | None = None
    ) -> None:
        """Ask each registered Skill Source factory for its default source and bind it.

        是否提供默认源由扩展包自决（``default_source_config()`` 返回 None 即不注册）。
        扩展系统未运行（如单测直接构造 SkillManager）时无工厂可问，直接不注册。
        """
        try:
            from jiuwenswarm.extensions.registry import ExtensionRegistry

            factories = ExtensionRegistry.get_instance().list_skill_source_extensions()
        except RuntimeError:
            factories = []
        skipped = skip or set()
        for factory in factories:
            try:
                config = factory.default_source_config()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "skill source extension %s failed to provide default source config",
                    getattr(factory, "provider_type", "?"),
                    exc_info=True,
                )
                continue
            if config is None or config.source_id in skipped:
                continue
            try:
                registry.bind_extension(config, factory)
            except (SourceRegistryError, ValueError) as exc:
                logger.warning(
                    "default skill source %s rejected: %s", config.source_id, exc
                )

    @staticmethod
    def _get_skill_source_extension(provider_type: str) -> SkillSourceExtension | None:
        """Look up a registered Skill Source factory.

        Returns None when the extension system is not running (e.g. unit tests
        instantiating SkillManager directly).
        """
        try:
            from jiuwenswarm.extensions.registry import ExtensionRegistry

            return ExtensionRegistry.get_instance().get_skill_source_extension(provider_type)
        except RuntimeError:
            return None

    @staticmethod
    def _source_config_custom_payload(record: dict[str, Any]) -> dict[str, Any] | None:
        raw = record.get("custom_config")
        if raw is None:
            raw = record.get("data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SourceRegistryError(
                    "source_misconfigured", "extension custom_config is not valid JSON"
                ) from exc
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _parse_source_config(raw: dict[str, Any]) -> SourceConfig:
        # trust_policy 已从管理面摘除：验签信任策略内聚到 Provider（由扩展工厂
        # 注入），管理面 dict 中的 trust_policy 键一律忽略。该键自 !5611 引入，
        # 无存量使用方；SourceConfig.trust_policy 字段本身保留（SDK 模型不动）。
        capabilities = raw.get("capabilities") or []
        if not isinstance(capabilities, list):
            raise SourceRegistryError("source_misconfigured", "capabilities must be an array")
        options = raw.get("options") or {}
        if not isinstance(options, dict):
            raise SourceRegistryError("source_misconfigured", "options must be an object")
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise SourceRegistryError("source_misconfigured", "priority must be an integer") from exc
        download_raw = raw.get("download_policy")
        download_policy = DownloadPolicy()
        if isinstance(download_raw, dict):
            allowed_hosts = download_raw.get("allowed_hosts") or []
            if not isinstance(allowed_hosts, list):
                raise SourceRegistryError(
                    "source_misconfigured", "download_policy.allowed_hosts must be an array"
                )
            try:
                max_bytes = int(download_raw.get("max_bytes", 64 * 1024 * 1024))
                timeout_seconds = float(download_raw.get("timeout_seconds", 60.0))
            except (TypeError, ValueError) as exc:
                raise SourceRegistryError(
                    "source_misconfigured", "download policy limits must be numeric"
                ) from exc
            if max_bytes <= 0 or timeout_seconds <= 0:
                raise SourceRegistryError(
                    "source_misconfigured", "download policy limits must be positive"
                )
            download_policy = DownloadPolicy(
                allowed_hosts=tuple(str(item).strip().lower() for item in allowed_hosts if str(item).strip()),
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
        return SourceConfig(
            source_id=str(raw.get("source_id") or "").strip(),
            provider_type=str(raw.get("provider_type") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            priority=priority,
            endpoint_ref=str(raw.get("endpoint_ref") or "").strip() or None,
            auth_ref=str(raw.get("auth_ref") or "").strip() or None,
            capabilities=frozenset(str(item) for item in capabilities),
            download_policy=download_policy,
            options=dict(options),
        )

    async def apply_skill_source_configs(
        self,
        extension_config: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Atomically apply ``custom_config.skill_sources`` from effective policy."""
        raw_sources: list[dict[str, Any]] = []
        for record in extension_config or []:
            if not isinstance(record, dict):
                continue
            custom = self._source_config_custom_payload(record)
            if custom is None or "skill_sources" not in custom:
                continue
            configured = custom.get("skill_sources")
            if not isinstance(configured, list):
                raise SourceRegistryError(
                    "source_misconfigured", "custom_config.skill_sources must be an array"
                )
            for item in configured:
                if not isinstance(item, dict):
                    raise SourceRegistryError(
                        "source_misconfigured", "skill_sources entries must be objects"
                    )
                raw_sources.append(item)
        if not raw_sources:
            return {
                "applied": False,
                "source_count": len(self._source_registry.list()),
            }

        configs = [self._parse_source_config(item) for item in raw_sources]
        candidate = SourceRegistry()
        configured_ids = {config.source_id for config in configs}
        # 默认源由扩展工厂自决是否提供（含企业版门控），与配置源同表绑定。
        self._register_default_sources(candidate, skip=configured_ids)

        for config in configs:
            extension = self._get_skill_source_extension(config.provider_type)
            if extension is None:
                raise SourceRegistryError(
                    "source_misconfigured",
                    f"provider_type is not registered: {config.provider_type}",
                )
            try:
                candidate.bind_extension(config, extension)
            except ValueError as exc:
                raise SourceRegistryError("source_misconfigured", str(exc)) from exc

        previous = self._source_registry
        self._source_registry = candidate
        await previous.close()
        return {"applied": True, "source_count": len(candidate.list())}

    def register_skill_source_provider(
        self,
        config: SourceConfig,
        provider: SkillSourceProvider,
        *,
        display_name: str | None = None,
    ) -> None:
        """Bind an extension-created Provider to this AgentServer runtime."""
        self._source_registry.register(config, provider, display_name=display_name)

    def bind_skill_source_extension(
        self,
        config: SourceConfig,
        extension: SkillSourceExtension,
        *,
        display_name: str | None = None,
    ) -> SkillSourceProvider:
        """Instantiate one configured source through a registered SPI factory."""
        return self._source_registry.bind_extension(
            config,
            extension,
            display_name=display_name,
        )

    # -----------------------------------------------------------------------
    # 公开 handler
    # -----------------------------------------------------------------------

    async def handle_skills_list(self, params: dict) -> dict:
        """返回所有可用 skill（本地 + marketplace 中未安装的）.

        params:
            refresh_marketplaces: bool (可选, 默认 False)
                个人版为 True 时，先对已配置 marketplace 执行 clone/pull，再扫描列表；
                企业版不消费 marketplace，忽略该参数。
            with_installed: bool (可选, 默认 False)
                为 True 时，同一次响应中附带 plugins（与 skills.installed 一致），
                避免网关串行处理两次 RPC 导致列表刷新超时或排队过久。
        """
        refresh_marketplaces = bool(params.get("refresh_marketplaces", False))
        # 企业版不消费 marketplace 扫描结果（下方 marketplace 恒为 []），
        # 跳过同步，避免无意义的 git clone/pull 拖慢列表请求（超时/502）。
        if refresh_marketplaces and not is_enterprise():
            await self._sync_marketplace_repos()
        # 每次列举前，把手动拷入 skills 目录、尚未登记的本地技能补登记为 local，
        # 使其无需重启 server、刷新"我的技能"即可显示（与导入本地技能一致）。
        await asyncio.to_thread(self._register_unmanaged_local_skills)
        local = self._scan_local_skills()
        # 内置技能（仓内内置）两版都扫描展示；企业版不再跳过 builtin。
        builtin = self._scan_builtin_skills()
        marketplace = [] if is_enterprise() else self._scan_marketplace_skills()
        out: dict[str, Any] = {"skills": local + builtin + marketplace}
        if bool(params.get("with_installed", False)):
            installed = await self.handle_skills_installed(params)
            out["plugins"] = installed.get("plugins") or []
        return out

    async def handle_skills_installed(self, params: dict) -> dict:
        """返回已安装的 marketplace 插件列表.

        按前端期望格式返回：plugin_name, marketplace, spec, version, installed_at, git_commit, skills[], enabled
        """
        raw_plugins = self._get_installed_plugins()
        plugins = []
        for p in raw_plugins:
            p = self._normalize_plugin(p)
            if str(p.get("source_type") or "").strip() in {"prebuilt", "user", "builtin"}:
                continue
            name = p.get("name", "")
            marketplace = p.get("marketplace", "")
            spec = f"{name}@{marketplace}" if marketplace else name
            plugin = {
                "plugin_name": name,
                "marketplace": marketplace,
                "spec": spec,
                "version": p.get("version", ""),
                "installed_at": p.get("installed_at", ""),
                "git_commit": p.get("commit", ""),
                "enabled": self.get_skill_enabled(name),
                "skills": [name] if name else [],
            }
            plugins.append(plugin)
        skills = [
            self._skill_installation_dto(record)
            for record in self.list_skill_installations()
            if str(record.get("source_type") or "").strip() in {"prebuilt", "user", "builtin"}
        ]
        return {"plugins": plugins, "skills": skills}

    async def handle_skills_enterprise_list(self, params: dict) -> dict:
        """企业兼容列表：复用 workspace 安装 DTO，并保留租户标识字段。"""
        payload = await self.handle_skills_installed(params)
        service_id = str(
            params.get("service_id") or self._service_id or ""
        ).strip()
        agent_id = str(
            params.get("agent_id") or self._agent_id or ""
        ).strip()
        for skill in payload.get("skills", []):
            if not isinstance(skill, dict):
                continue
            skill.update(
                {
                    "skill_name": skill.get("name"),
                    "skill_source": skill.get("origin"),
                    "skill_version": skill.get("version"),
                    "service_id": service_id,
                    "agent_id": agent_id,
                }
            )
        payload["service_id"] = service_id
        payload["agent_id"] = agent_id
        return payload

    async def handle_skills_get(self, params: dict) -> dict:
        """获取单个 skill 详情（name 必填）.

        返回字段转换：body -> content, path -> file_path
        """
        name = params.get("name")
        raw_origin = str(params.get("origin", "") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")

        # 收集本地 skills 目录中所有同 name 的目录（重名技能可能多个并存）
        matched_children: list[Path] = []
        # child -> (meta, local_skill 记录)。第一遍已读盘解析 meta，选中后直接复用，
        # 不再二次 _try_find_skill_file + _parse_skill_md（避免重复 I/O）。
        child_records: dict[Path, tuple[dict, dict | None]] = {}
        for child in self._skills_dir.iterdir():
            if child.name.startswith("_") or not child.is_dir():
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if meta is None:
                continue
            # 与 _scan_local_skills 保持一致：无 frontmatter 时 name 退化为文件名(SKILL)，
            # 此处用目录名修正，否则前端列表(目录名)与详情(文件名)对不上导致"未找到 skill"
            if meta.get("name") == md.stem:
                meta["name"] = child.name
            if meta.get("name") == name:
                matched_children.append(child)
                # 复用 _find_local_skill_for_dir：origin 精确匹配，避免重名串成同一条记录
                child_records[child] = (meta, self._find_local_skill_for_dir(child, meta))

        # origin 优先：若前端传了 origin，按 origin 在候选目录中精确定位对应的那一个，
        # 而不是返回遍历到的第一个——这是"重名技能点哪个详情就显示哪个"的关键。
        chosen: Path | None = None
        if raw_origin and matched_children:
            for child in matched_children:
                rec = child_records.get(child)
                ls_rec = rec[1] if rec else None
                rec_origin = str(ls_rec.get("origin", "") or "").strip() if isinstance(ls_rec, dict) else ""
                if rec_origin == raw_origin:
                    chosen = child
                    break
        if chosen is None and matched_children:
            # 没传 origin 或没匹配上：退回首条（保留原行为，向后兼容）
            chosen = matched_children[0]

        if len(matched_children) > 1:
            logger.debug(
                "[SkillDedup] skills.get name=%s 命中 %d 个同名磁盘目录: %s "
                "origin_param=%s 选中=%s",
                name, len(matched_children), [c.name for c in matched_children],
                raw_origin or "(none)", chosen.name if chosen else "(none)",
            )

        if chosen is not None:
            child = chosen
            rec = child_records.get(child)
            # 第一遍已读盘解析 meta 并修正过 name，这里直接复用，不再二次 I/O
            if rec is not None:
                meta, ls_rec = rec
                # 回填展示字段并取 origin：让前端详情页拿到准确信息，卸载时能按 origin
                # 精确定位目录，不再退回按 name 删（重名会误删另一个）
                rec_origin = (
                    self._apply_local_skill_meta(meta, ls_rec)
                    if isinstance(ls_rec, dict) else ""
                )
                # 字段转换以符合前端期望
                meta["content"] = meta.pop("body", "")
                meta["file_path"] = meta.pop("path", "")
                # origin 传给 _resolve_skill_source，确保同名不同源时返回该技能自己的 source，
                # 而不是遍历到的第一个同名 plugin。
                meta["source"] = self._resolve_skill_source(meta.get("name", ""), origin=rec_origin or None)
                meta["display_name"] = meta.get(
                    "display_name"
                ) or self._resolve_skill_display_name(meta.get("name", ""))
                meta["is_builtin"] = self._is_builtin_skill(meta.get("name", ""), self._get_installed_plugins(), child)
                builtin_dir = get_builtin_skills_dir()
                if builtin_dir.exists():
                    builtin_skill_path = builtin_dir / child.name
                    meta["is_builtin_source"] = builtin_skill_path.exists() and builtin_skill_path.is_dir()
                else:
                    meta["is_builtin_source"] = False
                meta["has_evolutions"] = (child / _EVOLUTION_FILENAME).is_file()
                self._apply_enabled_config(meta, meta.get("name", ""))
                plugin_record = self._find_installed_plugin_for_dir(child, meta, ls_rec)
                if plugin_record is not None:
                    self._apply_skill_installation_meta(meta, plugin_record)
                return meta

        # 再在 marketplace 目录中查找
        if self._marketplace_dir.exists():
            for repo_dir in self._marketplace_dir.iterdir():
                if not repo_dir.is_dir():
                    continue
                for plugin_dir in repo_dir.iterdir():
                    if not plugin_dir.is_dir():
                        continue
                    md = self._try_find_skill_file(plugin_dir)
                    if md is None:
                        continue
                    meta = self._parse_skill_md(md)
                    if meta is None:
                        continue
                    if meta.get("name") == md.stem:
                        meta["name"] = plugin_dir.name
                    if meta.get("name") == name:
                        # 字段转换以符合前端期望
                        meta["content"] = meta.pop("body", "")
                        meta["file_path"] = meta.pop("path", "")
                        marketplace_name = repo_dir.name
                        meta["source"] = marketplace_name
                        meta["marketplace"] = marketplace_name
                        meta["is_builtin"] = False
                        meta["is_builtin_source"] = False
                        meta["has_evolutions"] = False
                        self._apply_enabled_config(meta, meta.get("name", ""))
                        return meta

        raise ValueError(f"未找到 skill: {name}")

    async def handle_skills_toggle(self, params: dict) -> dict:
        """切换已安装本地 skill 的 enabled 状态。

        params:
            name: skill 名称
            origin: 可选，技能来源标识。提供时按 origin 精确定位 installed_plugin
                记录，避免重名技能误操作另一条。
            enabled: 目标状态
        """
        return await asyncio.to_thread(self._handle_skills_toggle_sync, params)

    @_state_transactional
    def _handle_skills_toggle_sync(self, params: dict) -> dict:
        """Apply one toggle while holding the workspace state transaction."""
        name = params.get("name", "")
        origin = str(params.get("origin", "") or "").strip() or None
        enabled = params.get("enabled")
        if not name:
            return {"success": False, "detail": "缺少参数: name"}
        if not isinstance(enabled, bool):
            return {"success": False, "detail": "缺少参数: enabled (bool)"}
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.toggle", "skill", name, exc)
            return {"success": False, "detail": str(exc)}

        installation = self._find_skill_installation(name=name, origin=origin)
        if (
            installation is not None
            and str(installation.get("source_type") or "").strip() in {"builtin", "prebuilt"}
        ):
            return {
                "success": False,
                "error_code": "prebuilt_not_toggleable",
                "error_message": "prebuilt skill is managed by administrator",
                "detail": "企业预置技能由管理员统一管理，不允许启停。",
            }

        self.set_skill_enabled(name, enabled)
        self._set_plugin_enabled(name, enabled, origin=origin)
        return {
            "success": True,
            "name": name,
            "enabled": enabled,
            "config": {"enabled": enabled},
            "detail": "配置已更新；下次 reload / rebuild / 新会话后执行面生效。",
        }

    async def handle_skills_retrieval_status(self, params: dict) -> dict:
        """返回本地 skill retrieval 索引状态."""
        from jiuwenswarm.symphony.skill_retrieval import get_skill_retrieval_status

        return await asyncio.to_thread(get_skill_retrieval_status, self)

    async def handle_skills_retrieval_index_build(self, params: dict) -> dict:
        """构建或复用本地 skill retrieval 索引."""
        from jiuwenswarm.symphony.skill_retrieval.build_coordinator import start_skill_index_build

        params = params or {}
        force = bool(params.get("force", False))
        source = str(params.get("source") or "web").strip() or "web"
        return await asyncio.to_thread(start_skill_index_build, self, force=force, source=source)

    async def handle_skills_retrieval_index_cancel(self, params: dict) -> dict:
        """请求取消本地 skill retrieval 索引构建."""
        from jiuwenswarm.symphony.skill_retrieval import cancel_skill_index_build

        return await asyncio.to_thread(cancel_skill_index_build, self)

    async def handle_skills_retrieval_search(self, params: dict) -> dict:
        """基于本地索引检索已安装 skills."""
        from jiuwenswarm.symphony.skill_retrieval import retrieve_skills

        query = str((params or {}).get("query") or "").strip()
        return await asyncio.to_thread(retrieve_skills, query, self)

    async def handle_skills_retrieval_tree(self, params: dict) -> dict:
        """返回本地 skill retrieval 树索引概览."""
        from jiuwenswarm.symphony.skill_retrieval import get_skill_retrieval_tree

        language = str((params or {}).get("language") or "cn").strip() or "cn"
        return await asyncio.to_thread(get_skill_retrieval_tree, self, language=language)

    async def handle_skills_evolution_status(self, params: dict) -> dict:
        """检查某个 skill 是否存在 evolutions.json."""
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.evolution.status", "skill", name, exc)
            raise ValueError(str(exc)) from exc
        evo_path = self._get_skill_evolution_path(name)
        return {
            "name": name,
            "exists": bool(evo_path and evo_path.is_file()),
        }

    async def handle_skills_evolution_get(self, params: dict) -> dict:
        """获取某个 skill 的 evolutions.json 内容（重点返回 entries）."""
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.evolution.get", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        evo_path = self._get_skill_evolution_path(name)
        if evo_path is None or not evo_path.is_file():
            return {
                "name": name,
                "exists": False,
                "valid": True,
                "skill_id": name,
                "version": "1.0.0",
                "updated_at": "",
                "entries": [],
            }

        try:
            raw = json.loads(evo_path.read_text(encoding="utf-8"))
            evo_file = EvolutionFile.from_dict(raw)
            return {
                "name": name,
                "exists": True,
                "valid": True,
                **evo_file.to_dict(),
            }
        except Exception as exc:
            logger.warning("读取 evolutions.json 失败: skill=%s error=%s", name, exc)
            return {
                "name": name,
                "exists": True,
                "valid": False,
                "detail": "evolutions.json 格式错误或读取失败",
                "skill_id": name,
                "version": "1.0.0",
                "updated_at": "",
                "entries": [],
            }

    async def handle_skills_evolution_save(self, params: dict) -> dict:
        """保存某个 skill 的 evolutions.json 条目列表."""
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.evolution.save", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        if not self._resolve_local_skill_dir(name):
            raise ValueError(f"未找到 skill: {name}")

        entries = params.get("entries")
        if not isinstance(entries, list):
            raise ValueError("参数 entries 必须是数组")

        normalized_entries: list[EvolutionEntry] = []
        for idx, item in enumerate(entries):
            if not isinstance(item, dict):
                raise ValueError(f"entries[{idx}] 必须是对象")
            entry_id = str(item.get("id") or "").strip()
            content = item.get("change", {}).get("content") if isinstance(item.get("change"), dict) else None
            if not entry_id:
                raise ValueError(f"entries[{idx}].id 不能为空")
            if not isinstance(content, str):
                raise ValueError(f"entries[{idx}].change.content 必须是字符串")
            normalized_entries.append(EvolutionEntry.from_dict(item))

        evo_path = self._get_skill_evolution_path(name)
        evo_file = EvolutionFile.empty(skill_id=name)
        if evo_path and evo_path.is_file():
            try:
                current = json.loads(evo_path.read_text(encoding="utf-8"))
                evo_file = EvolutionFile.from_dict(current)
            except Exception as exc:
                logger.warning("读取原 evolutions.json 失败，将以新内容覆盖: skill=%s error=%s", name, exc)

        evo_file.entries = normalized_entries
        evo_file.updated_at = datetime.now(timezone.utc).isoformat()
        if not evo_file.skill_id:
            evo_file.skill_id = name

        if evo_path is None:
            raise ValueError(f"未找到 skill: {name}")
        evo_path.parent.mkdir(parents=True, exist_ok=True)
        evo_path.write_text(
            json.dumps(evo_file.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "success": True,
            "name": name,
            "entry_count": len(evo_file.entries),
            "updated_at": evo_file.updated_at,
        }

    async def handle_skills_marketplace_list(self, params: dict) -> dict:
        """列出已配置的 marketplace 源.

        返回格式符合前端期望：name, url, install_location?, last_updated?
        """
        marketplaces = self._get_marketplaces()
        # 为每个 marketplace 添加前端期望的可选字段
        result = []
        for m in marketplaces:
            item = {
                "name": m.get("name", ""),
                "url": m.get("url", ""),
                "enabled": bool(m.get("enabled", True)),
                "install_location": m.get("install_location"),
                "last_updated": m.get("last_updated"),
            }
            result.append(item)
        return {"marketplaces": result}

    async def handle_skills_install(self, params: dict) -> dict:
        """安装 marketplace 中的 skill.

        params:
            spec: "plugin_name@marketplace_name"
            force: bool (可选, 默认 False)
        """
        spec = params.get("spec", "")
        force = params.get("force", False)

        if "@" not in spec:
            # Bare skill name — check if it matches a builtin skill
            try:
                safe_name = _safe_path_name(spec, "skill")
            except ValueError as exc:
                _log_rejected_name("skills.install", "skill", spec, exc)
                return {"success": False, "detail": str(exc)}
            builtin_dir = get_builtin_skills_dir()
            builtin_path = _safe_child_path(builtin_dir, safe_name, "skill")
            if builtin_path.exists() and builtin_path.is_dir():
                return await self.handle_skills_install_builtin({"name": safe_name, "force": force})
            return {"success": False, "detail": "spec 格式应为 skill@marketplace，内置技能可直接使用名称安装"}

        plugin_name, marketplace_name = spec.rsplit("@", 1)
        if not plugin_name or not marketplace_name:
            return {"success": False, "detail": "plugin 或 marketplace 名称为空"}
        try:
            plugin_name = _safe_path_name(plugin_name, "plugin")
            marketplace_name = _safe_path_name(marketplace_name, "marketplace")
        except ValueError as exc:
            _log_rejected_name("skills.install", "plugin/marketplace", spec, exc)
            return {"success": False, "detail": str(exc)}

        if marketplace_name == "builtin":
            return await self.handle_skills_install_builtin({"name": plugin_name, "force": force})

        # 查找 marketplace 配置
        marketplace = None
        for m in self._get_marketplaces():
            if m.get("name") == marketplace_name:
                marketplace = m
                break
        if marketplace is None:
            return {"success": False, "detail": f"未找到 marketplace: {marketplace_name}"}

        git_url = marketplace.get("url", "")
        if not git_url:
            return {"success": False, "detail": f"marketplace {marketplace_name} 缺少 url"}

        # 确保 marketplace 仓库已 clone
        repo_dir = _safe_child_path(self._marketplace_dir, marketplace_name, "marketplace")
        if repo_dir.exists():
            await self._git_pull(repo_dir)
        else:
            commit = await self._git_clone(git_url, repo_dir)
            if commit is None:
                return {"success": False, "detail": f"git clone 失败: {git_url}"}

        # 在仓库中查找 plugin 目录
        plugin_src = repo_dir / "skills" / plugin_name

        # 兼容单skill模式
        if not plugin_src.exists() or not plugin_src.is_dir():
            plugin_src = repo_dir
        if not plugin_src.is_dir():
            return {"success": False, "detail": f"在 marketplace 仓库中未找到 plugin: {plugin_name}"}

        md = self._try_find_skill_file(plugin_src)
        if md is None:
            return {"success": False, "detail": f"plugin {plugin_name} 缺少 SKILL.md"}

        # 复制到本地 skills 目录
        dest = _safe_child_path(self._skills_dir, plugin_name, "skill")
        if dest.exists():
            if not force:
                return {
                    "success": False,
                    "detail": f"skill {plugin_name} 已存在",
                    "detail_key": "skills.marketplace.errors.skillAlreadyInstalled",
                }
            _safe_rmtree(dest)
        shutil.copytree(plugin_src, dest)

        # 解析元数据并记录（添加 installed_at 时间戳）
        meta = self._parse_skill_md(self._try_find_skill_file(dest)) or {}
        commit_hash = await self._git_get_commit(repo_dir)
        self._add_installed_plugin(
            {
                "name": plugin_name,
                "marketplace": marketplace_name,
                "version": meta.get("version", ""),
                "commit": commit_hash or "",
                "source": marketplace_name,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._refresh_agent_data_indexes()

        return {"success": True}

    async def handle_skills_install_builtin(self, params: dict) -> dict:
        """安装内置技能.

        params:
            name: skill 名称
            force: 可选，是否强制覆盖重装。冲突时传 force=True 会删除旧目录后重新安装。
        """
        name = params.get("name", "")
        force = bool(params.get("force", False))
        if not name:
            return {"success": False, "detail": "缺少参数: name"}
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.install_builtin", "skill", name, exc)
            return {"success": False, "detail": str(exc)}

        builtin_dir = get_builtin_skills_dir()
        if not builtin_dir.exists():
            return {"success": False, "detail": "内置技能目录不存在"}

        src = _safe_child_path(builtin_dir, name, "skill")
        if not src.exists() or not src.is_dir():
            return {"success": False, "detail": f"未找到内置技能: {name}"}

        # 检查是否已经安装
        dest = _safe_child_path(self._skills_dir, name, "skill")
        if dest.exists() and dest.is_dir():
            if not force:
                return {
                    "success": False,
                    "detail": f"技能 {name} 已经安装",
                    "detail_key": "skills.builtin.errors.skillAlreadyInstalled",
                }
            _safe_rmtree(dest)

        # 复制技能到用户目录
        try:
            shutil.copytree(src, dest)
        except Exception as exc:
            logger.error("安装内置技能 copytree 失败: %s", exc)
            return {"success": False, "detail": f"安装失败: {exc}"}

        # 记录安装信息到状态文件
        meta = self._parse_skill_md(self._try_find_skill_file(dest)) or {}
        self._add_installed_plugin(
            {
                "name": name,
                "marketplace": "builtin",
                "version": meta.get("version", ""),
                "commit": "",
                "source": "builtin",
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        # 刷新索引
        self._refresh_agent_data_indexes()

        return {"success": True}

    @staticmethod
    def _normalize_online_search_identifier(source: str, identifier: str) -> str:
        """Build a conservative identity key for safe result merging."""
        value = str(identifier or "").strip()
        if not value:
            return f"{source}:"
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            scheme = "https" if parsed.hostname and parsed.hostname.lower() == "github.com" else parsed.scheme.lower()
            host = parsed.netloc.lower()
            path = parsed.path.rstrip("/") or "/"
            return f"url:{scheme}://{host}{path}"
        return f"{source}:{value.casefold()}"

    @staticmethod
    def _normalize_online_search_item(source: str, item: dict[str, Any], rank: int) -> dict[str, Any]:
        if source == "skillnet":
            name = str(item.get("skill_name") or item.get("name") or "").strip()
            description = str(item.get("skill_description") or item.get("description") or "").strip()
            identifier = str(item.get("skill_url") or item.get("url") or "").strip()
            version = ""
            author = str(item.get("author") or "").strip()
            native_score = item.get("score")
            if native_score is None:
                native_score = item.get("stars")
            category = str(item.get("category") or "").strip()
            updated_at = 0
        elif source == "clawhub":
            name = str(item.get("display_name") or item.get("slug") or "").strip()
            description = str(item.get("summary") or "").strip()
            identifier = str(item.get("slug") or "").strip()
            version = str(item.get("version") or "").strip()
            owner_handle = str(item.get("owner_handle") or "").strip()
            # ClawHub download needs ownerHandle when the same slug has multiple publishers.
            author = owner_handle
            native_score = item.get("score")
            category = ""
            updated_at = item.get("updated_at") or 0
        else:
            raise ValueError(f"unsupported online search source: {source}")

        normalized: dict[str, Any] = {
            "source": source,
            "name": name,
            "description": description,
            "identifier": identifier,
            "version": version,
            "author": author,
            "native_score": native_score,
            "category": category,
            "updated_at": updated_at,
            "source_rank": rank,
            "fusion_score": 1.0 / (_ONLINE_SEARCH_RRF_K + rank),
            "matched_sources": [
                {
                    "source": source,
                    "identifier": identifier,
                    "source_rank": rank,
                }
            ],
        }
        if source == "clawhub":
            normalized["owner_handle"] = owner_handle
            if owner_handle:
                normalized["matched_sources"][0]["owner_handle"] = owner_handle
        return normalized

    @classmethod
    def _aggregate_online_search_results(
        cls,
        query: str,
        source_results: dict[str, list[dict[str, Any]]],
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for source in sorted(source_results, key=lambda value: _ONLINE_SEARCH_SOURCE_ORDER.get(value, 99)):
            for rank, raw_item in enumerate(source_results[source], start=1):
                if not isinstance(raw_item, dict):
                    continue
                item = cls._normalize_online_search_item(source, raw_item, rank)
                if not item["identifier"] and not item["name"]:
                    continue
                identity_value = item["identifier"] or item["name"]
                if source == "clawhub":
                    owner_handle = str(item.get("owner_handle") or "").strip()
                    if owner_handle and identity_value:
                        # Keep ambiguous slugs from different publishers as distinct results.
                        identity_value = f"{owner_handle}/{identity_value}"
                identity = cls._normalize_online_search_identifier(source, identity_value)
                existing = merged.get(identity)
                if existing is None:
                    merged[identity] = item
                    continue
                existing["fusion_score"] += item["fusion_score"]
                existing["matched_sources"].extend(item["matched_sources"])
                existing["source_rank"] = min(existing["source_rank"], item["source_rank"])

        normalized_query = query.strip().casefold()
        items = list(merged.values())
        for item in items:
            item["exact_match"] = normalized_query in {
                str(item.get("name") or "").strip().casefold(),
                str(item.get("identifier") or "").strip().casefold(),
            }
            item["matched_source_count"] = len(
                {entry.get("source") for entry in item.get("matched_sources", []) if entry.get("source")}
            )

        items.sort(
            key=lambda item: (
                not bool(item.get("exact_match")),
                -float(item.get("fusion_score") or 0.0),
                -int(item.get("matched_source_count") or 0),
                int(item.get("source_rank") or 0),
                _ONLINE_SEARCH_SOURCE_ORDER.get(str(item.get("source") or ""), 99),
                str(item.get("identifier") or ""),
            )
        )
        return items[:limit]

    async def handle_skills_online_search(self, params: dict) -> dict:
        """Search the fixed online sources used by the Skills Online Search surface."""
        query = str(params.get("q", "")).strip()
        if not query:
            return {"success": False, "partial": False, "items": [], "sources": [], "detail": "缺少参数: q"}
        try:
            limit = int(params.get("limit", 20))
        except (TypeError, ValueError):
            return {
                "success": False,
                "partial": False,
                "items": [],
                "sources": [],
                "detail": "参数 limit 必须是整数",
            }
        limit = min(max(limit, 1), 50)
        calls: list[tuple[str, Awaitable[dict[str, Any]]]] = [
            (
                "skillnet",
                self.handle_skills_skillnet_search(
                    {"q": query, "limit": limit, "mode": "keyword"}
                ),
            )
        ]
        source_statuses: list[dict[str, Any]] = []
        if self._get_clawhub_token():
            calls.append(
                (
                    "clawhub",
                    self.handle_skills_clawhub_search({"q": query, "limit": limit}),
                )
            )
        else:
            source_statuses.append(
                {
                    "source": "clawhub",
                    "status": "skipped",
                    "count": 0,
                    "detail_key": "skills.clawhub.errors.tokenNotConfigured",
                }
            )

        payloads = await asyncio.gather(*(call for _, call in calls), return_exceptions=True)
        source_results: dict[str, list[dict[str, Any]]] = {}
        for (source, _), payload in zip(calls, payloads):
            if isinstance(payload, asyncio.CancelledError):
                raise payload
            if isinstance(payload, Exception):
                logger.error("在线技能聚合搜索失败: source=%s error=%s", source, payload)
                source_statuses.append(
                    {"source": source, "status": "error", "count": 0, "detail": str(payload)[:500]}
                )
                continue
            if not payload.get("success"):
                source_statuses.append(
                    {
                        "source": source,
                        "status": "error",
                        "count": 0,
                        "detail": str(payload.get("detail") or "")[:500],
                        "detail_key": payload.get("detail_key"),
                    }
                )
                continue
            skills = [item for item in payload.get("skills", []) if isinstance(item, dict)]
            source_results[source] = skills
            source_statuses.append({"source": source, "status": "success", "count": len(skills)})

        source_statuses.sort(key=lambda item: _ONLINE_SEARCH_SOURCE_ORDER.get(str(item.get("source") or ""), 99))
        any_success = any(item.get("status") == "success" for item in source_statuses)
        any_error = any(item.get("status") == "error" for item in source_statuses)
        return {
            "success": any_success,
            "partial": any_success and any_error,
            "query": query,
            "items": self._aggregate_online_search_results(query, source_results, limit),
            "sources": source_statuses,
        }

    async def handle_skills_skillnet_search(self, params: dict) -> dict:
        """在线搜索 SkillNet 技能."""
        query = str(params.get("q", "")).strip()
        if not query:
            return {"success": False, "detail": "缺少参数: q"}

        # 尽量与 SkillNet API 对齐，便于前端透传。
        search_kwargs: dict[str, Any] = {"q": query}

        mode = params.get("mode")
        if mode in ("keyword", "vector"):
            search_kwargs["mode"] = mode
        else:
            search_kwargs["mode"] = "keyword"

        if params.get("category"):
            search_kwargs["category"] = params.get("category")
        if params.get("limit") is not None:
            try:
                search_kwargs["limit"] = int(params.get("limit"))
            except Exception:
                return {"success": False, "detail": "参数 limit 必须是整数"}
        if params.get("page") is not None:
            try:
                search_kwargs["page"] = int(params.get("page"))
            except Exception:
                return {"success": False, "detail": "参数 page 必须是整数"}
        if params.get("min_stars") is not None:
            try:
                search_kwargs["min_stars"] = int(params.get("min_stars"))
            except Exception:
                return {"success": False, "detail": "参数 min_stars 必须是整数"}
        if params.get("sort_by"):
            search_kwargs["sort_by"] = params.get("sort_by")
        if params.get("threshold") is not None:
            try:
                search_kwargs["threshold"] = float(params.get("threshold"))
            except Exception:
                return {"success": False, "detail": "参数 threshold 必须是数字"}

        try:
            raw_results = await asyncio.to_thread(self._skillnet_search_sync, search_kwargs)
        except Exception as exc:
            logger.error("SkillNet 搜索失败: %s", exc)
            raw = str(exc).strip()
            if raw:
                return {"success": False, "detail": raw}
            return {
                "success": False,
                "detail": "搜索失败，请稍后重试。",
                "detail_key": "skills.skillNet.errors.searchFailedFallback",
            }

        normalized: list[dict[str, Any]] = []
        for item in raw_results:
            if hasattr(item, "dict"):
                try:
                    item = item.dict()
                except Exception:
                    item = vars(item)
            elif not isinstance(item, dict):
                item = vars(item)

            normalized.append(
                {
                    "skill_name": item.get("skill_name", item.get("name", "")),
                    "skill_description": item.get("skill_description", item.get("description", "")),
                    "author": item.get("author", ""),
                    "stars": item.get("stars", 0),
                    "skill_url": item.get("skill_url", item.get("url", "")),
                    "category": item.get("category", ""),
                }
            )

        return {
            "success": True,
            "query": query,
            "count": len(normalized),
            "skills": normalized,
        }

    async def handle_skills_skillnet_install(self, params: dict) -> dict:
        """从 SkillNet URL 异步安装：立即返回 install_id，不阻塞网关队列.

        前端应轮询 skills.skillnet.install_status 直至 status 为 done/failed。
        """
        skill_url = str(params.get("url", "")).strip()
        force = bool(params.get("force", False))
        if not skill_url:
            return {"success": False, "detail": "缺少参数: url"}

        mirror_url: str | None = None
        raw_mirror = params.get("mirror_url")
        if raw_mirror is not None:
            ms = str(raw_mirror).strip()
            if ms:
                if not _is_valid_http_mirror_url(ms):
                    return {
                        "success": False,
                        "detail": "mirror_url 不是有效的 http(s) 地址",
                        "detail_key": "skills.skillNet.errors.invalidMirrorUrl",
                    }
                mirror_url = ms

        install_id = uuid.uuid4().hex
        self._skillnet_install_jobs[install_id] = {"status": "pending"}
        asyncio.create_task(
            self._skillnet_install_background(install_id, skill_url, force, mirror_url),
            name=f"skillnet_install_{install_id[:8]}",
        )
        return {
            "success": True,
            "pending": True,
            "install_id": install_id,
        }

    async def handle_skills_skillnet_install_status(self, params: dict) -> dict:
        """查询 SkillNet 异步安装状态."""
        install_id = str(params.get("install_id", "")).strip()
        if not install_id:
            return {"success": False, "detail": "缺少参数: install_id"}
        job = self._skillnet_install_jobs.get(install_id)
        if job is None:
            return {
                "success": False,
                "detail": "安装会话已过期，请重新点击安装。",
                "detail_key": "skills.skillNet.errors.sessionExpired",
            }

        status = job.get("status", "pending")
        if status == "pending":
            return {"success": True, "status": "pending"}
        if status == "failed":
            out: dict[str, Any] = {
                "success": False,
                "status": "failed",
                "detail": job.get("detail", "安装失败"),
            }
            if "detail_key" in job:
                out["detail_key"] = job["detail_key"]
            if "detail_params" in job:
                out["detail_params"] = job["detail_params"]
            return out
        # done
        return {
            "success": True,
            "status": "done",
            "skill": job.get("skill"),
        }

    async def handle_skills_skillnet_evaluate(self, params: dict) -> dict:
        """使用 skillnet-ai 的 evaluate，LLM 配置复用 react.model_client_config + model_name."""
        skill_url = str(params.get("url", "")).strip()
        if not skill_url:
            return {"success": False, "detail": "缺少参数: url"}

        try:
            out = await asyncio.to_thread(SkillManager._skillnet_evaluate_sync, skill_url)
        except Exception as exc:
            logger.error("SkillNet 评估失败: %s", exc)
            raw = str(exc).strip()
            return {
                "success": False,
                "detail": raw or "评估失败，请稍后重试。",
                "detail_key": "skills.skillNet.errors.evaluateFailedFallback",
            }

        if not out.get("ok"):
            detail = str(out.get("detail", "")).strip() or "评估失败，请稍后重试。"
            resp: dict[str, Any] = {"success": False, "detail": detail}
            if out.get("detail_key"):
                resp["detail_key"] = out["detail_key"]
            return resp

        return {"success": True, "evaluation": out.get("evaluation")}

    async def handle_skills_clawhub_get_token(self, params: dict) -> dict:
        """获取 ClawHub CLI token（已掩码）."""
        token = self._get_clawhub_token()
        return {
            "success": True,
            "token": self._mask_clawhub_token(token),
            "has_token": bool(token),
        }

    async def handle_skills_clawhub_set_token(self, params: dict) -> dict:
        """设置 ClawHub CLI token."""
        token = str(params.get("token", "")).strip()
        self._set_clawhub_token(token)
        return {
            "success": True,
            "token": self._mask_clawhub_token(token),
        }

    async def handle_skills_clawhub_search(self, params: dict) -> dict:
        """从 ClawHub 搜索技能.

        params:
            q: 搜索查询字符串 (必需)
            limit: 结果数量限制 (可选)
        """
        query = str(params.get("q", "")).strip()
        if not query:
            return {"success": False, "detail": "缺少参数: q"}

        token = self._get_clawhub_token()
        if not token:
            return {
                "success": False,
                "detail": "未配置 ClawHub CLI token，请先配置",
                "detail_key": "skills.clawhub.errors.tokenNotConfigured",
            }

        limit = params.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except Exception:
                return {"success": False, "detail": "参数 limit 必须是整数"}

        try:
            base_url = "https://clawhub.ai"
            search_url = f"{base_url}/api/v1/search"

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            search_params = {"q": query}
            if limit is not None:
                search_params["limit"] = limit

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    search_url,
                    params=search_params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

                results = data.get("results", [])
                normalized = []
                for item in results:
                    normalized.append(
                        {
                            "slug": item.get("slug", ""),
                            "display_name": item.get("displayName", ""),
                            "summary": item.get("summary", ""),
                            "version": item.get("version", ""),
                            "updated_at": item.get("updatedAt", 0),
                            "owner_handle": item.get("ownerHandle", ""),
                        }
                    )

                return {
                    "success": True,
                    "query": query,
                    "count": len(normalized),
                    "skills": normalized,
                }
        except httpx.HTTPStatusError as exc:
            logger.error("ClawHub 搜索 HTTP 错误: %s", exc)
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            return {
                "success": False,
                "detail": detail,
                "detail_key": "skills.clawhub.errors.httpError",
            }
        except Exception as exc:
            logger.error("ClawHub 搜索失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.clawhub.errors.searchFailed",
            }

    async def handle_skills_clawhub_download(self, params: dict) -> dict:
        """从 ClawHub 下载技能.

        params:
            slug: skill slug (必需)
            owner_handle: 发布者标识 (可选，slug 有多个发布者时必需)
            display_name: 目录展示名 (可选；用于保留 ClawHub 大小写，避免安装后 Weather→weather)
            version: 版本号 (可选，默认 latest)
            tag: 标签 (可选，如 latest)
            force: 强制覆盖 (可选，默认 False)
        """
        slug = str(params.get("slug", "")).strip()
        force = bool(params.get("force", False))
        owner_handle = str(params.get("owner_handle") or "").strip()
        if not slug:
            return {"success": False, "detail": "缺少参数: slug"}
        try:
            slug = _safe_path_name(slug, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.clawhub.download", "skill", slug, exc)
            return {"success": False, "detail": str(exc)}

        token = self._get_clawhub_token()
        if not token:
            return {
                "success": False,
                "detail": "未配置 ClawHub CLI token，请先配置",
                "detail_key": "skills.clawhub.errors.tokenNotConfigured",
            }

        display_name = str(params.get("display_name") or "").strip()
        version = params.get("version")
        tag = params.get("tag")

        # 检查 skill 是否已安装
        dest = _safe_child_path(self._skills_dir, slug, "skill")
        if dest.exists() and not force:
            return {
                "success": False,
                "detail": f"技能 {slug} 已安装",
                "detail_key": "skills.clawhub.errors.skillAlreadyInstalled",
            }

        try:
            base_url = "https://clawhub.ai"
            download_url = f"{base_url}/api/v1/download"

            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            download_params = {"slug": slug}
            if owner_handle:
                download_params["ownerHandle"] = owner_handle
            if version:
                download_params["version"] = version
            if tag:
                download_params["tag"] = tag

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.get(
                    download_url,
                    params=download_params,
                    headers=headers,
                )
                response.raise_for_status()

                # 解压下载的内容
                with tempfile.TemporaryDirectory(prefix="jiuwenclari_clawhub_") as tmpdir:
                    tmp_path = Path(tmpdir)

                    self._safe_extract_zip_bytes_to_dir(response.content, tmp_path)

                    # 查找 skill 目录
                    skill_dir = self._locate_skill_dir(tmp_path)
                    if skill_dir is None:
                        return {
                            "success": False,
                            "detail": "下载内容不完整，未找到 SKILL.md",
                            "detail_key": "skills.clawhub.errors.skillMdNotFound",
                        }

                    # 解析元数据
                    md = self._try_find_skill_file(skill_dir)
                    meta = self._parse_skill_md(md) if md else None
                    if meta is None:
                        return {
                            "success": False,
                            "detail": "无法解析下载的技能文件",
                            "detail_key": "skills.clawhub.errors.parseSkillFailed",
                        }

                    # 删除已存在的
                    if dest.exists():
                        if not force:
                            return {
                                "success": False,
                                "detail": f"技能 {slug} 已安装",
                                "detail_key": "skills.clawhub.errors.skillAlreadyInstalled",
                            }
                        _safe_rmtree(dest)

                    # 复制到 skills 目录
                    shutil.copytree(skill_dir, dest)
                    for mirror_root in self._get_mirror_skills_dirs():
                        mirror_dest = _safe_child_path(mirror_root, slug, "skill")
                        if mirror_dest.exists():
                            if not force:
                                continue
                            _safe_rmtree(mirror_dest)
                        mirror_root.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(skill_dir, mirror_dest)

                    # skill_name 必须与磁盘扫描出的规范名（_resolve_skill_name）保持一致，
                    # 否则会被 _register_unmanaged_local_skills 当作"未登记的本地技能"
                    # 重复注册一条 source=local 的幽灵记录（表现为大小写不一致 + 重复条目）。
                    # 展示层面的大小写差异（如 Weather vs weather）改为通过 display_name 单独承载。
                    parsed_name = str(meta.get("name") or "").strip()
                    stem = md.stem if md else ""
                    if parsed_name and parsed_name != stem:
                        skill_name = parsed_name
                    else:
                        skill_name = slug
                    resolved_display_name = display_name if display_name else skill_name

                    self._add_local_skill(
                        {
                            "name": skill_name,
                            "display_name": resolved_display_name,
                            "origin": (
                                f"clawhub:{owner_handle}/{slug}"
                                if owner_handle
                                else f"clawhub:{slug}"
                            ),
                            "source": "clawhub",
                            "installed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    self._add_installed_plugin(
                        {
                            "name": skill_name,
                            "display_name": resolved_display_name,
                            "marketplace": "clawhub",
                            "version": meta.get("version", ""),
                            "commit": "",
                            "source": "clawhub",
                            # origin 与同处 _add_local_skill 一致，供身份键区分重名不同发布者
                            "origin": (
                                f"clawhub:{owner_handle}/{slug}"
                                if owner_handle
                                else f"clawhub:{slug}"
                            ),
                            "installed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    self._refresh_agent_data_indexes()
                    _safe_rmtree(skill_dir)
                    return {
                        "success": True,
                        "skill": {
                            "name": skill_name,
                            "display_name": resolved_display_name,
                            "source": "clawhub",
                        },
                    }

        except httpx.HTTPStatusError as exc:
            logger.error("ClawHub 下载 HTTP 错误: %s", exc)
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            return {
                "success": False,
                "detail": detail,
                "detail_key": "skills.clawhub.errors.httpError",
            }
        except Exception as exc:
            logger.error("ClawHub 下载失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.clawhub.errors.downloadFailed",
            }

    async def handle_skills_team_skills_hub_init(self, params: dict) -> dict:
        """初始化 TeamSkills 模板目录（最小可用脚手架）。"""
        name_raw = str(params.get("name") or "").strip()
        if not name_raw:
            return {"success": False, "detail": "缺少参数: name"}
        try:
            skill_name = _safe_path_name(name_raw, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.teamskillshub.init", "skill", name_raw, exc)
            return {"success": False, "detail": str(exc)}

        parent_raw = str(params.get("path") or ".").strip() or "."
        skill_type = str(
            params.get("skill_type")
            or params.get("plugin_type")
            or params.get("type")
            or "teamskills"
        ).strip().lower()
        if skill_type not in {"teamskills", "skill"}:
            return {"success": False, "detail": "type 仅支持: teamskills 或 skill"}
        force = bool(params.get("force", False))
        # 安全：path 仅允许为 skills 目录内的相对路径，禁止绝对路径与目录穿越。
        # skill_name 已由 _safe_path_name 校验，此处对父目录 path 收敛边界，
        # 避免 path 传入绝对路径（含 ~ 展开后的家目录）而在任意目录创建子目录
        # 并写入固定模板 SKILL.md。
        if parent_raw == "~" or parent_raw.startswith(("~/", "~\\")):
            return {
                "success": False,
                "detail": "path 仅支持相对路径（限制在 skills 目录内）",
            }
        parent_path_raw = Path(parent_raw)
        if parent_path_raw.is_absolute() or PureWindowsPath(parent_raw).is_absolute():
            return {
                "success": False,
                "detail": "path 仅支持相对路径（限制在 skills 目录内）",
            }
        skills_root = self._skills_dir.resolve()
        try:
            parent_dir = (self._skills_dir / parent_raw).resolve()
        except (ValueError, OSError) as exc:
            return {"success": False, "detail": f"path 无效: {exc}"}
        try:
            parent_dir.relative_to(skills_root)
        except ValueError:
            return {"success": False, "detail": "path 越界：仅允许在 skills 目录内创建"}
        if not parent_dir.exists() or not parent_dir.is_dir():
            return {"success": False, "detail": f"path 不是有效目录: {parent_dir}"}

        # target_dir 双保险：skill_name 已校验，仍确认最终落点不逃逸 skills 目录。
        target_dir = (parent_dir / skill_name).resolve()
        try:
            target_dir.relative_to(skills_root)
        except ValueError:
            return {"success": False, "detail": "目标路径越界：仅允许在 skills 目录内创建"}
        try:
            if target_dir.exists():
                if not target_dir.is_dir():
                    return {"success": False, "detail": f"目标路径不是目录: {target_dir}"}
                if any(target_dir.iterdir()):
                    if not force:
                        return {"success": False, "detail": f"目标目录非空: {target_dir}"}
                    _safe_rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            skill_file = target_dir / "SKILL.md"
            if skill_file.exists() and not force:
                return {"success": False, "detail": f"文件已存在: {skill_file}"}
            if skill_type == "teamskills":
                content = (
                    "---\n"
                    f"name: {skill_name}\n"
                    'description: "TODO: describe this team skill."\n'
                    "kind: team-skill\n"
                    "roles:\n"
                    "  - id: role_01\n"
                    "    purpose: role_01_purpose\n"
                    "  - id: role_02\n"
                    "    purpose: role_02_purpose\n"
                    "---\n\n"
                    f"# {skill_name}\n\n"
                    "## Instructions\n\n"
                    "TODO: add step-by-step guidance.\n"
                )
            else:
                content = (
                    "---\n"
                    f"name: {skill_name}\n"
                    'description: "TODO: describe this skill."\n'
                    "---\n\n"
                    f"# {skill_name}\n\n"
                    "## Instructions\n\n"
                    "TODO: add step-by-step guidance.\n"
                )

            skill_file.write_text(content, encoding="utf-8")
            self._refresh_agent_data_indexes()
            return {"success": True, "path": str(target_dir)}
        except Exception as exc:
            logger.error("Team Skills Hub init 失败: %s", exc)
            return {"success": False, "detail": str(exc)[:500]}

    async def handle_skills_team_skills_hub_validate(self, params: dict) -> dict:
        """校验 TeamSkills 目录结构与 SKILL.md 内容。"""
        path_raw = str(params.get("path") or "").strip()
        if not path_raw:
            return {"success": False, "detail": "缺少参数: path"}
        skill_root = Path(path_raw).expanduser().resolve()
        if not skill_root.exists() or not skill_root.is_dir():
            return {"success": False, "detail": f"path 不是有效目录: {skill_root}"}
        skill_md = self._try_find_skill_file(skill_root)
        if skill_md is None:
            return {"success": False, "detail": f"目录中未找到 SKILL.md: {skill_root}"}
        meta = self._parse_skill_md(skill_md)
        if meta is None:
            return {"success": False, "detail": f"无法解析 SKILL.md: {skill_md}"}

        skill_type = str(
            params.get("skill_type") or params.get("plugin_type") or params.get("type") or ""
        ).strip().lower()
        if skill_type not in {"teamskills", "skill"}:
            skill_type = "teamskills" if str(meta.get("kind", "")).strip() == "team-skill" else "skill"

        errors: list[str] = []
        if skill_type == "teamskills":
            roles = meta.get("roles", [])
            if not isinstance(roles, list) or not roles:
                errors.append("frontmatter `roles` must be a non-empty list")
            else:
                role_ids: list[str] = []
                for i, role in enumerate(roles):
                    if not isinstance(role, dict):
                        errors.append(f"roles[{i}] must be an object")
                        continue
                    if "id" not in role:
                        errors.append(f"roles[{i}] missing required field `id`")
                        continue
                    role_id = role.get("id")
                    if not isinstance(role_id, str) or not role_id.strip():
                        errors.append(f"roles[{i}] `id` must be a non-empty string")
                    else:
                        role_ids.append(role_id.strip())

                if len(role_ids) < 2:
                    errors.append(
                        "frontmatter `roles` must list at least 2 entries "
                        "with valid `id` (team-skill multi-role contract)."
                    )
                elif len(role_ids) != len(set(role_ids)):
                    errors.append("frontmatter `roles` must not repeat the same `id`")

        if errors:
            return {
                "success": False,
                "detail": "TeamSkills roles 校验失败" if skill_type == "teamskills" else "校验失败",
                "errors": errors,
            }

        return {
            "success": True,
            "path": str(skill_root),
            "skill_file": str(skill_md),
            "skill_type": skill_type,
            "name": str(meta.get("name", "")).strip(),
            "warnings": [],
        }

    async def handle_skills_team_skills_hub_pack(self, params: dict) -> dict:
        """将 TeamSkills 目录打包为 zip。"""
        path_raw = str(params.get("path") or "").strip()
        if not path_raw:
            return {"success": False, "detail": "缺少参数: path"}
        skill_root = Path(path_raw).expanduser().resolve()
        if not skill_root.exists() or not skill_root.is_dir():
            return {"success": False, "detail": f"path 不是有效目录: {skill_root}"}
        skill_md = self._try_find_skill_file(skill_root)
        if skill_md is None:
            return {"success": False, "detail": f"目录中未找到 SKILL.md: {skill_root}"}

        output_raw = str(params.get("output") or "out").strip() or "out"
        output_path = Path(output_raw).expanduser()
        if output_path.is_absolute():
            out_dir = output_path.resolve()
        else:
            out_dir = (skill_root / output_path).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        zip_path = out_dir / f"{skill_root.name}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for child in skill_root.rglob("*"):
                    if not child.is_file():
                        continue
                    rel = child.relative_to(skill_root).as_posix()
                    zf.write(child, arcname=rel)
            return {"success": True, "path": str(zip_path)}
        except Exception as exc:
            logger.error("Team Skills Hub pack 失败: %s", exc)
            return {"success": False, "detail": str(exc)[:500]}

    @staticmethod
    def _source_error(code: str, message: str) -> dict[str, Any]:
        return {
            "success": False,
            "error_code": code,
            "error_message": str(message)[:500],
        }

    @staticmethod
    def _source_context(params: dict[str, Any]) -> ProviderInvocationContext:
        metadata = params.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        return ProviderInvocationContext(
            request_id=str(params.get("request_id") or uuid.uuid4()),
            trace_id=str(metadata.get("trace_id") or "").strip() or None,
        )

    async def handle_skills_source_providers(self, params: dict) -> dict:
        """List configured sources without exposing endpoint or credentials."""
        del params
        return {
            "success": True,
            "providers": [item.to_dict() for item in self._source_registry.list()],
        }

    async def handle_skills_source_search(self, params: dict) -> dict:
        """Dispatch a normalized search request to one Skill Source Provider."""
        source_id = str(params.get("source_id") or "").strip()
        if not source_id:
            return self._source_error("missing_params", "source_id is required")
        try:
            page = int(params.get("page", 1))
            page_size = int(params.get("page_size", 20))
        except (TypeError, ValueError):
            return self._source_error("invalid_params", "page and page_size must be integers")
        if page < 1 or not 1 <= page_size <= 100:
            return self._source_error("invalid_params", "page must be >= 1 and page_size must be 1..100")
        filters = params.get("filter", {})
        if filters is None:
            filters = {}
        if not isinstance(filters, dict):
            return self._source_error("invalid_params", "filter must be an object")
        blocked_filter_keys = {
            "token", "authorization", "password", "secret", "signature",
            "download_url", "user_id", "group_id", "bot_id", "service_id", "agent_id",
        }
        if any(str(key).strip().lower() in blocked_filter_keys for key in filters):
            return self._source_error("invalid_params", "filter contains a protected field")

        try:
            provider = await self._source_registry.get(source_id, "search")
            result = await provider.search(
                SkillSearchRequest(
                    q=str(params.get("q") or "").strip(),
                    page=page,
                    page_size=page_size,
                    filters=filters,
                ),
                self._source_context(params),
            )
            items: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for candidate in result.items:
                if candidate.source_id != source_id:
                    raise SourceRegistryError(
                        "source_response_invalid", "provider returned a different source_id"
                    )
                if not candidate.skill_id or not candidate.version_id or not candidate.name:
                    raise SourceRegistryError(
                        "source_response_invalid", "provider returned an incomplete SkillCandidate"
                    )
                identity = (candidate.skill_id, candidate.version_id)
                if identity in seen:
                    raise SourceRegistryError(
                        "source_response_invalid", "provider returned a duplicate artifact"
                    )
                seen.add(identity)
                items.append(candidate.to_dict())
            return {
                "success": True,
                "source": source_id,
                "source_id": source_id,
                "items": items,
                "skills": items,
                "total": result.total,
                "page": page,
                "page_size": page_size,
                "count": len(items),
                "next_page": result.next_page,
            }
        except SourceRegistryError as exc:
            return self._source_error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skill Source search failed: source_id=%s error=%s", source_id, exc)
            return self._source_error("source_unavailable", str(exc))

    def _find_installation_by_skill_ref(
        self,
        source_id: str,
        skill_id: str,
    ) -> dict[str, Any] | None:
        for record in self._get_installed_plugins():
            if not isinstance(record, dict):
                continue
            if (
                str(record.get("source_id") or "").strip() == source_id
                and str(record.get("skill_id") or "").strip() == skill_id
            ):
                return record
        return None

    @staticmethod
    def _validate_source_artifact(
        descriptor: ArtifactDescriptor,
        *,
        source_id: str,
        skill_id: str,
        version_id: str,
    ) -> None:
        artifact_ref = descriptor.artifact_ref
        if (
            artifact_ref.skill_ref.source_id != source_id
            or artifact_ref.skill_ref.skill_id != skill_id
            or artifact_ref.version_id != version_id
        ):
            raise SourceRegistryError(
                "source_response_invalid", "provider returned a different ArtifactRef"
            )
        checksum = str(descriptor.checksum_sha256 or "").strip().lower()
        artifact_sha = str(descriptor.artifact_sha256 or "").strip().lower()
        for value in (checksum, artifact_sha):
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise SourceRegistryError("source_response_invalid", "invalid artifact SHA-256")

    async def _fetch_verified_source_artifact(
        self,
        *,
        source_id: str,
        skill_id: str,
        version_id: str,
        params: dict[str, Any],
    ) -> tuple[ArtifactDescriptor, bytes, dict[str, Any] | None]:
        """Fetch and verify one exact artifact for both install and update."""
        provider = await self._source_registry.get(source_id, "get_artifact")
        descriptor = await provider.get_artifact(
            SkillRef(source_id=source_id, skill_id=skill_id),
            version_id,
            self._source_context(params),
        )
        self._validate_source_artifact(
            descriptor,
            source_id=source_id,
            skill_id=skill_id,
            version_id=version_id,
        )
        config = self._source_registry.get_config(source_id)
        download_policy = config.download_policy
        allowed_hosts = tuple(download_policy.allowed_hosts) if download_policy else ()
        if not allowed_hosts:
            # SPI 来源 fail-closed：经扩展工厂/管理面注册的来源必须持有显式下载
            # 主机白名单（管理面 download_policy 或 Provider 声明的默认白名单），
            # 空白名单直接拒绝，不回退 legacy 白名单（import_local/teamskillshub
            # 内置链路行为不变）。
            raise SourceRegistryError(
                "source_misconfigured",
                f"skill source {source_id} 未配置 download_policy.allowed_hosts，拒绝下载",
            )
        self._assert_skill_download_url_allowed(
            descriptor.download_url,
            allowed_hosts=allowed_hosts,
        )
        body = await provider.download_artifact(descriptor, download_policy)
        if descriptor.content_length is not None and len(body) != descriptor.content_length:
            raise SourceRegistryError(
                "artifact_download_failed", "artifact content length does not match"
            )
        try:
            verification = await provider.verify_artifact(descriptor, body)
        except ArtifactVerificationError as exc:
            raise SourceRegistryError(exc.code, str(exc)) from exc
        return descriptor, body, dict(verification) if verification is not None else None

    @_state_transactional
    def _commit_source_skill_entity(
        self,
        skill_dir: Path,
        *,
        skill_name: str,
        source_id: str,
        skill_id: str,
        version_id: str,
        version: str,
        force: bool,
        fingerprint: str | None = None,
        verification: dict[str, Any] | None = None,
        market_display_name: str = "",
        author: str = "",
        source_type: str = "user",
    ) -> dict[str, Any]:
        """Atomically replace the entity, then commit the JSON installation record."""
        normalized_source_type = str(source_type or "user").strip() or "user"
        if normalized_source_type not in {"user", "prebuilt"}:
            raise ValueError(f"invalid source_type for source install: {source_type}")
        existing_ref = self._find_installation_by_skill_ref(source_id, skill_id)
        existing_name = str((existing_ref or {}).get("name") or "").strip()
        if existing_ref is not None and not force:
            raise SourceRegistryError(
                "skill_already_installed",
                f"skill {existing_name or skill_name} is already installed from {source_id}",
            )
        name_conflict = self._find_skill_installation(name=skill_name)
        if name_conflict is not None and name_conflict is not existing_ref:
            raise SourceRegistryError(
                "skill_name_conflict", f"skill name already installed: {skill_name}"
            )
        entity_dir = str((existing_ref or {}).get("entity_dir") or existing_name or skill_name).strip()
        dest = _safe_child_path(self._skills_dir, entity_dir, "skill")
        if dest.exists() and existing_ref is None:
            raise SourceRegistryError(
                "skill_name_conflict", f"skill directory already exists: {skill_name}"
            )
        staging = _safe_child_path(
            self._skills_dir,
            f"_source_staging_{uuid.uuid4().hex}",
            "skill staging",
        )
        backup = _safe_child_path(
            self._skills_dir,
            f"_source_backup_{uuid.uuid4().hex}",
            "skill backup",
        )
        shutil.copytree(skill_dir, staging)
        moved_old = False
        try:
            if dest.exists():
                dest.rename(backup)
                moved_old = True
            staging.rename(dest)
            record = self.record_skill_installation(
                name=skill_name,
                source_type=normalized_source_type,
                source=source_id,
                origin=f"{source_id}:{skill_id}",
                version=version,
                source_id=source_id,
                skill_id=skill_id,
                version_id=version_id,
                fingerprint=fingerprint,
                verification=verification,
                entity_dir=entity_dir,
                market_display_name=market_display_name,
                author=author,
                replace_by_name=normalized_source_type == "prebuilt",
            )
        except Exception:
            if dest.exists():
                _safe_rmtree(dest)
            if moved_old and backup.exists():
                backup.rename(dest)
            raise
        finally:
            if staging.exists():
                _safe_rmtree(staging)
        if backup.exists():
            _safe_rmtree(backup)
        self._refresh_agent_data_indexes()
        return self._skill_installation_dto(record)

    async def install_prebuilt_from_provider(
        self,
        *,
        source_id: str,
        skill_id: str,
        version_id: str,
        force: bool = True,
    ) -> dict[str, Any]:
        """Install one Provider artifact as ``source_type=prebuilt`` (enterprise reconcile)."""
        source_id = str(source_id or "").strip()
        skill_id = str(skill_id or "").strip()
        version_id = str(version_id or "").strip()
        if not source_id or not skill_id or not version_id:
            return {
                "ok": False,
                "error_code": "missing_params",
                "detail": "source_id, skill_id and version_id are required",
            }
        try:
            descriptor, body, verification = await self._fetch_verified_source_artifact(
                source_id=source_id,
                skill_id=skill_id,
                version_id=version_id,
                params={},
            )
            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_prebuilt_provider_") as tmpdir:
                tmp_path = Path(tmpdir)
                self._safe_extract_zip_bytes_to_dir(body, tmp_path)
                skill_dir = self._locate_skill_dir(tmp_path)
                if skill_dir is None:
                    raise SourceRegistryError(
                        "invalid_package", "downloaded content is missing SKILL.md"
                    )
                skill_md = self._try_find_skill_file(skill_dir)
                metadata = self._parse_skill_md(skill_md) if skill_md is not None else None
                if not isinstance(metadata, dict):
                    raise SourceRegistryError("invalid_package", "SKILL.md cannot be parsed")
                skill_name = _safe_path_name(
                    str(metadata.get("name") or skill_dir.name), "skill"
                )
                version = str(
                    metadata.get("version")
                    or descriptor.metadata.get("version")
                    or version_id
                ).strip()
                record = await asyncio.to_thread(
                    self._commit_source_skill_entity,
                    skill_dir,
                    skill_name=skill_name,
                    source_id=source_id,
                    skill_id=skill_id,
                    version_id=version_id,
                    version=version,
                    force=force,
                    fingerprint=descriptor.fingerprint,
                    verification=verification,
                    source_type="prebuilt",
                )
            return {
                "ok": True,
                "skill_name": str(record.get("name") or skill_name).strip(),
                "row": record,
                "version": version,
            }
        except SourceRegistryError as exc:
            return {"ok": False, "error_code": exc.code, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prebuilt provider install failed: source_id=%s skill_id=%s version_id=%s error=%s",
                source_id,
                skill_id,
                version_id,
                exc,
            )
            return {"ok": False, "error_code": "download_failed", "detail": str(exc)}

    async def handle_skills_source_install(self, params: dict) -> dict:
        """Install one exact Provider artifact through the common transaction."""
        source_id = str(params.get("source_id") or "").strip()
        skill_id = str(params.get("skill_id") or "").strip()
        version_id = str(params.get("version_id") or "").strip()
        if not source_id or not skill_id or not version_id:
            return self._source_error(
                "missing_params", "source_id, skill_id and version_id are required"
            )
        force = bool(params.get("force", False))
        # 市场展示名（技能广场卡片名）透传落盘，让「我的技能」与技能广场同名显示
        market_display_name = str(params.get("display_name") or "").strip()[:200]
        market_version = str(params.get("version") or "").strip()[:100]
        market_author = str(params.get("author") or "").strip()[:200]
        try:
            descriptor, body, verification = await self._fetch_verified_source_artifact(
                source_id=source_id,
                skill_id=skill_id,
                version_id=version_id,
                params=params,
            )
            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_source_install_") as tmpdir:
                tmp_path = Path(tmpdir)
                self._safe_extract_zip_bytes_to_dir(body, tmp_path)
                skill_dir = self._locate_skill_dir(tmp_path)
                if skill_dir is None:
                    raise SourceRegistryError(
                        "invalid_package", "downloaded content is missing SKILL.md"
                    )
                skill_md = self._try_find_skill_file(skill_dir)
                metadata = self._parse_skill_md(skill_md) if skill_md is not None else None
                if not isinstance(metadata, dict):
                    raise SourceRegistryError("invalid_package", "SKILL.md cannot be parsed")
                skill_name = _safe_path_name(
                    str(metadata.get("name") or skill_dir.name), "skill"
                )
                descriptor_metadata = dict(descriptor.metadata or {})
                version = str(
                    descriptor_metadata.get("version")
                    or market_version
                    or metadata.get("version")
                    or version_id
                ).strip()
                author = str(
                    descriptor_metadata.get("author")
                    or descriptor_metadata.get("publisher_name")
                    or descriptor_metadata.get("owner_display_name")
                    or market_author
                ).strip()[:200]
                record = await asyncio.to_thread(
                    self._commit_source_skill_entity,
                    skill_dir,
                    skill_name=skill_name,
                    source_id=source_id,
                    skill_id=skill_id,
                    version_id=version_id,
                    version=version,
                    force=force,
                    fingerprint=descriptor.fingerprint,
                    verification=verification,
                    market_display_name=market_display_name,
                    author=author,
                )
            skill_payload: dict[str, Any] = {}
            for key in (
                "installation_id", "name", "declared_name", "source_type",
                "source_id", "skill_id", "version_id", "version", "enabled",
                "installed", "removable", "consistency", "author",
            ):
                value = record.get(key)
                if value is not None:
                    skill_payload[key] = value
            return {"success": True, "skill": skill_payload}
        except SourceRegistryError as exc:
            return self._source_error(exc.code, str(exc))
        except ValueError as exc:
            return self._source_error("invalid_package", str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Skill Source install failed: source_id=%s skill_id=%s version_id=%s error=%s",
                source_id,
                skill_id,
                version_id,
                exc,
            )
            return self._source_error("source_install_failed", str(exc))

    async def handle_skills_update(self, params: dict) -> dict:
        """Update an installed SkillRef through the same verified install transaction."""
        source_id = str(params.get("source_id") or "").strip()
        skill_id = str(params.get("skill_id") or "").strip()
        target_version_id = str(
            params.get("target_version_id") or params.get("version_id") or ""
        ).strip()
        if not source_id or not skill_id or not target_version_id:
            return self._source_error(
                "missing_params",
                "source_id, skill_id and target_version_id are required",
            )
        existing = self._find_installation_by_skill_ref(source_id, skill_id)
        if existing is None:
            return self._source_error("skill_not_found", "installed SkillRef was not found")
        current_version_id = str(existing.get("version_id") or "").strip()
        expected = str(params.get("expected_current_version_id") or "").strip()
        if expected and expected != current_version_id:
            return self._source_error(
                "skill_version_conflict",
                f"expected {expected}, current version is {current_version_id}",
            )
        if target_version_id == current_version_id:
            return {"success": True, "skill": self._skill_installation_dto(existing)}
        install_params = dict(params)
        install_params.update(
            {
                "source_id": source_id,
                "skill_id": skill_id,
                "version_id": target_version_id,
                "force": True,
            }
        )
        return await self.handle_skills_source_install(install_params)

    async def handle_skills_updates_check(self, params: dict) -> dict:
        """Check configured Providers and cache only non-sensitive update status."""
        raw_source_ids = params.get("source_ids")
        if raw_source_ids is not None and not isinstance(raw_source_ids, list):
            return self._source_error("invalid_params", "source_ids must be an array")
        requested_sources = {
            str(value).strip()
            for value in (raw_source_ids or [])
            if str(value or "").strip()
        }
        legacy_source_id = str(params.get("source_id") or "").strip()
        if legacy_source_id:
            requested_sources.add(legacy_source_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self._get_installed_plugins():
            if not isinstance(record, dict):
                continue
            source_id = str(record.get("source_id") or "").strip()
            skill_id = str(record.get("skill_id") or "").strip()
            version_id = str(record.get("version_id") or "").strip()
            if not source_id or not skill_id or not version_id:
                continue
            if requested_sources and source_id not in requested_sources:
                continue
            grouped.setdefault(source_id, []).append(record)

        items: list[dict[str, Any]] = []
        pending_updates: dict[str, dict[str, Any]] = {}
        checked_at = datetime.now(timezone.utc).isoformat()
        for source_id, records in grouped.items():
            try:
                provider = await self._source_registry.get(source_id, "check_updates")
                installed = tuple(
                    InstalledArtifact(
                        source_id=source_id,
                        skill_id=str(record.get("skill_id")),
                        version_id=str(record.get("version_id")),
                        version=str(record.get("version") or "") or None,
                        fingerprint=str(record.get("fingerprint") or "") or None,
                    )
                    for record in records
                )
                statuses = await provider.check_updates(
                    installed,
                    self._source_context(params),
                )
                by_ref = {
                    (status.skill_ref.source_id, status.skill_ref.skill_id): status
                    for status in statuses
                }
                for record in records:
                    identity = (source_id, str(record.get("skill_id")))
                    status = by_ref.get(identity)
                    if status is None:
                        raise SourceRegistryError(
                            "source_response_invalid",
                            "provider omitted an installed SkillRef",
                        )
                    public_status = {
                        "installation_id": record.get("installation_id"),
                        "name": record.get("name"),
                        "source_id": source_id,
                        "skill_id": status.skill_ref.skill_id,
                        "current_version_id": status.current_version_id,
                        "current_version": record.get("version"),
                        "latest_version_id": status.latest_version_id,
                        "latest_version": status.latest_version,
                        "has_update": bool(status.has_update),
                        "fingerprint_matched": status.fingerprint_matched,
                        "accessible": status.accessible,
                        "downloadable": status.downloadable,
                        "remote_status": status.remote_status,
                        "error_code": status.error_code,
                        "checked_at": checked_at,
                    }
                    items.append(
                        {key: value for key, value in public_status.items() if value is not None}
                    )
                    installation_id = str(
                        record.get("installation_id") or ""
                    ).strip()
                    if installation_id:
                        pending_updates[installation_id] = {
                            "updatable": bool(status.has_update),
                            "latest_version_id": status.latest_version_id,
                            "latest_version": status.latest_version,
                            "fingerprint_matched": status.fingerprint_matched,
                            "accessible": status.accessible,
                            "downloadable": status.downloadable,
                            "remote_status": status.remote_status,
                            "update_checked_at": checked_at,
                        }
            except Exception as exc:  # noqa: BLE001
                code = exc.code if isinstance(exc, SourceRegistryError) else "update_check_failed"
                logger.warning("Skill update check failed: source_id=%s error=%s", source_id, exc)
                for record in records:
                    items.append(
                        {
                            "installation_id": record.get("installation_id"),
                            "name": record.get("name"),
                            "source_id": source_id,
                            "skill_id": record.get("skill_id"),
                            "current_version_id": record.get("version_id"),
                            "has_update": False,
                            "remote_status": "unknown",
                            "error_code": code,
                            "checked_at": checked_at,
                        }
                    )
        await asyncio.to_thread(
            self._commit_skill_update_statuses,
            pending_updates,
        )
        return {"success": True, "items": items, "count": len(items)}

    async def handle_skills_team_skills_hub_info(self, params: dict) -> dict:
        """查询 Team Skills Hub 技能版本详情（/api/v1/artifacts/{asset_id}）。"""
        asset_id = str(params.get("asset_id", "")).strip()
        if not asset_id:
            return {"success": False, "detail": "缺少参数: asset_id"}
        version = str(params.get("version", "")).strip()
        if not version:
            return {"success": False, "detail": "缺少参数: version"}
        base_url = self._get_team_skills_hub_base_url(str(params.get("market_url") or "").strip() or None)
        try:
            detail = await self._team_skills_hub_http_get_data(
                f"/api/v1/artifacts/{asset_id}",
                params={"version": version},
                timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
                base_url=base_url,
            )
            if not isinstance(detail, dict):
                return {"success": False, "detail": "marketplace 返回数据格式错误"}
            return {"success": True, "asset_id": asset_id, "version": version, "data": detail}
        except Exception as exc:
            logger.error("Team Skills Hub 详情查询失败: %s", exc)
            return {"success": False, "detail": str(exc)[:500]}

    async def handle_skills_team_skills_hub_search(self, params: dict) -> dict:
        """从 Team Skills Hub 搜索技能（/api/v1/plugins）。"""
        query = str(params.get("q", "")).strip()

        page_size_raw = params.get("page_size", params.get("limit", 20))
        try:
            page_size = max(1, min(int(page_size_raw), 100))
        except Exception:
            return {"success": False, "detail": "参数 page_size 必须是整数"}

        page = params.get("page", 1)
        try:
            page = max(1, int(page))
        except Exception:
            return {"success": False, "detail": "参数 page 必须是整数"}

        skill_type = str(params.get("skill_type") or params.get("plugin_type") or "").strip()
        author = str(params.get("author", "")).strip()
        search_asset_id = str(params.get("search_asset_id", "")).strip()
        search_asset_type = str(params.get("search_asset_type", "")).strip()
        search_publisher_id = str(params.get("search_publisher_id", "")).strip()
        order_by = str(params.get("order_by", "install_count")).strip() or "install_count"
        desc_raw = params.get("desc", True)
        desc = str(desc_raw).strip().lower() in {"1", "true", "yes", "on"}
        base_url = self._get_team_skills_hub_base_url(str(params.get("market_url") or "").strip() or None)

        try:
            query_params: dict[str, Any] = {
                "page": page,
                "page_size": page_size,
                "order_by": order_by,
                "desc": str(desc).lower(),
            }
            if query:
                query_params["search_keyword"] = query
            if skill_type:
                query_params["plugin_type"] = skill_type
            if author:
                query_params["publisher_name"] = author
            if search_asset_id:
                query_params["asset_id"] = search_asset_id
            if search_asset_type:
                query_params["asset_type"] = search_asset_type
            if search_publisher_id:
                query_params["publisher_id"] = search_publisher_id
            data = await self._team_skills_hub_http_get_data(
                "/api/v1/plugins",
                params=query_params,
                timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
                base_url=base_url,
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            normalized: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                asset_id = str(item.get("asset_id", "")).strip()
                name = str(item.get("name", "")).strip() or asset_id
                normalized.append(
                    {
                        "asset_id": asset_id,
                        "name": name,
                        "display_name": str(item.get("display_name", "")).strip() or name,
                        "summary": str(item.get("short_desc", "")).strip(),
                        "version": str(item.get("latest_version", "")).strip(),
                        "updated_at": int(item.get("update_time") or 0),
                    }
                )
            return {
                "success": True,
                "query": query,
                "count": len(normalized),
                "skills": normalized,
            }
        except Exception as exc:
            logger.error("Team Skills Hub 搜索失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.teamskillshub.errors.searchFailed",
            }

    async def handle_skills_team_skills_hub_install(self, params: dict) -> dict:
        """从 Team Skills Hub 安装技能（/api/v1/artifacts/{asset_id}）。"""
        asset_id = str(params.get("asset_id", "")).strip()
        if not asset_id:
            return {"success": False, "detail": "缺少参数: asset_id"}

        force = bool(params.get("force", False))
        version = params.get("version")
        version_str = str(version).strip() if version is not None else ""

        # 前端在搜索结果里已拿到市场的展示文案，随 install 请求一起传入，落盘后
        # 供「我的技能」页显示与搜索页一致的描述（short_desc 字段名对齐市场原始字段，
        # 前端 TeamSkillsHubSkillItem 里它叫 summary）。缺省时回退 SKILL.md description。
        market_short_desc = str(params.get("short_desc", "") or "").strip()[:500]
        market_display_name = str(params.get("display_name", "") or "").strip()[:200]

        try:
            base_url = self._get_team_skills_hub_base_url(str(params.get("market_url") or "").strip() or None)
            artifact_data = await self._team_skills_hub_http_get_data(
                f"/api/v1/artifacts/{asset_id}",
                params={"version": version_str} if version_str else None,
                timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
                base_url=base_url,
            )
            if not isinstance(artifact_data, dict):
                return {"success": False, "detail": "marketplace 返回数据格式错误"}

            download_url = str(artifact_data.get("download_url", "")).strip()
            if not download_url:
                return {"success": False, "detail": "marketplace 未返回 download_url"}
            self._assert_team_skills_hub_download_url_allowed(download_url)
            checksum_sha256 = str(artifact_data.get("checksum_sha256", "")).strip()

            artifact_bytes = await self._download_zip_and_verify(download_url, checksum_sha256=checksum_sha256)

            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_team_skills_hub_") as tmpdir:
                tmp_path = Path(tmpdir)
                # 与 ClawHub 一致：从内存解压，避免在临时目录写入 skill.zip（扁平包时 copytree 曾误拷入安装目录）。
                self._safe_extract_zip_bytes_to_dir(artifact_bytes, tmp_path)

                skill_dir = self._locate_skill_dir(tmp_path)
                if skill_dir is None:
                    return {"success": False, "detail": "下载内容不完整，未找到 SKILL.md"}

                md = self._try_find_skill_file(skill_dir)
                meta = self._parse_skill_md(md) if md else None
                if meta is None:
                    return {"success": False, "detail": "无法解析下载的技能文件"}

                skill_name = str(meta.get("name", "")).strip() or asset_id
                output_raw = str(params.get("output", "")).strip()
                use_custom_output = bool(output_raw)
                install_root = Path(output_raw).expanduser().resolve() if use_custom_output else self._skills_dir
                install_root.mkdir(parents=True, exist_ok=True)
                dest = install_root / skill_name
                if dest.exists():
                    if not force:
                        return {
                            "success": False,
                            "detail": f"技能 {skill_name} 已安装",
                            "detail_key": "skills.teamskillshub.errors.skillAlreadyInstalled",
                        }
                    _safe_rmtree(dest)

                shutil.copytree(skill_dir, dest)
                if use_custom_output:
                    return {
                        "success": True,
                        "skill": {
                            "name": skill_name,
                            "source": "teamskillshub",
                            "asset_id": asset_id,
                            "path": str(dest),
                        },
                    }
                for mirror_root in self._get_mirror_skills_dirs():
                    mirror_dest = mirror_root / skill_name
                    if mirror_dest.exists():
                        if not force:
                            continue
                        _safe_rmtree(mirror_dest)
                    mirror_root.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(skill_dir, mirror_dest)

                installed_at = datetime.now(timezone.utc).isoformat()
                # force 覆盖时：清理同 name 同 source 的旧 origin 记录，避免 asset_id 不同导致残留
                if force:
                    for ls in list(self._state.get("local_skills", [])):
                        if (
                            isinstance(ls, dict)
                            and ls.get("name") == skill_name
                            and ls.get("source") == "teamskillshub"
                        ):
                            old_origin = str(ls.get("origin", "") or "").strip()
                            if old_origin:
                                self._remove_local_skill(skill_name, origin=old_origin)
                    for p in list(self._state.get("installed_plugins", [])):
                        if isinstance(p, dict) and p.get("name") == skill_name and p.get("source") == "teamskillshub":
                            old_origin = str(p.get("origin", "") or "").strip()
                            if old_origin:
                                self._remove_installed_plugin(skill_name, origin=old_origin)

                self._add_local_skill(
                    {
                        "name": skill_name,
                        "origin": f"teamskillshub:{asset_id}",
                        "source": "teamskillshub",
                        "installed_at": installed_at,
                        # 市场展示文案落盘：供「我的技能」页显示与搜索页一致的描述
                        "market_short_desc": market_short_desc,
                        "market_display_name": market_display_name or str(
                            artifact_data.get("display_name", "") or ""
                        ).strip(),
                    }
                )
                self._add_installed_plugin(
                    {
                        "name": skill_name,
                        "marketplace": "teamskillshub",
                        "version": str(meta.get("version", "")).strip()
                        or str(artifact_data.get("version", "")).strip(),
                        "commit": "",
                        "source": "teamskillshub",
                        # origin 与同处 _add_local_skill 一致，供身份键精确区分
                        "origin": f"teamskillshub:{asset_id}",
                        "installed_at": installed_at,
                    }
                )
                self._refresh_agent_data_indexes()
                return {
                    "success": True,
                    "skill": {
                        "name": skill_name,
                        "source": "teamskillshub",
                        "asset_id": asset_id,
                        "path": str(dest),
                    },
                }
        except Exception as exc:
            logger.error("Team Skills Hub 安装失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.teamskillshub.errors.installFailed",
            }

    async def handle_skills_team_skills_hub_publish(self, params: dict) -> dict:
        """发布 TeamSkills（对齐 jiuwen-teamskills 的 /api/v1/plugins 协议）。"""
        auth = self._resolve_teamskills_hub_auth(params)
        if auth.get("error"):
            return {"success": False, "detail": str(auth["error"])}

        plugin_version = str(params.get("version") or "").strip()
        if not plugin_version:
            return {"success": False, "detail": "缺少参数: version"}

        plugin_id = str(params.get("skill_id") or "").strip() or None
        version_desc = str(params.get("version_desc") or "").strip()
        force = bool(params.get("force", False))
        base_url = self._get_team_skills_hub_base_url(str(params.get("market_url") or "").strip() or None)

        path_raw = str(params.get("path") or "").strip()
        file_raw = str(params.get("file") or "").strip()
        if not path_raw and not file_raw:
            return {"success": False, "detail": "缺少参数: path 或 file"}

        try:
            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_teamskills_publish_") as tmpdir:
                zip_path = self._prepare_teamskills_publish_zip(
                    path_raw=path_raw,
                    file_raw=file_raw,
                    plugin_version=plugin_version,
                    tmpdir=Path(tmpdir),
                )
                checksum_sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
                response_data = await self._teamskills_hub_publish_request(
                    base_url=base_url,
                    zip_path=zip_path,
                    checksum_sha256=checksum_sha256,
                    plugin_id=plugin_id,
                    plugin_version=plugin_version,
                    version_desc=version_desc,
                    force=force,
                    token=auth.get("token"),
                    system_token=auth.get("system_token"),
                )
                skill_id = str(
                    response_data.get("asset_id")
                    or response_data.get("plugin_id")
                    or plugin_id
                    or ""
                ).strip()
                name = str(response_data.get("name") or "").strip()
                version = str(response_data.get("version") or plugin_version).strip() or plugin_version
                return {"success": True, "skill_id": skill_id, "name": name, "version": version}
        except Exception as exc:
            logger.error("Team Skills Hub 发布失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.teamskillshub.errors.publishFailed",
            }

    async def handle_skills_team_skills_hub_delete(self, params: dict) -> dict:
        """删除 TeamSkills（对齐 jiuwen-teamskills 的 DELETE /api/v1/plugins/...）。"""
        skill_id = str(params.get("skill_id") or "").strip()
        if not skill_id:
            return {"success": False, "detail": "缺少参数: skill_id"}

        auth = self._resolve_teamskills_hub_auth(params)
        if auth.get("error"):
            return {"success": False, "detail": str(auth["error"])}

        version = str(params.get("version") or "all").strip() or "all"
        base_url = self._get_team_skills_hub_base_url(str(params.get("market_url") or "").strip() or None)
        try:
            await self._teamskills_hub_delete_request(
                base_url=base_url,
                skill_id=skill_id,
                version=version,
                token=auth.get("token"),
                system_token=auth.get("system_token"),
            )
            return {"success": True, "skill_id": skill_id, "version": version}
        except Exception as exc:
            logger.error("Team Skills Hub 删除失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.teamskillshub.errors.deleteFailed",
            }

    async def _skillnet_install_background(
        self,
        install_id: str,
        skill_url: str,
        force: bool,
        mirror_url: str | None = None,
    ) -> None:
        try:
            result = await asyncio.to_thread(self._skillnet_install_files_sync, skill_url, force, mirror_url)
        except Exception as exc:
            logger.error("SkillNet 后台安装异常: %s", exc)
            raw = str(exc).strip()
            self._skillnet_install_jobs[install_id] = {
                "status": "failed",
                "detail": raw or "安装失败，请重试。",
                **(
                    {}
                    if raw
                    else {
                        "detail_key": "skills.skillNet.errors.installFailedFallback",
                    }
                ),
            }
            return

        if not result.get("ok"):
            job_entry: dict[str, Any] = {
                "status": "failed",
                "detail": result.get("detail", "安装失败，请重试。"),
            }
            if result.get("detail_key"):
                job_entry["detail_key"] = result["detail_key"]
            if result.get("detail_params") is not None:
                job_entry["detail_params"] = result["detail_params"]
            self._skillnet_install_jobs[install_id] = job_entry
            return

        skill_name = result["skill_name"]
        meta = result["meta"]
        skill_url_stored = result["skill_url"]
        try:
            self._add_local_skill(
                {
                    "name": skill_name,
                    "origin": skill_url_stored,
                    "source": "skillnet",
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._add_installed_plugin(
                {
                    "name": skill_name,
                    "marketplace": "skillnet",
                    "version": meta.get("version", ""),
                    "commit": "",
                    "source": "skillnet",
                    # origin 与同处 _add_local_skill 一致（skill_url），供身份键精确区分
                    "origin": skill_url_stored,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._refresh_agent_data_indexes()
        except Exception as exc:
            logger.error("SkillNet 写入状态失败: %s", exc)
            self._skillnet_install_jobs[install_id] = {
                "status": "failed",
                "detail": "安装完成但保存配置失败，请刷新页面重试。",
                "detail_key": "skills.skillNet.errors.saveConfigFailed",
            }
            return

        hook = self._skillnet_install_complete_hook
        if hook is not None:
            try:
                await hook()
            except Exception as exc:
                logger.error("SkillNet 安装完成后 hook 失败: %s", exc)
                self._skillnet_install_jobs[install_id] = {
                    "status": "failed",
                    "detail": "技能已安装，请手动刷新页面生效。",
                    "detail_key": "skills.skillNet.errors.reloadRequired",
                }
                return

        self._skillnet_install_jobs[install_id] = {
            "status": "done",
            "skill": {"name": skill_name, "source": "skillnet"},
        }

    @_state_transactional
    def _commit_skillnet_entity(
        self,
        skill_dir: Path,
        *,
        skill_name: str,
        force: bool,
        protected_source_types: frozenset[str],
    ) -> dict[str, Any] | None:
        """Commit a downloaded entity without overwriting protected installations."""
        existing = self._find_skill_installation(name=skill_name)
        existing_type = str((existing or {}).get("source_type") or "").strip()
        if existing_type in protected_source_types:
            return {
                "ok": False,
                "detail": f"skill name already installed as {existing_type}: {skill_name}",
                "error_code": "skill_name_conflict",
            }

        dest = _safe_child_path(self._skills_dir, skill_name, "skill")
        if dest.exists():
            if not force:
                return {
                    "ok": False,
                    "detail": "该技能已安装。",
                    "detail_key": "skills.skillNet.errors.skillAlreadyInstalled",
                }
            _safe_rmtree(dest)

        shutil.copytree(skill_dir, dest)
        for mirror_root in self._get_mirror_skills_dirs():
            mirror_dest = _safe_child_path(mirror_root, skill_name, "skill")
            if mirror_dest.exists():
                if not force:
                    continue
                _safe_rmtree(mirror_dest)
            mirror_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, mirror_dest)
        return None

    def _skillnet_install_files_sync(
        self,
        skill_url: str,
        force: bool,
        mirror_url: str | None = None,
        checksum_sha256: str = "",
        *,
        protected_source_types: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """在工作线程中下载并拷贝到 skills 目录；返回 ok / skill_name / meta / skill_url.

        ``checksum_sha256`` 非空时对下载的归档做完整性校验（管理面预置链路），
        无归档字节流的来源（GitHub 目录链接）直接拒绝，避免静默绕过校验。
        """
        try:
            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_skillnet_") as tmpdir:
                tmp_path = Path(tmpdir)

                if self._is_github_skill_folder_url(skill_url):
                    if checksum_sha256.strip():
                        return {
                            "ok": False,
                            "detail": "该来源不支持 SHA256 校验，已拒绝下载。",
                            "error_code": "checksum_unsupported",
                        }
                    download_path_str = self._skillnet_download_sync(
                        skill_url, str(tmp_path), mirror_url
                    )
                    if not download_path_str:
                        return {
                            "ok": False,
                            "detail": "下载失败，请重试。",
                            "detail_key": "skills.skillNet.errors.downloadFailed",
                        }
                    download_path = Path(download_path_str).resolve()
                elif self._is_http_download_target(skill_url):
                    self._assert_skill_download_url_allowed(skill_url)
                    artifact_bytes = self._download_http_archive_bytes_sync(skill_url)
                    if not _sha256_matches(artifact_bytes, checksum_sha256):
                        return {
                            "ok": False,
                            "detail": "下载文件校验失败（SHA256 不匹配）。",
                            "error_code": "checksum_mismatch",
                        }
                    download_path = tmp_path / "http_download"
                    download_path.mkdir(parents=True, exist_ok=True)
                    self._extract_archive_bytes_to_dir(artifact_bytes, download_path)
                else:
                    return {
                        "ok": False,
                        "detail": (
                            "不支持的 skill_url：需为 GitHub tree/blob 目录链接，"
                            "或 HTTPS 直链 zip/tar 归档。"
                        ),
                    }

                if not download_path.exists():
                    return {
                        "ok": False,
                        "detail": "下载失败，请重试。",
                        "detail_key": "skills.skillNet.errors.downloadFailed",
                    }

                # 库在部分文件下载失败时仍会返回路径，只有找到 SKILL.md 才视为下载完整
                skill_dir = self._locate_skill_dir(download_path)
                if skill_dir is None:
                    return {
                        "ok": False,
                        "detail": "下载未完成或内容不完整，未找到 SKILL.md，请重试。",
                        "detail_key": "skills.skillNet.errors.skillMdNotFound",
                    }

                md = self._try_find_skill_file(skill_dir)
                meta = self._parse_skill_md(md) if md else None
                if meta is None:
                    return {
                        "ok": False,
                        "detail": "无法解析下载的技能文件",
                        "detail_key": "skills.skillNet.errors.parseSkillFailed",
                    }

                raw_skill_name = meta.get("name", skill_dir.name)
                try:
                    skill_name = _safe_path_name(raw_skill_name, "skill")
                except ValueError as exc:
                    _log_rejected_name("skills.skillnet.install", "skill", raw_skill_name, exc)
                    return {"ok": False, "detail": str(exc)}
                commit_error = self._commit_skillnet_entity(
                    skill_dir,
                    skill_name=skill_name,
                    force=force,
                    protected_source_types=protected_source_types,
                )
                if commit_error is not None:
                    return commit_error
                _safe_rmtree(skill_dir)
                return {
                    "ok": True,
                    "skill_name": skill_name,
                    "meta": meta,
                    "skill_url": skill_url,
                }
        except SkillNetEmptyDownloadError as exc:
            logger.error("SkillNet 下载失败: %s", exc)
            out: dict[str, Any] = {
                "ok": False,
                "detail_key": exc.detail_key,
                "detail": "",
            }
            out["detail_params"] = exc.detail_params
            return out
        except SkillNetInstallError as exc:
            logger.error("SkillNet 安装失败: %s", exc.detail or exc.detail_key)
            out2: dict[str, Any] = {
                "ok": False,
                "detail_key": exc.detail_key,
                "detail": exc.detail,
            }
            if exc.detail_params:
                out2["detail_params"] = exc.detail_params
            return out2
        except Exception as exc:
            logger.error("SkillNet 下载失败: %s", exc)
            raw = str(exc).strip()
            detail = raw or "安装失败，请重试。"
            extra: dict[str, Any] = {}
            if not raw:
                extra["detail_key"] = "skills.skillNet.errors.installFailedFallback"
            return {"ok": False, "detail": detail, **extra}

    def install_skill_sync(
        self,
        skill_url: str,
        force: bool = True,
        mirror_url: str | None = None,
        checksum_sha256: str = "",
        *,
        protected_source_types: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """同步安装 skill（线程安全，可在 ``asyncio.to_thread`` 中调用）.

        封装 ``_skillnet_install_files_sync``，供外部模块使用。
        ``checksum_sha256`` 非空时校验归档完整性（管理面预置链路）。
        """
        return self._skillnet_install_files_sync(
            skill_url,
            force,
            mirror_url,
            checksum_sha256=checksum_sha256,
            protected_source_types=protected_source_types,
        )

    async def handle_skills_uninstall(self, params: dict) -> dict:
        """卸载已安装的 skill.

        params:
            name: skill 名称
            origin: 可选，技能来源标识（如 ``clawhub:owner/slug``、URL）。提供时按
                origin 精确定位目录与记录，避免重名技能误删另一个。
        """
        return await asyncio.to_thread(self._handle_skills_uninstall_sync, params)

    @_state_transactional
    def _handle_skills_uninstall_sync(self, params: dict) -> dict:
        """Uninstall one skill while holding the workspace state transaction."""
        raw_name = params.get("name", "")
        raw_origin = str(params.get("origin", "") or "").strip()
        if not raw_name:
            return {"success": False, "detail": "缺少参数: name"}

        installation = self._find_skill_installation(
            name=str(raw_name),
            origin=raw_origin or None,
        )
        if (
            installation is not None
            and str(installation.get("source_type") or "").strip() in {"builtin", "prebuilt"}
        ):
            return {
                "success": False,
                "error_code": "prebuilt_not_removable",
                "error_message": "prebuilt skill is managed by administrator",
                "detail": "企业预置技能由管理员统一管理，不允许卸载。",
            }

        # 优先用 origin 精确定位目录（ClawHub 目录名 = slug，可由 origin 直推）
        dest = None
        name = ""
        if installation is not None and installation.get("entity_dir"):
            name = _safe_path_name(str(installation.get("name") or raw_name), "skill")
            entity_path = _safe_child_path(self._skills_dir, str(installation["entity_dir"]), "skill")
            if not entity_path.is_dir():
                return {"success": False, "detail": f"未找到 skill: {name}"}
            dest = entity_path
        if dest is None and raw_origin:
            dest = self._resolve_local_skill_dir_by_origin(raw_origin)
            # origin 命中时 name 用于 builtin 校验/记录回退
            name = raw_name

        if dest is None:
            try:
                name = _safe_path_name(raw_name, "skill")
            except ValueError as exc:
                # name 含不安全字符（如 /），从 local_skills 的 origin 提取 slug
                slug = ""
                for ls in self._state.get("local_skills", []):
                    if isinstance(ls, dict) and ls.get("name") == raw_name:
                        origin = str(ls.get("origin", ""))
                        if ":" in origin:
                            slug = origin.rsplit(":", 1)[-1]
                        break
                if slug:
                    try:
                        name = _safe_path_name(slug, "skill")
                    except ValueError:
                        _log_rejected_name("skills.uninstall", "skill", raw_name, exc)
                        return {"success": False, "detail": str(exc)}
                else:
                    _log_rejected_name("skills.uninstall", "skill", raw_name, exc)
                    return {"success": False, "detail": str(exc)}

            # 使用 _resolve_local_skill_dir 正确解析技能目录（处理 name 与文件夹名称不一致的情况）
            dest = self._resolve_local_skill_dir(name)

        if dest is None:
            return {"success": False, "detail": f"未找到 skill: {name or raw_name}"}

        # 检查是否为真正的内置技能（源码目录中的，不允许删除）
        builtin_dir = get_builtin_skills_dir()
        if builtin_dir.exists():
            # 检查 builtin 技能目录中是否有该技能
            builtin_skill_path = None
            direct_builtin = _safe_child_path(builtin_dir, name, "skill")
            if direct_builtin.exists() and direct_builtin.is_dir():
                builtin_skill_path = direct_builtin
            else:
                # 遍历 builtin 目录，通过解析 SKILL.md 查找匹配的技能
                for child in builtin_dir.iterdir():
                    if not child.is_dir() or child.name.startswith("_"):
                        continue
                    md = self._try_find_skill_file(child)
                    if md is None:
                        continue
                    meta = self._parse_skill_md(md)
                    if meta and meta.get("name") == name:
                        builtin_skill_path = child
                        break

            if builtin_skill_path:
                if dest.resolve() == builtin_skill_path.resolve():
                    return {"success": False, "detail": "内置技能不允许删除"}

        _safe_rmtree(dest)

        # 处理 mirror 根目录中的技能
        for mirror_root in self._get_mirror_skills_dirs():
            mirror_dest = _safe_child_path(mirror_root, dest.name, "skill")
            if mirror_dest.exists() and mirror_dest.is_dir():
                _safe_rmtree(mirror_dest)

        # 按身份删除记录：origin 优先，避免误删同名另一条
        self._remove_installed_plugin(raw_name, origin=raw_origin or None)
        self._remove_local_skill(raw_name, origin=raw_origin or None)
        # 卸载时一并清掉该 skill 的 enabled 配置，避免重装同名 skill 时沿用旧的禁用状态。
        self.remove_skill_config(raw_name)
        self._refresh_agent_data_indexes()
        return {"success": True}

    async def handle_skills_import_local(self, params: dict) -> dict:
        """从本地路径或远程归档 URL 导入 skill."""
        raw_path = params.get("path", "")
        force = bool(params.get("force", False))
        checksum_sha256 = str(params.get("checksum_sha256", "") or "").strip()
        logger.info(
            "[SkillManager] import_local called: path=%r force=%s remote=%s",
            raw_path,
            force,
            self._is_http_download_target(str(raw_path).strip()),
        )
        if not raw_path:
            return {"success": False, "detail": "缺少参数: path"}

        remote_url = str(raw_path).strip()
        if self._is_http_download_target(remote_url):
            try:
                return await self._import_skill_from_remote_archive(
                    download_url=remote_url,
                    force=force,
                    checksum_sha256=checksum_sha256,
                )
            except Exception as exc:
                logger.error("remote archive import failed: %s", exc)
                return {"success": False, "detail": str(exc)[:500]}

        return self._import_local_from_path(Path(raw_path), force=force, origin=str(raw_path))

    def _import_local_from_path(self, src: Path, *, force: bool, origin: str) -> dict[str, Any]:
        logger.info(
            "[SkillManager] import_local_from_path start: src=%s origin=%s force=%s",
            src,
            origin,
            force,
        )
        if not src.exists():
            return {"success": False, "detail": f"路径不存在: {origin}"}

        if src.is_file():
            meta = self._parse_skill_md(src)
            if meta is None:
                return {"success": False, "detail": "无法解析 skill 文件"}
            raw_skill_name = meta.get("name", src.stem)
            try:
                skill_name = _safe_path_name(raw_skill_name, "skill")
            except ValueError as exc:
                _log_rejected_name("skills.import_local", "skill", raw_skill_name, exc)
                return {"success": False, "detail": str(exc)}
            dest = _safe_child_path(self._skills_dir, skill_name, "skill")
            if dest.exists():
                if not force:
                    return {"success": False, "detail": f"skill {skill_name} 已存在"}
                _safe_rmtree(dest)
            try:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / src.name)
            except OSError as exc:
                return _handle_copy_error(exc, dest, "local import file", src)
        elif src.is_dir():
            md = self._try_find_skill_file(src)
            if md is None:
                return {"success": False, "detail": f"目录中未找到 SKILL.md: {origin}"}
            meta = self._parse_skill_md(md) or {}
            raw_skill_name = meta.get("name", src.name)
            try:
                skill_name = _safe_path_name(raw_skill_name, "skill")
            except ValueError as exc:
                _log_rejected_name("skills.import_local", "skill", raw_skill_name, exc)
                return {"success": False, "detail": str(exc)}
            dest = _safe_child_path(self._skills_dir, skill_name, "skill")
            if dest.exists():
                if not force:
                    return {"success": False, "detail": f"skill {skill_name} 已存在"}
                _safe_rmtree(dest)
            try:
                shutil.copytree(src, dest)
            except OSError as exc:
                return _handle_copy_error(exc, dest, "local import dir", src)
        else:
            return {"success": False, "detail": f"不支持的路径类型: {origin}"}

        self._add_local_skill({"name": skill_name, "origin": origin, "source": "local"})
        self._refresh_agent_data_indexes()
        logger.info(
            "[SkillManager] import_local_from_path done: skill_name=%s origin=%s dest=%s",
            skill_name,
            origin,
            self._skills_dir / skill_name,
        )
        return {"success": True, "skill": {"name": skill_name}}

    def _download_web_skill_bytes(self, download_url: str) -> bytes:
        """同步下载技能归档（仅 Web 验签安装使用；不改 import_local 远程路径）。"""
        self._assert_import_local_download_url_allowed(download_url)
        timeout = max(30.0, _IMPORT_LOCAL_REMOTE_TIMEOUT)
        ssl_verify = _get_ssl_verify()
        _maybe_disable_insecure_warning()
        with requests.Session() as session:
            # 仅在关闭证书校验时挂载跳过校验的 Adapter；开启时走 requests 默认校验。
            if not ssl_verify:
                session.mount("https://", _ImportLocalTLSAdapter())
            logger.info(
                "[SkillManager] web skill download: url=%s ssl_verify=%s",
                download_url,
                ssl_verify,
            )
            with session.get(
                download_url.strip(),
                timeout=timeout,
                stream=True,
                allow_redirects=False,
                verify=ssl_verify,
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        chunks.append(chunk)
                body = b"".join(chunks)
        if not body:
            raise RuntimeError("下载内容为空")
        return body

    def _install_web_skill_dir(self, skill_dir: Path, *, skill_name: str) -> dict[str, Any]:
        """企业 Web 安装专用：先完成实体落盘，再由调用方提交 workspace 状态。"""
        name = str(skill_name or "").strip()
        if not name:
            return {"success": False, "detail": "skill name is required"}
        if not skill_dir.exists() or not skill_dir.is_dir():
            return {"success": False, "detail": f"skill dir missing: {skill_dir}"}
        try:
            safe = _safe_path_name(name, "skill")
        except ValueError as exc:
            return {"success": False, "detail": str(exc)}
        dest = _safe_child_path(self._skills_dir, safe, "skill")
        if dest.exists():
            _safe_rmtree(dest)
        shutil.copytree(skill_dir, dest)
        logger.info(
            "[SkillManager] web skill installed to disk: name=%s dest=%s",
            safe,
            dest,
        )
        return {"success": True, "skill": {"name": safe}}

    def remove_skill_directory(self, skill_name: str) -> None:
        name = str(skill_name or "").strip()
        if not name:
            return
        try:
            safe = _safe_path_name(name, "skill")
        except ValueError:
            return
        dest = _safe_child_path(self._skills_dir, safe, "skill")
        if dest.exists() and dest.is_dir():
            _safe_rmtree(dest)

    @_state_transactional
    def _commit_web_skill_install(
        self,
        skill_dir: Path,
        *,
        skill_name: str,
        skill_version: str | None,
        source_url: str,
    ) -> dict[str, Any]:
        """Commit the user Skill entity and ledger record in one transaction."""
        existing = self._find_skill_installation(name=skill_name)
        if existing is not None:
            existing_type = str(existing.get("source_type") or "").strip()
            existing_origin = str(existing.get("origin") or "").strip()
            existing_version = str(existing.get("version") or "").strip()
            if existing_type == "prebuilt":
                return {
                    "success": False,
                    "installed": False,
                    "error_code": "skill_name_conflict",
                    "error_message": (
                        f"skill `{skill_name}` is prebuilt and cannot be overwritten"
                    ),
                }
            if existing_type != "user" or existing_origin != source_url:
                return {
                    "success": False,
                    "installed": False,
                    "error_code": "skill_name_conflict",
                    "error_message": f"skill name already installed: {skill_name}",
                }
            if existing_version == str(skill_version or ""):
                return {
                    "success": False,
                    "installed": False,
                    "already_installed": True,
                    "error_code": "skill_already_installed",
                    "name": skill_name,
                    "error_message": (
                        f"skill `{skill_name}` already installed with the same version"
                    ),
                }

        import_result = self._install_web_skill_dir(skill_dir, skill_name=skill_name)
        if not import_result.get("success"):
            return {
                "success": False,
                "error_code": "install_failed",
                "error_message": str(import_result.get("detail") or "install failed"),
            }
        installed_name = str(
            (import_result.get("skill") or {}).get("name") or skill_name
        ).strip()

        try:
            self.record_skill_installation(
                name=installed_name,
                source_type="user",
                source="web",
                origin=source_url,
                version=str(skill_version or ""),
            )
        except SkillNameConflictError as exc:
            self.remove_skill_directory(installed_name)
            return {
                "success": False,
                "installed": False,
                "error_code": "skill_name_conflict",
                "error_message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            self.remove_skill_directory(installed_name)
            return {
                "success": False,
                "error_code": "state_write_failed",
                "error_message": str(exc)[:500],
            }
        return {
            "success": True,
            "skill": {"name": installed_name, "version": skill_version},
        }

    async def handle_skills_web_install(self, params: dict) -> dict:
        """企业 Web 安装兼容入口：下载、校验、落盘并提交 workspace JSON。"""
        from jiuwenswarm.agents.harness.common.installed_skill import (
            verify_skill_download_hmac,
        )

        url = str(params.get("url") or "").strip()
        signature = str(params.get("signature") or "").strip()
        if not url:
            return {
                "success": False,
                "error_code": "missing_params",
                "error_message": "url is required",
            }

        service_id = str(params.get("service_id") or self._service_id or "").strip()
        agent_id = str(params.get("agent_id") or self._agent_id or "").strip()
        if not service_id or not agent_id:
            return {
                "success": False,
                "error_code": "missing_tenant",
                "error_message": "service_id and agent_id are required",
            }

        try:
            content = await asyncio.to_thread(self._download_web_skill_bytes, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillManager] enterprise install download failed: %s", exc)
            return {
                "success": False,
                "error_code": "download_failed",
                "error_message": str(exc)[:500],
            }

        # 签名可选：有则验 HMAC；无则跳过验签直接解压安装
        if signature and not verify_skill_download_hmac(content, signature):
            return {
                "success": False,
                "error_code": "signature_invalid",
                "error_message": "HMAC verification failed",
            }

        with tempfile.TemporaryDirectory(prefix="jiuwenclaw_enterprise_install_") as tmpdir:
            tmp_path = Path(tmpdir)
            try:
                self._extract_archive_bytes_to_dir(content, tmp_path)
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "error_code": "extract_failed",
                    "error_message": str(exc)[:500],
                }
            skill_dir = self._locate_skill_dir(tmp_path)
            if skill_dir is None:
                return {
                    "success": False,
                    "error_code": "invalid_package",
                    "error_message": "downloaded content missing SKILL.md",
                }
            md = self._try_find_skill_file(skill_dir)
            meta = self._parse_skill_md(md) if md is not None else {}
            meta = meta or {}
            raw_name = meta.get("name") or skill_dir.name
            try:
                skill_name = _safe_path_name(str(raw_name), "skill")
            except ValueError as exc:
                return {
                    "success": False,
                    "error_code": "invalid_skill_name",
                    "error_message": str(exc),
                }
            skill_version = str(meta.get("version") or "").strip() or None

            return await asyncio.to_thread(
                self._commit_web_skill_install,
                skill_dir,
                skill_name=skill_name,
                skill_version=skill_version,
                source_url=url,
            )

    async def handle_skills_web_uninstall(self, params: dict) -> dict:
        """企业 Web 卸载兼容入口：仅允许删除 workspace 中的 user 安装。"""

        name = str(params.get("name") or "").strip()
        if not name:
            return {
                "success": False,
                "error_code": "missing_params",
                "error_message": "name is required",
            }
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            return {
                "success": False,
                "error_code": "invalid_skill_name",
                "error_message": str(exc),
            }

        service_id = str(params.get("service_id") or self._service_id or "").strip()
        agent_id = str(params.get("agent_id") or self._agent_id or "").strip()
        if not service_id or not agent_id:
            return {
                "success": False,
                "error_code": "missing_tenant",
                "error_message": "service_id and agent_id are required",
            }

        origin = str(params.get("origin") or "").strip()
        if origin:
            matches = [
                record for record in self.list_skill_installations()
                if str(record.get("origin") or "").strip() == origin
            ]
            installation = matches[0] if len(matches) == 1 else None
        else:
            installation = self._find_skill_installation(name=name)
        if installation is None:
            return {
                "success": False,
                "error_code": "not_found",
                "error_message": f"skill `{name}` is not installed",
            }
        if str(installation.get("source_type") or "").strip() != "user":
            return {
                "success": False,
                "error_code": "prebuilt_not_removable",
                "error_message": f"skill `{name}` is prebuilt and cannot be uninstalled",
            }

        name = str(installation.get("name") or "").strip()
        result = await self.handle_skills_uninstall(
            {"name": name, "origin": str(installation.get("origin") or "")}
        )
        if result.get("success"):
            return {"success": True, "name": name}
        return {
            "success": False,
            "error_code": str(result.get("error_code") or "uninstall_failed"),
            "error_message": str(
                result.get("error_message") or result.get("detail") or "uninstall failed"
            ),
        }

    async def _import_skill_from_remote_archive(
        self,
        *,
        download_url: str,
        force: bool,
        checksum_sha256: str = "",
    ) -> dict[str, Any]:
        """Download archive by URL, extract by type, then reuse local import flow."""
        self._assert_import_local_download_url_allowed(download_url)
        timeout = max(30.0, _IMPORT_LOCAL_REMOTE_TIMEOUT)
        logger.info(
            "[SkillManager] remote import start: url=%s force=%s timeout=%s",
            download_url,
            force,
            timeout,
        )

        def _download_with_requests() -> bytes:
            ssl_verify = _get_ssl_verify()
            _maybe_disable_insecure_warning()
            with requests.Session() as session:
                # 仅在关闭证书校验时挂载跳过校验的 Adapter；开启时走 requests 默认校验。
                if not ssl_verify:
                    session.mount("https://", _ImportLocalTLSAdapter())
                logger.info(
                    "[SkillManager] remote import downloading: url=%s ssl_verify=%s",
                    download_url,
                    ssl_verify,
                )
                with session.get(
                    download_url.strip(),
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                    verify=ssl_verify,
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            chunks.append(chunk)
                    body = b"".join(chunks)
            logger.info(
                "[SkillManager] remote import downloaded: url=%s bytes=%s",
                download_url,
                len(body),
            )
            if not body:
                raise RuntimeError("下载内容为空")

            if not _sha256_matches(body, checksum_sha256):
                raise RuntimeError("下载文件校验失败（SHA256 不匹配）")
            return body

        artifact_bytes = _download_with_requests()

        with tempfile.TemporaryDirectory(prefix="jiuwenswarm_import_local_") as tmpdir:
            tmp_path = Path(tmpdir)
            logger.info("[SkillManager] remote import extracting: url=%s tmpdir=%s", download_url, tmp_path)
            self._extract_archive_bytes_to_dir(artifact_bytes, tmp_path)
            skill_dir = self._locate_skill_dir(tmp_path)
            if skill_dir is None:
                return {"success": False, "detail": "下载内容不完整，未找到 SKILL.md"}
            logger.info("[SkillManager] remote import extracted: url=%s skill_dir=%s", download_url, skill_dir)
            return self._import_local_from_path(skill_dir, force=force, origin=download_url)

    async def handle_skills_marketplace_add(self, params: dict) -> dict:
        """添加 marketplace 源.

        params:
            name: marketplace 名称
            url: git 仓库 URL
        """
        name = params.get("name", "")
        url = params.get("url", "")
        if not name or not url:
            return {"success": False, "detail": "缺少参数: name 和 url"}
        try:
            name = _safe_path_name(name, "marketplace")
        except ValueError as exc:
            _log_rejected_name("skills.marketplace.add", "marketplace", name, exc)
            return {"success": False, "detail": str(exc)}

        # 检查是否已存在
        for m in self._get_marketplaces():
            if m.get("name") == name:
                return {"success": False, "detail": f"marketplace 已存在: {name}"}

        # 新增源默认禁用，避免未经确认就触发远程同步。
        self._add_marketplace({"name": name, "url": url, "enabled": False})
        return {"success": True}

    async def handle_skills_marketplace_remove(self, params: dict) -> dict:
        """删除 marketplace 源.

        params:
            name: marketplace 名称
            remove_cache: 是否删除本地仓库缓存（可选，默认 True）
        """
        name = params.get("name", "")
        remove_cache = params.get("remove_cache", True)
        if not name:
            return {"success": False, "detail": "缺少参数: name"}
        try:
            name = _safe_path_name(name, "marketplace")
        except ValueError as exc:
            _log_rejected_name("skills.marketplace.remove", "marketplace", name, exc)
            return {"success": False, "detail": str(exc)}

        removed = self._remove_marketplace(name)
        if not removed:
            return {"success": False, "detail": f"marketplace 不存在: {name}"}

        cache_removed = False
        if bool(remove_cache):
            repo_dir = _safe_child_path(self._marketplace_dir, name, "marketplace")
            if repo_dir.exists() and repo_dir.is_dir():
                try:
                    _safe_rmtree(repo_dir)
                    cache_removed = True
                except Exception as exc:
                    logger.warning("删除 marketplace 缓存失败: %s", exc)

        return {
            "success": True,
            "name": name,
            "cache_removed": cache_removed,
        }

    async def handle_skills_marketplace_toggle(self, params: dict) -> dict:
        """启用或禁用 marketplace 源.

        params:
            name: marketplace 名称
            enabled: 目标状态
        """
        name = params.get("name", "")
        enabled = params.get("enabled")
        if not name:
            return {"success": False, "detail": "缺少参数: name"}
        if not isinstance(enabled, bool):
            return {"success": False, "detail": "缺少参数: enabled (bool)"}
        try:
            name = _safe_path_name(name, "marketplace")
        except ValueError as exc:
            _log_rejected_name("skills.marketplace.toggle", "marketplace", name, exc)
            return {"success": False, "detail": str(exc)}

        marketplace = next(
            (m for m in self._get_marketplaces() if m.get("name") == name),
            None,
        )
        if marketplace is None:
            return {"success": False, "detail": f"marketplace 不存在: {name}"}

        if enabled:
            repo_dir = _safe_child_path(self._marketplace_dir, name, "marketplace")
            url = marketplace.get("url", "")
            if not url:
                return {"success": False, "detail": f"marketplace {name} 缺少 url"}

            detail = "已启用"
            if repo_dir.exists():
                commit = await self._git_pull(repo_dir)
                if commit is None:
                    return {"success": False, "name": name, "enabled": False, "detail": "git pull 失败"}
                detail = "已启用并执行 git pull"
            else:
                commit = await self._git_clone(url, repo_dir)
                if commit is None:
                    return {"success": False, "name": name, "enabled": False, "detail": "git clone 失败"}
                detail = "已启用并执行 git clone"

            self._set_marketplace_enabled(name, True)
            self._set_marketplace_last_updated(name)
            return {"success": True, "name": name, "enabled": True, "detail": detail}

        # 禁用：删除本地缓存目录，不卸载已安装 skill。
        repo_dir = _safe_child_path(self._marketplace_dir, name, "marketplace")
        cache_removed = False
        if repo_dir.exists() and repo_dir.is_dir():
            cache_removed = _safe_rmtree(repo_dir)
            if not cache_removed:
                return {"success": False, "name": name, "enabled": True, "detail": "删除本地缓存失败"}

        self._set_marketplace_enabled(name, False)
        self._set_marketplace_last_updated(name)
        return {
            "success": True,
            "name": name,
            "enabled": False,
            "cache_removed": cache_removed,
            "detail": "已禁用并删除本地缓存" if cache_removed else "已禁用（无本地缓存）",
        }

    # -----------------------------------------------------------------------
    # SKILL.md 解析
    # -----------------------------------------------------------------------

    @staticmethod
    def _coerce_str_list(val: Any) -> list[str]:
        """frontmatter 里 tags/allowed_tools 可能是逗号分隔字符串，统一为 list[str]."""
        if val is None:
            return []
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return []
            if "," in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s]
        return [str(val)]

    @staticmethod
    def _convert_yaml_date(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: SkillManager._convert_yaml_date(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [SkillManager._convert_yaml_date(item) for item in obj]
        if isinstance(obj, date) and not isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    @staticmethod
    def _parse_skill_md(path: Path) -> dict | None:
        """解析 SKILL.md，提取 YAML frontmatter 和正文.

        支持两种格式:
        1. 有 frontmatter（--- 分隔的 YAML 头 + 正文）
        2. 无 frontmatter（整个文件作为 body，name 从文件名推断）
        """
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("无法读取文件: %s", path)
            return None

        meta: dict[str, Any] = {}
        body = text

        # 尝试解析 frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            body = fm_match.group(2).strip()
            # 优先完整 YAML（支持 description: >- 多行、嵌套等），与 Team Skills Hub register_skill 一致
            try:
                loaded = yaml.safe_load(fm_text)
                if isinstance(loaded, dict):
                    loaded = SkillManager._convert_yaml_date(loaded)
                    meta = {str(k): v for k, v in loaded.items()}
                else:
                    meta = {}
            except Exception:
                meta = {}
            if not meta:
                # 回退：逐行 key: value（旧逻辑，无 PyYAML 语义）
                for line in fm_text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r"^(\w[\w_-]*)\s*:\s*(.*)", line)
                    if m:
                        key = m.group(1)
                        val = m.group(2).strip()
                        if val.startswith("[") and val.endswith("]"):
                            inner = val[1:-1]
                            val = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
                        elif val.startswith(("'", '"')) and val.endswith(("'", '"')):
                            val = val[1:-1]
                        meta[key] = val

        # 如果没有 name，从文件名推断
        if "name" not in meta:
            meta["name"] = path.stem

        # 默认字段
        meta.setdefault("description", "")
        meta.setdefault("version", "")
        meta.setdefault("author", "")
        meta["tags"] = SkillManager._coerce_str_list(meta.get("tags"))
        meta["allowed_tools"] = SkillManager._coerce_str_list(meta.get("allowed_tools"))

        meta["body"] = body
        meta["path"] = str(path)

        return meta

    @staticmethod
    def _try_find_skill_file(directory: Path) -> Path | None:
        """在目录中查找 skill 文件.

        优先查找 SKILL.md，其次查找任意 .md 文件.
        """
        skill_md = directory / "SKILL.md"
        if skill_md.is_file():
            return skill_md

        # 兼容：查找任意 .md 文件
        md_files = list(directory.glob("*.md"))
        if md_files:
            return md_files[0]

        return None

    # -----------------------------------------------------------------------
    # 内置技能判断
    # -----------------------------------------------------------------------

    def _is_builtin_skill(self, skill_name: str, installed_plugins: list[dict], skill_path: Path | None = None) -> bool:
        """判断技能是否为内置技能.

        内置技能的判断标准：
        1. 不在 local_skills 中（用户本地导入）
        2. 不在 installed_plugins 中（marketplace安装）
        3. 实际路径在源码内置路径下（通过 skill_path 参数判断）

        注意：如果 skill_path 不为 None，则直接判断该路径是否在 builtin_dir 下，
        这比仅通过 skill_name 判断更准确，避免名称冲突导致的误判。
        """
        try:
            # 检查是否在 local_skills 中（用户本地导入或SkillNet下载）
            for local_skill in self._state.get("local_skills", []):
                if local_skill.get("name") == skill_name:
                    return False

            # 检查是否在 installed_plugins 中记录（marketplace安装的）
            for plugin in installed_plugins:
                if plugin.get("name") == skill_name:
                    return False

            # 如果提供了 skill_path，直接判断该路径是否在 builtin_dir 下
            if skill_path is not None:
                builtin_dir = get_builtin_skills_dir()
                if builtin_dir.exists():
                    return skill_path.resolve().parent == builtin_dir.resolve()
                return False

            # 没有提供 skill_path 时，回退到通过 skill_name 判断（兼容旧代码）
            builtin_dir = get_builtin_skills_dir()
            if builtin_dir.exists():
                builtin_skill_path = _safe_child_path(builtin_dir, skill_name, "skill")
                return builtin_skill_path.exists() and builtin_skill_path.is_dir()
            return False
        except Exception:
            return False

    @staticmethod
    def _slug_from_clawhub_origin(origin: str) -> str:
        """从 ClawHub origin 中提取 slug（最后 / 后的部分）。

        origin 可能有两种形态：
        - ``clawhub:owner/slug``（有发布者）
        - ``clawhub:slug``（无发布者）
        磁盘目录名恒为 slug，因此取最后一个 ``/`` 后的部分即可。
        非 ClawHub origin（不以 clawhub: 开头）返回空字符串——调用方应
        先确认 ``origin.startswith("clawhub:")`` 再调用此方法。
        """
        origin = origin.strip()
        if not origin.startswith("clawhub:"):
            return ""
        raw = origin.split(":", 1)[1].strip()
        if not raw:
            return ""
        return raw.rsplit("/", 1)[-1]

    @staticmethod
    def _dir_origin_candidates(child: Path) -> list[str]:
        """磁盘 skill 目录可能对应的 origin 候选（用于在 local_skills 中精确匹配）。

        ClawHub 装的目录名是 slug，其 local_skill.origin = ``clawhub:owner/slug`` 或
        ``clawhub:slug``，可反推出 ``clawhub:{目录名}`` 候选。其余来源（SkillNet URL、
        teamskillshub:asset_id）的目录名是 skill_name，无法由目录名反推 origin，这里
        只给 ClawHub 候选，不命中再由调用方按 name 回退。
        """
        slug = child.name
        if slug and not slug.startswith("_"):
            return [f"clawhub:{slug}"]
        return []

    def _find_local_skill_for_dir(self, child: Path, meta: dict) -> dict | None:
        """定位磁盘目录对应的 local_skill 记录，优先按 origin 精确匹配，再按 name 回退。

        重名技能（如两个 ClawHub slug 不同但 SKILL.md name 相同）必须按 origin
        精确区分，否则两条目录会串台成同一条 origin。origin 匹配失败时回退到
        按 name 取首条——SkillNet/TeamSkillsHub 用 skill_name 做目录名，重名时
        装不进两个（目录碰撞），所以 name 回退不会错配到另一个来源。
        """
        local = self._state.get("local_skills", [])
        if not isinstance(local, list):
            return None
        name = str(meta.get("name") or "").strip()
        # 1) origin 精确匹配（ClawHub 目录名 = slug）
        candidates = self._dir_origin_candidates(child)
        for candidate in candidates:
            for ls in local:
                if isinstance(ls, dict) and str(ls.get("origin", "") or "").strip() == candidate:
                    return ls
        # 2) 按 name 回退（SkillNet / TeamSkillsHub / import_local）
        if name:
            for ls in local:
                if isinstance(ls, dict) and str(ls.get("name", "") or "").strip() == name:
                    return ls
        return None

    def _find_installed_plugin_for_dir(
        self, child: Path, meta: dict, ls_record: dict | None
    ) -> dict | None:
        """定位磁盘目录对应的 installed_plugin 记录，origin 优先、name 回退."""
        plugins = self._get_installed_plugins()
        # 1) 用 local_skill 的 origin 精确匹配（ClawHub/teamskillshub/skillnet）
        if ls_record is not None and isinstance(ls_record, dict):
            origin = str(ls_record.get("origin", "") or "").strip()
            if origin:
                for p in plugins:
                    if isinstance(p, dict) and str(p.get("origin", "") or "").strip() == origin:
                        return p
        # 2) origin 候选（ClawHub 目录名 = slug）
        for candidate in self._dir_origin_candidates(child):
            for p in plugins:
                if isinstance(p, dict) and str(p.get("origin", "") or "").strip() == candidate:
                    return p
        # 3) 按 name 回退
        name = str(meta.get("name") or "").strip()
        if name:
            for p in plugins:
                if isinstance(p, dict) and str(p.get("name", "") or "").strip() == name:
                    return p
        return None

    # -----------------------------------------------------------------------
    # 目录扫描
    # -----------------------------------------------------------------------

    def _scan_local_skills(self) -> list[dict]:
        """扫描 agent/skills/ 下的本地 skill（跳过 _marketplace）."""
        results: list[dict] = []
        if not self._skills_dir.exists():
            return results

        for child in self._skills_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if meta is None:
                continue

            if meta.get("name") == md.stem:
                meta["name"] = child.name

            # 优先按 origin 精确匹配磁盘目录，避免同名技能串台
            ls_record = self._find_local_skill_for_dir(child, meta)
            plugin_record = self._find_installed_plugin_for_dir(child, meta, ls_record)

            # source 语义与原逻辑一致：先取 installed_plugins 的 source，再用
            # local_skills 覆盖；local_skills 同时回填 origin / display_name 供前端对照。
            source = "project"
            if plugin_record is not None:
                p_source = plugin_record.get("source", "project")
                if p_source == "project" and plugin_record.get("marketplace"):
                    source = plugin_record.get("marketplace", "project")
                else:
                    source = p_source
            if ls_record is not None and isinstance(ls_record, dict):
                ls_source = ls_record.get("source", "local")
                if ls_source:
                    source = ls_source
                # 回填展示字段（origin 精确匹配保证同名不同来源各拿各的，不串台）
                self._apply_local_skill_meta(meta, ls_record)

            meta["source"] = source
            if not str(meta.get("display_name") or "").strip():
                meta["display_name"] = meta.get("name", "")
            meta["installed"] = True
            meta["enabled"] = self.get_skill_enabled(meta.get("name", ""))
            if plugin_record is not None:
                self._apply_skill_installation_meta(meta, plugin_record)
            # 判断是否为内置技能（传入 child 路径，通过实际路径判断）
            meta["is_builtin"] = self._is_builtin_skill(meta.get("name", ""), self._get_installed_plugins(), child)
            builtin_dir = get_builtin_skills_dir()
            if builtin_dir.exists():
                builtin_skill_path = builtin_dir / child.name
                meta["is_builtin_source"] = builtin_skill_path.exists() and builtin_skill_path.is_dir()
            else:
                meta["is_builtin_source"] = False
            meta["has_evolutions"] = (child / _EVOLUTION_FILENAME).is_file()
            # 不在列表中返回 body
            meta.pop("body", None)
            results.append(meta)

        return results

    def _scan_builtin_skills(self) -> list[dict]:
        """扫描内置技能目录中尚未安装到用户目录的技能.

        返回的技能列表仅包含那些存在于内置目录但尚未在用户目录中的技能。
        """
        results: list[dict] = []
        builtin_dir = get_builtin_skills_dir()
        user_skills_dir = self._skills_dir

        if not builtin_dir.exists() or not builtin_dir.is_dir():
            return results

        for child in builtin_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue

            # 检查该技能是否已经在用户目录中安装
            user_skill_path = user_skills_dir / child.name
            if user_skill_path.exists() and user_skill_path.is_dir():
                continue  # 已安装，跳过

            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if meta is None:
                continue

            # 设置内置技能的标记
            meta["name"] = self._resolve_skill_name(child, md, meta)
            meta["source"] = "builtin"
            meta["is_builtin"] = True
            meta["is_builtin_source"] = True
            meta["installed"] = False
            meta["has_evolutions"] = False
            self._apply_enabled_config(meta, meta.get("name", ""))
            # 不在列表中返回 body
            meta.pop("body", None)
            results.append(meta)

        return results

    def _resolve_skill_source(self, skill_name: str, origin: str | None = None) -> str:
        """解析 skill 来源（local / project / marketplace 名称）.

        若传入了 origin，优先按 origin 精确匹配 installed_plugin 记录，避免
        重名技能（SKILL.md name 同但来源不同）拿到错误的 source。
        """
        if not skill_name:
            return "project"

        for plugin in self._get_installed_plugins():
            if origin:
                plugin_origin = str(plugin.get("origin", "") or "").strip()
                if plugin_origin and plugin_origin == origin and plugin.get("name") == skill_name:
                    source = plugin.get("source")
                    marketplace = plugin.get("marketplace")
                    if source == "project" and isinstance(marketplace, str) and marketplace:
                        return marketplace
                    if isinstance(source, str) and source:
                        return source
                    if isinstance(marketplace, str) and marketplace:
                        return marketplace
                    return "project"
            elif plugin.get("name") == skill_name:
                source = plugin.get("source")
                marketplace = plugin.get("marketplace")
                if source == "project" and isinstance(marketplace, str) and marketplace:
                    return marketplace
                if isinstance(source, str) and source:
                    return source
                if isinstance(marketplace, str) and marketplace:
                    return marketplace
                return "project"

        for local_skill in self._state.get("local_skills", []):
            if origin:
                ls_origin = str(local_skill.get("origin", "") or "").strip()
                if ls_origin and ls_origin == origin and local_skill.get("name") == skill_name:
                    return "local"
            elif local_skill.get("name") == skill_name:
                return "local"

        return "project"

    def _resolve_skill_display_name(self, skill_name: str) -> str:
        """解析 skill 展示名（优先 local_skills/installed_plugins 记录的 display_name）."""
        if not skill_name:
            return skill_name
        for local_skill in self._state.get("local_skills", []):
            if local_skill.get("name") == skill_name:
                display_name = str(local_skill.get("display_name") or "").strip()
                if display_name:
                    return display_name
                break
        for plugin in self._get_installed_plugins():
            if plugin.get("name") == skill_name:
                display_name = str(
                    plugin.get("market_display_name")
                    or plugin.get("display_name")
                    or ""
                ).strip()
                if display_name:
                    return display_name
                break
        return skill_name

    def _resolve_local_skill_dir(self, skill_name: str) -> Path | None:
        """根据 skill name 定位本地技能目录（仅 agent/skills 下）."""
        try:
            direct = _safe_child_path(self._skills_dir, skill_name, "skill")
        except ValueError:
            return None
        if direct.is_dir():
            return direct

        if not self._skills_dir.exists():
            return None

        for child in self._skills_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if meta and meta.get("name") == skill_name:
                return child
        return None

    def _resolve_local_skill_dir_by_origin(self, origin: str) -> Path | None:
        """根据 origin 精确定位本地技能目录.

        ClawHub 的目录名 = slug，origin 形如 ``clawhub:owner/slug`` 或
        ``clawhub:slug``，可通过 slug 直连 ``_skills_dir/{slug}``，无需遍历、
        不受同名干扰。其余来源（SkillNet URL、``teamskillshub:{asset_id}``）
        的目录名是 skill_name，无法由 origin 反推，这里通过 local_skills
        记录里的 name 间接解析。
        """
        origin = (origin or "").strip()
        if not origin:
            return None
        # ClawHub: origin = ``clawhub:owner/slug`` 或 ``clawhub:slug``，目录名 = slug
        if origin.startswith("clawhub:"):
            slug = self._slug_from_clawhub_origin(origin)
            if not slug:
                return None
            try:
                direct = _safe_child_path(self._skills_dir, slug, "skill")
            except ValueError:
                return None
            if direct.is_dir():
                return direct
            # 兜底：遍历按 origin 匹配 local_skill 的 name 再解析
            ls_name = ""
            for ls in self._state.get("local_skills", []):
                if isinstance(ls, dict) and str(ls.get("origin", "") or "").strip() == origin:
                    ls_name = str(ls.get("name", "") or "").strip()
                    break
            if ls_name:
                return self._resolve_local_skill_dir(ls_name)
            return None
        # 其余来源：从 local_skills 找到该 origin 记录的 name，再按 name 解析目录
        ls_name = ""
        for ls in self._state.get("local_skills", []):
            if isinstance(ls, dict) and str(ls.get("origin", "") or "").strip() == origin:
                ls_name = str(ls.get("name", "") or "").strip()
                break
        if ls_name:
            return self._resolve_local_skill_dir(ls_name)
        return None

    def _get_skill_evolution_path(self, skill_name: str) -> Path | None:
        skill_dir = self._resolve_local_skill_dir(skill_name)
        if skill_dir is None:
            return None
        return skill_dir / _EVOLUTION_FILENAME

    def _scan_marketplace_skills(self) -> list[dict]:
        """扫描 _marketplace/ 下已 clone 的仓库中未安装的 skill.

        扫描路径：_marketplace/{marketplace_name}/skills/{plugin_name}
        """
        results: list[dict] = []
        if not self._marketplace_dir.exists():
            return results

        installed_names = {p.get("name") for p in self._get_installed_plugins()}

        enabled_marketplaces = {
            m.get("name") for m in self._get_marketplaces() if bool(m.get("enabled", True)) and m.get("name")
        }

        for repo_dir in self._marketplace_dir.iterdir():
            if not repo_dir.is_dir():
                continue
            marketplace_name = repo_dir.name
            if marketplace_name not in enabled_marketplaces:
                continue

            # 检查 skills 子目录是否存在
            skills_dir = repo_dir / "skills"
            # 单skill兼容
            is_skills_dir = True
            if not skills_dir.exists() or not skills_dir.is_dir():
                # 如果没有 skills 子目录，尝试直接扫描 repo_dir（兼容旧结构）
                skills_dir = repo_dir
                is_skills_dir = False

            for plugin_dir in skills_dir.iterdir():
                if not is_skills_dir and plugin_dir != repo_dir:
                    continue
                if not plugin_dir.is_dir():
                    continue
                # 跳过 git 元数据和以 _ 开头的目录
                if plugin_dir.name.startswith((".", "_")):
                    continue
                md = self._try_find_skill_file(plugin_dir)
                if md is None:
                    continue
                meta = self._parse_skill_md(md)
                if meta is None:
                    continue

                # 跳过已安装的
                if meta.get("name") in installed_names:
                    continue

                # source 直接返回 marketplace 名称，便于前端安装时自动拼接 spec
                meta["source"] = marketplace_name
                meta["marketplace"] = marketplace_name
                meta["is_builtin"] = False
                meta["installed"] = False
                meta["has_evolutions"] = False
                self._apply_enabled_config(meta, meta.get("name", ""))
                meta.pop("body", None)
                results.append(meta)

        return results

    def _get_mirror_skills_dirs(self) -> list[Path]:
        """返回需要镜像同步的 skills 目录（不包含当前运行目录）.

        注意：开发模式下不返回源码目录作为镜像目标，避免用户下载的
        skill被复制到源码目录，重启后被误判为内置skill。
        """
        mirrors: list[Path] = []
        if is_package_installation():
            return []
        try:
            source_repo_root = Path(__file__).resolve().parents[2]
            source_resources_skills_dir = source_repo_root / "jiuwenswarm" / "resources" / "agent" / "skills"
            # 开发模式下不将源码目录作为镜像目标
            # 这样用户下载的skill只保存在用户目录，不会污染源码目录
            if (
                source_resources_skills_dir.exists()
                and source_resources_skills_dir.resolve() != self._skills_dir.resolve()
                and source_resources_skills_dir.resolve() != get_builtin_skills_dir().resolve()
            ):
                mirrors.append(source_resources_skills_dir)
        except Exception:
            return []
        return mirrors

    @staticmethod
    def _normalize_lang_suffix(name: str) -> str:
        """将 xxxx_zh.MD / xxxx_en.MD 规范为 xxxx.MD（去除 _zh/_en 后缀）。"""
        stem, suffix = name.rpartition(".")[0], name.rpartition(".")[2]
        suffix_lower = suffix.lower()
        if suffix_lower in ("md", "mdx"):
            stem_lower = stem.lower()
            if stem_lower.endswith("_zh"):
                stem = stem[:-3]
            elif stem_lower.endswith("_en"):
                stem = stem[:-3]
        return f"{stem}.{suffix}" if stem else name

    @staticmethod
    def _generate_agent_data_for_workspace(workspace_root: Path) -> None:
        """Generate agent/jiuwenclaw_workspace/agent-data.json from agent tree."""
        agent_root = workspace_root.resolve()
        output_path = (agent_root / "agent-data.json").resolve()
        root_folder_key = "__root__"

        if not agent_root.exists() or not agent_root.is_dir():
            return

        folder_data: dict[str, list[dict[str, str | bool]]] = {}
        seen_paths: dict[str, set[str]] = {}
        for entry in sorted(agent_root.rglob("*")):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            # Skip files in hidden directories (e.g., .agent_history)
            if any(part.startswith(".") for part in entry.relative_to(agent_root).parts):
                continue
            relative_folder_path = entry.parent.relative_to(agent_root.parent).as_posix()
            folder_key = root_folder_key if relative_folder_path == "." else relative_folder_path

            display_name = SkillManager._normalize_lang_suffix(entry.name)
            display_path = (
                f"agent/{relative_folder_path}/{display_name}".replace("/.", "/").replace("//", "/")
                if relative_folder_path != "."
                else f"agent/{display_name}"
            )

            seen = seen_paths.setdefault(folder_key, set())
            if display_path in seen:
                continue
            seen.add(display_path)

            folder_data.setdefault(folder_key, []).append(
                {
                    "name": display_name,
                    "path": display_path,
                    "isMarkdown": entry.suffix.lower() in {".md", ".mdx"},
                }
            )

        sorted_folder_data = {
            folder_key: sorted(files, key=lambda item: item["path"])
            for folder_key, files in sorted(folder_data.items(), key=lambda item: item[0])
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(sorted_folder_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _refresh_agent_data_indexes(self) -> None:
        """Refresh agent-data.json for runtime and mirror workspaces."""
        workspace_roots: set[Path] = {self._agent_root.resolve()}
        for mirror_root in self._get_mirror_skills_dirs():
            try:
                # mirror_root = .../agent/skills → agent 根目录为其 parent
                workspace_roots.add(mirror_root.parent.resolve())
            except Exception:
                continue
        for workspace_root in workspace_roots:
            try:
                self._generate_agent_data_for_workspace(workspace_root)
            except Exception as exc:
                logger.warning("重建 agent-data.json 失败: agent_root=%s error=%s", workspace_root, exc)

    @staticmethod
    def _locate_skill_dir(path: Path) -> Path | None:
        """定位包含 SKILL.md 的目录（优先当前目录，再向下递归）；文件名大小写不敏感."""
        if path.is_file() and path.name.lower() == "skill.md":
            return path.parent
        if path.is_dir():
            direct = path / "SKILL.md"
            if direct.is_file():
                return path
            for md in path.rglob("SKILL.md"):
                if md.is_file():
                    return md.parent
            # 兼容小写 skill.md（如 Linux 下仓库命名）
            for md in path.rglob("*.md"):
                if md.is_file() and md.name.lower() == "skill.md":
                    return md.parent
        return None

    @staticmethod
    def _get_team_skills_hub_base_url(override_url: str | None = None) -> str:
        raw = (override_url or os.getenv("TEAM_SKILLS_HUB_BASE_URL") or _TEAM_SKILLS_HUB_BASE_URL_DEFAULT).strip()
        return raw.rstrip("/")

    @staticmethod
    def _resolve_teamskills_hub_auth(params: dict[str, Any]) -> dict[str, str]:
        token = str(params.get("token") or "").strip()
        system_token = str(params.get("system_token") or "").strip()
        has_user = bool(token)
        has_system = bool(system_token)
        if has_user == has_system:
            return {"error": "请且仅请提供一种鉴权：token 或 system_token"}
        if has_system:
            return {"system_token": system_token}
        return {"token": token}

    def _prepare_teamskills_publish_zip(
        self,
        *,
        path_raw: str,
        file_raw: str,
        plugin_version: str,
        tmpdir: Path,
    ) -> Path:
        """对齐 jiuwen-teamskills：上传前规范化 zip，确保包含合法 plugin.yaml."""
        if file_raw:
            src_zip = Path(file_raw).expanduser().resolve()
            if not src_zip.is_file():
                raise RuntimeError(f"zip 文件不存在: {src_zip}")
            if src_zip.suffix.lower() != ".zip":
                raise RuntimeError("file 必须是 .zip 文件")
            stage_dir = tmpdir / "zip_stage"
            stage_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._safe_extract_zip_to_dir(src_zip, stage_dir)
            except zipfile.BadZipFile as exc:
                raise RuntimeError(f"zip 文件损坏或格式非法: {src_zip}") from exc
            return self._build_teamskills_publish_zip_from_root(stage_dir, plugin_version, tmpdir)

        root = Path(path_raw).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise RuntimeError(f"path 不是有效目录: {root}")
        return self._build_teamskills_publish_zip_from_root(root, plugin_version, tmpdir)

    def _build_teamskills_publish_zip_from_root(
        self,
        root: Path,
        plugin_version: str,
        tmpdir: Path,
    ) -> Path:
        skill_dir = self._locate_skill_dir(root)
        if skill_dir is None:
            raise RuntimeError("发布目录中未找到 SKILL.md")
        skill_md = skill_dir / "SKILL.md"
        meta = self._parse_skill_md(skill_md)
        if not meta:
            raise RuntimeError("SKILL.md 解析失败")

        skill_name = str(meta.get("name") or "").strip()
        if not skill_name:
            raise RuntimeError("SKILL.md frontmatter 缺少 name")
        description = str(meta.get("description") or "").strip() or skill_name
        display_name = str(meta.get("display_name") or "").strip() or skill_name
        author = str(meta.get("author") or "").strip() or "unknown"
        tags = meta.get("tags")
        if isinstance(tags, list):
            normalized_tags = [str(t).strip() for t in tags if str(t).strip()]
        elif isinstance(tags, str) and tags.strip():
            normalized_tags = [tags.strip()]
        else:
            normalized_tags = []
        if not normalized_tags:
            normalized_tags = ["teamskills"]

        # 与 jiuwen-teamskills 兼容：market publish 仍使用 runtime.type=skill
        plugin_yaml_payload = {
            "name": skill_name,
            "version": plugin_version,
            "display_name": display_name,
            "description": description,
            "runtime": {"type": "skill"},
            "metadata": {
                "author": author,
                "tags": normalized_tags,
            },
        }

        zip_path = tmpdir / "teamskills_publish_normalized.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{skill_name}/plugin.yaml",
                yaml.safe_dump(plugin_yaml_payload, sort_keys=False, allow_unicode=True),
            )
            readme = root / "README.md"
            if readme.is_file():
                zf.write(readme, arcname=f"{skill_name}/README.md")
            for child in skill_dir.rglob("*"):
                if not child.is_file():
                    continue
                rel = child.relative_to(skill_dir).as_posix()
                zf.write(child, arcname=f"{skill_name}/{skill_name}/{rel}")
        return zip_path

    @staticmethod
    def _normalize_teamskills_hub_http_error(resp: httpx.Response) -> str:
        detail = (resp.text or "").strip()[:300]
        if not detail:
            return f"Team Skills Hub API 错误 HTTP {resp.status_code}"
        return f"Team Skills Hub API 错误 HTTP {resp.status_code}: {detail}"

    async def _teamskills_hub_publish_request(
        self,
        *,
        base_url: str,
        zip_path: Path,
        checksum_sha256: str,
        plugin_id: str | None,
        plugin_version: str,
        version_desc: str,
        force: bool,
        token: str | None,
        system_token: str | None,
    ) -> dict[str, Any]:
        req_url = f"{base_url}/api/v1/plugins"
        headers: dict[str, str] = {"X-Checksum-SHA256": checksum_sha256}
        if system_token:
            headers["X-System-Token"] = system_token
        else:
            headers["Authorization"] = f"Bearer {token}"

        data: dict[str, str] = {
            "force": "true" if force else "false",
            "version_desc": version_desc,
            "plugin_version": plugin_version,
        }
        if plugin_id:
            data["plugin_id"] = plugin_id
        files = {"file": (zip_path.name, zip_path.read_bytes(), "application/zip")}

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            resp = await client.post(req_url, data=data, files=files, headers=headers)
        if not resp.is_success:
            raise RuntimeError(self._normalize_teamskills_hub_http_error(resp))
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Team Skills Hub API 响应格式错误")
        code = payload.get("code", 200)
        if int(code) != 200:
            message = str(payload.get("message") or "").strip() or "Team Skills Hub API 返回失败"
            raise RuntimeError(message)
        data_payload = payload.get("data")
        if isinstance(data_payload, dict):
            return data_payload
        if data_payload is None:
            return {}
        raise RuntimeError("Team Skills Hub API 响应 data 格式错误")

    async def _teamskills_hub_delete_request(
        self,
        *,
        base_url: str,
        skill_id: str,
        version: str,
        token: str | None,
        system_token: str | None,
    ) -> None:
        req_url = f"{base_url}/api/v1/plugins/{quote(skill_id, safe='')}/versions/{quote(version, safe='')}"
        headers: dict[str, str] = {}
        if system_token:
            headers["X-System-Token"] = system_token
        else:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            resp = await client.delete(req_url, headers=headers)
        if not resp.is_success:
            raise RuntimeError(self._normalize_teamskills_hub_http_error(resp))

    @staticmethod
    def _get_team_skills_hub_allowed_download_hosts() -> list[str]:
        raw = (os.getenv("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS") or "").strip()
        if not raw:
            return list(_TEAM_SKILLS_HUB_DEFAULT_ALLOWED_DOWNLOAD_HOSTS)
        hosts: list[str] = []
        for token in raw.split(","):
            host = token.strip().lower()
            if not host:
                continue
            hosts.append(host)
        return hosts or list(_TEAM_SKILLS_HUB_DEFAULT_ALLOWED_DOWNLOAD_HOSTS)

    @staticmethod
    def _get_import_local_allowed_download_hosts() -> list[str]:
        raw = (os.getenv("IMPORT_LOCAL_ALLOWED_DOWNLOAD_HOSTS") or "").strip()
        if not raw:
            return list(_IMPORT_LOCAL_DEFAULT_ALLOWED_DOWNLOAD_HOSTS)
        hosts: list[str] = []
        for token in raw.split(","):
            host = token.strip().lower()
            if not host:
                continue
            hosts.append(host)
        return hosts or list(_IMPORT_LOCAL_DEFAULT_ALLOWED_DOWNLOAD_HOSTS)

    @staticmethod
    def _assert_team_skills_hub_download_url_allowed(download_url: str) -> None:
        parsed = urlparse(download_url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("Team Skills Hub download_url 必须使用 HTTP 或 HTTPS")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise RuntimeError("Team Skills Hub download_url 缺少主机名")
        for rule in SkillManager._get_team_skills_hub_allowed_download_hosts():
            # 支持 .example.com 后缀匹配与 * 单段通配（如 a.*.c.com）。
            if rule.startswith("."):
                if host.endswith(rule):
                    return
                continue
            if SkillManager._team_skills_hub_host_matches_rule(host, rule):
                return
        raise RuntimeError(f"Team Skills Hub download_url host 不在白名单: {host}")

    @staticmethod
    def _assert_import_local_download_url_allowed(download_url: str) -> None:
        parsed = urlparse(download_url)
        if parsed.scheme != "https":
            raise RuntimeError("远程导入 URL 必须使用 HTTPS")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise RuntimeError("远程导入 URL 缺少主机名")
        for rule in SkillManager._get_import_local_allowed_download_hosts():
            if rule.startswith("."):
                if host.endswith(rule):
                    return
                continue
            if SkillManager._team_skills_hub_host_matches_rule(host, rule):
                return
        raise RuntimeError(f"远程导入 URL host 不在白名单: {host}")

    @staticmethod
    def _team_skills_hub_host_matches_rule(host: str, rule: str) -> bool:
        host_parts = host.split(".")
        rule_parts = rule.split(".")
        if len(host_parts) != len(rule_parts):
            return False
        for host_part, rule_part in zip(host_parts, rule_parts):
            if rule_part == "*":
                continue
            if host_part != rule_part:
                return False
        return True

    @staticmethod
    def _is_http_download_target(value: str) -> bool:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _is_github_skill_folder_url(url: str) -> bool:
        """GitHub tree/blob 目录链接，走 skillnet-ai 按文件拉取。"""
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        if host not in ("github.com", "www.github.com"):
            return False
        path = (parsed.path or "").lower()
        return "/tree/" in path or "/blob/" in path

    def _assert_skill_download_url_allowed(
        self,
        download_url: str,
        *,
        allowed_hosts: tuple[str, ...] | None = None,
    ) -> None:
        """SkillHub / 远程归档下载主机白名单（import_local 与 Team Skills Hub 并集）。"""
        if allowed_hosts:
            parsed = urlparse(str(download_url or "").strip())
            host = (parsed.hostname or "").strip().lower()
            if parsed.scheme != "https":
                raise RuntimeError("skill 下载 URL 必须使用 HTTPS")
            if not host:
                raise RuntimeError("skill 下载 URL 缺少主机名")
            if parsed.username or parsed.password:
                raise RuntimeError("skill 下载 URL 不能包含用户信息")
            if not any(
                SkillManager._team_skills_hub_host_matches_rule(host, rule)
                for rule in allowed_hosts
            ):
                raise RuntimeError(
                    f"skill 下载 URL 主机不在来源白名单: {host}"
                    "，请在来源 download_policy.allowed_hosts 中配置该主机"
                )
            return
        errors: list[str] = []
        for checker in (
            self._assert_import_local_download_url_allowed,
            self._assert_team_skills_hub_download_url_allowed,
        ):
            try:
                checker(download_url)
                return
            except RuntimeError as exc:
                errors.append(str(exc))
        host = (urlparse(download_url).hostname or "").strip().lower() or "unknown"
        raise RuntimeError(
            f"skill 下载 URL 主机不在白名单: {host}"
            + (f" ({errors[0]})" if errors else "")
            + "，请在来源 download_policy.allowed_hosts 中配置该主机"
        )

    def _download_http_archive_bytes_sync(self, download_url: str) -> bytes:
        """同步下载 HTTPS 直链 zip/tar 归档（SkillHub CDN 等）。"""
        timeout = max(30.0, _IMPORT_LOCAL_REMOTE_TIMEOUT)
        logger.info("[SkillManager] skill archive download start: url=%s", download_url)
        with requests.Session() as session:
            session.mount("https://", _ImportLocalTLSAdapter())
            with session.get(
                download_url.strip(),
                timeout=timeout,
                stream=True,
                allow_redirects=True,
                verify=False,
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        chunks.append(chunk)
                body = b"".join(chunks)
        logger.info(
            "[SkillManager] skill archive download done: url=%s bytes=%s",
            download_url,
            len(body),
        )
        if not body:
            raise RuntimeError("下载内容为空")
        if not self._detect_archive_format(body):
            raise RuntimeError("下载内容不是受支持的归档格式，目前仅支持 zip/tar/tar.gz/tgz")
        return body

    async def _team_skills_hub_http_get_data(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = _TEAM_SKILLS_HUB_MARKET_TIMEOUT,
        base_url: str | None = None,
    ) -> Any:
        base_url = (base_url or self._get_team_skills_hub_base_url()).rstrip("/")
        rel_path = path if path.startswith("/") else f"/{path}"
        req_url = f"{base_url}{rel_path}"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.get(req_url, params=params)
        except Exception as exc:
            raise RuntimeError(f"无法连接 Team Skills Hub: {exc}") from exc

        if not resp.is_success:
            detail = (resp.text or "").strip()[:300]
            raise RuntimeError(f"Team Skills Hub API 错误 HTTP {resp.status_code}: {detail}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Team Skills Hub API 响应不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Team Skills Hub API 响应格式错误")

        code = payload.get("code", 200)
        if int(code) != 200:
            message = str(payload.get("message", "")).strip() or "Team Skills Hub API 返回失败"
            raise RuntimeError(message)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Team Skills Hub API 响应 data 格式错误")
        return data

    @staticmethod
    def _safe_extract_zip_members_into(zf: zipfile.ZipFile, dest_root: Path) -> None:
        """将已打开的 ZIP 成员解压到 dest_root（须为 resolve() 后的目录），拒绝 Zip Slip。"""
        for info in zf.infolist():
            raw = (info.filename or "").replace("\\", "/")
            if not raw or raw.startswith("/"):
                continue
            if "\0" in raw:
                raise RuntimeError("ZIP 包含非法文件名")
            is_dir = raw.endswith("/") or info.is_dir()
            rel_str = raw.rstrip("/")
            if not rel_str:
                continue
            rel = PurePosixPath(rel_str)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError("ZIP 包含非法路径")
            dest_path = dest_root.joinpath(*rel.parts)
            try:
                dest_path = dest_path.resolve()
                dest_path.relative_to(dest_root)
            except ValueError as exc:
                raise RuntimeError("ZIP 路径越界") from exc
            if is_dir:
                dest_path.mkdir(parents=True, exist_ok=True)
                continue
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src:
                dest_path.write_bytes(src.read())

    @staticmethod
    def _safe_extract_zip_bytes_to_dir(data: bytes, dest_dir: Path) -> None:
        """将 ZIP 字节解压到 dest_dir（不落盘 staging zip，与 ClawHub extractall 语义一致）。"""
        dest_root = dest_dir.resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            SkillManager._safe_extract_zip_members_into(zf, dest_root)

    @staticmethod
    def _safe_extract_zip_to_dir(zip_path: Path, dest_dir: Path) -> None:
        """将 ZIP 文件解压到 dest_dir，拒绝 Zip Slip（..、绝对路径、写出目标目录外）。"""
        dest_root = dest_dir.resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            SkillManager._safe_extract_zip_members_into(zf, dest_root)

    @staticmethod
    def _safe_extract_tar_to_dir(tar_path: Path, dest_dir: Path) -> None:
        """Extract TAR/TAR.GZ/TGZ safely into dest_dir."""
        dest_root = dest_dir.resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r:*") as tf:
            for member in tf.getmembers():
                raw = (member.name or "").replace("\\", "/")
                if not raw or raw.startswith("/"):
                    continue
                if "\0" in raw:
                    raise RuntimeError("归档包含非法文件名")
                rel = PurePosixPath(raw.rstrip("/"))
                if not rel.parts:
                    continue
                if rel.is_absolute() or ".." in rel.parts:
                    raise RuntimeError("归档包含非法路径")
                if member.islnk() or member.issym():
                    raise RuntimeError("归档包含链接文件，已拒绝导入")
                dest_path = dest_root.joinpath(*rel.parts)
                try:
                    dest_path = dest_path.resolve()
                    dest_path.relative_to(dest_root)
                except ValueError as exc:
                    raise RuntimeError("归档路径越界") from exc
                if member.isdir():
                    dest_path.mkdir(parents=True, exist_ok=True)
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with extracted:
                    dest_path.write_bytes(extracted.read())

    @staticmethod
    def _detect_archive_format(body: bytes) -> str:
        if len(body) >= 4 and body.startswith(b"PK"):
            return "zip"
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*"):
                return "tar"
        except tarfile.TarError:
            pass
        return ""

    def _extract_archive_bytes_to_dir(self, body: bytes, dest_dir: Path) -> None:
        archive_format = self._detect_archive_format(body)
        logger.info(
            "[SkillManager] extract archive: format=%s bytes=%s dest_dir=%s",
            archive_format or "unknown",
            len(body),
            dest_dir,
        )
        if archive_format == "zip":
            archive_path = dest_dir / "artifact.zip"
            archive_path.write_bytes(body)
            self._safe_extract_zip_to_dir(archive_path, dest_dir)
            return
        if archive_format == "tar":
            archive_path = dest_dir / "artifact.tar"
            archive_path.write_bytes(body)
            self._safe_extract_tar_to_dir(archive_path, dest_dir)
            return
        raise RuntimeError("下载内容不是受支持的归档格式，目前仅支持 zip/tar/tar.gz/tgz")

    async def _download_remote_archive_and_verify(
        self,
        download_url: str,
        *,
        checksum_sha256: str = "",
        timeout: float | None = None,
    ) -> bytes:
        timeout = max(30.0, timeout or _IMPORT_LOCAL_REMOTE_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
            body = resp.content or b""

        if not body:
            raise RuntimeError("下载内容为空")

        if not _sha256_matches(body, checksum_sha256):
            raise RuntimeError("下载文件校验失败（SHA256 不匹配）")

        archive_format = self._detect_archive_format(body)
        if archive_format == "zip":
            try:
                with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
                    if zf.testzip() is not None:
                        raise RuntimeError("下载 ZIP 文件已损坏")
            except zipfile.BadZipFile as exc:
                raise RuntimeError("下载内容不是有效 ZIP 文件") from exc
            return body
        if archive_format == "tar":
            try:
                with tarfile.open(fileobj=io.BytesIO(body), mode="r:*"):
                    pass
            except tarfile.TarError as exc:
                raise RuntimeError("下载内容不是有效 TAR 归档") from exc
            return body
        raise RuntimeError("下载内容不是受支持的归档格式，目前仅支持 zip/tar/tar.gz/tgz")

    async def _download_zip_and_verify(
        self,
        download_url: str,
        *,
        checksum_sha256: str = "",
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        parsed = urlparse(str(download_url or "").strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("skill 下载 URL 必须是 HTTPS 地址")
        timeout = max(30.0, timeout or _TEAM_SKILLS_HUB_MARKET_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(download_url)
            if resp.is_redirect:
                raise RuntimeError("skill 下载不支持自动跳转，请提供最终制品地址")
            resp.raise_for_status()
            body = resp.content or b""

        if not body:
            raise RuntimeError("下载内容为空")
        if max_bytes is not None and len(body) > max_bytes:
            raise RuntimeError("下载内容超过来源允许的最大大小")
        if len(body) < 4 or not body.startswith(b"PK"):
            raise RuntimeError("下载内容不是 ZIP 文件")

        if not _sha256_matches(body, checksum_sha256):
            raise RuntimeError("下载文件校验失败（SHA256 不匹配）")

        try:
            with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
                if zf.testzip() is not None:
                    raise RuntimeError("下载 ZIP 文件已损坏")
        except zipfile.BadZipFile as exc:
            raise RuntimeError("下载内容不是有效 ZIP 文件") from exc
        return body

    @staticmethod
    def _get_github_token() -> str:
        return str(get_local_config("GITHUB_TOKEN", "") or "").strip()

    @staticmethod
    def _skillnet_eval_llm_params() -> dict[str, str | None]:
        """与主对话一致的 API Key / Base URL / 模型名（config.yaml react 段）."""
        try:
            from jiuwenswarm.common.config import get_config
        except Exception:
            return {
                "api_key": str(get_local_config("API_KEY", "") or "").strip() or None,
                "base_url": str(get_local_config("API_BASE", "") or "").strip() or None,
                "model": str(get_local_config("MODEL_NAME", "gpt-4o") or "gpt-4o").strip(),
            }

        cfg = get_config() or {}
        react = cfg.get("react") or {}
        mcc = react.get("model_client_config") or {}
        api_key = (mcc.get("api_key") or get_local_config("API_KEY", "") or "").strip()
        base_url = (mcc.get("api_base") or get_local_config("API_BASE", "") or "").strip()
        model = (react.get("model_name") or get_local_config("MODEL_NAME", "gpt-4o") or "gpt-4o").strip()
        if base_url.endswith("/chat/completions"):
            base_url = base_url.rsplit("/chat/completions", 1)[0]
        return {
            "api_key": api_key or None,
            "base_url": base_url or None,
            "model": model or "gpt-4o",
        }

    @staticmethod
    def _skillnet_evaluate_sync(skill_url: str) -> dict[str, Any]:
        """同步 evaluate，供 asyncio.to_thread 调用."""
        try:
            from skillnet_ai import SkillNetClient
            from skillnet_ai.client import SkillNetError
        except Exception:
            return {
                "ok": False,
                "detail": "未安装 skillnet-ai，请先安装依赖: pip install skillnet-ai",
                "detail_key": "skills.skillNet.errors.skillnetAiMissing",
            }

        llm = SkillManager._skillnet_eval_llm_params()
        if not llm.get("api_key"):
            return {
                "ok": False,
                "detail": "",
                "detail_key": "skills.skillNet.errors.evaluateNoApiKey",
            }

        kwargs: dict[str, Any] = {
            "api_key": llm["api_key"],
            "base_url": llm["base_url"],
            "github_token": SkillManager._get_github_token() or None,
        }
        try:
            with _skillnet_network_context():
                client = SkillNetClient(**kwargs)
                result = client.evaluate(target=skill_url, model=str(llm["model"]))
        except SkillNetError as exc:
            return {"ok": False, "detail": str(exc).strip() or "评估失败。"}
        except Exception as exc:
            logger.exception("SkillNet evaluate 异常")
            return {"ok": False, "detail": str(exc).strip() or "评估失败。"}

        if not isinstance(result, dict):
            return {"ok": True, "evaluation": result}
        return {"ok": True, "evaluation": result}

    @staticmethod
    def _skillnet_search_sync(search_kwargs: dict[str, Any]) -> list[Any]:
        """同步调用 skillnet-ai search，供 asyncio.to_thread 使用."""
        try:
            from skillnet_ai.searcher import SkillNetSearcher
        except Exception as exc:
            raise RuntimeError("未安装 skillnet-ai，请先安装依赖: pip install skillnet-ai") from exc

        with _skillnet_network_context():
            searcher = SkillNetSearcher()
            _configure_skillnet_requests_session(searcher.session)
            results = searcher.search(**search_kwargs)
        if results is None:
            return []
        if isinstance(results, list):
            return results
        return list(results)

    @staticmethod
    def _github_skillnet_install_error_context(skill_url: str) -> str:
        """下载失败时拉 GitHub Contents 与 rate_limit，把官方 message 等拼给前端."""
        try:
            from skillnet_ai.downloader import SkillDownloader
        except ImportError:
            return ""

        dl = SkillDownloader(api_token=SkillManager._get_github_token())
        _configure_skillnet_requests_session(dl.session)
        parsed = dl._parse_github_url(skill_url)
        if not parsed:
            return ""

        owner, repo, ref, dir_path, _ = parsed
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{dir_path}?ref={ref}"
        try:
            with _skillnet_network_context():
                r = dl.session.get(api, timeout=_SKILLNET_DOWNLOAD_TIMEOUT)
        except Exception as exc:
            logger.debug("SkillNet 安装错误上下文: GitHub Contents 请求失败: %s", exc)
            return ""

        parts: list[str] = []
        if r.status_code != 200:
            try:
                body = r.json()
                msg = body.get("message")
                if isinstance(msg, str) and msg.strip():
                    parts.append(msg.strip()[:800])
                else:
                    raw = (r.text or "").strip()[:500]
                    if raw:
                        parts.append(f"HTTP {r.status_code}: {raw}")
            except Exception as exc:
                logger.debug("SkillNet 安装错误上下文: 解析 GitHub 错误 JSON 失败: %s", exc)
                raw = (r.text or "").strip()[:500]
                if raw:
                    parts.append(f"HTTP {r.status_code}: {raw}")

            if r.status_code == 403 or any("rate limit" in p.lower() for p in parts):
                try:
                    with _skillnet_network_context():
                        rl = dl.session.get("https://api.github.com/rate_limit", timeout=12)
                    if rl.status_code == 200:
                        core = rl.json().get("resources", {}).get("core") or {}
                        rem, lim = core.get("remaining"), core.get("limit")
                        if rem is not None and lim is not None:
                            parts.append(
                                f"GitHub 核心 API 剩余 {rem}/{lim}，"
                                "可在配置页「第三方服务」填写 github_token（GITHUB_TOKEN）提高额度"
                            )
                except Exception as exc:
                    logger.debug(
                        "SkillNet 安装错误上下文: GitHub rate_limit 请求失败: %s",
                        exc,
                    )

        return " | ".join(parts) if parts else ""

    @staticmethod
    def _skillnet_download_sync(skill_url: str, target_dir: str, mirror_url: str | None = None) -> str:
        """同步调用 skillnet-ai download；失败时附带 GitHub API 返回说明（如前端的限流文案）。"""
        try:
            from skillnet_ai.downloader import SkillDownloader, GitHubAPIError
        except Exception as exc:
            raise RuntimeError("未安装 skillnet-ai，请先安装依赖: pip install skillnet-ai") from exc

        token = SkillManager._get_github_token()
        dl_kwargs: dict[str, Any] = {
            "api_token": token,
            "timeout": _SKILLNET_DOWNLOAD_TIMEOUT,
            "max_retries": _SKILLNET_MAX_RETRIES,
        }
        if mirror_url:
            dl_kwargs["mirror_url"] = mirror_url
        with _skillnet_network_context():
            downloader = SkillDownloader(**dl_kwargs)
            _configure_skillnet_requests_session(downloader.session)
            try:
                local_path = downloader.download(folder_url=skill_url, target_dir=target_dir)
            except GitHubAPIError as exc:
                raise _map_github_api_error(exc) from exc
            except Exception as exc:
                ctx = SkillManager._github_skillnet_install_error_context(skill_url)
                if ctx:
                    raise RuntimeError(f"{exc} | {ctx}") from exc
                raise
        if not local_path:
            # skillnet-ai 在多种情况下会无异常地返回 None：URL 无效、目录下列表为空、
            # 或 Contents API 成功但拉 raw 文件全部失败（超时/网络）等，库未区分原因。
            ctx = SkillManager._github_skillnet_install_error_context(skill_url)
            raise SkillNetEmptyDownloadError(github_context=ctx)
        return str(local_path)

    async def _git_clone(self, url: str, dest: Path) -> str | None:
        """浅克隆 git 仓库，返回 commit hash 或 None."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return await self._git_get_commit(dest)
        staging = dest.with_name(f".{dest.name}.clone-{uuid.uuid4().hex}")

        def cleanup_partial_clone() -> None:
            if not staging.exists():
                return
            if not _safe_rmtree(staging):
                logger.warning("清理未完成的 git clone 目录失败: %s", staging)

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                url,
                str(staging),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_MARKETPLACE_GIT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                await proc.communicate()
                logger.error(
                    "git clone 超时（%.1fs）: %s",
                    _MARKETPLACE_GIT_TIMEOUT_SECONDS,
                    url,
                )
                cleanup_partial_clone()
                return None
            if proc.returncode != 0:
                logger.error("git clone 失败: %s", stderr.decode(errors="replace"))
                cleanup_partial_clone()
                return None
            try:
                staging.rename(dest)
            except OSError:
                # 另一个并发 clone 已率先发布时，只清理本请求独占的暂存目录。
                if dest.exists():
                    cleanup_partial_clone()
                    return await self._git_get_commit(dest)
                raise
            return await self._git_get_commit(dest)
        except Exception as exc:
            cleanup_partial_clone()
            logger.error("git clone 异常: %s", exc)
            return None

    async def _git_pull(self, repo_path: Path) -> str | None:
        """拉取最新代码，返回 commit hash 或 None."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo_path),
                "pull",
                "--ff-only",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_MARKETPLACE_GIT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                await proc.communicate()
                logger.warning(
                    "git pull 超时（%.1fs）: %s",
                    _MARKETPLACE_GIT_TIMEOUT_SECONDS,
                    repo_path,
                )
                return None
            if proc.returncode != 0:
                logger.warning("git pull 失败: %s", stderr.decode(errors="replace"))
                return None
            return await self._git_get_commit(repo_path)
        except Exception as exc:
            logger.warning("git pull 异常: %s", exc)
            return None

    async def _git_get_commit(self, repo_path: Path) -> str | None:
        """获取当前 HEAD commit hash."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo_path),
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            return stdout.decode().strip()
        except Exception:
            return None

    async def _sync_marketplace_repos(self) -> None:
        """同步所有已配置 marketplace 到本地目录（存在则 pull，不存在则 clone）."""
        marketplaces = [m for m in self._get_marketplaces() if bool(m.get("enabled", True))]
        if not marketplaces:
            return

        self._marketplace_dir.mkdir(parents=True, exist_ok=True)

        for marketplace in marketplaces:
            name = marketplace.get("name", "")
            url = marketplace.get("url", "")
            if not name or not url:
                continue
            try:
                repo_dir = _safe_child_path(self._marketplace_dir, name, "marketplace")
            except ValueError as exc:
                _log_rejected_name("skills.marketplace.sync", "marketplace", name, exc)
                continue
            try:
                if repo_dir.exists():
                    await self._git_pull(repo_dir)
                else:
                    await self._git_clone(url, repo_dir)
            except Exception as exc:
                logger.warning(
                    "同步 marketplace 失败: name=%s url=%s error=%s",
                    name,
                    url,
                    exc,
                )

    # -----------------------------------------------------------------------
    # Plugin handlers (plugins.*)
    # -----------------------------------------------------------------------

    async def handle_plugins_list(self, params: dict) -> dict:
        """列出所有已安装插件（含启用/禁用状态）."""
        raw = self._get_installed_plugins()
        plugins = []
        for p in raw:
            p = self._normalize_plugin(p)
            name = p.get("name", "")
            marketplace = p.get("marketplace", "")
            spec = f"{name}@{marketplace}" if marketplace else name
            plugins.append({
                "plugin_name": name,
                "marketplace": marketplace,
                "spec": spec,
                "version": p.get("version", ""),
                "installed_at": p.get("installed_at", ""),
                "git_commit": p.get("commit", ""),
                "skills": p.get("skills", [name] if name else []),
                "enabled": p.get("enabled", True),
            })
        return {"plugins": plugins}

    async def handle_plugins_install(self, params: dict) -> dict:
        """安装插件，支持 marketplace spec 或本地路径/URL."""
        spec = params.get("spec", "")
        if not spec:
            return {"success": False, "detail": "缺少参数: spec"}

        # 检测本地路径或 URL
        is_local = spec.startswith("/") or spec.startswith("./") or spec.startswith("../") or spec.startswith("~")
        is_url = spec.startswith("http://") or spec.startswith("https://")

        if is_local or is_url:
            # 使用 import_local 流程，然后注册为 plugin
            result = await self.handle_skills_import_local({"path": spec, "force": params.get("force", False)})
            if result.get("success"):
                # 从返回结果中提取 plugin name 并设为 enabled
                skill_name = result.get("skill", {}).get("name", "") if isinstance(result.get("skill"), dict) else ""
                if not skill_name:
                    # fallback: 从路径提取名称
                    skill_name = os.path.basename(spec.rstrip("/").rstrip(".git"))
                # 确保在 installed_plugins 中有记录
                existing = [p for p in self._get_installed_plugins() if p.get("name") == skill_name]
                if not existing:
                    self._add_installed_plugin({
                        "name": skill_name,
                        "marketplace": "",
                        "version": "",
                        "commit": "",
                        "source": "local",
                        "installed_at": datetime.now(timezone.utc).isoformat(),
                        "skills": [skill_name],
                    })
                self._set_plugin_enabled(skill_name, True)
            return result

        # marketplace install
        result = await self.handle_skills_install(params)
        if result.get("success"):
            plugin_name = spec.rsplit("@", 1)[0] if "@" in spec else spec
            self._set_plugin_enabled(plugin_name, True)
        return result

    async def handle_plugins_uninstall(self, params: dict) -> dict:
        """卸载插件，复用 skills.uninstall 逻辑."""
        name = params.get("name", "")
        if name:
            try:
                safe_name = _safe_path_name(name, "plugin")
                disabled_dir = self._skills_dir / "_disabled_plugins" / safe_name
                if disabled_dir.exists():
                    _safe_rmtree(disabled_dir)
            except ValueError:
                pass
        return await self.handle_skills_uninstall(params)

    async def handle_plugins_enable(self, params: dict) -> dict:
        """启用插件."""
        name = params.get("name", "")
        if not name:
            return {"success": False, "detail": "缺少参数: name"}
        ok = self._set_plugin_enabled(name, True)
        if not ok:
            return {"success": False, "detail": f"未找到插件: {name}"}
        return {"success": True, "detail": f"插件 {name} 已启用，请执行 /reload-plugins 使其生效"}

    async def handle_plugins_disable(self, params: dict) -> dict:
        """禁用插件."""
        name = params.get("name", "")
        if not name:
            return {"success": False, "detail": "缺少参数: name"}
        ok = self._set_plugin_enabled(name, False)
        if not ok:
            return {"success": False, "detail": f"未找到插件: {name}"}
        return {"success": True, "detail": f"插件 {name} 已禁用，请执行 /reload-plugins 使其生效"}

    async def handle_plugins_reload(self, params: dict) -> dict:
        """重载插件：根据 enabled 状态物理移动技能目录，然后统计摘要."""
        # 规范化所有插件记录
        for p in self._get_installed_plugins():
            self._normalize_plugin(p)
        self._save_state()

        # 根据 enabled 状态物理移动技能目录
        disabled_cache = self._skills_dir / "_disabled_plugins"
        all_plugins = self._get_installed_plugins()

        for p in all_plugins:
            name = p.get("name", "")
            if not name:
                continue
            skill_dir = self._skills_dir / name
            cached_dir = disabled_cache / name

            if p.get("enabled", True):
                # 启用状态：从 disabled 缓存恢复
                if cached_dir.exists() and not skill_dir.exists():
                    disabled_cache.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(cached_dir), str(skill_dir))
            else:
                # 禁用状态：移动到 disabled 缓存
                if skill_dir.exists():
                    disabled_cache.mkdir(parents=True, exist_ok=True)
                    if cached_dir.exists():
                        _safe_rmtree(cached_dir)
                    shutil.move(str(skill_dir), str(cached_dir))

        # 统计
        enabled_plugins = [p for p in all_plugins if p.get("enabled", True)]
        disabled_plugins = [p for p in all_plugins if not p.get("enabled", True)]

        total_skills = 0
        for p in enabled_plugins:
            skills = p.get("skills", [])
            if not skills and p.get("name"):
                skills = [p["name"]]
            total_skills += len(skills)

        return {
            "success": True,
            "plugins_count": len(enabled_plugins),
            "disabled_count": len(disabled_plugins),
            "skills_count": total_skills,
            "detail": f"Reloaded: {len(enabled_plugins)} plugins · {total_skills} skills"
            + (f" ({len(disabled_plugins)} disabled)" if disabled_plugins else ""),
        }

    # -----------------------------------------------------------------------
    # 状态持久化
    # -----------------------------------------------------------------------

    @contextmanager
    def state_transaction(self) -> Iterator[None]:
        """Reload and mutate enterprise Skill state under one workspace lock.

        Public transaction entry used by the ``_state_transactional`` decorator
        and by user-install / uninstall / whitelist paths that mutate
        ``skills_state.json``. Holds a cross-pod file lock (portalocker) on the
        workspace ledger so concurrent AgentServer pods sharing one workspace
        never lose each other's writes. Non-enterprise or non-persistent
        instances yield immediately (no lock, no reload).
        """
        if not is_enterprise() or not self._persist_skills_state:
            yield
            return
        with enterprise_skill_state_lock(self._state_file) as outermost:
            if outermost:
                self._state = self._load_state()
            yield

    def _load_state(self) -> dict[str, Any]:
        """加载 skills_state.json，失败时返回默认空状态."""
        try:
            if self._state_file.exists():
                state = json.loads(self._state_file.read_text(encoding="utf-8"))
                self._normalize_state(state)
                return state
        except Exception:
            logger.warning("加载 skills_state.json 失败，使用默认空状态")
        default_state = {"marketplaces": [], "installed_plugins": [], "local_skills": []}
        self._normalize_state(default_state)
        return default_state

    def _save_state(self) -> None:
        """持久化状态到 skills_state.json（企业路径可关闭）。"""
        if not self._persist_skills_state:
            return
        temp_path: Path | None = None
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._state_file.with_name(
                f".{self._state_file.name}.{uuid.uuid4().hex}.tmp"
            )
            temp_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self._state_file)
        except Exception:
            logger.error("保存 skills_state.json 失败")
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    logger.debug("清理 skills_state.json 临时文件失败: %s", temp_path)

    def _get_marketplaces(self) -> list[dict]:
        marketplaces = self._state.get("marketplaces", [])
        normalized = self.normalize_marketplaces(marketplaces)
        # 仅当结构发生变化时写回，避免每次读取都触盘。
        if normalized != marketplaces:
            self._state["marketplaces"] = normalized
            self._save_state()
        return normalized

    def _add_marketplace(self, marketplace: dict) -> None:
        self._state.setdefault("marketplaces", []).append(marketplace)
        self._state["marketplaces"] = self.normalize_marketplaces(self._state.get("marketplaces", []))
        self._save_state()

    def _remove_marketplace(self, name: str) -> bool:
        marketplaces = self._state.get("marketplaces", [])
        kept = [m for m in marketplaces if m.get("name") != name]
        if len(kept) == len(marketplaces):
            return False
        self._state["marketplaces"] = self.normalize_marketplaces(kept)
        self._save_state()
        return True

    def _set_marketplace_enabled(self, name: str, enabled: bool) -> bool:
        marketplaces = self.normalize_marketplaces(self._state.get("marketplaces", []))
        updated = False
        for marketplace in marketplaces:
            if marketplace.get("name") == name:
                marketplace["enabled"] = bool(enabled)
                updated = True
                break
        if updated:
            self._state["marketplaces"] = marketplaces
            self._save_state()
        return updated

    def _set_marketplace_last_updated(self, name: str) -> bool:
        marketplaces = self.normalize_marketplaces(self._state.get("marketplaces", []))
        updated = False
        for marketplace in marketplaces:
            if marketplace.get("name") == name:
                marketplace["last_updated"] = datetime.now(timezone.utc).isoformat()
                updated = True
                break
        if updated:
            self._state["marketplaces"] = marketplaces
            self._save_state()
        return updated

    @staticmethod
    def normalize_marketplaces(raw_marketplaces: Any) -> list[dict]:
        normalized: list[dict] = []
        if not isinstance(raw_marketplaces, list):
            return normalized
        for item in raw_marketplaces:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            url = item.get("url", "")
            if not name or not url:
                continue
            normalized.append(
                {
                    **item,
                    "name": name,
                    "url": url,
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        return normalized

    def _normalize_state(self, state: dict[str, Any]) -> None:
        state.setdefault("marketplaces", [])
        state.setdefault("installed_plugins", [])
        state.setdefault("local_skills", [])
        state.setdefault("skill_configs", {})
        state["marketplaces"] = self.normalize_marketplaces(state.get("marketplaces"))
        state["local_skills"] = normalize_local_skills(
            state.get("local_skills"),
            self._collect_existing_local_skill_names(),
            self._collect_existing_clawhub_origins(),
        )
        state["skill_configs"] = normalize_skill_configs(state.get("skill_configs"))

    def _get_installed_plugins(self) -> list[dict]:
        return self._state.get("installed_plugins", [])

    # -----------------------------------------------------------------------
    # 供 AgentServer 内部其它组件复用的轻量公开查询接口
    # -----------------------------------------------------------------------

    def get_installed_plugins(self) -> list[dict]:
        """返回已安装插件记录的拷贝。"""
        return list(self._get_installed_plugins())

    def list_skill_installations(self) -> list[dict[str, Any]]:
        """Return workspace installation records without exposing mutable state."""
        return [
            dict(record)
            for record in self._get_installed_plugins()
            if isinstance(record, dict)
        ]

    def find_skill_dir_by_identity(self, *, skill_id: str, version: str) -> str:
        """盘→账本回填定位：返回 SKILL.md 声明同 ``skill_id+version`` 的目录名，未命中返回空串."""
        sid = str(skill_id or "").strip()
        ver = str(version or "").strip()
        if not sid or not ver or not self._skills_dir.exists():
            return ""
        for child in sorted(self._skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if not isinstance(meta, dict):
                continue
            if str(meta.get("skill_id") or "").strip() != sid:
                continue
            if str(meta.get("version") or "").strip() != ver:
                continue
            return child.name
        return ""

    def _skill_installation_entity_ready(self, record: dict[str, Any]) -> bool:
        entity_dir = str(record.get("entity_dir") or record.get("name") or "").strip()
        if not entity_dir:
            return False
        try:
            entity_path = _safe_child_path(self._skills_dir, entity_dir, "skill")
        except ValueError:
            return False
        return entity_path.is_dir() and (entity_path / "SKILL.md").is_file()

    def _installation_enabled(self, record: dict[str, Any]) -> bool:
        name = str(record.get("name") or "").strip()
        configs = self._state.get("skill_configs")
        if isinstance(configs, dict) and isinstance(configs.get(name), dict):
            return bool(configs[name].get("enabled", True))
        return bool(record.get("enabled", True))

    def _skill_installation_dto(self, record: dict[str, Any]) -> dict[str, Any]:
        dto = dict(record)
        ready = self._skill_installation_entity_ready(record)
        source_type = str(record.get("source_type") or "").strip()
        dto.update(
            {
                "installed": ready,
                "enabled": self._installation_enabled(record),
                "removable": source_type == "user",
                "consistency": "ok" if ready else "inconsistent",
            }
        )
        if source_type == "prebuilt":
            dto["sync_status"] = "synced" if ready else "failed"
        return dto

    def _apply_skill_installation_meta(
        self,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        dto = self._skill_installation_dto(record)
        for key in (
            "installation_id",
            "source_type",
            "source_id",
            "skill_id",
            "version_id",
            "version",
            "origin",
            "installed_at",
            "updated_at",
            "installed",
            "enabled",
            "removable",
            "sync_status",
            "consistency",
            "market_display_name",
            "author",
        ):
            if key in dto:
                payload[key] = dto[key]
        source = str(dto.get("source") or "").strip()
        if source:
            payload["source"] = source

    def _find_skill_installation(
        self,
        *,
        name: str,
        origin: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_name = str(name or "").strip()
        normalized_origin = str(origin or "").strip()
        for record in self._get_installed_plugins():
            if not isinstance(record, dict):
                continue
            record_name = str(record.get("name") or "").strip()
            record_origin = str(record.get("origin") or "").strip()
            if normalized_origin:
                if record_origin == normalized_origin and record_name == normalized_name:
                    return record
                continue
            if record_name == normalized_name:
                return record
        return None

    @_state_transactional
    def record_skill_installation(
        self,
        *,
        name: str,
        source_type: str,
        origin: str = "",
        source: str = "",
        version: str = "",
        skill_id: str | None = None,
        source_id: str | None = None,
        version_id: str | None = None,
        installation_id: str | None = None,
        fingerprint: str | None = None,
        verification: dict[str, Any] | None = None,
        entity_dir: str | None = None,
        market_display_name: str | None = None,
        author: str | None = None,
        replace_by_name: bool = False,
    ) -> dict[str, Any]:
        """Create or update one managed installation in workspace JSON."""
        safe_name = _safe_path_name(name, "skill")
        normalized_type = str(source_type or "").strip()
        if normalized_type not in {"prebuilt", "user", "builtin"}:
            raise ValueError(f"invalid source_type: {source_type}")

        normalized_origin = str(origin or "").strip()
        normalized_source_id = str(source_id or "").strip()
        normalized_skill_id = str(skill_id or "").strip()
        existing = self._find_skill_installation(name=safe_name) if replace_by_name else None
        if existing is None and normalized_skill_id and not replace_by_name:
            for candidate in self._get_installed_plugins():
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("skill_id") or "").strip() != normalized_skill_id:
                    continue
                candidate_source_id = str(candidate.get("source_id") or "").strip()
                if normalized_source_id and candidate_source_id not in {"", normalized_source_id}:
                    continue
                existing = candidate
                break
        if existing is None:
            existing = self._find_skill_installation(
                name=safe_name,
                origin=normalized_origin or None,
            )
        if existing is None or str(existing.get("name") or "").strip() != safe_name:
            # 全新记录，或按 skill_id 命中但名字已变（模板改名、skill_id 不变），
            # 都需确认不存在另一条 name == safe_name 的记录，避免出现两条同名。
            conflicting = self._find_skill_installation(name=safe_name)
            if conflicting is not None:
                conflict_origin = str(conflicting.get("origin") or "").strip()
                if conflict_origin != normalized_origin:
                    raise SkillNameConflictError(
                        f"skill name already installed: {safe_name}"
                    )

        now = datetime.now(timezone.utc).isoformat()
        record: dict[str, Any] = dict(existing or {})
        stable_entity_dir = _safe_path_name(
            str(record.get("entity_dir") or entity_dir or safe_name),
            "skill",
        )
        record.update(
            {
                "installation_id": str(
                    record.get("installation_id")
                    or installation_id
                    or uuid.uuid4()
                ),
                "name": safe_name,
                "declared_name": safe_name,
                "entity_dir": stable_entity_dir,
                "source_type": normalized_type,
                "source": str(source or "").strip(),
                "origin": normalized_origin,
                "version": str(version or "").strip(),
                "installed_at": str(record.get("installed_at") or now),
                "updated_at": now,
                "enabled": bool(record.get("enabled", True)),
            }
        )
        for key, value in (
            ("source_id", source_id),
            ("skill_id", skill_id),
            ("version_id", version_id),
            ("fingerprint", fingerprint),
            ("market_display_name", market_display_name),
            ("author", author),
        ):
            normalized = str(value or "").strip()
            if normalized:
                record[key] = normalized
        if isinstance(verification, dict):
            record["verification"] = dict(verification)

        if existing is None:
            self._add_installed_plugin(record)
        else:
            existing_type = str(existing.get("source_type") or "").strip()
            if existing_type and existing_type != normalized_type:
                raise SkillNameConflictError(
                    f"cannot overwrite {existing_type} skill with {normalized_type}: {safe_name}"
                )
            records = self._state.setdefault("installed_plugins", [])
            for index, candidate in enumerate(records):
                if candidate is existing:
                    records[index] = self._normalize_plugin(record)
                    break
            self._save_state()
        return dict(record)

    @_state_transactional
    def remove_skill_installation(
        self,
        *,
        name: str,
        origin: str | None = None,
        expected_source_type: str | None = None,
    ) -> bool:
        """Remove a matching workspace record, optionally guarded by source type."""
        record = self._find_skill_installation(name=name, origin=origin)
        if record is None:
            return False
        expected = str(expected_source_type or "").strip()
        if expected and str(record.get("source_type") or "").strip() != expected:
            return False
        records = self._state.get("installed_plugins", [])
        self._state["installed_plugins"] = [
            item for item in records if item is not record
        ]
        self._save_state()
        return True

    @_state_transactional
    def remove_skill_installation_entity(
        self,
        *,
        name: str,
        origin: str | None = None,
        expected_source_type: str,
    ) -> bool:
        """Atomically remove a guarded installation record and its skill entity.

        The record is reloaded and its source type is checked before touching
        disk. This prevents a stale prebuilt synchronizer from deleting a
        same-name user skill that another AgentServer pod has installed.
        """
        record = self._find_skill_installation(name=name, origin=origin)
        if record is None:
            return False
        expected = str(expected_source_type or "").strip()
        if not expected or str(record.get("source_type") or "").strip() != expected:
            return False

        entity_dir = _safe_path_name(
            str(record.get("entity_dir") or record.get("name") or name),
            "skill",
        )
        entity_path = _safe_child_path(self._skills_dir, entity_dir, "skill")
        if entity_path.is_dir():
            _safe_rmtree(entity_path)
        for mirror_root in self._get_mirror_skills_dirs():
            mirror_path = _safe_child_path(mirror_root, entity_dir, "skill")
            if mirror_path.is_dir():
                _safe_rmtree(mirror_path)

        return self.remove_skill_installation(
            name=name,
            origin=origin,
            expected_source_type=expected,
        )

    @_state_transactional
    def _commit_skill_update_statuses(
        self,
        updates: dict[str, dict[str, Any]],
    ) -> None:
        """Merge update-check results into the latest workspace ledger."""
        if not updates:
            return
        changed = False
        for record in self._get_installed_plugins():
            if not isinstance(record, dict):
                continue
            installation_id = str(record.get("installation_id") or "").strip()
            values = updates.get(installation_id)
            if values is None:
                continue
            record.update(values)
            changed = True
        if changed:
            self._save_state()

    def list_enabled_skill_names(self) -> list[str]:
        """Return enabled, disk-backed skill names for the current workspace."""
        names: list[str] = []
        seen: set[str] = set()
        for skill in self._scan_local_skills():
            name = str(skill.get("name") or "").strip()
            if not name or name in seen or skill.get("enabled") is False:
                continue
            seen.add(name)
            names.append(name)
        return names

    def get_local_skills(self) -> list[dict]:
        """返回本地技能安装记录的拷贝。"""
        return list(self._state.get("local_skills", []))

    @staticmethod
    def _resolve_skill_name(child: Path, md: Path, meta: dict) -> str:
        """Canonical skill name for a folder: parsed name, or folder name as fallback.

        ``_parse_skill_md`` degrades ``name`` to the markdown file stem (e.g.
        "SKILL") when there is no frontmatter; in that case the folder name is the
        authoritative name used at runtime. Keep this consistent everywhere that
        derives a skill name from disk.
        """
        parsed_name = str(meta.get("name") or "").strip()
        if parsed_name in ("", md.stem):
            return child.name
        return parsed_name

    def _collect_existing_local_skill_names(self) -> set[str]:
        names: set[str] = set()
        if not self._skills_dir.exists():
            return names

        for child in self._skills_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if not isinstance(meta, dict):
                continue
            name = self._resolve_skill_name(child, md, meta)
            if name:
                names.add(name)
        return names

    def _collect_existing_clawhub_origins(self) -> set[str]:
        """磁盘上可由目录名反推的 ClawHub origin 集合（``clawhub:{slug}``）。

        ClawHub 装的目录名 = slug，其记录 origin = ``clawhub:owner/slug`` 或
        ``clawhub:slug``，可由目录名反推出 ``clawhub:{slug}`` 作为存活证据。
        重名下若某 slug 目录被删（卸载另一来源覆盖等），对应 origin 不在此集合，
        但 clawhub 记录 origin 可能带 owner，需按 slug 后缀匹配而非全等（见
        normalize_local_skills）。其余来源的目录名是 skill_name，反推出的
        ``clawhub:{skill_name}`` 不会与任何真实记录 origin 相等，无副作用。
        """
        origins: set[str] = set()
        if not self._skills_dir.exists():
            return origins
        for child in self._skills_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            origins.add(f"clawhub:{child.name}")
        return origins

    @_state_transactional
    def _register_unmanaged_local_skills(self) -> None:
        """Auto-register skills that exist on disk but were never recorded.

        A skill folder manually copied into the skills dir (or otherwise present
        without going through any install flow) has no entry in installed_plugins
        nor local_skills. Such skills run fine but are treated as ``source=project``
        by the UI and are excluded from the registry, so they can't be shown in
        "我的技能", uninstalled, or disabled the same way imported skills are.

        Here we give each of them a local_skills record (identical shape to the
        one ``import_local`` writes), making them fully equivalent to skills
        imported via the UI. Built-in skills (folders that also exist under the
        package builtin dir) are deliberately excluded so their source/behavior
        is preserved.

        判断"是否已登记"用 origin 优先：两个重名 ClawHub 技能（SKILL.md name 相同
        但 slug 不同）此前会被旧逻辑（按 name）误判为"已登记"而跳过，导致第二条
        成为孤儿。改用 origin 集合判断后，每个目录都能独立补登。孤儿目录的 origin
        用 ``local:{目录名}`` 兜底，确保身份唯一且不与真实 clawhub origin 混淆。
        """
        if not self._skills_dir.exists():
            return

        local = self._state.get("local_skills", [])
        registered_origins: set[str] = set()
        registered_names: set[str] = set()
        for ls in local:
            if not isinstance(ls, dict):
                continue
            origin = str(ls.get("origin", "") or "").strip()
            if origin:
                registered_origins.add(origin)
            name = str(ls.get("name", "") or "").strip()
            if name:
                registered_names.add(name)

        plugins = self._get_installed_plugins()
        # installed_plugins 里已登记的 name 集合（旧式记录可能无 origin，按 name 认领）
        plugin_registered_names: set[str] = set()
        for p in plugins:
            if isinstance(p, dict):
                pname = str(p.get("name", "") or "").strip()
                if pname:
                    plugin_registered_names.add(pname)

        builtin_dir = get_builtin_skills_dir()
        builtin_exists = builtin_dir.exists()
        changed = False

        for child in self._skills_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if not isinstance(meta, dict):
                continue
            name = self._resolve_skill_name(child, md, meta)
            if not name:
                continue
            # 排除内置技能（用户目录下与 builtin 目录同名的文件夹），避免改变其来源/行为。
            if builtin_exists and (builtin_dir / child.name).is_dir():
                continue
            # 已被 local_skills 或 installed_plugins 认领的 name 跳过。
            # 注意：历史重名 ClawHub 孤儿（两条 slug 目录但记录被覆盖只剩一条）
            # 在此会被漏登——属边缘历史脏数据，用户重新卸载重装即走新逻辑修正。
            if name in registered_names or name in plugin_registered_names:
                continue
            # 兜底 origin：local:{目录名}，确保身份唯一且不与真实 clawhub origin 混淆
            inferred_origin = f"local:{child.name}"
            if inferred_origin in registered_origins:
                continue
            self._add_local_skill(
                {"name": name, "origin": inferred_origin, "source": "local"}
            )
            registered_origins.add(inferred_origin)
            registered_names.add(name)
            changed = True

        if changed:
            self._refresh_agent_data_indexes()

    def _register_builtin_skills(self) -> None:
        """企业版：把仓库内置技能复制进 tenant workspace 并登记为 builtin（不可卸载）。

        个人版不执行——内置技能由 ``_scan_builtin_skills`` 以"可浏览、未安装"形态
        展示。幂等：builtin 只填真空——同名已有完整记录（prebuilt/user/builtin 就绪）
        一律跳过；仅修复 entity 残缺的 builtin。优先级 prebuilt > user > builtin，
        避免把管理面下发的 prebuilt 改写为 builtin 后与白名单 sync 互相翻转。
        """
        if not is_enterprise():
            return
        builtin_dir = get_builtin_skills_dir()
        if not builtin_dir.exists() or not builtin_dir.is_dir():
            return

        changed = False
        for child in sorted(builtin_dir.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            md = self._try_find_skill_file(child)
            if md is None:
                continue
            meta = self._parse_skill_md(md)
            if meta is None:
                continue
            name = self._resolve_skill_name(child, md, meta)
            if not name:
                continue

            existing = self._find_skill_installation(name=name)
            existing_type = str(existing.get("source_type") or "").strip() if existing else ""
            if existing is not None:
                if existing_type == "builtin" and not self._skill_installation_entity_ready(
                    existing
                ):
                    # builtin 记录残缺（entity 目录丢失）：走下方路径重新复制并登记。
                    pass
                else:
                    # 同名已有完整记录（prebuilt/user/builtin 就绪）：builtin 只填真空，
                    # 不改写其它来源的登记类型（优先级 prebuilt > user > builtin）。
                    continue

            dest = _safe_child_path(self._skills_dir, child.name, "skill")
            if not (dest.is_dir() and (dest / "SKILL.md").is_file()):
                if dest.exists():
                    _safe_rmtree(dest)
                try:
                    shutil.copytree(child, dest)
                except Exception as exc:
                    logger.warning(
                        "[SkillManager] 复制内置技能失败: name=%s error=%s", name, exc
                    )
                    continue

            try:
                self.record_skill_installation(
                    name=name,
                    source_type="builtin",
                    source="builtin",
                    origin=f"builtin:{child.name}",
                    version=str(meta.get("version") or "").strip(),
                    entity_dir=child.name,
                )
                changed = True
            except SkillNameConflictError as exc:
                logger.warning(
                    "[SkillManager] 内置技能登记冲突（同名其它来源）: name=%s error=%s",
                    name,
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "[SkillManager] 内置技能登记失败: name=%s error=%s", name, exc
                )

        if changed:
            self._refresh_agent_data_indexes()

    def _apply_enabled_config(
        self,
        payload: dict[str, Any],
        skill_name: str,
        *,
        default_enabled: bool | None = None,
    ) -> None:
        enabled = (
            default_enabled
            if default_enabled is not None and not skill_name
            else self.get_skill_enabled(skill_name)
        )
        payload["enabled"] = enabled
        payload["config"] = {"enabled": enabled}

    def get_skill_enabled(self, skill_name: str) -> bool:
        return get_skill_enabled(self._state, skill_name)

    def set_skill_enabled(self, skill_name: str, enabled: bool) -> None:
        set_skill_enabled(self._state, skill_name, enabled)
        self._save_state()

    def remove_skill_config(self, skill_name: str) -> None:
        if remove_skill_config(self._state, skill_name):
            self._save_state()

    def list_disabled_skills(self) -> list[str]:
        return list_disabled_skills(self._state)

    def list_execution_disabled_skills(self) -> list[str]:
        return list_execution_disabled_skills(self._state)

    def get_skill_meta(self, skill_name: str) -> dict[str, Any] | None:
        """返回本地 skill 的解析元数据，附带目录与 skill 文件路径。"""
        skill_dir = self._resolve_local_skill_dir(skill_name)
        if skill_dir is None:
            return None
        skill_file = self._try_find_skill_file(skill_dir)
        if skill_file is None:
            return None
        meta = self._parse_skill_md(skill_file)
        if meta is None:
            return None
        meta["skill_dir"] = str(skill_dir)
        meta["skill_file"] = str(skill_file)
        return meta

    def is_builtin_skill(self, skill_name: str) -> bool:
        """判断当前运行目录中的 skill 是否为真正的内置技能。"""
        if not skill_name:
            return False
        try:
            dest = _safe_child_path(self._skills_dir, skill_name, "skill")
        except ValueError:
            return False
        builtin_dir = get_builtin_skills_dir()
        if not builtin_dir.exists():
            return False
        try:
            builtin_skill_path = _safe_child_path(builtin_dir, skill_name, "skill")
        except ValueError:
            return False
        if not builtin_skill_path.exists() or not builtin_skill_path.is_dir():
            return False
        return dest.exists() and dest.is_dir() and dest.resolve() == builtin_skill_path.resolve()

    def _add_installed_plugin(self, plugin: dict) -> None:
        plugins = self._state.setdefault("installed_plugins", [])
        plugin = self._normalize_plugin(plugin)
        key = self._record_identity_key(plugin)
        for i, p in enumerate(plugins):
            if self._record_identity_key(p) == key:
                plugins[i] = plugin
                self._save_state()
                return
        plugins.append(plugin)
        self._save_state()

    def _remove_installed_plugin(self, name: str, origin: str | None = None) -> None:
        plugins = self._state.get("installed_plugins", [])
        # origin 优先：按身份删除，避免重名技能误删另一条
        if origin is not None:
            self._state["installed_plugins"] = [
                p for p in plugins
                if not self._record_matches(p, name=name, origin=origin)
            ]
        else:
            self._state["installed_plugins"] = [p for p in plugins if p.get("name") != name]
        self._save_state()

    def _set_plugin_enabled(self, name: str, enabled: bool, origin: str | None = None) -> bool:
        """设置插件的启用/禁用状态。

        若传入了 origin，优先按 origin 精确匹配 installed_plugin 记录（避免重名
        技能误操作另一条）。无 origin 时按 name 回退（向后兼容）。
        """
        plugins = self._state.get("installed_plugins", [])
        updated = False
        for p in plugins:
            if origin is not None:
                rec_origin = str(p.get("origin", "") or "").strip()
                if rec_origin and rec_origin == origin and p.get("name") == name:
                    p["enabled"] = bool(enabled)
                    updated = True
                    break
            elif p.get("name") == name:
                p["enabled"] = bool(enabled)
                updated = True
                break
        if not updated:
            return False
        self._save_state()
        return True

    @staticmethod
    def _normalize_plugin(p: dict) -> dict:
        """规范化插件记录，补全 enabled 字段."""
        p.setdefault("enabled", True)
        return p

    def _add_local_skill(self, skill: dict) -> None:
        local = self._state.setdefault("local_skills", [])
        key = self._record_identity_key(skill)
        # 更新已有记录
        for i, s in enumerate(local):
            if self._record_identity_key(s) == key:
                local[i] = skill
                self._save_state()
                return
        local.append(skill)
        self._save_state()

    def _remove_local_skill(self, name: str, origin: str | None = None) -> None:
        local = self._state.get("local_skills", [])
        # origin 优先：按身份删除，避免重名技能误删另一条
        if origin is not None:
            self._state["local_skills"] = [
                s for s in local
                if not self._record_matches(s, name=name, origin=origin)
            ]
        else:
            self._state["local_skills"] = [s for s in local if s.get("name") != name]
        self._save_state()

    @staticmethod
    def _record_identity_key(record: dict) -> str:
        """记录身份键：优先 origin（区分同名不同来源），origin 为空时回退 name.

        不同来源的两个技能可能 SKILL.md name 相同但 origin 不同（如
        ``clawhub:owner/slug-a`` 与 ``clawhub:owner/slug-b``）。用 origin 作身份键
        才能让两者并存于 local_skills / installed_plugins 而不互相覆盖。
        无 origin 的历史记录（local/import_local 的 URL 等）回退到 name。
        """
        origin = str(record.get("origin", "") or "").strip()
        if origin:
            return f"origin:{origin}"
        return f"name:{str(record.get('name', '') or '').strip()}"

    @classmethod
    def _record_matches(cls, record: dict, *, name: str, origin: str) -> bool:
        """判断记录是否匹配给定 name+origin 身份。origin 优先，name 作兜底.

        origin 形如 ``clawhub:owner/slug`` / ``clawhub:slug`` / ``teamskillshub:asset_id`` / URL。
        name 是 SKILL.md 里的技能名。
        """
        rec_origin = str(record.get("origin", "") or "").strip()
        rec_name = str(record.get("name", "") or "").strip()
        if rec_origin:
            return rec_origin == origin and (not name or rec_name == name)
        # 无 origin 的历史记录：按 name 兜底匹配（向后兼容）
        return bool(name) and rec_name == name

    @staticmethod
    def _apply_local_skill_meta(meta: dict, ls_rec: dict) -> str:
        """把 local_skill 记录上的展示字段回填到 meta，并返回 origin.

        回填 origin / display_name / market_short_desc / market_display_name，
        供「我的技能」页与详情页显示与安装来源一致的信息。origin 精确匹配保证
        同名不同来源的两条各拿各的，不串台。返回 origin 供调用方做 source 解析。
        """
        rec_origin = str(ls_rec.get("origin", "") or "").strip()
        if rec_origin:
            meta["origin"] = rec_origin
        for field, target in (
            ("display_name", "display_name"),
            ("market_short_desc", "market_short_desc"),
            ("market_display_name", "market_display_name"),
        ):
            val = ls_rec.get(field)
            if isinstance(val, str) and val.strip():
                meta[target] = val.strip()
        return rec_origin

    # -----------------------------------------------------------------------
    # ClawHub 相关方法
    # -----------------------------------------------------------------------

    def _get_clawhub_token(self) -> str:
        """获取 ClawHub CLI token."""
        return (self._state.get("clawhub", {}).get("token") or "").strip()

    def _set_clawhub_token(self, token: str) -> None:
        """设置 ClawHub CLI token（掩码处理）。"""
        self._state.setdefault("clawhub", {})["token"] = token.strip() if token.strip() else ""
        self._save_state()

    @staticmethod
    def _mask_clawhub_token(token: str) -> str:
        """掩码处理 ClawHub token。"""
        if not token:
            return ""
        if len(token) <= 8:
            return "*" * len(token)
        return token[:4] + "*" * (len(token) - 8) + token[-4:]
