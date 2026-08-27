# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillManager - 管理 skills 的加载、安装、卸载与 marketplace 操作."""

from __future__ import annotations

import logging
import asyncio
import hashlib
import io
import json
import os
import re
import sys
import shutil
import ssl
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
import zipfile
from datetime import datetime, date, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse
import yaml
import urllib3
import httpx
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from openjiuwen.agent_evolving.checkpointing.evolution_store import (
    EvolutionLog as EvolutionFile,
    EvolutionRecord as EvolutionEntry,
)
from jiuwenswarm.common.utils import (
    get_agent_root_dir,
    get_agent_skills_dir,
    get_builtin_skills_dir,
    is_package_installation,
)
from jiuwenswarm.server.runtime.skill.archive_store import (
    ARCHIVE_DIRNAME,
    SkillArchiveError,
    build_versions_list_payload,
    compute_content_checksum,
    get_current_version,
    get_installed_asset_id,
    get_last_published_version,
    resolve_version_content_root,
    touch_version_metadata,
    update_publish_remote_metadata,
    version_content_dir,
    write_skillhub_first_install_index,
)
from jiuwenswarm.server.runtime.skill.skill_files import (
    DEFAULT_TEXT_PREVIEW_MAX_BYTES,
    SkillFilesError,
    guess_mime_type,
    is_text_previewable,
    list_skill_workspace_files,
    read_text_preview,
    resolve_skill_relative_file,
)
from jiuwenswarm.server.runtime.skill.skill_type import SKILL_TYPE_SWARM, detect_skill_type


def _get_ssl_verify() -> bool:
    """延迟导入以规避循环依赖：ssl_config 所在的 tools 包 __init__ 会回引本模块。"""
    from jiuwenswarm.agents.harness.common.tools.ssl_config import get_ssl_verify

    return get_ssl_verify()

logger = logging.getLogger(__name__)

ERROR_SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
ERROR_SKILL_DOWNLOAD_TOKEN_INVALID = "SKILL_DOWNLOAD_TOKEN_INVALID"
ERROR_SKILL_INVALID_PACKAGE = "SKILL_INVALID_PACKAGE"
ERROR_SKILL_INVALID_METADATA = "SKILL_INVALID_METADATA"
ERROR_SKILL_RESERVED_PATH = "SKILL_RESERVED_PATH"
ERROR_SKILL_IMPORT_OVERWRITE_REQUIRED = "SKILL_IMPORT_OVERWRITE_REQUIRED"
ERROR_SKILL_BUILTIN_READ_ONLY = "SKILL_BUILTIN_READ_ONLY"
ERROR_SKILL_ALREADY_EXISTS = "SKILL_ALREADY_EXISTS"
ERROR_SKILL_NAME_CONFLICT = "SKILL_NAME_CONFLICT"
ERROR_SKILL_UNSAFE_PATH = "SKILL_UNSAFE_PATH"
ERROR_SKILL_FILE_TOO_LARGE = "SKILL_FILE_TOO_LARGE"
ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT = "SKILL_KNOWLEDGE_INPUT_CONFLICT"
ERROR_SKILL_PUBLISH_VERSION_CONFLICT = "SKILL_PUBLISH_VERSION_CONFLICT"
ERROR_SKILLHUB_INSTALL_FAILED = "SKILLHUB_INSTALL_FAILED"
ERROR_SKILLHUB_PUBLISH_FAILED = "SKILLHUB_PUBLISH_FAILED"
ERROR_SKILLHUB_DETAIL_NOT_FOUND = "SKILLHUB_DETAIL_NOT_FOUND"
ERROR_SKILLHUB_DETAIL_FAILED = "SKILLHUB_DETAIL_FAILED"

_DETAIL_KEY_SKILLHUB_DETAIL_NOT_FOUND = "skills.swarmskillshub.errors.detailNotFound"
_DETAIL_KEY_SKILLHUB_DETAIL_FAILED = "skills.swarmskillshub.errors.detailFailed"

# ZIP 解包配额（防 zip bomb）
_SKILL_ZIP_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_SKILL_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_SKILL_ZIP_MAX_FILE_COUNT = 5000
_SKILL_ZIP_MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
_SKILL_ZIP_MAX_PATH_DEPTH = 20


class SkillRpcError(Exception):
    """Skill RPC 稳定业务错误（带 code，供前端/网关透传）."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

_SKILLNET_DOWNLOAD_TIMEOUT: int = int(os.environ.get("SKILLNET_DOWNLOAD_TIMEOUT", "60"))
_SKILLNET_MAX_RETRIES: int = int(os.environ.get("SKILLNET_MAX_RETRIES", "3"))
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
# 本地导入源的基础黑名单追加入口（逗号分隔绝对路径，只收紧不放宽）。
_IMPORT_LOCAL_FORBIDDEN_DIRS_ENV = "IMPORT_LOCAL_FORBIDDEN_DIRS"
# 本地导入结构校验：SKILL.md 开头（仅允许前置空行）必须是 --- frontmatter。
_SKILL_FRONTMATTER_RE = re.compile(r"^(?:\s*\n)*---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
_ONLINE_SEARCH_RRF_K = 60
_ONLINE_SEARCH_SOURCE_ORDER = {"skillnet": 0, "teamskillshub": 1, "clawhub": 2}
_ONLINE_SEARCH_SOURCE_TIMEOUT = 30.0
_TEAM_SKILL_PLUGIN_TYPES = {"swarmskill", "swarm-skill", "teamskills", "team-skill"}
_SINGLE_SKILL_PLUGIN_TYPES = {"skill"}


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


def _has_effective_evolutions(skill_dir: Path | None) -> bool:
    """是否存在可展示/可应用的有效经验（文件存在且 entries 非空）."""
    if skill_dir is None or not skill_dir.is_dir():
        return False
    evo_path = skill_dir / _EVOLUTION_FILENAME
    if not evo_path.is_file():
        return False
    try:
        raw = json.loads(evo_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    entries = raw.get("entries")
    return isinstance(entries, list) and len(entries) > 0


def _clear_evolutions_file(skill_dir: Path | None) -> None:
    """删除目录下的 evolutions.json（rebuild 消耗经验后隐藏经验入口）."""
    if skill_dir is None or not skill_dir.is_dir():
        return
    evo_path = skill_dir / _EVOLUTION_FILENAME
    if evo_path.is_file():
        try:
            evo_path.unlink()
        except OSError:
            logger.warning("删除 evolutions.json 失败: path=%s", evo_path)


def _get_agent_root_dir() -> "Path":
    return get_agent_root_dir()


def _get_marketplace_dir() -> "Path":
    return get_agent_skills_dir() / "_marketplace"


def _get_state_file() -> "Path":
    return get_state_file()


from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
    get_registered_skill_names,
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
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _get_free_search_proxy_url() -> str:
    return str(os.environ.get(_FREE_SEARCH_PROXY_URL_ENV, "") or "").strip()


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


# A skill name is also its directory name inside the single global library, so a
# name that could never be such a directory can never match a real skill. These
# are the characters no filesystem this runtime targets accepts in a name:
# Windows rejects them outright, and ``:`` additionally spells a drive or an NTFS
# alternate data stream.
_INVALID_SKILL_NAME_CHARS = frozenset('<>:"|?*')

# Windows device names, reserved whatever the extension is.
_RESERVED_SKILL_NAME_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

# Generous upper bound: real names are dashes-and-words slugs, while most
# filesystems stop accepting a single component well before this.
_MAX_SKILL_NAME_LENGTH = 128


def _validate_skill_name(name: str) -> str:
    """Validate one skill name before it is persisted into visibility metadata.

    Visibility names are compared against library directory names and are never
    joined onto a path, so an odd name is not a traversal today. It would still
    be written to disk forever, and a later reader that does resolve it as a
    directory would inherit the problem; reject it at the door instead.

    The rule stays deliberately permissive — letters of any script, digits,
    dots, dashes, underscores and spaces all pass, because a skill name is
    simply the name of its library directory — so that a legitimately named
    skill is never silently dropped.

    Args:
        name: Stripped, non-empty candidate name.

    Returns:
        The accepted name.

    Raises:
        ValueError: The name is not usable as a skill name.
    """
    safe = _safe_path_name(name, "skill")
    if len(safe) > _MAX_SKILL_NAME_LENGTH:
        raise ValueError(f"invalid skill name: longer than {_MAX_SKILL_NAME_LENGTH} characters")
    # "..." and friends: relative-path lookalikes that no installer produces.
    if set(safe) <= {"."}:
        raise ValueError(f"invalid skill name: {safe}")
    # A leading dot marks sidecar state (the ".<name>.lock" files the visibility
    # writer creates, ".git", ...), not a skill a user meant to authorize.
    if safe.startswith("."):
        raise ValueError(f"invalid skill name: {safe}")
    # Windows silently drops a trailing dot from a filename, so such a name
    # could never round-trip to the directory it claims to address.
    if safe.endswith("."):
        raise ValueError(f"invalid skill name: {safe}")
    if _INVALID_SKILL_NAME_CHARS.intersection(safe):
        raise ValueError(f"invalid skill name: {safe}")
    # Control characters only. A plain space is legal: a skill name is whatever
    # its library directory is called, and directories may contain spaces.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in safe):
        raise ValueError("invalid skill name: contains a control character")
    if safe.split(".", 1)[0].upper() in _RESERVED_SKILL_NAME_STEMS:
        raise ValueError(f"invalid skill name: {safe}")
    return safe


def _coerce_skill_name_list(raw: Any, field_label: str) -> list[str]:
    """Normalize an RPC-supplied skill-name list.

    Accepts a list/tuple/set of names or a single name, drops blanks,
    non-string entries and names :func:`_validate_skill_name` rejects, and
    preserves nothing else: the visibility writer sorts and de-duplicates what
    it receives. A rejected name is logged and skipped rather than raised, so
    one bad entry never aborts an otherwise valid authorization change.

    Args:
        raw: Raw ``allow`` / ``deny`` value from the request params.
        field_label: Parameter name reported in the rejection warning.

    Returns:
        Cleaned list of skill names; empty when nothing usable was supplied.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    else:
        return []
    names: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        try:
            names.append(_validate_skill_name(name))
        except ValueError as exc:
            _log_rejected_name(f"skills.visibility.{field_label}", "skill", item, exc)
    return names


def _compose_workspace_skill_visibility(
    member_path: Path | None,
    member_id: str,
    team_path: Path | None,
    team_id: str,
) -> tuple[set[str], set[str]]:
    """Compose the effective skill allow / deny sets of one team workspace.

    The composition rule is owned by openJiuWen's team skill layer:
    ``enabled = member.allow UNION team.allow`` and
    ``disabled = member.deny UNION team.deny UNION globally disabled skills``.

    An empty enabled set means "inherit the whole library", never "deny
    everything" — substituting the full name set would freeze the view against
    Skills installed later.

    Args:
        member_path: Member ``skills-visibility.json`` path; None when the
            request addresses a team document only.
        member_id: Member name recorded as the document id.
        team_path: Team ``skills-visibility.json`` path, or None.
        team_id: Team name recorded as the team document id.

    Returns:
        Tuple of (enabled skill names, disabled skill names).
    """
    from openjiuwen.agent_teams.skill.visibility import (
        SCOPE_MEMBER,
        SCOPE_TEAM,
        SkillVisibility,
        compose_skill_visibility,
        read_skill_visibility,
    )

    from jiuwenswarm.server.runtime.skill.skilldev.state_utils import load_execution_disabled_skills

    if member_path is None:
        member = SkillVisibility(scope=SCOPE_MEMBER, id=member_id)
    else:
        member = read_skill_visibility(member_path, scope=SCOPE_MEMBER, entity_id=member_id)
    team = None
    if team_path is not None:
        team = read_skill_visibility(team_path, scope=SCOPE_TEAM, entity_id=team_id)
    return compose_skill_visibility(member, team, load_execution_disabled_skills())


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


def _handle_copy_error(
    exc: BaseException,
    dest: Path,
    logger_prefix: str,
    src: Path | None = None,
    *,
    cleanup_dest: bool = True,
) -> dict[str, Any]:
    """处理文件/目录复制失败的统一错误处理函数.
    
    Args:
        exc: 捕获到的 OSError 异常（包括 shutil.Error）
        dest: 目标路径
        logger_prefix: 日志前缀（用于区分不同操作）
        src: 源路径（可选，用于日志记录）
        cleanup_dest: 是否清理目标目录。新建半成品可清；覆盖已有 Skill 时必须为 False。
    
    Returns:
        错误响应字典
    """
    # 记录错误日志
    if src:
        logger.error("[SkillManager] %s copy failed: src=%s dest=%s error=%s", logger_prefix, src, dest, exc)
    else:
        logger.error("[SkillManager] %s copy failed: dest=%s error=%s", logger_prefix, dest, exc)
    
    # 仅清理新建过程中的半成品；绝不能删除已有 Skill
    if cleanup_dest and dest.exists():
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


class SkillManager:
    """Skill 管理器，对应 skills.* 请求方法."""

    def __init__(self, workspace_dir: str | None = None) -> None:
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
        self._state: dict[str, Any] = self._load_state()
        # 把手动拷入 skills 目录、未经任何安装流程登记的本地技能，自动补登记到
        # local_skills，使其与"导入本地技能"完全等价（可展示/卸载/查看详情/禁用）。
        self._register_unmanaged_local_skills()
        # SkillNet 异步安装：install 立即返回 install_id，后台下载；完成后调用 hook 重载 Agent
        self._skillnet_install_complete_hook: Callable[[], Awaitable[None]] | None = None

    @property
    def _skillnet_install_jobs(self) -> dict[str, dict[str, Any]]:
        """进程级共享的 SkillNet 安装任务表（见模块常量 ``_SKILLNET_INSTALL_JOBS``）."""
        return _SKILLNET_INSTALL_JOBS

    def set_skillnet_install_complete_hook(self, hook: Callable[[], Awaitable[None]] | None) -> None:
        """安装成功落盘后回调（通常为重载 Agent 实例）."""
        self._skillnet_install_complete_hook = hook

    # -----------------------------------------------------------------------
    # 公开 handler
    # -----------------------------------------------------------------------

    async def handle_skills_list(self, params: dict) -> dict:
        """返回所有可用 skill（本地 + marketplace 中未安装的）.

        params:
            refresh_marketplaces: bool (可选, 默认 False)
                为 True 时，先对已配置 marketplace 执行 clone/pull，再扫描列表。
            with_installed: bool (可选, 默认 False)
                为 True 时，同一次响应中附带 plugins（与 skills.installed 一致），
                避免网关串行处理两次 RPC 导致列表刷新超时或排队过久。
        """
        refresh_marketplaces = bool(params.get("refresh_marketplaces", False))
        if refresh_marketplaces:
            await self._sync_marketplace_repos()
        # 每次列举前，把手动拷入 skills 目录、尚未登记的本地技能补登记为 local，
        # 使其无需重启 server、刷新"我的技能"即可显示（与导入本地技能一致）。
        self._register_unmanaged_local_skills()
        local = self._scan_local_skills()
        builtin = self._scan_builtin_skills()
        marketplace = self._scan_marketplace_skills()
        out: dict[str, Any] = {"skills": local + builtin + marketplace}
        if bool(params.get("with_installed", False)):
            installed = await self.handle_skills_installed(params)
            out["plugins"] = installed.get("plugins") or []
        return out

    async def handle_skills_installed(self, params: dict) -> dict:
        """返回已安装的 marketplace 插件列表.

        版本不再来自 ``installed_plugins[].version``，而是从各 Skill
        根目录 ``.archive/versions/index.json`` 读取。
        """
        raw_plugins = self._get_installed_plugins()
        plugins = []
        for p in raw_plugins:
            p = self._normalize_plugin(p)
            name = str(p.get("name") or "").strip()
            marketplace_raw = p.get("marketplace")
            if isinstance(marketplace_raw, str) and marketplace_raw.strip():
                marketplace: str | None = marketplace_raw.strip()
            else:
                marketplace = None

            raw_spec = p.get("spec")
            spec: dict[str, Any] | None
            if isinstance(raw_spec, dict):
                spec = raw_spec
            else:
                # 契约为 object|null；历史字符串 spec 不再回传
                spec = None

            installed_raw = p.get("installed_at")
            if isinstance(installed_raw, str) and installed_raw.strip():
                installed_at: str | None = installed_raw.strip()
            else:
                installed_at = None

            commit_raw = p.get("commit")
            if commit_raw is None:
                commit_raw = p.get("git_commit")
            if isinstance(commit_raw, str) and commit_raw.strip():
                git_commit: str | None = commit_raw.strip()
            else:
                git_commit = None

            raw_skills = p.get("skills")
            if not isinstance(raw_skills, list) or not raw_skills:
                raw_skills = [name] if name else []
            skills_out: list[dict[str, Any]] = []
            for item in raw_skills:
                if isinstance(item, dict):
                    skill_name = str(item.get("name") or "").strip()
                else:
                    skill_name = str(item or "").strip()
                if not skill_name:
                    continue
                skill_dir = self._resolve_local_skill_dir(skill_name)
                try:
                    version = get_current_version(skill_dir)
                except SkillArchiveError:
                    version = None
                skills_out.append({"name": skill_name, "version": version})
            plugin = {
                "plugin_name": name,
                "marketplace": marketplace,
                "spec": spec,
                "installed_at": installed_at,
                "git_commit": git_commit,
                "enabled": self.get_skill_enabled(name),
                "skills": skills_out,
            }
            plugins.append(plugin)
        return {"plugins": plugins}

    async def handle_skills_get(self, params: dict) -> dict:
        """获取单个 skill 详情（name 必填）.

        可选 ``version``：传入时只读 ``.archive`` 完整版本副本，不回退 workspace，
        也不修改 workspace / 默认版本。
        """
        name = params.get("name")
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(str(name), "skill")
        except ValueError as exc:
            _log_rejected_name("skills.get", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        version_param = params.get("version")
        version_requested: str | None = None
        if version_param is not None and str(version_param).strip():
            version_requested = str(version_param).strip()

        skill_dir, base_meta = self._locate_skill_for_get(name)
        if skill_dir is None or base_meta is None:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")

        read_root = skill_dir
        response_version: str | None
        if version_requested is not None:
            try:
                read_root = resolve_version_content_root(skill_dir, version_requested)
            except SkillArchiveError as exc:
                raise SkillRpcError(exc.code, exc.message) from exc
            response_version = version_requested
        else:
            try:
                response_version = get_current_version(skill_dir)
            except SkillArchiveError as exc:
                raise SkillRpcError(exc.code, exc.message) from exc

        md = self._try_find_skill_file(read_root)
        if md is None:
            if version_requested is not None:
                raise SkillRpcError(
                    "SKILL_VERSION_NOT_FOUND",
                    f"版本副本缺少 SKILL.md: {version_requested}",
                )
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")

        meta = self._parse_skill_md(md)
        if meta is None:
            if version_requested is not None:
                raise SkillRpcError(
                    "SKILL_VERSION_CONTENT_INVALID",
                    f"版本副本 SKILL.md 无法解析: {version_requested}",
                )
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")

        # 展示身份仍以 workspace/登记信息为准；正文来自 read_root
        meta["name"] = base_meta.get("name") or name
        if meta.get("name") == md.stem:
            meta["name"] = skill_dir.name if version_requested is None else name
        meta["content"] = meta.pop("body", "")
        meta["file_path"] = meta.pop("path", "")
        meta["source"] = base_meta.get("source") or self._resolve_skill_source(name)
        meta["display_name"] = base_meta.get("display_name") or self._resolve_skill_display_name(name)
        meta["is_builtin"] = bool(base_meta.get("is_builtin", False))
        meta["is_builtin_source"] = bool(base_meta.get("is_builtin_source", False))
        if "marketplace" in base_meta:
            meta["marketplace"] = base_meta.get("marketplace")
        meta["has_evolutions"] = _has_effective_evolutions(
            skill_dir if version_requested is None else read_root
        )
        self._apply_enabled_config(meta, name)
        meta["version"] = response_version
        meta["skill_type"] = detect_skill_type(skill_dir if version_requested is None else read_root)

        # 返回前改写本地相对图片为受控预览 URL（不改磁盘）
        session_id = str(params.get("_session_id") or params.get("session_id") or "").strip()
        try:
            from jiuwenswarm.server.runtime.skill.skill_content_images import (
                rewrite_skill_markdown_images,
            )

            meta["content"] = rewrite_skill_markdown_images(
                str(meta.get("content") or ""),
                skill_name=str(meta.get("name") or name),
                version=response_version if version_requested is not None else None,
                # workspace 查询时 version 字段可能是 current_version，但图片根仍是 workspace
                content_root=read_root,
                session_id=session_id,
            )
        except Exception:
            logger.debug(
                "[skills.get] 图片改写失败，返回原文: skill=%s", name, exc_info=True
            )
        return meta

    async def handle_skills_versions_list(self, params: dict) -> dict:
        """列出指定 Skill 的本地产品版本（只读 ``.archive``，不访问 SkillHub）."""
        name = str((params or {}).get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.versions.list", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        skill_dir = self._resolve_local_skill_dir(name)
        if skill_dir is None:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")

        try:
            return build_versions_list_payload(name, skill_dir)
        except SkillArchiveError as exc:
            raise SkillRpcError(exc.code, exc.message) from exc

    async def handle_skills_files_list(self, params: dict) -> dict:
        """列出 Skill workspace 工作副本文件树（隐藏 ``.archive``）."""
        params = params or {}
        if "version" in params and params.get("version") is not None:
            raise ValueError("skills.files.list 不接受 version 参数，仅列出 workspace 工作副本")
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.files.list", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        skill_dir = self._resolve_local_skill_dir(name)
        if skill_dir is None:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")
        try:
            files = list_skill_workspace_files(skill_dir)
        except SkillFilesError as exc:
            raise SkillRpcError(exc.code, exc.message) from exc
        return {"name": name, "files": files}

    async def handle_skills_files_get(self, params: dict) -> dict:
        """预览 Skill workspace 中的单个文件."""
        params = params or {}
        if "version" in params and params.get("version") is not None:
            raise ValueError("skills.files.get 不接受 version 参数，仅预览 workspace 工作副本")
        name = str(params.get("name") or "").strip()
        rel_path = str(params.get("path") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        if not rel_path:
            raise ValueError("缺少参数: path")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.files.get", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        skill_dir = self._resolve_local_skill_dir(name)
        if skill_dir is None:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")

        try:
            file_path, normalized = resolve_skill_relative_file(skill_dir, rel_path)
        except SkillFilesError as exc:
            raise SkillRpcError(exc.code, exc.message) from exc

        mime_type = guess_mime_type(file_path)
        try:
            size = int(file_path.stat().st_size)
        except OSError as exc:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"无法读取文件: {normalized}") from exc

        out: dict[str, Any] = {
            "name": name,
            "path": normalized,
            "type": "file",
            "mime_type": mime_type,
            "size": size,
        }

        preview: str | None = None
        if is_text_previewable(file_path, mime_type):
            try:
                preview = read_text_preview(file_path, max_bytes=DEFAULT_TEXT_PREVIEW_MAX_BYTES)
            except SkillFilesError as exc:
                raise SkillRpcError(exc.code, exc.message) from exc

        if preview is not None:
            out["encoding"] = "utf-8"
            out["content"] = preview
            return out

        # session_id 仅用于签发短期 download_url，不属于业务契约字段
        session_id = str(params.get("session_id") or "").strip()
        from jiuwenswarm.agents.harness.common.tools.web_file_download import build_file_download_info

        download_info = build_file_download_info(
            str(file_path),
            file_path.name,
            session_id,
            expires_in=600,
        )
        out["download_url"] = download_info["download_url"]
        return out

    async def handle_skills_rebuild(self, params: dict) -> dict:
        """将现有经验应用到无版本 workspace 或指定本地版本副本.

        逻辑与 ``/evolve_rebuild`` 对齐：领域准备（归档 + 过滤 + 清空
        evolutions + 拼 Agent prompt），由上层触发 Agent follow-up 改写
        ``SKILL.md``。实现位于本类，不依赖 evolution_slash 模块。
        """
        params = params or {}
        if "version" not in params:
            raise ValueError("缺少参数: version（本地无版本 Skill 请显式传 null）")
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.rebuild", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        version_raw = params.get("version")
        if version_raw is None:
            version = None
        elif isinstance(version_raw, str) and not version_raw.strip():
            raise ValueError("version 为空时请传 null")
        else:
            version = str(version_raw).strip()

        user_intent = params.get("user_intent")
        if user_intent is not None:
            user_intent = str(user_intent).strip() or None
        language = str(params.get("language") or "cn").strip() or "cn"

        skill_dir = self._resolve_local_skill_dir(name)
        if skill_dir is None:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")
        if self._is_builtin_skill(name, self._get_installed_plugins(), skill_dir):
            raise SkillRpcError("SKILL_BUILTIN_READ_ONLY", f"内置 Skill 不可 rebuild: {name}")

        try:
            return await self._rebuild_skill_with_evolutions(
                name,
                skill_dir,
                version,
                user_intent=user_intent,
                language=language,
            )
        except SkillRpcError:
            raise
        except SkillArchiveError as exc:
            raise SkillRpcError(exc.code, exc.message) from exc
        except Exception as exc:
            logger.warning("[SkillManager] skills.rebuild failed: skill=%s error=%s", name, exc)
            raise SkillRpcError("SKILL_REBUILD_FAILED", f"rebuild 失败: {exc}") from exc

    async def handle_skills_toggle(self, params: dict) -> dict:
        """切换已安装本地 skill 的 enabled 状态。"""
        name = params.get("name", "")
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

        self.set_skill_enabled(name, enabled)
        return {
            "success": True,
            "name": name,
            "enabled": enabled,
            "config": {"enabled": enabled},
            "detail": "配置已更新；下次 reload / rebuild / 新会话后执行面生效。",
        }

    @staticmethod
    def _resolve_skill_visibility_target(params: dict) -> tuple[str, str, Path] | dict:
        """Resolve the visibility document addressed by an RPC request.

        Args:
            params: RPC parameters carrying ``scope``, ``team_name`` and, for the
                member scope, ``member_name``.

        Returns:
            Tuple of (scope, entity id, metadata path), or an error payload dict
            when the request is malformed.
        """
        from openjiuwen.agent_teams.paths import (
            member_skill_visibility_path,
            team_skill_visibility_path,
        )
        from openjiuwen.agent_teams.skill.visibility import SCOPE_MEMBER, SCOPE_TEAM

        scope = str(params.get("scope") or SCOPE_MEMBER).strip()
        if scope not in (SCOPE_MEMBER, SCOPE_TEAM):
            return {"success": False, "detail": f"无效参数: scope={scope}（应为 member 或 team）"}
        try:
            team_name = _safe_path_name(params.get("team_name"), "team")
        except ValueError as exc:
            _log_rejected_name("skills.visibility", "team", params.get("team_name"), exc)
            return {"success": False, "detail": str(exc)}

        if scope == SCOPE_TEAM:
            return SCOPE_TEAM, team_name, team_skill_visibility_path(team_name)

        try:
            member_name = _safe_path_name(params.get("member_name"), "member")
        except ValueError as exc:
            _log_rejected_name("skills.visibility", "member", params.get("member_name"), exc)
            return {"success": False, "detail": str(exc)}
        return SCOPE_MEMBER, member_name, member_skill_visibility_path(team_name, member_name)

    @staticmethod
    def _compose_skill_visibility_for_params(params: dict, scope: str) -> tuple[set[str], set[str]]:
        """Compose the effective allow / deny sets addressed by an RPC request.

        Args:
            params: RPC parameters already validated by
                :meth:`_resolve_skill_visibility_target`.
            scope: Resolved scope, ``member`` or ``team``.

        Returns:
            Tuple of (enabled skill names, disabled skill names).
        """
        from openjiuwen.agent_teams.paths import (
            member_skill_visibility_path,
            team_skill_visibility_path,
        )
        from openjiuwen.agent_teams.skill.visibility import SCOPE_MEMBER

        team_name = str(params.get("team_name") or "").strip()
        team_path = team_skill_visibility_path(team_name)
        if scope != SCOPE_MEMBER:
            # A team document has no member half; compose it against itself so
            # the team's own deny list and the global disabled set still apply.
            return _compose_workspace_skill_visibility(
                member_path=None,
                member_id="",
                team_path=team_path,
                team_id=team_name,
            )
        member_name = str(params.get("member_name") or "").strip()
        return _compose_workspace_skill_visibility(
            member_path=member_skill_visibility_path(team_name, member_name),
            member_id=member_name,
            team_path=team_path,
            team_id=team_name,
        )

    async def handle_skills_visibility_get(self, params: dict) -> dict:
        """读取某个 member/team 的 skill 可见性 metadata.

        params:
            scope: "member" 或 "team"
            team_name: 团队名
            member_name: 成员名（scope=member 时必填）

        Returns:
            allow / deny 名单，以及合成后的 enabled / disabled 生效集合。
            metadata 不存在时返回一份空文档（空 allow 表示继承全库）。
        """
        from openjiuwen.agent_teams.skill.visibility import read_skill_visibility

        target = await asyncio.to_thread(self._resolve_skill_visibility_target, params)
        if isinstance(target, dict):
            return target
        scope, entity_id, path = target

        visibility = await asyncio.to_thread(
            read_skill_visibility,
            path,
            scope=scope,
            entity_id=entity_id,
        )
        enabled, disabled = await asyncio.to_thread(
            self._compose_skill_visibility_for_params,
            params,
            scope,
        )
        return {
            "success": True,
            "scope": scope,
            "id": entity_id,
            "path": str(path),
            "allow": visibility.allow,
            "deny": visibility.deny,
            "bootstrapped_from": visibility.bootstrapped_from,
            "enabled_skills": sorted(enabled),
            "disabled_skills": sorted(disabled),
        }

    async def handle_skills_visibility_set(self, params: dict) -> dict:
        """设置某个 member/team 的 skill 可见性 metadata.

        全量替换 allow 与 deny 两份名单。空 allow 表示继承全库而非全禁；
        deny 无条件优先于 allow。写入经跨进程文件锁 + 原子替换落盘。

        params:
            scope: "member" 或 "team"
            team_name: 团队名
            member_name: 成员名（scope=member 时必填）
            allow: skill 名列表，缺省或空表示继承全库
            deny: skill 名列表，缺省或空表示不禁用

        Returns:
            落盘后的 allow / deny 名单与合成后的生效集合。
        """
        from openjiuwen.agent_teams.skill.file_lock import FileLockTimeout
        from openjiuwen.agent_teams.skill.visibility import set_skill_visibility

        target = await asyncio.to_thread(self._resolve_skill_visibility_target, params)
        if isinstance(target, dict):
            return target
        scope, entity_id, path = target

        allow = _coerce_skill_name_list(params.get("allow"), "allow")
        deny = _coerce_skill_name_list(params.get("deny"), "deny")
        try:
            visibility = await asyncio.to_thread(
                set_skill_visibility,
                path,
                scope=scope,
                entity_id=entity_id,
                allow=allow,
                deny=deny,
            )
        except FileLockTimeout as exc:
            logger.warning("[SkillVisibility] lock timeout: path=%s error=%s", path, exc)
            return {"success": False, "detail": "可见性文件被占用，请稍后重试。"}
        except OSError as exc:
            logger.warning("[SkillVisibility] write failed: path=%s error=%s", path, exc)
            return {"success": False, "detail": f"写入可见性配置失败: {exc}"}

        enabled, disabled = await asyncio.to_thread(
            self._compose_skill_visibility_for_params,
            params,
            scope,
        )
        return {
            "success": True,
            "scope": scope,
            "id": entity_id,
            "path": str(path),
            "allow": visibility.allow,
            "deny": visibility.deny,
            "enabled_skills": sorted(enabled),
            "disabled_skills": sorted(disabled),
        }

    async def handle_skills_visibility_update(self, params: dict) -> dict:
        """增量修改某个 member/team 的 skill 可见性 metadata.

        与全量 set 的区别在于「读-改-写」发生在**同一次持锁内**：两个客户端
        并发授权时互不覆盖，各自的增删都会保留。语义仍为空 allow 表示继承全库，
        deny 无条件优先于 allow。

        params:
            scope: "member" 或 "team"
            team_name: 团队名
            member_name: 成员名（scope=member 时必填）
            add_allow: 追加到 allow 名单的 skill 名列表
            remove_allow: 从 allow 名单移除的 skill 名列表
            add_deny: 追加到 deny 名单的 skill 名列表
            remove_deny: 从 deny 名单移除的 skill 名列表

        Returns:
            落盘后的 allow / deny 名单与合成后的生效集合。
        """
        from openjiuwen.agent_teams.skill.file_lock import FileLockTimeout
        from openjiuwen.agent_teams.skill.visibility import update_skill_visibility

        target = await asyncio.to_thread(self._resolve_skill_visibility_target, params)
        if isinstance(target, dict):
            return target
        scope, entity_id, path = target

        add_allow = _coerce_skill_name_list(params.get("add_allow"), "add_allow")
        remove_allow = _coerce_skill_name_list(params.get("remove_allow"), "remove_allow")
        add_deny = _coerce_skill_name_list(params.get("add_deny"), "add_deny")
        remove_deny = _coerce_skill_name_list(params.get("remove_deny"), "remove_deny")
        try:
            visibility = await asyncio.to_thread(
                update_skill_visibility,
                path,
                scope=scope,
                entity_id=entity_id,
                add_allow=add_allow,
                remove_allow=remove_allow,
                add_deny=add_deny,
                remove_deny=remove_deny,
            )
        except FileLockTimeout as exc:
            logger.warning("[SkillVisibility] lock timeout: path=%s error=%s", path, exc)
            return {"success": False, "detail": "可见性文件被占用，请稍后重试。"}
        except OSError as exc:
            logger.warning("[SkillVisibility] update failed: path=%s error=%s", path, exc)
            return {"success": False, "detail": f"写入可见性配置失败: {exc}"}

        enabled, disabled = await asyncio.to_thread(
            self._compose_skill_visibility_for_params,
            params,
            scope,
        )
        return {
            "success": True,
            "scope": scope,
            "id": entity_id,
            "path": str(path),
            "allow": visibility.allow,
            "deny": visibility.deny,
            "enabled_skills": sorted(enabled),
            "disabled_skills": sorted(disabled),
        }

    def _skill_retrieval_records(self):
        """Return the same live enabled inventory used by Agent discovery."""

        from openjiuwen.symphony.discovery import scan_skill_directories
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            skill_sources_from_manager,
        )

        return scan_skill_directories(
            [str(self._skills_dir)],
            disabled_skills=self.list_execution_disabled_skills(),
            source_by_name=skill_sources_from_manager(self),
        ).items

    def _skill_taxonomy_runtime(self, records_provider=None):
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            skill_retrieval_artifact_root,
        )
        from jiuwenswarm.symphony.skill_retrieval import SkillTaxonomyRuntime

        return SkillTaxonomyRuntime(
            records_provider=records_provider or self._skill_retrieval_records,
            index_root=skill_retrieval_artifact_root(),
        )

    @staticmethod
    def _skill_retrieval_mode() -> str:
        from jiuwenswarm.common.config import get_config

        config = get_config() or {}
        symphony = config.get("symphony") if isinstance(config, dict) else {}
        retrieval = (
            symphony.get("skill_retrieval") if isinstance(symphony, dict) else {}
        )
        raw = (
            str(retrieval.get("mode") or "auto").strip().lower()
            if isinstance(retrieval, dict)
            else "auto"
        )
        return "flat" if raw == "flat" else "auto"

    async def handle_skills_retrieval_status(self, params: dict) -> dict:
        """Return the live flat/indexed directory and optional build status."""

        params = params if isinstance(params, dict) else {}
        session_profile = params.get("_session_profile")
        session_profile = session_profile if isinstance(session_profile, dict) else None
        from dataclasses import replace

        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            build_discovery_settings,
            is_skill_retrieval_enabled,
            is_skill_retrieval_index_enabled,
            resolve_skill_retrieval_strategy,
            skill_retrieval_artifact_root,
            skill_sources_from_manager,
        )
        from openjiuwen.symphony.discovery import SkillFS, scan_skill_directories

        disabled = {str(name) for name in self.list_execution_disabled_skills()}
        all_documents = await asyncio.to_thread(
            scan_skill_directories,
            [str(self._skills_dir)],
            source_by_name=skill_sources_from_manager(self),
        )
        visible_documents = tuple(
            record
            for record in all_documents.items
            if record.worker_id not in disabled
        )
        config = get_config() or {}
        configured_enabled = is_skill_retrieval_enabled(config)
        configured_index_enabled = is_skill_retrieval_index_enabled(config)
        enabled = configured_enabled
        index_enabled = configured_index_enabled
        artifact_root = skill_retrieval_artifact_root()
        settings = build_discovery_settings(config)
        flat_directory = SkillFS(
            lambda: visible_documents,
            settings=replace(settings, use_existing_index=False),
            artifact_root=artifact_root,
            index_root=artifact_root,
        )
        snapshot = flat_directory.prompt_snapshot()
        candidate_scale = "small" if snapshot.all_candidates_included else "large"
        directory = flat_directory
        if enabled and index_enabled and candidate_scale == "large":
            directory = SkillFS(
                lambda: visible_documents,
                settings=replace(settings, use_existing_index=True),
                artifact_root=artifact_root,
                index_root=artifact_root,
            )
        build = await asyncio.to_thread(
            self._skill_taxonomy_runtime(lambda: visible_documents).status,
        )
        artifact = directory.artifact
        profile_scope = "default"
        profile_session_id = ""
        profile_model_name = ""
        pinned_index_revision = ""
        estimated_candidate_tokens = snapshot.estimated_candidate_tokens
        candidate_budget_tokens = snapshot.candidate_budget_tokens
        layout = artifact.layout
        index_state = artifact.index_state
        searchable_count = len(visible_documents)
        profile_recovery_error = ""
        if session_profile is not None:
            profile_scope = "session"
            profile_session_id = str(session_profile.get("session_id") or "")
            profile_model_name = str(session_profile.get("model_name") or "")
            pinned_index_revision = str(
                session_profile.get("pinned_index_revision") or ""
            )
            enabled = configured_enabled and bool(session_profile.get("enabled"))
            index_enabled = bool(session_profile.get("index_enabled"))
            if session_profile.get("candidate_scale") in {"small", "large"}:
                candidate_scale = str(session_profile["candidate_scale"])
            estimated_candidate_tokens = max(
                0,
                int(
                    session_profile.get("estimated_candidate_tokens")
                    or estimated_candidate_tokens
                ),
            )
            candidate_budget_tokens = max(
                1,
                int(
                    session_profile.get("candidate_budget_tokens")
                    or candidate_budget_tokens
                ),
            )
            if session_profile.get("layout") in {"flat", "tree"}:
                layout = str(session_profile["layout"])
            if session_profile.get("index_state") in {
                "missing",
                "fresh",
                "stale",
                "not-required",
            }:
                index_state = str(session_profile["index_state"])
            searchable_count = max(
                0,
                int(session_profile.get("searchable_count") or searchable_count),
            )
            profile_recovery_error = str(
                session_profile.get("profile_recovery_error") or ""
            )
        else:
            models = config.get("models") if isinstance(config, dict) else {}
            defaults = models.get("defaults") if isinstance(models, dict) else None
            if isinstance(defaults, list):
                entry = next(
                    (
                        item
                        for item in defaults
                        if isinstance(item, dict) and item.get("is_default") is True
                    ),
                    next((item for item in defaults if isinstance(item, dict)), {}),
                )
                client = entry.get("model_client_config") or {}
                if isinstance(client, dict):
                    profile_model_name = str(client.get("model_name") or "")
        public_index_state = (
            "not-required" if enabled and candidate_scale == "small" else index_state
        )
        requested_strategy = (
            str(session_profile.get("effective_strategy") or "")
            if session_profile is not None
            else ""
        )
        effective_strategy = "legacy"
        if enabled:
            effective_strategy = (
                requested_strategy
                if requested_strategy
                in {"small_full", "large_flat", "indexed", "indexed_stale"}
                else resolve_skill_retrieval_strategy(
                    enabled=True,
                    candidate_scale=candidate_scale,
                    layout=layout,
                    index_state=public_index_state,
                )
            )
        index_recommended = (
            enabled and candidate_scale == "large" and not index_enabled
        )
        build_supported = (
            configured_enabled
            and configured_index_enabled
            and candidate_scale == "large"
        )
        logs = build.get("logs") if isinstance(build.get("logs"), list) else []
        return {
            "enabled": enabled,
            "index_enabled": index_enabled,
            "mode": self._skill_retrieval_mode(),
            "candidate_scale": candidate_scale,
            "estimated_candidate_tokens": estimated_candidate_tokens,
            "candidate_budget_tokens": candidate_budget_tokens,
            "effective_strategy": effective_strategy,
            "layout": layout,
            "index_state": public_index_state,
            "index_required": index_recommended,
            "index_recommended": index_recommended,
            "build_supported": build_supported,
            "build_status": str(build.get("status") or "idle"),
            "build_stage": str(build.get("stage") or ""),
            "build_progress": float(build.get("progress") or 0.0),
            "build_message": str(build.get("message") or ""),
            "build_error": str(build.get("error") or ""),
            "build_id": str(build.get("build_id") or ""),
            "build_logs": logs[-50:],
            "index_exists": bool(build.get("index_exists")),
            "fresh": bool(build.get("fresh")),
            "indexed_count": int(build.get("indexed_count") or 0),
            "installed_count": all_documents.count,
            "installed_enabled_count": len(visible_documents),
            "searchable_count": searchable_count,
            "tool_name": "skill_index",
            "profile_scope": profile_scope,
            "profile_session_id": profile_session_id,
            "profile_model_name": profile_model_name,
            "pinned_index_revision": pinned_index_revision,
            "profile_recovery_error": profile_recovery_error,
        }

    async def handle_skills_retrieval_index_build(self, params: dict) -> dict:
        """Explicitly start Symphony's background taxonomy builder."""

        status = await self.handle_skills_retrieval_status(params)
        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            is_skill_retrieval_enabled,
            is_skill_retrieval_index_enabled,
        )

        config = get_config() or {}
        if not is_skill_retrieval_enabled(config):
            return {
                "success": False,
                "error_code": "skill_retrieval_disabled",
                "effective_strategy": "legacy",
                "build_status": status["build_status"],
                "detail": "Enable Skill retrieval before building its taxonomy.",
            }
        if not is_skill_retrieval_index_enabled(config):
            return {
                "success": False,
                "error_code": "skill_index_disabled",
                "effective_strategy": status["effective_strategy"],
                "build_status": status["build_status"],
                "detail": "Enable the Skill taxonomy switch before building it.",
            }
        if status["candidate_scale"] == "small":
            return {
                "success": False,
                "error_code": "skill_index_not_needed",
                "effective_strategy": "small_full",
                "build_status": status["build_status"],
                "detail": (
                    "The complete Skill metadata snapshot fits below the configured "
                    "threshold; no taxonomy build is needed."
                ),
            }

        raw_force = (params or {}).get("force", False)
        force = (
            raw_force.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(raw_force, str)
            else bool(raw_force)
        )
        try:
            payload = await asyncio.to_thread(
                self._skill_taxonomy_runtime().start_build,
                force=force,
            )
        except Exception as exc:
            logger.exception("Unable to start Skill taxonomy build")
            return {
                "success": False,
                "mode": self._skill_retrieval_mode(),
                "effective_strategy": status["effective_strategy"],
                "build_status": "failed",
                "detail": str(exc),
            }
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "success": bool(payload.get("success")),
            "background": bool(payload.get("success")),
            "mode": self._skill_retrieval_mode(),
            "effective_strategy": status["effective_strategy"],
            "build_status": str(data.get("state") or "running"),
            "build_id": str(data.get("build_id") or ""),
            "detail": str(payload.get("result") or ""),
        }

    async def handle_skills_retrieval_index_cancel(self, params: dict) -> dict:
        """Cancel the taxonomy builder without touching its previous index.

        Cancellation remains available after either switch is disabled because
        it is cleanup, not taxonomy construction or consumption.
        """

        build_id = str((params or {}).get("build_id") or "").strip() or None
        try:
            payload = await asyncio.to_thread(
                self._skill_taxonomy_runtime().cancel,
                build_id=build_id,
            )
        except Exception as exc:
            logger.exception("Unable to cancel Skill taxonomy build")
            return {
                "success": False,
                "mode": self._skill_retrieval_mode(),
                "build_status": "failed",
                "build_id": build_id or "",
                "detail": str(exc),
            }
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "success": bool(payload.get("success")),
            "mode": self._skill_retrieval_mode(),
            "build_status": str(data.get("state") or "idle"),
            "build_id": str(data.get("build_id") or ""),
            "detail": str(payload.get("result") or ""),
        }

    async def handle_skills_retrieval_search(self, params: dict) -> dict:
        """Search installed Skills through the same directory Tool used by Agents."""
        from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
            SkillRetrievalToolkit,
            skill_sources_from_manager,
        )

        query = str((params or {}).get("query") or "").strip()
        if not query:
            return {
                "success": False,
                "mode": self._skill_retrieval_mode(),
                "detail": "query must not be empty",
                "matches": [],
            }
        toolkit = SkillRetrievalToolkit(
            skill_directories=[str(self._skills_dir)],
            disabled_skills=self.list_execution_disabled_skills,
            source_by_name=lambda: skill_sources_from_manager(self),
        )
        result = await toolkit.skill_index(
            operation="search",
            query=query,
            paths=["/"],
            match="content",
            result="files",
            pipeline=[{"operation": "limit", "lines": 10}],
        )
        diagnostics = dict(result.detailed_output)
        return {
            "success": not bool(diagnostics.get("error")),
            "mode": self._skill_retrieval_mode(),
            "query": query,
            "result": str(result),
            "detailed_output": diagnostics,
        }

    async def handle_skills_retrieval_tree(self, params: dict) -> dict:
        """Keep the legacy tree-view RPC closed; Agents browse with list."""

        del params
        return {
            "success": False,
            "mode": self._skill_retrieval_mode(),
            "unsupported": True,
            "tree_supported": False,
            "nodes": [],
            "detail": "Browse the Skill taxonomy through skill_index list operations.",
        }

    async def handle_skills_graph_build(self, params: dict) -> dict:
        """Start a background Skill Graph build."""
        from jiuwenswarm.symphony.service import get_swarm_symphony_service

        value = (params or {}).get("force", False)
        force = (
            value.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(value, str)
            else bool(value)
        )
        return await get_swarm_symphony_service().start_refresh_graph(force=force)

    async def handle_skills_graph_status(self, params: dict) -> dict:
        """Return Skill Graph build and freshness status."""
        from jiuwenswarm.symphony.service import get_swarm_symphony_service

        del params
        return await get_swarm_symphony_service().graph_status()

    async def handle_skills_graph_get(self, params: dict) -> dict:
        """Return the current published Skill Graph."""
        from jiuwenswarm.symphony.service import get_swarm_symphony_service

        del params
        return await get_swarm_symphony_service().graph()

    async def handle_skills_graph_cancel(self, params: dict) -> dict:
        """Cancel the active Skill Graph build while retaining checkpoints."""
        from jiuwenswarm.symphony.service import get_swarm_symphony_service

        del params
        return await get_swarm_symphony_service().cancel_build()

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
        skill_dir = evo_path.parent if evo_path is not None else None
        return {
            "name": name,
            "exists": _has_effective_evolutions(skill_dir),
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
            return self._normalize_evolution_get_payload(
                name=name,
                exists=False,
                valid=True,
                raw=None,
            )

        try:
            raw = json.loads(evo_path.read_text(encoding="utf-8"))
            evo_file = EvolutionFile.from_dict(raw)
            return self._normalize_evolution_get_payload(
                name=name,
                exists=True,
                valid=True,
                raw=evo_file.to_dict(),
            )
        except Exception as exc:
            logger.warning("读取 evolutions.json 失败: skill=%s error=%s", name, exc)
            return self._normalize_evolution_get_payload(
                name=name,
                exists=True,
                valid=False,
                raw=None,
                detail="evolutions.json 格式错误或读取失败",
            )

    @staticmethod
    def _normalize_evolution_get_payload(
        *,
        name: str,
        exists: bool,
        valid: bool,
        raw: dict[str, Any] | None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """规范化 EvolutionLog 响应字段."""
        data = dict(raw) if isinstance(raw, dict) else {}
        skill_id = data.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            skill_id = name
        else:
            skill_id = skill_id.strip()

        version_raw = data.get("version")
        # version 为 EvolutionLog 数据结构版本，须为非空字符串；缺省/非法时回落为 "1.0.0"
        if isinstance(version_raw, str) and version_raw.strip():
            version = version_raw.strip()
        else:
            version = "1.0.0"

        updated_at_raw = data.get("updated_at")
        updated_at = (
            str(updated_at_raw).strip()
            if isinstance(updated_at_raw, str) and updated_at_raw.strip()
            else ""
        )

        entries_out: list[dict[str, Any]] = []
        entries = data.get("entries")
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                try:
                    entry = EvolutionEntry.from_dict(item).to_dict()
                except Exception:
                    continue
                # 可选字段仅非空时保留
                for opt_key in ("skill_version", "summary"):
                    val = entry.get(opt_key)
                    if val is None or (isinstance(val, str) and not val.strip()):
                        entry.pop(opt_key, None)
                change = entry.get("change")
                if isinstance(change, dict):
                    for opt_key in (
                        "skip_reason",
                        "merge_target",
                        "script_filename",
                        "script_language",
                        "script_purpose",
                    ):
                        val = change.get(opt_key)
                        if val is None or (isinstance(val, str) and not str(val).strip()):
                            change.pop(opt_key, None)
                usage = entry.get("usage_stats")
                if isinstance(usage, dict):
                    for opt_key in ("last_presented_at", "last_evaluated_at"):
                        val = usage.get(opt_key)
                        if val is None or (isinstance(val, str) and not str(val).strip()):
                            usage.pop(opt_key, None)
                entries_out.append(entry)

        out: dict[str, Any] = {
            "name": name,
            "exists": bool(exists),
            "valid": bool(valid),
            "skill_id": skill_id,
            "version": version,
            "updated_at": updated_at,
            "entries": entries_out,
        }
        if detail:
            out["detail"] = detail
        elif not valid and "detail" not in out:
            out["detail"] = "evolutions.json 格式错误或读取失败"
        return out

    async def handle_skills_evolution_save(self, params: dict) -> dict:
        """保存某个 skill 的 evolutions.json 条目列表.

        无版本 Skill 只写 workspace；存在默认版本时，同一可回滚流程中同步
        更新默认版本副本中的 ``evolutions.json``，不改变产品版本号。
        """
        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("缺少参数: name")
        try:
            name = _safe_path_name(name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.evolution.save", "skill", name, exc)
            raise ValueError(str(exc)) from exc

        skill_dir = self._resolve_local_skill_dir(name)
        if skill_dir is None:
            raise SkillRpcError(ERROR_SKILL_NOT_FOUND, f"未找到 skill: {name}")
        if self._is_builtin_skill(name, self._get_installed_plugins(), skill_dir):
            raise SkillRpcError("SKILL_BUILTIN_READ_ONLY", f"内置 Skill 不可保存经验: {name}")

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

        evo_path = skill_dir / _EVOLUTION_FILENAME
        evo_file = EvolutionFile.empty(skill_id=name)
        if evo_path.is_file():
            try:
                current = json.loads(evo_path.read_text(encoding="utf-8"))
                evo_file = EvolutionFile.from_dict(current)
            except Exception as exc:
                logger.warning("读取原 evolutions.json 失败，将以新内容覆盖: skill=%s error=%s", name, exc)

        evo_file.entries = normalized_entries
        evo_file.updated_at = datetime.now(timezone.utc).isoformat()
        if not evo_file.skill_id:
            evo_file.skill_id = name

        payload_text = json.dumps(evo_file.to_dict(), ensure_ascii=False, indent=2)
        default_version = None
        try:
            default_version = get_current_version(skill_dir)
        except SkillArchiveError as exc:
            raise SkillRpcError(exc.code, exc.message) from exc

        workspace_backup: str | None = None
        version_evo_path: Path | None = None
        version_backup: str | None = None
        version_existed = False

        try:
            if evo_path.is_file():
                workspace_backup = evo_path.read_text(encoding="utf-8")
            evo_path.parent.mkdir(parents=True, exist_ok=True)
            evo_path.write_text(payload_text, encoding="utf-8")

            if default_version:
                content_root = resolve_version_content_root(skill_dir, default_version)
                version_evo_path = content_root / _EVOLUTION_FILENAME
                version_existed = version_evo_path.is_file()
                if version_existed:
                    version_backup = version_evo_path.read_text(encoding="utf-8")
                version_evo_path.parent.mkdir(parents=True, exist_ok=True)
                version_evo_path.write_text(payload_text, encoding="utf-8")
                touch_version_metadata(skill_dir, default_version)
        except Exception as exc:
            # 可回滚：恢复 workspace 与默认版本副本
            try:
                if workspace_backup is None:
                    if evo_path.exists():
                        evo_path.unlink()
                else:
                    evo_path.write_text(workspace_backup, encoding="utf-8")
                if version_evo_path is not None:
                    if not version_existed:
                        if version_evo_path.exists():
                            version_evo_path.unlink()
                    elif version_backup is not None:
                        version_evo_path.write_text(version_backup, encoding="utf-8")
            except Exception:
                logger.warning("evolution.save 回滚失败: skill=%s", name, exc_info=True)
            if isinstance(exc, SkillArchiveError):
                raise SkillRpcError(exc.code, exc.message) from exc
            raise RuntimeError(f"保存经验失败: {exc}") from exc

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
                return await self.handle_skills_install_builtin({"name": safe_name})
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
            return await self.handle_skills_install_builtin({"name": plugin_name})

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
                return {"success": False, "detail": f"skill {plugin_name} 已存在"}
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
        """
        name = params.get("name", "")
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
            return {"success": False, "detail": f"技能 {name} 已经安装"}

        # 复制技能到用户目录
        try:
            shutil.copytree(src, dest)
        except Exception as exc:
            logger.error("安装内置技能失败: %s", exc)
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
        """Build a source-scoped identity key for conservative deduplication."""
        value = str(identifier or "").strip()
        if not value:
            return f"{source}:"
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            scheme = "https" if parsed.hostname and parsed.hostname.lower() == "github.com" else parsed.scheme.lower()
            host = parsed.netloc.lower()
            path = parsed.path.rstrip("/") or "/"
            return f"{source}:url:{scheme}://{host}{path}"
        return f"{source}:{value.casefold()}"

    @staticmethod
    def _classify_team_skill_plugin_type(plugin_type: Any) -> bool | None:
        """Map source-specific plugin types to the stable API classification."""
        normalized = str(plugin_type or "").strip().casefold()
        if normalized in _TEAM_SKILL_PLUGIN_TYPES:
            return True
        if normalized in _SINGLE_SKILL_PLUGIN_TYPES:
            return False
        return None

    @staticmethod
    def _normalize_online_search_item(source: str, item: dict[str, Any], rank: int) -> dict[str, Any]:
        if source == "skillnet":
            name = str(item.get("skill_name") or item.get("name") or "").strip()
            display_name = str(item.get("display_name") or name).strip() or name
            description = str(item.get("skill_description") or item.get("description") or "").strip()
            identifier = str(item.get("skill_url") or item.get("url") or "").strip()
            version = ""
            author = str(item.get("author") or "").strip()
            native_score = item.get("score")
            if native_score is None:
                native_score = item.get("stars")
            category = str(item.get("category") or "").strip()
            updated_at = 0
            is_team_skill = False
        elif source == "clawhub":
            name = str(item.get("slug") or item.get("name") or "").strip()
            display_name = str(item.get("display_name") or name).strip() or name
            description = str(item.get("summary") or "").strip()
            identifier = str(item.get("slug") or "").strip()
            version = str(item.get("version") or "").strip()
            owner_handle = str(item.get("owner_handle") or "").strip()
            # ClawHub download needs ownerHandle when the same slug has multiple publishers.
            author = owner_handle
            native_score = item.get("score")
            category = ""
            updated_at = item.get("updated_at") or 0
            is_team_skill = False
        elif source == "teamskillshub":
            identifier = str(item.get("asset_id") or item.get("identifier") or "").strip()
            name = str(item.get("name") or identifier).strip() or identifier
            display_name = str(item.get("display_name") or name).strip() or name
            description = str(item.get("summary") or item.get("description") or "").strip()
            version = str(item.get("version") or item.get("latest_version") or "").strip()
            author = str(item.get("author") or item.get("publisher_name") or "").strip()
            native_score = item.get("native_score")
            if native_score is None:
                native_score = item.get("install_count")
            category = str(item.get("category") or item.get("category_name") or "").strip()
            updated_at = item.get("updated_at") or item.get("update_time") or 0
            raw_team_skill = item.get("is_team_skill")
            if isinstance(raw_team_skill, bool):
                is_team_skill = raw_team_skill
            else:
                classified = SkillManager._classify_team_skill_plugin_type(item.get("plugin_type"))
                if classified is None:
                    raise ValueError(
                        "unsupported teamskillshub plugin_type: "
                        f"{item.get('plugin_type')!r}"
                    )
                is_team_skill = classified
        else:
            raise ValueError(f"unsupported online search source: {source}")

        normalized: dict[str, Any] = {
            "source": source,
            "name": name,
            "display_name": display_name,
            "description": description,
            "identifier": identifier,
            "version": version,
            "author": author,
            "is_team_skill": is_team_skill,
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
                try:
                    item = cls._normalize_online_search_item(source, raw_item, rank)
                except ValueError as exc:
                    logger.warning("忽略无法归一化的在线技能结果: source=%s error=%s", source, exc)
                    continue
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
                str(item.get("display_name") or "").strip().casefold(),
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
        """Search every currently available third-party source for Skills Marketplace."""
        params = params or {}
        query = str(params.get("query") if params.get("query") is not None else params.get("q", "")).strip()
        if not query:
            return {
                "success": False,
                "partial": False,
                "items": [],
                "sources": [],
                "detail": "缺少参数: query",
            }
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
                "teamskillshub",
                self.handle_skills_team_skills_hub_search({"q": query, "limit": limit}),
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

        payloads = await asyncio.gather(
            *(asyncio.wait_for(call, timeout=_ONLINE_SEARCH_SOURCE_TIMEOUT) for _, call in calls),
            return_exceptions=True,
        )
        source_results: dict[str, list[dict[str, Any]]] = {}
        for (source, _), payload in zip(calls, payloads):
            if isinstance(payload, asyncio.CancelledError):
                raise payload
            if isinstance(payload, Exception):
                logger.error("在线技能聚合搜索失败: source=%s error=%s", source, payload)
                status: dict[str, Any] = {"source": source, "status": "error", "count": 0}
                if isinstance(payload, TimeoutError):
                    status["detail_key"] = "skills.onlineSearch.sourceTimeout"
                else:
                    status["detail"] = str(payload)[:500]
                source_statuses.append(status)
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
            "success": any_success or not any_error,
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

        owner_handle = str(params.get("owner_handle") or "").strip()
        display_name = str(params.get("display_name") or "").strip()
        version = params.get("version")
        tag = params.get("tag")
        force = bool(params.get("force", False))

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
        try:
            self._assert_import_local_source_safe(path_raw)
        except ValueError as exc:
            return {"success": False, "detail": str(exc)}

        skill_root = Path(path_raw).expanduser().resolve()
        if not skill_root.exists() or not skill_root.is_dir():
            return {"success": False, "detail": f"path 不是有效目录: {skill_root}"}
        try:
            self._validate_local_skill_source(skill_root)
        except ValueError as exc:
            return {"success": False, "detail": str(exc)}

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
        """将 TeamSkills 目录打包为 zip（含 plugin.yaml，排除根级 ``.archive/``）."""
        path_raw = str(params.get("path") or "").strip()
        if not path_raw:
            return {"success": False, "detail": "缺少参数: path"}
        try:
            self._assert_import_local_source_safe(path_raw)
        except ValueError as exc:
            return {"success": False, "detail": str(exc)}

        skill_root = Path(path_raw).expanduser().resolve()
        if not skill_root.exists() or not skill_root.is_dir():
            return {"success": False, "detail": f"path 不是有效目录: {skill_root}"}
        try:
            self._validate_local_skill_source(skill_root)
        except ValueError as exc:
            return {"success": False, "detail": str(exc)}

        skill_dir = self._locate_skill_dir(skill_root) or skill_root
        skill_md = self._try_find_skill_file(skill_dir)
        if skill_md is None:
            return {"success": False, "detail": f"目录中未找到 SKILL.md: {skill_root}"}
        meta = self._parse_skill_md(skill_md)
        if meta is None:
            return {"success": False, "detail": "无法解析 SKILL.md"}
        skill_name = str(params.get("skill_name") or meta.get("name") or "").strip()
        description = str(meta.get("description") or "").strip()
        if not skill_name or not description:
            return {
                "success": False,
                "detail": "SKILL.md YAML 须包含非空 name 与 description",
                "code": ERROR_SKILL_INVALID_METADATA,
            }
        # 前端可传 version/display_name 覆盖 SKILL.md 值
        plugin_version = str(params.get("version") or "").strip() or "1.0.0"
        display_name = str(params.get("display_name") or meta.get("display_name") or "").strip() or skill_name
        author = str(meta.get("author") or "").strip() or "unknown"
        tags = meta.get("tags")
        if isinstance(tags, list):
            normalized_tags = [str(t).strip() for t in tags if str(t).strip()]
        elif isinstance(tags, str) and tags.strip():
            normalized_tags = [tags.strip()]
        else:
            normalized_tags = ["teamskills"]

        # 生成 plugin.yaml（与 _build_teamskills_publish_zip_from_root 对齐）
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
                # plugin.yaml 放在 {skill_name}/plugin.yaml
                zf.writestr(
                    f"{skill_name}/plugin.yaml",
                    yaml.safe_dump(plugin_yaml_payload, sort_keys=False, allow_unicode=True),
                )
                # README.md
                readme = skill_root / "README.md"
                if readme.is_file():
                    zf.write(readme, arcname=f"{skill_name}/README.md")
                # 技能文件
                for child in skill_root.rglob("*"):
                    if not child.is_file():
                        continue
                    if self._is_under_skill_archive(skill_root, child):
                        continue
                    rel = child.relative_to(skill_root).as_posix()
                    zf.write(child, arcname=f"{skill_name}/{skill_name}/{rel}")
            checksum = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
            return {"success": True, "path": str(zip_path), "checksum_sha256": checksum}
        except Exception as exc:
            logger.error("Team Skills Hub pack 失败: %s", exc)
            return {"success": False, "detail": str(exc)[:500]}

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
                plugin_type = str(item.get("plugin_type", "")).strip().casefold()
                is_team_skill = self._classify_team_skill_plugin_type(plugin_type)
                if is_team_skill is None:
                    logger.warning(
                        "忽略未知类型的 Team Skills Hub 搜索结果: asset_id=%s plugin_type=%r",
                        asset_id,
                        plugin_type,
                    )
                    continue
                normalized.append(
                    {
                        "asset_id": asset_id,
                        "name": name,
                        "display_name": str(item.get("display_name", "")).strip() or name,
                        "summary": str(item.get("short_desc", "")).strip(),
                        "version": str(item.get("latest_version", "")).strip(),
                        "author": str(item.get("publisher_name", "")).strip(),
                        "category": str(item.get("category_name", "")).strip(),
                        "native_score": item.get("install_count"),
                        "updated_at": int(item.get("update_time") or 0),
                        "plugin_type": plugin_type,
                        "is_team_skill": is_team_skill,
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

    async def handle_skills_swarm_skills_hub_recommend(self, params: dict) -> dict:
        """转发 Swarm Skills Hub 个性化推荐（POST /api/v1/recommend）。

        有效 token / system_token（含环境变量）时带鉴权头，走个性化召回。
        缺凭证时仍 POST、不带鉴权头：SkillHub 按空 user_id 走 Redis 下载量 TopK。
        无效 Bearer 同样由 SkillHub 视为匿名冷启动，不再 401。

        Hub 推荐本身不带 plugin_type；若传入 plugin_type/skill_type，则在 enrich
        后按 plugins 元数据过滤（对齐 SkillHub 市场 list + order_by=recommend）。
        enrich 时 plugins 查不到的项（下架/不可见）会丢掉；HTTP 失败则保留原项。
        """
        top_k_raw = params.get("top_k", params.get("limit", 10))
        try:
            top_k = max(1, min(int(top_k_raw), 500))
        except Exception:
            return {
                "success": False,
                "detail": "参数 top_k 必须是整数",
                "detail_key": "skills.swarmskillshub.errors.recommendFailed",
            }

        user_id = str(params.get("user_id") or "").strip()
        request_id = str(params.get("request_id") or "").strip()
        category_id = str(params.get("category_id") or "").strip()
        timestamp = params.get("timestamp")
        plugin_type_raw = str(params.get("plugin_type") or params.get("skill_type") or "").strip()
        plugin_types = self._parse_hub_plugin_types(plugin_type_raw)
        enrich_raw = params.get("enrich", True)
        if isinstance(enrich_raw, bool):
            enrich = enrich_raw
        else:
            enrich = str(enrich_raw).strip().lower() not in {"0", "false", "no", "off"}
        # 按类型过滤依赖 plugins 元数据，必须 enrich。
        if plugin_types:
            enrich = True
        base_url = self._get_team_skills_hub_base_url(str(params.get("market_url") or "").strip() or None)

        # 有类型过滤时多拉一些再筛，避免 top_k 滤完不够。
        fetch_k = top_k
        if plugin_types:
            fetch_k = min(500, max(top_k * 5, top_k))

        auth = self._resolve_teamskills_hub_auth_with_env(params)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if not auth.get("error"):
            if auth.get("system_token"):
                headers["X-System-Token"] = str(auth["system_token"])
            elif auth.get("token"):
                headers["Authorization"] = f"Bearer {auth['token']}"
        # No auth header: SkillHub cold-starts with source=topk_install (Redis), not MySQL install_count.

        body: dict[str, Any] = {
            "user_id": user_id,
            "request_id": request_id,
            "top_k": fetch_k,
            "category_id": category_id,
        }
        if timestamp is not None:
            body["timestamp"] = timestamp

        try:
            data = await self._team_skills_hub_http_post_data(
                "/api/v1/recommend",
                json_body=body,
                headers=headers,
                timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
                base_url=base_url,
            )
            raw_items = data.get("items", []) if isinstance(data, dict) else []
            skills: list[dict[str, Any]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                asset_id = str(item.get("asset_id", "")).strip()
                if not asset_id:
                    continue
                try:
                    score = float(item.get("score", 0.0))
                except Exception:
                    score = 0.0
                skills.append(
                    {
                        "asset_id": asset_id,
                        "score": score,
                        "name": asset_id,
                        "display_name": asset_id,
                        "summary": "",
                        "version": "",
                        "updated_at": 0,
                        "plugin_type": "",
                        "tags": [],
                    }
                )

            # Hub cold-start may ignore top_k and return the full snapshot; cap before enrich
            # so we do not N+1 storm /plugins for hundreds of ids.
            skills = skills[:fetch_k]

            if enrich and skills:
                skills = await self._enrich_swarmskills_recommend_skills(skills, base_url=base_url)
                if plugin_types:
                    allowed = set(plugin_types)
                    skills = [
                        s
                        for s in skills
                        if self._normalize_hub_plugin_type(str(s.get("plugin_type") or "")) in allowed
                    ]
            skills = skills[:top_k]

            hub_user_id = "" if not isinstance(data, dict) else str(data.get("user_id") or "")
            return {
                "success": True,
                "request_id": str((data or {}).get("request_id") or request_id),
                "user_id": hub_user_id,
                "source": str((data or {}).get("source") or ""),
                "category_id": str((data or {}).get("category_id") or category_id),
                "plugin_type": ",".join(plugin_types) if plugin_types else "",
                "count": len(skills),
                "skills": skills,
                "items": skills,
            }
        except Exception as exc:
            logger.error("Swarm Skills Hub 推荐失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.swarmskillshub.errors.recommendFailed",
            }

    async def handle_skills_team_skills_hub_install(self, params: dict) -> dict:
        """从 Team Skills Hub 安装技能（首次安装落盘完整版本副本）."""
        asset_id = str(params.get("asset_id", "")).strip()
        if not asset_id:
            return {"success": False, "detail": "缺少参数: asset_id"}

        force = bool(params.get("force", False))
        version = params.get("version")
        version_str = str(version).strip() if version is not None else ""

        existing = self._find_skill_dir_by_installed_asset_id(asset_id)
        if existing is not None:
            return {
                "success": True,
                "skill": {
                    "name": existing.name,
                    "source": "teamskillshub",
                    "asset_id": asset_id,
                    "path": str(existing),
                },
            }

        try:
            base_url = self._get_team_skills_hub_base_url(
                str(params.get("market_url") or "").strip() or None
            )
            # 页面不传 version 时取远端最新；兼容内部诊断传 version
            artifact_data = await self._team_skills_hub_http_get_data(
                f"/api/v1/artifacts/{asset_id}",
                params={"version": version_str} if version_str else None,
                timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
                base_url=base_url,
            )
            if not isinstance(artifact_data, dict):
                raise SkillRpcError(
                    ERROR_SKILLHUB_INSTALL_FAILED,
                    "marketplace 返回数据格式错误",
                )

            download_url = str(artifact_data.get("download_url", "")).strip()
            if not download_url:
                raise SkillRpcError(
                    ERROR_SKILLHUB_INSTALL_FAILED,
                    "marketplace 未返回 download_url",
                )
            self._assert_team_skills_hub_download_url_allowed(download_url)
            checksum_sha256 = str(artifact_data.get("checksum_sha256", "")).strip()

            artifact_bytes = await self._download_zip_and_verify(
                download_url, checksum_sha256=checksum_sha256
            )

            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_team_skills_hub_") as tmpdir:
                tmp_path = Path(tmpdir)
                self._safe_extract_zip_bytes_to_dir(artifact_bytes, tmp_path)

                skill_dir = self._locate_skill_dir(tmp_path)
                if skill_dir is None:
                    raise SkillRpcError(
                        ERROR_SKILL_INVALID_PACKAGE,
                        "下载内容不完整，未找到 SKILL.md",
                    )

                meta = self._assert_skill_package_safe(skill_dir)
                # 触发类型识别（与导入路径一致）
                detect_skill_type(skill_dir)

                raw_skill_name = str(meta.get("name") or "").strip() or asset_id
                try:
                    skill_name = _safe_path_name(raw_skill_name, "skill")
                except ValueError as exc:
                    raise SkillRpcError(ERROR_SKILL_INVALID_METADATA, str(exc)) from exc

                product_version = (
                    str(artifact_data.get("version") or "").strip()
                    or str(meta.get("version") or "").strip()
                )
                if not product_version:
                    raise SkillRpcError(
                        ERROR_SKILLHUB_INSTALL_FAILED,
                        "marketplace 未返回可用版本号",
                    )

                output_raw = str(params.get("output", "")).strip()
                use_custom_output = bool(output_raw)
                install_root = (
                    Path(output_raw).expanduser().resolve()
                    if use_custom_output
                    else self._skills_dir
                )
                install_root.mkdir(parents=True, exist_ok=True)
                dest = install_root / skill_name

                if dest.exists():
                    # 即使 force=true 也不得覆盖同名本地 Skill
                    installed_same = get_installed_asset_id(dest) == asset_id
                    if installed_same:
                        return {
                            "success": True,
                            "skill": {
                                "name": skill_name,
                                "source": "teamskillshub",
                                "asset_id": asset_id,
                                "path": str(dest),
                            },
                        }
                    raise SkillRpcError(
                        ERROR_SKILL_NAME_CONFLICT,
                        f"本地已存在同名 Skill: {skill_name}",
                    )
                # force 仅保留兼容字段，本期禁止用于删除覆盖
                _ = force

                self._install_teamskills_hub_first_copy(
                    skill_dir=skill_dir,
                    dest=dest,
                    asset_id=asset_id,
                    product_version=product_version,
                )

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
                        continue
                    mirror_root.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(
                        skill_dir,
                        mirror_dest,
                        ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
                    )

                installed_at = datetime.now(timezone.utc).isoformat()
                self._add_local_skill(
                    {
                        "name": skill_name,
                        "origin": f"teamskillshub:{asset_id}",
                        "source": "teamskillshub",
                        "installed_at": installed_at,
                    }
                )
                self._add_installed_plugin(
                    {
                        "name": skill_name,
                        "marketplace": "teamskillshub",
                        "commit": "",
                        "source": "teamskillshub",
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
        except SkillRpcError:
            raise
        except Exception as exc:
            logger.error("Team Skills Hub 安装失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.teamskillshub.errors.installFailed",
                "code": ERROR_SKILLHUB_INSTALL_FAILED,
            }

    async def handle_skills_team_skills_hub_publish(self, params: dict) -> dict:
        """发布 TeamSkills；成功后仅写入本地远端发布元数据."""
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

        local_skill_dir = self._resolve_teamskills_publish_local_skill_dir(
            path_raw=path_raw, file_raw=file_raw
        )
        if local_skill_dir is not None:
            last_published = get_last_published_version(local_skill_dir)
            if (
                last_published is not None
                and last_published == plugin_version
                and not force
            ):
                raise SkillRpcError(
                    ERROR_SKILL_PUBLISH_VERSION_CONFLICT,
                    f"发布版本与最近已发布版本冲突: {plugin_version}",
                )

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

                meta_dir = local_skill_dir
                if meta_dir is None and name:
                    candidate = self._skills_dir / name
                    if candidate.is_dir():
                        meta_dir = candidate
                if meta_dir is not None and meta_dir.is_dir() and skill_id:
                    update_publish_remote_metadata(
                        meta_dir,
                        remote_asset_id=skill_id,
                        last_published_version=version,
                    )

                return {"success": True, "skill_id": skill_id, "name": name, "version": version}
        except SkillRpcError:
            raise
        except Exception as exc:
            logger.error("Team Skills Hub 发布失败: %s", exc)
            return {
                "success": False,
                "detail": str(exc)[:500],
                "detail_key": "skills.teamskillshub.errors.publishFailed",
                "code": ERROR_SKILLHUB_PUBLISH_FAILED,
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

    async def handle_skills_swarm_skills_hub_detail(self, params: dict) -> dict:
        """按 asset_id 查询 SkillHub 最新公开版本详情（skills.swarmskillshub.detail）."""
        try:
            asset_id = _safe_path_name(str(params.get("asset_id") or ""), "asset_id")
        except ValueError as exc:
            _log_rejected_name("skills.swarmskillshub.detail", "asset_id", params.get("asset_id"), exc)
            return {
                "success": False,
                "detail": "asset_id 无效",
                "detail_key": _DETAIL_KEY_SKILLHUB_DETAIL_FAILED,
                "code": ERROR_SKILLHUB_DETAIL_FAILED,
            }

        # 普通页面不得覆盖 market_url / token；统一使用后端 Team Skills Hub 配置。
        base_url = self._get_team_skills_hub_base_url()
        auth = self._resolve_teamskills_hub_server_auth()
        try:
            plugin_item = await self._team_skills_hub_get_public_plugin_item(
                asset_id,
                base_url=base_url,
                token=auth.get("token"),
                system_token=auth.get("system_token"),
            )
            public_latest_version = str(plugin_item.get("public_latest_version") or "").strip()
            if not public_latest_version:
                return {
                    "success": False,
                    "detail": "SkillHub 资产无公开版本",
                    "detail_key": _DETAIL_KEY_SKILLHUB_DETAIL_NOT_FOUND,
                    "code": ERROR_SKILLHUB_DETAIL_NOT_FOUND,
                }

            version_detail = await self._team_skills_hub_get_version_detail(
                asset_id,
                public_latest_version,
                base_url=base_url,
                token=auth.get("token"),
                system_token=auth.get("system_token"),
            )
            resp_asset_id = str(version_detail.get("asset_id") or "").strip()
            resp_version = str(version_detail.get("version") or "").strip()
            if resp_asset_id != asset_id or resp_version != public_latest_version:
                return {
                    "success": False,
                    "detail": "SkillHub 版本详情与解析结果不一致",
                    "detail_key": _DETAIL_KEY_SKILLHUB_DETAIL_FAILED,
                    "code": ERROR_SKILLHUB_DETAIL_FAILED,
                }

            data = self._merge_skillhub_public_detail(plugin_item, version_detail)
            return {
                "success": True,
                "asset_id": asset_id,
                "version": public_latest_version,
                "data": data,
            }
        except SkillRpcError as exc:
            detail_key = (
                _DETAIL_KEY_SKILLHUB_DETAIL_NOT_FOUND
                if exc.code == ERROR_SKILLHUB_DETAIL_NOT_FOUND
                else _DETAIL_KEY_SKILLHUB_DETAIL_FAILED
            )
            return {
                "success": False,
                "detail": str(exc.message)[:300],
                "detail_key": detail_key,
                "code": exc.code,
            }
        except Exception as exc:
            logger.error("SkillHub 详情查询失败: %s", exc)
            return {
                "success": False,
                "detail": "SkillHub 详情请求失败",
                "detail_key": _DETAIL_KEY_SKILLHUB_DETAIL_FAILED,
                "code": ERROR_SKILLHUB_DETAIL_FAILED,
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

    def _skillnet_install_files_sync(
        self, skill_url: str, force: bool, mirror_url: str | None = None
    ) -> dict[str, Any]:
        """在工作线程中下载并拷贝到 skills 目录；返回 ok / skill_name / meta / skill_url."""
        try:
            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_skillnet_") as tmpdir:
                tmp_path = Path(tmpdir)
                download_path_str = self._skillnet_download_sync(skill_url, str(tmp_path), mirror_url)
                download_path = Path(download_path_str).resolve()
                if not download_path.exists():
                    return {
                        "ok": False,
                        "detail": "下载失败，请重试。",
                        "detail_key": "skills.skillNet.errors.downloadFailed",
                    }

                # 库在部分文件下载失败时仍会返回路径，只有找到 SKILL.md 才视为下载完整，才继续后续逻辑
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

    async def handle_skills_uninstall(self, params: dict) -> dict:
        """卸载已安装的 skill.

        params:
            name: skill 名称
        """
        raw_name = params.get("name", "")
        if not raw_name:
            return {"success": False, "detail": "缺少参数: name"}
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
            return {"success": False, "detail": f"未找到 skill: {name}"}

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

        self._remove_installed_plugin(raw_name)
        self._remove_local_skill(raw_name)
        # 卸载时一并清掉该 skill 的 enabled 配置，避免重装同名 skill 时沿用旧的禁用状态。
        self.remove_skill_config(raw_name)
        self._refresh_agent_data_indexes()
        return {"success": True}

    async def handle_skills_import_local(self, params: dict) -> dict:
        """从 download_token、本地路径或远程归档 URL 导入 skill.

        ``download_token`` 与 ``path`` 必须且只能提供一个。
        """
        params = params or {}
        download_token = str(params.get("download_token") or "").strip()
        raw_path = params.get("path")
        path_str = str(raw_path).strip() if raw_path is not None else ""
        force = bool(params.get("force", False))
        checksum_sha256 = str(params.get("checksum_sha256", "") or "").strip()
        session_id = str(
            params.get("_session_id") or params.get("session_id") or ""
        ).strip()

        has_token = bool(download_token)
        has_path = bool(path_str)
        if has_token == has_path:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "download_token 与 path 必须且只能提供一个",
            )

        logger.info(
            "[SkillManager] import_local called: token=%s path=%r force=%s remote=%s",
            bool(download_token),
            path_str if has_path else "",
            force,
            self._is_http_download_target(path_str) if has_path else False,
        )

        if has_token:
            return await self._import_local_from_download_token(
                download_token,
                force=force,
                session_id=session_id,
            )

        if self._is_http_download_target(path_str):
            try:
                return await self._import_skill_from_remote_archive(
                    download_url=path_str,
                    force=force,
                    checksum_sha256=checksum_sha256,
                )
            except SkillRpcError:
                raise
            except Exception as exc:
                logger.error("remote archive import failed: %s", exc)
                return {"success": False, "detail": str(exc)[:500]}

        return self._import_local_from_path(
            Path(path_str).expanduser(), force=force, origin=path_str
        )

    async def _import_local_from_download_token(
        self,
        download_token: str,
        *,
        force: bool,
        session_id: str,
    ) -> dict[str, Any]:
        """通过 send_file_to_user 签发的短期令牌导入 Skill 包."""
        from jiuwenswarm.agents.harness.common.tools.web_file_download import (
            validate_file_download_token,
        )

        if not session_id:
            raise SkillRpcError(
                ERROR_SKILL_DOWNLOAD_TOKEN_INVALID,
                "缺少会话信息，无法校验 download_token",
            )

        payload = validate_file_download_token(
            download_token,
            session_id=session_id,
            check_expiry=True,
        )
        if not payload:
            raise SkillRpcError(
                ERROR_SKILL_DOWNLOAD_TOKEN_INVALID,
                "download_token 无效、已过期或会话不匹配",
            )

        file_path_raw = str(payload.get("path") or "").strip()
        if not file_path_raw:
            raise SkillRpcError(
                ERROR_SKILL_DOWNLOAD_TOKEN_INVALID,
                "download_token 未绑定有效文件路径",
            )

        src = Path(file_path_raw)
        try:
            src = src.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SkillRpcError(
                ERROR_SKILL_DOWNLOAD_TOKEN_INVALID,
                f"令牌指向的文件不存在: {file_path_raw}",
            ) from exc

        if not self._is_skill_package_file(src):
            raise SkillRpcError(
                ERROR_SKILL_DOWNLOAD_TOKEN_INVALID,
                "令牌文件不是可识别的 .skill 或 Skill zip 包",
            )

        with tempfile.TemporaryDirectory(prefix="jiuwenswarm_import_token_") as tmpdir:
            tmp_path = Path(tmpdir)
            try:
                skill_dir = self._extract_skill_package_file(src, tmp_path)
            except SkillRpcError:
                raise
            except Exception as exc:
                logger.warning("[SkillManager] token package extract failed: %s", exc)
                raise SkillRpcError(
                    ERROR_SKILL_INVALID_PACKAGE,
                    f"无法解包 Skill 文件: {exc}",
                ) from exc
            return self._install_imported_skill_dir(
                skill_dir,
                force=force,
                origin=f"download_token:{src.name}",
            )

    @staticmethod
    def _is_skill_package_file(path: Path) -> bool:
        """判断是否为可导入的 Skill 包文件（``.skill`` / ``.zip``）."""
        if not path.is_file():
            return False
        name = path.name.lower()
        return (
            name.endswith(".zip")
            or name.endswith(".skill")
            or name.endswith(".skill.zip")
        )

    def _extract_skill_package_file(self, src: Path, dest_dir: Path) -> Path:
        """安全解包 Skill 包文件，返回含 SKILL.md 的根目录."""
        try:
            self._safe_extract_zip_to_dir(src, dest_dir)
        except zipfile.BadZipFile as exc:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "不是有效的 Skill zip/.skill 包",
            ) from exc
        except SkillRpcError:
            raise
        except Exception as exc:
            msg = str(exc)
            lower = msg.lower()
            path_related = "路径" in msg or "path" in lower
            zip_or_link = "zip" in lower or "symlink" in lower
            if path_related or zip_or_link:
                raise SkillRpcError(ERROR_SKILL_UNSAFE_PATH, msg) from exc
            raise

        skill_dir = self._locate_skill_dir(dest_dir)
        if skill_dir is None:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "包内未找到 SKILL.md，不是有效 Skill 包",
            )
        self._assert_skill_package_safe(skill_dir)
        return skill_dir

    async def handle_skills_import_upload(self, params: dict) -> dict:
        """浏览器上传的本地 ``.zip`` Skill 包安装到 workspace.

        仅接受 ``.zip``；``overwrite=false`` 且同名已存在时返回 ``SKILL_ALREADY_EXISTS``。
        """
        params = params or {}
        path_str = str(params.get("path") or "").strip()
        overwrite = bool(params.get("overwrite", False))
        if not path_str:
            raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "缺少上传文件 path")

        src = Path(path_str)
        try:
            src = src.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                f"上传文件不存在: {path_str}",
            ) from exc

        if not src.is_file():
            raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "上传内容不是文件")
        name_lower = src.name.lower()
        if name_lower.endswith(".skill") or name_lower.endswith(".skill.zip"):
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "仅接受 .zip 文件",
            )
        if not name_lower.endswith(".zip"):
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "仅接受 .zip 文件",
            )

        size = src.stat().st_size
        if size > _SKILL_ZIP_MAX_UPLOAD_BYTES:
            raise SkillRpcError(
                ERROR_SKILL_FILE_TOO_LARGE,
                f"上传包超过限制（{_SKILL_ZIP_MAX_UPLOAD_BYTES} 字节）",
            )

        with tempfile.TemporaryDirectory(prefix="jiuwenswarm_import_upload_") as tmpdir:
            skill_dir = self._extract_skill_package_file(src, Path(tmpdir))
            return self._install_imported_skill_dir(
                skill_dir,
                force=overwrite,
                origin="file-api:upload",
                conflict_code=ERROR_SKILL_ALREADY_EXISTS,
            )

    async def handle_skills_create_from_knowledge(self, params: dict) -> dict:
        """知识转 Skill：校验输入并准备隔离临时目录与 Agent follow-up.

        由上层静默跑主 Agent 后，再调用 ``_finalize_create_from_knowledge`` 安装。
        """
        params = params or {}
        link = str(params.get("link") or "").strip()
        file_path = str(params.get("file_path") or "").strip()
        skill_description = str(params.get("skill_description") or "").strip()

        has_link = bool(link)
        has_file = bool(file_path)
        if has_link == has_file:
            raise SkillRpcError(
                ERROR_SKILL_KNOWLEDGE_INPUT_CONFLICT,
                "link 与 file 必须且只能提供一个",
            )

        if has_file:
            src = Path(file_path)
            try:
                src = src.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise SkillRpcError(
                    ERROR_SKILL_INVALID_PACKAGE,
                    f"知识文档不存在: {file_path}",
                ) from exc
            if not src.is_file():
                raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "知识文档不是文件")
            size = src.stat().st_size
            if size > _SKILL_ZIP_MAX_UPLOAD_BYTES:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"知识文档超过限制（{_SKILL_ZIP_MAX_UPLOAD_BYTES} 字节）",
                )
            file_path = str(src)

        output_dir = Path(tempfile.mkdtemp(prefix="jiuwenswarm_skill_from_knowledge_"))
        skills = ["skill-omni-creation"] if has_link else ["skill-creator-router"]
        prompt_parts = [
            "请根据以下知识来源生成一个完整、可安装的 Skill，并写入指定输出目录。",
            f"输出目录（必须在此目录内生成含 SKILL.md 的 Skill 根目录）: {output_dir}",
            "要求：",
            "1. SKILL.md 必须含有效 YAML front matter，name 与 description 均为非空字符串；",
            "2. 不得创建根级 .archive/；",
            "3. 不要调用 send_file_to_user；不要打包投递给用户；直接把完整 Skill 目录写到输出目录；",
            "4. 生成后确保输出目录内可唯一定位 Skill 根目录。",
        ]
        if skill_description:
            prompt_parts.append(
                f"用户对目标 Skill 的补充约束（不得当作 Skill 名称直接使用）: {skill_description}"
            )
        if has_link:
            prompt_parts.append(f"知识来源链接: {link}")
        else:
            prompt_parts.append(f"知识来源本地文档绝对路径: {file_path}")

        return {
            "success": True,
            "result_type": "followup",
            "action": "run_create_from_knowledge",
            "followup_prompt": "\n".join(prompt_parts),
            "skills": skills,
            "output_dir": str(output_dir),
            "input_file": file_path if has_file else "",
            "link": link if has_link else "",
            "skill_description": skill_description,
            "trusted_dirs": [str(output_dir)]
            + ([str(Path(file_path).parent)] if has_file else []),
        }

    def finalize_create_from_knowledge(self, output_dir: str | Path) -> dict[str, Any]:
        """校验隔离目录中的生成结果并安装到 workspace（不覆盖已有同名 Skill）."""
        root = Path(output_dir)
        if not root.is_dir():
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "知识转 Skill 输出目录不存在",
            )
        skill_dir = self._locate_skill_dir(root)
        if skill_dir is None:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_PACKAGE,
                "未在输出目录找到有效 Skill（缺少 SKILL.md）",
            )
        self._assert_skill_package_safe(skill_dir)
        installed = self._install_imported_skill_dir(
            skill_dir,
            force=False,
            origin="file-api:create-from-knowledge",
            conflict_code=ERROR_SKILL_ALREADY_EXISTS,
        )
        if not installed.get("success"):
            return installed
        skill = dict(installed.get("skill") or {})
        workspace_path = str(skill.get("workspace_path") or "")
        content = ""
        if workspace_path:
            md = Path(workspace_path) / "SKILL.md"
            if md.is_file():
                content = md.read_text(encoding="utf-8")
        skill["content"] = content
        skill["version"] = None
        skill["source"] = "local"
        return {"success": True, "skill": skill}

    def _assert_skill_package_safe(
        self,
        skill_dir: Path,
        *,
        source_trusted: bool = False,
    ) -> dict[str, Any]:
        """校验包结构：拒根级 .archive，要求有效 name；本地导入还要求 description.

        ``source_trusted=True`` 用于远端归档等已过 host/解压校验的来源：
        与主仓 develop 一致，仅要求非空 name，允许缺 description。
        """
        if (skill_dir / ARCHIVE_DIRNAME).exists():
            raise SkillRpcError(
                ERROR_SKILL_RESERVED_PATH,
                "Skill 包不得包含根级 .archive/",
            )
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            # locate 可能找到子目录
            found = self._try_find_skill_file(skill_dir)
            if found is None:
                raise SkillRpcError(ERROR_SKILL_INVALID_PACKAGE, "缺少 SKILL.md")
            md = found
        meta = self._parse_skill_md(md)
        if meta is None:
            raise SkillRpcError(ERROR_SKILL_INVALID_METADATA, "无法解析 SKILL.md")
        name = str(meta.get("name") or "").strip()
        description = str(meta.get("description") or "").strip()
        if not name:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_METADATA,
                "SKILL.md YAML 须包含非空 name",
            )
        if not source_trusted and not description:
            raise SkillRpcError(
                ERROR_SKILL_INVALID_METADATA,
                "SKILL.md YAML 须包含非空 name 与 description",
            )
        return meta

    def _install_imported_skill_dir(
        self,
        src: Path,
        *,
        force: bool,
        origin: str,
        source_trusted: bool = False,
        conflict_code: str = ERROR_SKILL_IMPORT_OVERWRITE_REQUIRED,
    ) -> dict[str, Any]:
        """把已校验的 Skill 目录安装到 workspace，并返回约定的 skill 字段."""
        meta = self._assert_skill_package_safe(src, source_trusted=source_trusted)
        raw_skill_name = str(meta.get("name") or "").strip()
        try:
            skill_name = _safe_path_name(raw_skill_name, "skill")
        except ValueError as exc:
            _log_rejected_name("skills.import_local", "skill", raw_skill_name, exc)
            return {"success": False, "detail": str(exc)}

        dest = _safe_child_path(self._skills_dir, skill_name, "skill")
        existing = dest.exists()
        preserved_source = "local"
        preserved_version: str | None = None

        if existing:
            if self._is_builtin_skill(skill_name, self._get_installed_plugins(), dest):
                raise SkillRpcError(
                    ERROR_SKILL_BUILTIN_READ_ONLY,
                    f"内置 Skill 不可覆盖: {skill_name}",
                )
            if not force:
                raise SkillRpcError(
                    conflict_code or ERROR_SKILL_IMPORT_OVERWRITE_REQUIRED,
                    f"skill {skill_name} 已存在，需确认覆盖",
                )
            preserved_source = self._resolve_display_source_for_import(skill_name)
            try:
                preserved_version = get_current_version(dest)
            except SkillArchiveError:
                preserved_version = None
            try:
                self._overwrite_skill_workspace_preserving_archive(dest, src)
            except OSError as exc:
                # 覆盖失败时绝不能删除原 Skill；shutil.Error 继承 OSError
                return _handle_copy_error(
                    exc,
                    dest,
                    "local import overwrite",
                    src,
                    cleanup_dest=False,
                )
        else:
            try:
                shutil.copytree(
                    src,
                    dest,
                    ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
                )
            except OSError as exc:
                return _handle_copy_error(exc, dest, "local import dir", src)

        # 新建时不把包内 version 当作产品版本；覆盖时保留原 current_version
        if not existing:
            preserved_version = None

        existing_record = next(
            (
                s
                for s in self._state.get("local_skills", [])
                if isinstance(s, dict) and s.get("name") == skill_name
            ),
            None,
        )
        record = {
            "name": skill_name,
            "origin": origin,
            "source": (
                existing_record.get("source")
                if isinstance(existing_record, dict) and existing_record.get("source")
                else preserved_source
            ),
        }
        if isinstance(existing_record, dict) and existing_record.get("display_name"):
            record["display_name"] = existing_record.get("display_name")
        self._add_local_skill(record)
        self._refresh_agent_data_indexes()

        skill_type = detect_skill_type(dest)
        description = str(meta.get("description") or "").strip()
        source = self._resolve_display_source_for_import(skill_name)
        logger.info(
            "[SkillManager] import_local done: skill=%s origin=%s dest=%s overwrite=%s",
            skill_name,
            origin,
            dest,
            existing,
        )
        return {
            "success": True,
            "skill": {
                "name": skill_name,
                "description": description,
                "version": preserved_version,
                "skill_type": skill_type,
                "source": source,
                "workspace_path": str(dest),
            },
        }

    def _resolve_display_source_for_import(self, skill_name: str) -> str:
        """覆盖导入时保留已有展示来源；新建默认为 local."""
        for local_skill in self._state.get("local_skills", []):
            if isinstance(local_skill, dict) and local_skill.get("name") == skill_name:
                src = local_skill.get("source")
                if isinstance(src, str) and src.strip():
                    return src.strip()
        resolved = self._resolve_skill_source(skill_name)
        return resolved if resolved else "local"

    def _overwrite_skill_workspace_preserving_archive(
        self, dest: Path, src: Path
    ) -> None:
        """用新内容覆盖 workspace，保留 ``.archive``；若有默认版本则同步该版本副本.

        先在旁路拼装完整新目录，再原子替换；任一步失败都回滚，不破坏原 Skill。
        """
        archive_dir = dest / ARCHIVE_DIRNAME
        default_version: str | None = None
        try:
            default_version = get_current_version(dest)
        except SkillArchiveError:
            default_version = None

        staged = dest.with_name(f".{dest.name}.new_import_{uuid.uuid4().hex[:8]}")
        if staged.exists():
            _safe_rmtree(staged)

        try:
            # 旁路准备新内容，不动原目录
            shutil.copytree(
                src,
                staged,
                ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
            )
            # 把原 .archive 拷进 staged（拷贝而非挪走，失败时原目录仍完整）
            if archive_dir.exists():
                shutil.copytree(archive_dir, staged / ARCHIVE_DIRNAME)

            # 原子替换；内部失败会把 backup 恢复为 dest
            self.atomic_replace_dir(dest, staged)
        except Exception:
            if staged.exists():
                _safe_rmtree(staged)
            raise

        if default_version:
            try:
                content_root = resolve_version_content_root(dest, default_version)
            except SkillArchiveError:
                content_root = None
            if content_root is not None:
                self.copy_workspace_business_to_version(dest, content_root)
                touch_version_metadata(dest, default_version)

    def _import_local_from_path(
        self,
        src: Path,
        *,
        force: bool,
        origin: str,
        source_trusted: bool = False,
    ) -> dict[str, Any]:
        logger.info(
            "[SkillManager] import_local_from_path start: src=%s origin=%s force=%s trusted=%s",
            src,
            origin,
            force,
            source_trusted,
        )
        if not source_trusted:
            try:
                self._assert_import_local_source_safe(str(src))
            except ValueError as exc:
                logger.warning(
                    "[SkillManager] import_local_from_path rejected source: src=%s origin=%s error=%s",
                    src,
                    origin,
                    exc,
                )
                return {"success": False, "detail": str(exc)}

        if not src.exists():
            return {"success": False, "detail": f"路径不存在: {origin}"}

        # HEAD: 支持 .skill / .zip 包文件导入
        if src.is_file() and self._is_skill_package_file(src):
            with tempfile.TemporaryDirectory(prefix="jiuwenswarm_import_pkg_") as tmpdir:
                try:
                    skill_dir = self._extract_skill_package_file(src, Path(tmpdir))
                except SkillRpcError as exc:
                    return {"success": False, "detail": str(exc), "code": exc.code}
                return self._install_imported_skill_dir(
                    skill_dir, force=force, origin=origin
                )

        if source_trusted:
            # 远端归档导入：源已过 host 白名单 + 防 slip 解压（含拒符号链接），
            # 直接复用本流程，name 沿用下方原宽松解析。
            skill_name = None
        else:
            try:
                skill_name = self._validate_local_skill_source(src)
            except ValueError as exc:
                logger.warning(
                    "[SkillManager] import_local_from_path rejected skill: src=%s origin=%s error=%s",
                    src,
                    origin,
                    exc,
                )
                return {"success": False, "detail": str(exc)}

        if src.is_file():
            meta = self._parse_skill_md(src)
            if meta is None:
                return {"success": False, "detail": "无法解析 skill 文件"}
            raw_skill_name = meta.get("name", src.stem)
            description = str(meta.get("description") or "").strip()
            if source_trusted:
                try:
                    skill_name = _safe_path_name(raw_skill_name, "skill")
                except ValueError as exc:
                    _log_rejected_name("skills.import_local", "skill", raw_skill_name, exc)
                    return {"success": False, "detail": str(exc)}
            elif not str(raw_skill_name or "").strip() or not description:
                raise SkillRpcError(
                    ERROR_SKILL_INVALID_METADATA,
                    "SKILL.md YAML 须包含非空 name 与 description",
                )
            dest = _safe_child_path(self._skills_dir, skill_name, "skill")
            if dest.exists():
                if self._is_builtin_skill(skill_name, self._get_installed_plugins(), dest):
                    raise SkillRpcError(
                        ERROR_SKILL_BUILTIN_READ_ONLY,
                        f"内置 Skill 不可覆盖: {skill_name}",
                    )
                if not force:
                    raise SkillRpcError(
                        ERROR_SKILL_IMPORT_OVERWRITE_REQUIRED,
                        f"skill {skill_name} 已存在，需确认覆盖（force=true）",
                    )
                # 单文件覆盖：保留 .archive，仅替换该 md
                preserved_version = None
                try:
                    preserved_version = get_current_version(dest)
                except SkillArchiveError:
                    preserved_version = None
                dest.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dest / src.name)
                except OSError as exc:
                    return _handle_copy_error(
                        exc, dest, "local import file", src, cleanup_dest=False
                    )
                self._add_local_skill(
                    {
                        "name": skill_name,
                        "origin": origin,
                        "source": self._resolve_display_source_for_import(skill_name),
                    }
                )
                self._refresh_agent_data_indexes()
                return {
                    "success": True,
                    "skill": {
                        "name": skill_name,
                        "description": description,
                        "version": preserved_version,
                        "skill_type": detect_skill_type(dest),
                        "source": self._resolve_display_source_for_import(skill_name),
                        "workspace_path": str(dest),
                    },
                }
            try:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / src.name)
            except OSError as exc:
                return _handle_copy_error(exc, dest, "local import file", src)
            self._add_local_skill(
                {
                    "name": skill_name,
                    "origin": origin,
                    "source": "local",
                }
            )
            self._refresh_agent_data_indexes()
            return {
                "success": True,
                "skill": {
                    "name": skill_name,
                    "description": description,
                    "version": None,
                    "skill_type": detect_skill_type(dest),
                    "source": "local",
                    "workspace_path": str(dest),
                },
            }

        if src.is_dir():
            md = self._try_find_skill_file(src)
            if md is None:
                return {"success": False, "detail": f"目录中未找到 SKILL.md: {origin}"}
            # 统一走安装路径（保留 .archive / skill_type 等）；
            # source_trusted 时放宽 description，兼容远端归档。
            try:
                return self._install_imported_skill_dir(
                    src,
                    force=force,
                    origin=origin,
                    source_trusted=source_trusted,
                )
            except SkillRpcError as exc:
                # 兼容旧调用方：部分路径场景仍返回 success=false
                if exc.code in {
                    ERROR_SKILL_INVALID_METADATA,
                    ERROR_SKILL_RESERVED_PATH,
                    ERROR_SKILL_INVALID_PACKAGE,
                }:
                    raise
                raise

        return {"success": False, "detail": f"不支持的路径类型: {origin}"}

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

            expected = checksum_sha256.strip().lower()
            if expected:
                digest = hashlib.sha256(body).hexdigest().lower()
                if digest != expected:
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
            return self._import_local_from_path(
                skill_dir,
                force=force,
                origin=download_url,
                source_trusted=True,
            )


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

    # -----------------------------------------------------------------------
    # 目录扫描
    # -----------------------------------------------------------------------

    def _scan_local_skills(self) -> list[dict]:
        """扫描 agent/skills/ 下的本地 skill（跳过 _marketplace）.

        Also scans connected MCPs' bundled skills (under
        <workspace>/mcp/skills/<name>/). An MCP's skills surface only while
        it is connected — toggling it on/off controls whether its skills load.
        """
        results: list[dict] = []

        # 1) user skills under <workspace>/skills/
        if self._skills_dir.exists():
            for child in self._skills_dir.iterdir():
                if not child.is_dir() or child.name.startswith("_"):
                    continue
                meta = self._scan_one_skill_dir(child)
                if meta is not None:
                    results.append(meta)

        # 2) connected MCP skill dirs (only connected MCPs). skill_installer
        # normalizes all MCP skills to the nested shape mcp/skills/<name>/<skill>/,
        # so each child dir here is one skill (no flat-layout special case).
        for entry in self._mcp_skills_dirs():
            cdir = Path(str(entry.get("dir", "")))
            if not cdir.is_dir():
                continue
            for child in cdir.iterdir():
                if not child.is_dir() or child.name.startswith("_"):
                    continue
                meta = self._scan_one_skill_dir(child, source_default="mcp")
                if meta is not None:
                    results.append(meta)

        return results

    def _scan_one_skill_dir(
        self, child: Path, *, source_default: str = "project"
    ) -> dict[str, Any] | None:
        """Scan a single skill directory -> meta dict, or None if not a skill."""
        md = self._try_find_skill_file(child)
        if md is None:
            return None
        meta = self._parse_skill_md(md)
        if meta is None:
            return None

        # 工作区技能身份以目录名为准，避免 frontmatter name 与目录不一致时
        # （如 skill-creator-normal 仍写 name: skill-creator）产生重复列表项。
        meta["name"] = child.name

        # 判断 source 类型
        installed = self._get_installed_plugins()
        source = source_default
        for p in installed:
            if p.get("name") == meta.get("name"):
                source = p.get("source", source_default)
                if source == source_default and p.get("marketplace"):
                    source = p.get("marketplace", source_default)
                break
        # 检查是否通过 import_local / SkillNet 等写入 local_skills（含 origin 供前端对照 skill_url）
        for ls in self._state.get("local_skills", []):
            if ls.get("name") == meta.get("name"):
                source = ls.get("source", source_default) if isinstance(ls, dict) else source_default
                if isinstance(ls, dict):
                    origin = ls.get("origin")
                    if isinstance(origin, str) and origin.strip():
                        meta["origin"] = origin.strip()
                    display_name = ls.get("display_name")
                    if isinstance(display_name, str) and display_name.strip():
                        meta["display_name"] = display_name.strip()
                break

        meta["source"] = source
        if not str(meta.get("display_name") or "").strip():
            meta["display_name"] = meta.get("name", "")
        meta["installed"] = True
        meta["enabled"] = self.get_skill_enabled(meta.get("name", ""))
        # 判断是否为内置技能（传入 child 路径，通过实际路径判断）
        meta["is_builtin"] = self._is_builtin_skill(meta.get("name", ""), self._get_installed_plugins(), child)
        builtin_dir = get_builtin_skills_dir()
        if builtin_dir.exists():
            builtin_skill_path = builtin_dir / child.name
            meta["is_builtin_source"] = builtin_skill_path.exists() and builtin_skill_path.is_dir()
        else:
            meta["is_builtin_source"] = False
        meta["has_evolutions"] = _has_effective_evolutions(child)
        self.apply_archive_version_and_type(meta, child)
        # 不在列表中返回 body
        meta.pop("body", None)
        return meta

    def _scan_builtin_skills(self) -> list[dict]:
        """扫描内置技能目录中尚未安装到用户目录的技能.

        返回的技能列表仅包含那些存在于内置目录但尚未在用户目录中的技能。
        """
        results: list[dict] = []
        builtin_dir = get_builtin_skills_dir()
        user_skills_dir = get_agent_skills_dir()

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
            self.apply_archive_version_and_type(meta, child)
            # 不在列表中返回 body
            meta.pop("body", None)
            results.append(meta)

        return results

    def _resolve_skill_source(self, skill_name: str) -> str:
        """解析 skill 来源（local / project / marketplace 名称）."""
        if not skill_name:
            return "project"

        for plugin in self._get_installed_plugins():
            if plugin.get("name") == skill_name:
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
            if local_skill.get("name") == skill_name:
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
                display_name = str(plugin.get("display_name") or "").strip()
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

    @staticmethod
    def apply_archive_version_and_type(meta: dict, skill_dir: Path | None) -> None:
        """用 ``.archive`` 当前版本覆盖 YAML version，并写入 skill_type."""
        try:
            meta["version"] = get_current_version(skill_dir)
        except SkillArchiveError:
            # 列表接口不应因单个损坏索引失败；详情/版本列表再严格报错
            meta["version"] = None
        meta["skill_type"] = detect_skill_type(skill_dir)

    async def _prepare_evolve_rebuild_followup(
        self,
        store: Any,
        skill_name: str,
        *,
        user_intent: str | None = None,
        language: str = "cn",
        subject: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """复用 ``/evolve_rebuild`` 领域准备逻辑，生成 Agent follow-up prompt.

        与 ``evolution_slash._handle_evolve_rebuild`` 核心步骤一致（不含 slash
        参数校验 / 可见性校验）：``ExperienceRebuildService.prepare_rebuild_context``
        + ``build_rebuild_command_prompt``。
        """
        from openjiuwen.agent_evolving.experience.rebuild import ExperienceRebuildService
        from openjiuwen.harness.rails.evolution.commands import build_rebuild_command_prompt

        skill_name = str(skill_name or "").strip()
        if not skill_name:
            return {
                "result_type": "error",
                "output": "请指定 Skill 名称",
            }

        resolved_subject = subject or self._evolution_subject_for_skill(
            skill_name, Path(store.base_dir) / skill_name
        )
        rebuild_service = ExperienceRebuildService(store=store)
        try:
            rebuild_context = await rebuild_service.prepare_rebuild_context(
                resolved_subject,
                user_intent=user_intent,
            )
        except Exception as exc:
            logger.warning("[SkillManager] skills.rebuild prepare failed: %s", exc)
            return {"result_type": "error", "output": f"重建失败：{exc}"}

        if rebuild_context is None:
            return {
                "result_type": "error",
                "output": f"Skill '{skill_name}' 未生成可执行的重建指令。",
            }

        archive_error = rebuild_context.get("archive_error")
        if archive_error is not None:
            return {
                "result_type": "error",
                "output": (
                    f"重建失败：无法归档 Skill '{skill_name}' 的旧版本：{archive_error}"
                ),
            }
        if not rebuild_context.get("archive_pair"):
            return {
                "result_type": "error",
                "output": f"重建失败：无法归档 Skill '{skill_name}' 的旧版本。",
            }

        prompt = build_rebuild_command_prompt(
            subject=resolved_subject,
            user_intent=user_intent,
            rebuild_context=rebuild_context,
            language=language,
        )
        return {
            "result_type": "followup",
            "action": "run_rebuild_followup",
            "followup_prompt": prompt,
            "skill_name": skill_name,
        }

    async def _rebuild_skill_with_evolutions(
        self,
        name: str,
        skill_dir: Path,
        version: str | None,
        *,
        user_intent: str | None = None,
        language: str = "cn",
    ) -> dict[str, Any]:
        """与 ``/evolve_rebuild`` 相同：领域准备 + 拼 Agent follow-up prompt.

        版本约束：
        - ``version=null``：在无版本 workspace 上执行；
        - ``version`` 非空：在临时目录对版本副本 prepare，再写回版本副本；
          默认版本同步到 workspace，供 Agent 按 skill 名改写；非默认版本由上层
          在 Agent 运行前后做 workspace 临时切换。
        """
        from openjiuwen.agent_evolving.checkpointing.evolution_store import EvolutionStore

        evo_path = skill_dir / _EVOLUTION_FILENAME
        if not evo_path.is_file():
            raise SkillRpcError("SKILL_REBUILD_FAILED", "无 evolutions.json，无法 rebuild")

        try:
            evo_raw = json.loads(evo_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SkillRpcError("SKILL_REBUILD_FAILED", f"evolutions.json 无效: {exc}") from exc
        entries = evo_raw.get("entries") if isinstance(evo_raw, dict) else None
        if not isinstance(entries, list) or not entries:
            raise SkillRpcError("SKILL_REBUILD_FAILED", "没有可应用的待固化经验")

        default_version = get_current_version(skill_dir)
        subject = self._evolution_subject_for_skill(name, skill_dir)

        if version is None:
            if default_version is not None:
                raise SkillRpcError(
                    "SKILL_REBUILD_FAILED",
                    "该 Skill 已有产品版本，请传入 version；本地无版本时才传 null",
                )
            store = EvolutionStore(str(self._skills_dir))
            prepare_result = await self._prepare_evolve_rebuild_followup(
                store,
                name,
                user_intent=user_intent,
                language=language,
                subject=subject,
            )
            payload = self._rebuild_followup_to_payload(
                prepare_result,
                version=None,
                is_default=False,
                skill_dir=skill_dir,
                content_root=None,
                swap_workspace=False,
            )
            # 经验已消费：删除 evolutions，使 has_evolutions=false，详情不再展示经验入口
            _clear_evolutions_file(skill_dir)
            return payload

        content_root = resolve_version_content_root(skill_dir, version)
        is_default = default_version is not None and version == default_version

        with tempfile.TemporaryDirectory(prefix="skill-rebuild-") as tmp:
            tmp_root = Path(tmp)
            work_copy = tmp_root / "content"
            shutil.copytree(
                content_root,
                work_copy,
                ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
                dirs_exist_ok=False,
            )
            shutil.copy2(evo_path, work_copy / _EVOLUTION_FILENAME)

            staging_skills = tmp_root / "skills"
            staging_skills.mkdir(parents=True, exist_ok=True)
            staged = staging_skills / name
            if staged.exists():
                _safe_rmtree(staged)
            shutil.move(str(work_copy), str(staged))

            staging_store = EvolutionStore(str(staging_skills))
            prepare_result = await self._prepare_evolve_rebuild_followup(
                staging_store,
                name,
                user_intent=user_intent,
                language=language,
                subject=subject,
            )
            if prepare_result.get("result_type") == "error":
                return self._rebuild_followup_to_payload(
                    prepare_result,
                    version=version,
                    is_default=is_default,
                    skill_dir=skill_dir,
                    content_root=content_root,
                    swap_workspace=False,
                )

            # prepare 后 mid-state 可能仍留空 evolutions.json；写回前去掉，避免 has_evolutions 误判
            _clear_evolutions_file(staged)
            self.atomic_replace_dir(content_root, staged)

            if is_default:
                self.sync_workspace_from_version_content(skill_dir, content_root)

        touch_version_metadata(skill_dir, version)
        # workspace 侧经验已用于本次 rebuild，统一清除，列表/详情 has_evolutions=false
        _clear_evolutions_file(skill_dir)
        _clear_evolutions_file(content_root)

        return self._rebuild_followup_to_payload(
            prepare_result,
            version=version,
            is_default=is_default,
            skill_dir=skill_dir,
            content_root=content_root,
            swap_workspace=bool(version) and not is_default,
        )

    @staticmethod
    def _rebuild_followup_to_payload(
        prepare_result: dict[str, Any],
        *,
        version: str | None,
        is_default: bool,
        skill_dir: Path,
        content_root: Path | None,
        swap_workspace: bool,
    ) -> dict[str, Any]:
        """把 rebuild 领域准备结果转为 skills.rebuild 载荷."""
        if prepare_result.get("result_type") == "error":
            detail = str(prepare_result.get("output") or "evolve_rebuild 失败")
            raise SkillRpcError("SKILL_REBUILD_FAILED", detail)

        if prepare_result.get("result_type") != "followup":
            raise SkillRpcError("SKILL_REBUILD_FAILED", "evolve_rebuild 未生成 follow-up")

        prompt = str(prepare_result.get("followup_prompt") or "").strip()
        if not prompt:
            raise SkillRpcError("SKILL_REBUILD_FAILED", "evolve_rebuild follow-up prompt 为空")

        return {
            "success": True,
            "result_type": "followup",
            "action": prepare_result.get("action") or "run_rebuild_followup",
            "followup_prompt": prompt,
            "skill_name": prepare_result.get("skill_name"),
            "rebuild_target": {
                "version": version,
                "is_default": is_default,
                "skill_dir": str(skill_dir),
                "content_root": str(content_root) if content_root is not None else None,
                "swap_workspace": swap_workspace,
            },
        }

    @staticmethod
    def _evolution_subject_for_skill(name: str, skill_dir: Path) -> dict[str, str]:
        """构造 evolve_rebuild 所需的 subject 信封."""
        skill_type = detect_skill_type(skill_dir)
        kind = "swarm-skill" if skill_type == SKILL_TYPE_SWARM else "skill"
        return {"kind": kind, "name": name}

    def copy_workspace_business_to_version(self, skill_dir: Path, content_root: Path) -> None:
        """把 workspace 业务内容覆盖到版本副本."""
        content_root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="skill-ver-sync-", dir=str(content_root.parent)) as tmp:
            staged = Path(tmp) / "content"
            shutil.copytree(
                skill_dir,
                staged,
                ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
            )
            self.atomic_replace_dir(content_root, staged)

    @staticmethod
    def atomic_replace_dir(dest: Path, src: Path) -> None:
        """用 src 目录内容替换 dest；优先同父目录 rename，失败则 copy+清理."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = dest.with_name(dest.name + ".bak_rebuild")
        staged = dest.with_name(dest.name + ".new_rebuild")
        if backup.exists():
            _safe_rmtree(backup)
        if staged.exists():
            _safe_rmtree(staged)

        # 先落到 dest 同父目录，避免跨盘 rename 失败
        try:
            src.rename(staged)
        except OSError:
            shutil.copytree(src, staged)
            _safe_rmtree(src)

        replaced = False
        try:
            if dest.exists():
                dest.rename(backup)
                replaced = True
            staged.rename(dest)
            if backup.exists():
                _safe_rmtree(backup)
        except Exception:
            if replaced and backup.exists() and not dest.exists():
                backup.rename(dest)
            if staged.exists():
                _safe_rmtree(staged)
            raise

    @staticmethod
    def sync_workspace_from_version_content(skill_dir: Path, content_root: Path) -> None:
        """将版本副本业务内容同步到 workspace，保留根级 ``.archive``."""
        archive_dir = skill_dir / ARCHIVE_DIRNAME
        archive_backup: Path | None = None
        with tempfile.TemporaryDirectory(prefix="skill-ws-sync-", dir=str(skill_dir.parent)) as tmp:
            tmp_path = Path(tmp)
            staged = tmp_path / "workspace"
            shutil.copytree(
                content_root,
                staged,
                ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
            )
            if archive_dir.exists():
                archive_backup = tmp_path / ARCHIVE_DIRNAME
                shutil.move(str(archive_dir), str(archive_backup))

            # 清空 workspace 非 archive 内容
            for child in list(skill_dir.iterdir()):
                if child.name == ARCHIVE_DIRNAME:
                    continue
                if child.is_dir() and not child.is_symlink():
                    _safe_rmtree(child)
                else:
                    child.unlink(missing_ok=True)

            for child in staged.iterdir():
                target = skill_dir / child.name
                shutil.move(str(child), str(target))

            if archive_backup is not None and archive_backup.exists():
                if archive_dir.exists():
                    _safe_rmtree(archive_dir)
                shutil.move(str(archive_backup), str(archive_dir))

    def _locate_skill_for_get(self, name: str) -> tuple[Path | None, dict | None]:
        """定位 skills.get 所需的 workspace/marketplace 目录及展示元数据."""
        # 本地 skills 目录
        if self._skills_dir.exists():
            for child in self._skills_dir.iterdir():
                if child.name.startswith("_") or not child.is_dir():
                    continue
                md = self._try_find_skill_file(child)
                if md is None:
                    continue
                meta = self._parse_skill_md(md)
                if meta is None:
                    continue
                if meta.get("name") == md.stem:
                    meta["name"] = child.name
                if meta.get("name") != name:
                    continue
                base = {
                    "name": meta.get("name", name),
                    "source": self._resolve_skill_source(meta.get("name", "")),
                    "display_name": self._resolve_skill_display_name(meta.get("name", "")),
                    "is_builtin": self._is_builtin_skill(
                        meta.get("name", ""), self._get_installed_plugins(), child
                    ),
                    "is_builtin_source": False,
                }
                builtin_dir = get_builtin_skills_dir()
                if builtin_dir.exists():
                    builtin_skill_path = builtin_dir / child.name
                    base["is_builtin_source"] = (
                        builtin_skill_path.exists() and builtin_skill_path.is_dir()
                    )
                return child, base

        # marketplace 未安装副本（只支持读 workspace 语义，无本地产品版本）
        if self._marketplace_dir.exists():
            for repo_dir in self._marketplace_dir.iterdir():
                if not repo_dir.is_dir():
                    continue
                skills_dir = repo_dir / "skills"
                scan_roots = [skills_dir] if skills_dir.is_dir() else [repo_dir]
                for scan_root in scan_roots:
                    for plugin_dir in scan_root.iterdir():
                        if not plugin_dir.is_dir() or plugin_dir.name.startswith((".", "_")):
                            continue
                        md = self._try_find_skill_file(plugin_dir)
                        if md is None:
                            continue
                        meta = self._parse_skill_md(md)
                        if meta is None:
                            continue
                        if meta.get("name") == md.stem:
                            meta["name"] = plugin_dir.name
                        if meta.get("name") != name:
                            continue
                        return plugin_dir, {
                            "name": meta.get("name", name),
                            "source": repo_dir.name,
                            "marketplace": repo_dir.name,
                            "display_name": meta.get("name", name),
                            "is_builtin": False,
                            "is_builtin_source": False,
                        }
        return None, None

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
                self.apply_archive_version_and_type(meta, plugin_dir)
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
        """Generate agent/workspace/agent-data.json from agent tree."""
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

    @staticmethod
    def _resolve_teamskills_hub_auth_with_env(params: dict[str, Any]) -> dict[str, str]:
        """推荐等需鉴权接口：params 优先；否则回落环境变量（优先 system token）。"""
        token = str(params.get("token") or "").strip()
        system_token = str(params.get("system_token") or "").strip()
        if token or system_token:
            return SkillManager._resolve_teamskills_hub_auth(params)

        env_system = str(os.getenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN") or "").strip()
        if env_system:
            return {"system_token": env_system}
        env_user = str(os.getenv("TEAM_SKILLS_HUB_USER_TOKEN") or "").strip()
        if env_user:
            return {"token": env_user}
        return {
            "error": (
                "未配置 Team Skills Hub 鉴权：请提供 token 或 system_token，"
                "或设置 TEAM_SKILLS_HUB_SYSTEM_TOKEN / TEAM_SKILLS_HUB_USER_TOKEN"
            )
        }

    @staticmethod
    def _resolve_teamskills_hub_server_auth() -> dict[str, str]:
        """从后端环境读取 Team Skills Hub 认证；不接受前端覆盖."""
        system_token = (os.getenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN") or "").strip()
        if system_token:
            return {"system_token": system_token}
        user_token = (os.getenv("TEAM_SKILLS_HUB_USER_TOKEN") or "").strip()
        if user_token:
            return {"token": user_token}
        return {}

    @staticmethod
    def _teamskills_hub_auth_headers(
        *,
        token: str | None = None,
        system_token: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        system = str(system_token or "").strip()
        user = str(token or "").strip()
        if system:
            headers["X-System-Token"] = system
        elif user:
            headers["Authorization"] = f"Bearer {user}"
        return headers

    @staticmethod
    def _skillhub_nullable_str(raw: Any) -> str | None:
        if raw is None:
            return None
        return str(raw)

    @staticmethod
    def _skillhub_required_str(raw: Any) -> str:
        if raw is None:
            return ""
        return str(raw)

    @staticmethod
    def _skillhub_count(raw: Any) -> int:
        if raw is None or raw == "":
            return 0
        try:
            return int(raw)
        except Exception:
            return 0

    @staticmethod
    def _skillhub_str_list(raw: Any) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]

    @staticmethod
    def _skillhub_review_summary(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, dict):
            return raw
        return None

    @staticmethod
    def _skillhub_review_sections(raw: Any) -> list[Any]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        return []

    @staticmethod
    def _skillhub_average_rating(raw: Any) -> float | int | None:
        if raw is None or raw == "":
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return raw
        try:
            return float(raw)
        except Exception:
            return None

    @classmethod
    def _merge_skillhub_public_detail(
        cls,
        plugin_item: dict[str, Any],
        version_detail: dict[str, Any],
    ) -> dict[str, Any]:
        """按设计白名单合并资产列表字段与版本详情字段，禁止整体透传."""
        return {
            "asset_id": cls._skillhub_required_str(version_detail.get("asset_id")),
            "version": cls._skillhub_required_str(version_detail.get("version")),
            "asset_type": cls._skillhub_nullable_str(version_detail.get("asset_type")),
            "plugin_type": cls._skillhub_nullable_str(version_detail.get("plugin_type")),
            "name": cls._skillhub_required_str(version_detail.get("name")),
            "display_name": cls._skillhub_required_str(version_detail.get("display_name")),
            "short_desc": cls._skillhub_required_str(version_detail.get("short_desc")),
            "detail_desc": cls._skillhub_required_str(version_detail.get("detail_desc")),
            "icon_uri": cls._skillhub_nullable_str(version_detail.get("icon_uri")),
            "publisher_id": cls._skillhub_nullable_str(version_detail.get("publisher_id")),
            "publisher_name": cls._skillhub_nullable_str(version_detail.get("publisher_name")),
            "tags": cls._skillhub_str_list(version_detail.get("tags")),
            "category_id": cls._skillhub_nullable_str(version_detail.get("category_id")),
            "category_name": cls._skillhub_nullable_str(version_detail.get("category_name")),
            "certification": cls._skillhub_nullable_str(version_detail.get("certification")),
            "changelog": cls._skillhub_nullable_str(version_detail.get("changelog")),
            "install_count": cls._skillhub_count(version_detail.get("install_count")),
            "view_count": cls._skillhub_count(version_detail.get("view_count")),
            # 交互计数与创建时间来自资产列表接口
            "like_count": cls._skillhub_count(plugin_item.get("like_count")),
            "star_count": cls._skillhub_count(plugin_item.get("star_count")),
            "review_count": cls._skillhub_count(plugin_item.get("review_count")),
            "average_rating": cls._skillhub_average_rating(plugin_item.get("average_rating")),
            "create_time": plugin_item.get("create_time")
            if plugin_item.get("create_time") is not None
            else None,
            "update_time": version_detail.get("update_time")
            if version_detail.get("update_time") is not None
            else None,
            "review_summary": cls._skillhub_review_summary(version_detail.get("review_summary")),
            "review_sections": cls._skillhub_review_sections(version_detail.get("review_sections")),
        }

    async def _team_skills_hub_get_public_plugin_item(
        self,
        asset_id: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        system_token: str | None = None,
    ) -> dict[str, Any]:
        """按 asset_id 精确查询资产列表项，用于解析 public_latest_version."""
        data = await self._team_skills_hub_http_get_data(
            "/api/v1/plugins",
            params={"asset_id": asset_id, "page": 1, "page_size": 1},
            timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
            base_url=base_url,
            token=token,
            system_token=system_token,
        )
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SkillRpcError(ERROR_SKILLHUB_DETAIL_FAILED, "SkillHub 资产列表响应格式错误")
        matched: dict[str, Any] | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset_id") or "").strip() == asset_id:
                matched = item
                break
        if matched is None:
            raise SkillRpcError(ERROR_SKILLHUB_DETAIL_NOT_FOUND, "SkillHub 资产不存在")
        return matched

    async def _team_skills_hub_get_version_detail(
        self,
        asset_id: str,
        version: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        system_token: str | None = None,
    ) -> dict[str, Any]:
        """查询指定公开版本详情（原生 /plugins/{asset_id}/versions/{version}）."""
        path = (
            f"/api/v1/plugins/{quote(asset_id, safe='')}"
            f"/versions/{quote(version, safe='')}"
        )
        data = await self._team_skills_hub_http_get_data(
            path,
            timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
            base_url=base_url,
            token=token,
            system_token=system_token,
            not_found_code=ERROR_SKILLHUB_DETAIL_NOT_FOUND,
        )
        if not isinstance(data, dict):
            raise SkillRpcError(ERROR_SKILLHUB_DETAIL_FAILED, "SkillHub 版本详情响应格式错误")
        return data

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
                if self._is_under_skill_archive(skill_dir, child):
                    continue
                rel = child.relative_to(skill_dir).as_posix()
                zf.write(child, arcname=f"{skill_name}/{skill_name}/{rel}")
        return zip_path

    def _find_skill_dir_by_installed_asset_id(self, asset_id: str) -> Path | None:
        """按 ``installed_asset_id`` 查找本地 Skill 目录."""
        target = str(asset_id or "").strip()
        if not target or not self._skills_dir.is_dir():
            return None
        for child in self._skills_dir.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if get_installed_asset_id(child) == target:
                return child
        return None

    @staticmethod
    def _install_teamskills_hub_first_copy(
        *,
        skill_dir: Path,
        dest: Path,
        asset_id: str,
        product_version: str,
    ) -> None:
        """首次安装：workspace 工作副本 + ``.archive`` 完整版本副本."""
        storage_id = f"ver-{uuid.uuid4().hex[:12]}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 先落工作副本（不含包内根级 .archive，校验阶段已拒绝）
        shutil.copytree(
            skill_dir,
            dest,
            ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
        )
        content_root = version_content_dir(dest, storage_id)
        content_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            skill_dir,
            content_root,
            ignore=shutil.ignore_patterns(ARCHIVE_DIRNAME),
        )
        checksum = compute_content_checksum(content_root)
        write_skillhub_first_install_index(
            dest,
            version=product_version,
            asset_id=asset_id,
            storage_id=storage_id,
            checksum_sha256=checksum,
        )

    def _resolve_teamskills_publish_local_skill_dir(
        self,
        *,
        path_raw: str,
        file_raw: str,
    ) -> Path | None:
        """解析发布请求对应的本地 Skill 根目录（用于读写发布元数据）."""
        if path_raw:
            root = Path(path_raw).expanduser().resolve()
            if root.is_dir():
                located = self._locate_skill_dir(root)
                return located or root
            return None
        # 仅 file 时无法可靠定位本地 workspace；发布成功后再按 name 回落
        _ = file_raw
        return None

    @staticmethod
    def _is_under_skill_archive(skill_root: Path, path: Path) -> bool:
        """路径是否位于 Skill 根级 ``.archive/`` 下."""
        try:
            rel = path.resolve().relative_to(skill_root.resolve())
        except ValueError:
            return False
        return bool(rel.parts) and rel.parts[0] == ARCHIVE_DIRNAME

    @staticmethod
    def _normalize_teamskills_hub_updated_at(raw: Any) -> str | None:
        """将 Hub 的 update_time 规范为 ISO 字符串或 null."""
        if raw is None or raw == "":
            return None
        if isinstance(raw, (int, float)):
            ts = int(raw)
            if ts <= 0:
                return None
            # Hub 可能返回秒或毫秒
            if ts > 10_000_000_000:
                ts = ts // 1000
            return (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        text = str(raw).strip()
        return text or None

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
    def _get_import_local_forbidden_roots() -> list[Path]:
        """本地导入源的基础黑名单（高危根目录）。

        黑名单只作兜底，主防线是 :meth:`_validate_local_skill_source` 的结构校验，
        符号链接拒绝（:meth:`_assert_import_local_source_safe`）是黑名单的主要补强。
        按运行平台计算：Unix-like 系统根、Windows 系统根、家目录敏感子目录。
        运维可通过 ``IMPORT_LOCAL_FORBIDDEN_DIRS`` env 追加（只收紧不放宽）。
        """
        roots: list[Path] = []
        if os.name == "nt":
            # Windows：不黑名单整盘根或 Users 根，避免锁死用户目录下的本地导入；
            # 单独禁止系统目录与 Users 下的 Default / Public 公共配置。
            system_drive = Path(os.environ.get("SystemDrive", "C:") + os.sep)
            for name in ("Windows", "Program Files", "Program Files (x86)", "ProgramData"):
                roots.append(system_drive / name)
            users_root = system_drive / "Users"
            roots.extend((users_root / "Default", users_root / "Public"))
        else:
            for p in (
                "/etc",
                "/proc",
                "/sys",
                "/dev",
                "/boot",
                "/root",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/var",
                "/run",
                "/srv",
            ):
                roots.append(Path(p))
            if sys.platform == "darwin":
                roots.extend(Path(p) for p in ("/System", "/Library"))
        home = Path.home()
        for name in (
            ".ssh",
            ".aws",
            ".gnupg",
            ".config",
            ".git",
            ".gradle",
            ".m2",
            ".cache",
            ".local",
            ".pki",
            ".kube",
            ".docker",
        ):
            roots.append(home / name)
        raw = (os.getenv(_IMPORT_LOCAL_FORBIDDEN_DIRS_ENV) or "").strip()
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                extra = Path(token).expanduser()
                if extra.is_absolute():
                    roots.append(extra)
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning(
                    "[SkillManager] %s 忽略无效项 %r: %s",
                    _IMPORT_LOCAL_FORBIDDEN_DIRS_ENV,
                    token,
                    exc,
                )
        seen: set[str] = set()
        unique: list[Path] = []
        for root in roots:
            key = os.path.normcase(str(root))
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    @staticmethod
    def _path_is_link_or_junction(path: Path) -> bool:
        """判断路径是否为符号链接；Windows 上再补 junction/reparse point 判断。

        ``Path.is_junction`` 自 Python 3.12 才存在，3.11 兼容下回退为仅符号链接判断。
        """
        if path.is_symlink():
            return True
        if os.name == "nt":
            is_junction = getattr(path, "is_junction", None)
            if is_junction is not None:
                return bool(is_junction())
        return False

    @staticmethod
    def _path_is_within_root(path: Path, root: Path) -> bool:
        """按文件系统对象身份判断 path 是否位于已存在的 root 下。"""
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            return False
        for candidate in (path, *path.parents):
            try:
                if candidate.samefile(resolved_root):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _assert_import_local_source_safe(self, raw_path: str) -> None:
        """拒绝符号链接、URL 协议与落在基础黑名单根下的本地导入源。

        必须在任何 ``exists()`` / 读取之前调用，否则读取本身即构成任意文件读取。
        ``~/...`` 按 JiuwenClaw 服务进程用户的 home 展开后再校验。
        绝对路径判定按服务端平台进行：Unix-like 拒绝非绝对与 Windows 风格路径；
        Windows 拒绝非绝对、drive-relative（``C:foo``）与无盘符 root-relative
        （``\\Windows\\...``），接受 ``C:\\...``、``D:\\...`` 与 UNC。
        """
        raw = str(raw_path or "").strip()
        if not raw:
            raise ValueError("缺少参数: path")
        if "://" in raw.lower():
            raise ValueError("仅支持本地文件路径，不支持 URL 协议")
        if "\0" in raw:
            raise ValueError("path 包含非法字符")
        expanded = Path(raw).expanduser()
        if os.name == "nt":
            if not expanded.is_absolute():
                raise ValueError("path 仅支持绝对路径")
            if not PureWindowsPath(raw).drive:
                raise ValueError("path 必须包含盘符或 UNC 根")
        else:
            if not expanded.is_absolute():
                raise ValueError("path 仅支持绝对路径")
            if PureWindowsPath(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw):
                raise ValueError("path 不支持 Windows 风格路径")
        if self._path_is_link_or_junction(expanded):
            raise ValueError(f"path 不支持符号链接: {raw}")
        try:
            resolved = expanded.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(f"path 无效: {exc}") from exc
        for root in self._get_import_local_forbidden_roots():
            if self._path_is_within_root(resolved, root):
                raise ValueError(f"path 位于禁止导入的目录: {raw}")
        if resolved.is_dir():
            for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
                for name in dirnames + filenames:
                    if self._path_is_link_or_junction(Path(dirpath) / name):
                        raise ValueError(f"path 目录内不允许包含符号链接: {raw}")

    @staticmethod
    def _validate_local_skill_source(src: Path) -> str:
        """对本地导入源做结构校验，返回校验通过的技能名。

        主防线：目录必须含精确 ``SKILL.md``（单文件则文件本身必须是合法
        ``SKILL.md``），且文件开头（仅允许前置空行）必须是含 ``name`` 与
        ``description`` 的 YAML frontmatter；``name`` 再通过
        :func:`_validate_skill_name` 校验。失败抛 ``ValueError``。
        """
        if src.is_dir():
            skill_file = src / "SKILL.md"
            if not skill_file.is_file():
                raise ValueError(f"目录中未找到 SKILL.md: {src}")
        elif src.is_file():
            skill_file = src
        else:
            raise ValueError(f"不支持的路径类型: {src}")
        try:
            text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"无法读取 SKILL.md: {skill_file}") from exc
        match = _SKILL_FRONTMATTER_RE.match(text)
        if not match:
            raise ValueError(f"SKILL.md 开头必须是 --- frontmatter（仅允许前置空行）: {skill_file}")
        try:
            loaded = yaml.safe_load(match.group(1))
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValueError(f"SKILL.md frontmatter YAML 无效: {skill_file}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"SKILL.md frontmatter 必须是 YAML 对象: {skill_file}")
        raw_name = loaded.get("name")
        if not raw_name:
            raise ValueError(f"SKILL.md frontmatter 缺少 name: {skill_file}")
        description = loaded.get("description")
        if not description or not str(description).strip():
            raise ValueError(f"SKILL.md frontmatter 缺少 description: {skill_file}")
        try:
            skill_name = _validate_skill_name(str(raw_name))
        except ValueError as exc:
            raise ValueError(f"invalid skill name: {exc}") from exc
        return skill_name

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

    async def _team_skills_hub_http_get_data(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = _TEAM_SKILLS_HUB_MARKET_TIMEOUT,
        base_url: str | None = None,
        token: str | None = None,
        system_token: str | None = None,
        not_found_code: str | None = None,
    ) -> Any:
        base_url = (base_url or self._get_team_skills_hub_base_url()).rstrip("/")
        rel_path = path if path.startswith("/") else f"/{path}"
        req_url = f"{base_url}{rel_path}"
        headers = self._teamskills_hub_auth_headers(token=token, system_token=system_token)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.get(req_url, params=params, headers=headers or None)
        except Exception as exc:
            raise RuntimeError(f"无法连接 Team Skills Hub: {exc}") from exc

        if resp.status_code == 404:
            if not_found_code:
                raise SkillRpcError(not_found_code, "SkillHub 资源不存在")
            raise RuntimeError("Team Skills Hub API 错误 HTTP 404")

        if not resp.is_success:
            # 不向调用方透传上游响应体，避免泄露敏感信息。
            raise RuntimeError(f"Team Skills Hub API 错误 HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Team Skills Hub API 响应不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Team Skills Hub API 响应格式错误")

        code = payload.get("code", 200)
        try:
            code_int = int(code)
        except Exception as exc:
            raise RuntimeError("Team Skills Hub API 响应 code 格式错误") from exc
        if code_int != 200:
            raise RuntimeError("Team Skills Hub API 返回失败")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Team Skills Hub API 响应 data 格式错误")
        return data

    async def _team_skills_hub_http_post_data(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float = _TEAM_SKILLS_HUB_MARKET_TIMEOUT,
        base_url: str | None = None,
    ) -> Any:
        base_url = (base_url or self._get_team_skills_hub_base_url()).rstrip("/")
        rel_path = path if path.startswith("/") else f"/{path}"
        req_url = f"{base_url}{rel_path}"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.post(req_url, json=json_body, headers=headers or {})
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
    def _normalize_hub_plugin_type(raw: str) -> str:
        """Align with SkillHub: teamskills is an alias of swarmskill."""
        normalized = (raw or "").strip().lower()
        return "swarmskill" if normalized == "teamskills" else normalized

    @classmethod
    def _parse_hub_plugin_types(cls, raw: str) -> list[str]:
        """Parse plugin_type / skill_type (comma-separated) into normalized list."""
        out: list[str] = []
        for part in str(raw or "").split(","):
            normalized = cls._normalize_hub_plugin_type(part)
            if normalized and normalized not in out:
                out.append(normalized)
        return out

    async def _enrich_swarmskills_recommend_skills(
        self,
        skills: list[dict[str, Any]],
        *,
        base_url: str,
    ) -> list[dict[str, Any]]:
        """用 /api/v1/plugins?asset_id= 补齐推荐结果的展示字段。

        plugins 列表默认排除 OFFLINE：查不到则视为下架/不可见并丢掉。
        HTTP 失败时保留原项，避免瞬时故障把整页推荐滤空。
        """

        async def _one(item: dict[str, Any]) -> dict[str, Any] | None:
            asset_id = str(item.get("asset_id") or "").strip()
            if not asset_id:
                return None
            try:
                data = await self._team_skills_hub_http_get_data(
                    "/api/v1/plugins",
                    params={"asset_id": asset_id, "page": 1, "page_size": 1},
                    timeout=_TEAM_SKILLS_HUB_MARKET_TIMEOUT,
                    base_url=base_url,
                )
                rows = data.get("items", []) if isinstance(data, dict) else []
                row = rows[0] if rows and isinstance(rows[0], dict) else None
                if not row:
                    logger.info("推荐结果不可见，已过滤 asset_id=%s", asset_id)
                    return None
                name = str(row.get("name", "")).strip() or asset_id
                plugin_type = self._normalize_hub_plugin_type(str(row.get("plugin_type") or ""))
                return {
                    **item,
                    "name": name,
                    "display_name": str(row.get("display_name", "")).strip() or name,
                    "summary": str(row.get("short_desc", "")).strip(),
                    "version": str(row.get("latest_version", "")).strip(),
                    "updated_at": int(row.get("update_time") or 0),
                    "plugin_type": plugin_type,
                    "tags": self._coerce_str_list(row.get("tags")),
                }
            except Exception as exc:
                logger.warning("推荐结果补齐失败 asset_id=%s: %s", asset_id, exc)
                return item

        enriched = await asyncio.gather(*[_one(s) for s in skills])
        return [item for item in enriched if item is not None]

    @staticmethod
    def _safe_extract_zip_members_into(zf: zipfile.ZipFile, dest_root: Path) -> None:
        """将已打开的 ZIP 成员解压到 dest_root（须为 resolve() 后的目录），拒绝 Zip Slip。

        同时限制解包总大小、文件数量、单文件大小和目录深度。
        """
        file_count = 0
        total_uncompressed = 0
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
            if len(rel.parts) > _SKILL_ZIP_MAX_PATH_DEPTH:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"ZIP 目录深度超过限制（{_SKILL_ZIP_MAX_PATH_DEPTH}）",
                )
            dest_path = dest_root.joinpath(*rel.parts)
            try:
                dest_path = dest_path.resolve()
                dest_path.relative_to(dest_root)
            except ValueError as exc:
                raise RuntimeError("ZIP 路径越界") from exc
            if is_dir:
                dest_path.mkdir(parents=True, exist_ok=True)
                continue
            file_count += 1
            if file_count > _SKILL_ZIP_MAX_FILE_COUNT:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"ZIP 文件数量超过限制（{_SKILL_ZIP_MAX_FILE_COUNT}）",
                )
            declared = int(getattr(info, "file_size", 0) or 0)
            if declared > _SKILL_ZIP_MAX_SINGLE_FILE_BYTES:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"ZIP 单文件超过限制（{_SKILL_ZIP_MAX_SINGLE_FILE_BYTES} 字节）",
                )
            if total_uncompressed + max(declared, 0) > _SKILL_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"ZIP 解包总大小超过限制（{_SKILL_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES} 字节）",
                )
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src:
                data = src.read(_SKILL_ZIP_MAX_SINGLE_FILE_BYTES + 1)
            if len(data) > _SKILL_ZIP_MAX_SINGLE_FILE_BYTES:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"ZIP 单文件超过限制（{_SKILL_ZIP_MAX_SINGLE_FILE_BYTES} 字节）",
                )
            total_uncompressed += len(data)
            if total_uncompressed > _SKILL_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise SkillRpcError(
                    ERROR_SKILL_FILE_TOO_LARGE,
                    f"ZIP 解包总大小超过限制（{_SKILL_ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES} 字节）",
                )
            dest_path.write_bytes(data)

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

        expected = checksum_sha256.strip().lower()
        if expected:
            digest = hashlib.sha256(body).hexdigest().lower()
            if digest != expected:
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
    ) -> bytes:
        timeout = max(30.0, timeout or _TEAM_SKILLS_HUB_MARKET_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
            body = resp.content or b""

        if not body:
            raise RuntimeError("下载内容为空")
        if len(body) < 4 or not body.startswith(b"PK"):
            raise RuntimeError("下载内容不是 ZIP 文件")

        expected = checksum_sha256.strip().lower()
        if expected:
            digest = hashlib.sha256(body).hexdigest().lower()
            if digest != expected:
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
        return (os.getenv("GITHUB_TOKEN") or "").strip()

    @staticmethod
    def _skillnet_eval_llm_params() -> dict[str, str | None]:
        """与主对话一致的 API Key / Base URL / 模型名（config.yaml react 段）."""
        try:
            from jiuwenswarm.common.config import get_config
        except Exception:
            return {
                "api_key": (os.getenv("API_KEY") or "").strip() or None,
                "base_url": (os.getenv("API_BASE") or "").strip() or None,
                "model": (os.getenv("MODEL_NAME") or "gpt-4o").strip(),
            }

        cfg = get_config() or {}
        react = cfg.get("react") or {}
        mcc = react.get("model_client_config") or {}
        api_key = (mcc.get("api_key") or os.getenv("API_KEY") or "").strip()
        base_url = (mcc.get("api_base") or os.getenv("API_BASE") or "").strip()
        model = (react.get("model_name") or os.getenv("MODEL_NAME") or "gpt-4o").strip()
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
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                url,
                str(dest),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error("git clone 失败: %s", stderr.decode(errors="replace"))
                return None
            return await self._git_get_commit(dest)
        except Exception as exc:
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
            _, stderr = await proc.communicate()
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

    def _load_state(self) -> dict[str, Any]:
        """加载 skills_state.json，失败时返回默认空状态."""
        try:
            if self._state_file.exists():
                state = json.loads(self._state_file.read_text(encoding="utf-8"))
                plugins_before = state.get("installed_plugins")
                had_plugin_version = isinstance(plugins_before, list) and any(
                    isinstance(p, dict) and "version" in p for p in plugins_before
                )
                self._normalize_state(state)
                if had_plugin_version:
                    # 立即落盘，避免旧 version 字段残留
                    try:
                        self._state_file.parent.mkdir(parents=True, exist_ok=True)
                        self._state_file.write_text(
                            json.dumps(state, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except Exception:
                        logger.warning("迁移移除 installed_plugins.version 后写回失败")
                return state
        except Exception:
            logger.warning("加载 skills_state.json 失败，使用默认空状态")
        default_state = {"marketplaces": [], "installed_plugins": [], "local_skills": []}
        self._normalize_state(default_state)
        return default_state

    def _save_state(self) -> None:
        """持久化状态到 skills_state.json."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.error("保存 skills_state.json 失败")

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
        # 产品版本迁出到 .archive/versions/index.json，迁移时删除旧字段
        plugins = state.get("installed_plugins")
        if isinstance(plugins, list):
            migrated = False
            for p in plugins:
                if isinstance(p, dict) and "version" in p:
                    p.pop("version", None)
                    migrated = True
            if migrated:
                state["installed_plugins"] = plugins
        state["local_skills"] = normalize_local_skills(
            state.get("local_skills"),
            self._collect_existing_local_skill_names(),
        )
        state["skill_configs"] = normalize_skill_configs(state.get("skill_configs"))

    def _get_installed_plugins(self) -> list[dict]:
        return self._state.get("installed_plugins", [])

    # MCP-bundled skills live under <workspace>/mcp/skills/<name>/<skill>/,
    # physically isolated from user skills. The scan list is derived from
    # state.json's connected records (see state_store.connected_mcp_skill_dirs)
    # — an MCP's skills surface only while it is connected.

    @staticmethod
    def _mcp_skills_dirs() -> list[dict[str, str]]:
        """MCP skills dirs to scan: derived from state.json's connected records.

        Returns ``[{"name", "dir"}]`` for each connected MCP whose
        ``mcp/skills/<name>/`` dir exists on disk.
        """
        try:
            from jiuwenswarm.server.runtime.mcp.state_store import (
                connected_mcp_skill_dirs,
            )
            return connected_mcp_skill_dirs()
        except Exception:  # noqa: BLE001
            return []

    # -----------------------------------------------------------------------
    # 供 AgentServer 内部其它组件复用的轻量公开查询接口
    # -----------------------------------------------------------------------

    def get_installed_plugins(self) -> list[dict]:
        """返回已安装插件记录的拷贝。"""
        return list(self._get_installed_plugins())

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
        """
        if not self._skills_dir.exists():
            return

        registered = get_registered_skill_names(self._state)
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
            if not name or name in registered:
                continue
            # 排除内置技能（用户目录下与 builtin 目录同名的文件夹），避免改变其来源/行为。
            if builtin_exists and (builtin_dir / child.name).is_dir():
                continue
            self._add_local_skill({"name": name, "origin": "local", "source": "local"})
            registered.add(name)
            changed = True

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

    def reload_state(self) -> None:
        """重新从 skills_state.json 加载状态。

        SkillManager 在 __init__ 时加载一次 _state，之后内存缓存。外部流程
        （如 MCP 连接器经独立 SkillManager 实例写 skill_configs）落盘后，本实例
        的 _state 仍是旧值——list_disabled_skills 看不到新 disabled 的 skill。
        refresh_skill_rails 调用本方法先重载再算 disabled_skills，确保磁盘最新值
        生效。
        """
        self._state = self._load_state()

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
        for i, p in enumerate(plugins):
            if p.get("name") == plugin.get("name"):
                plugins[i] = plugin
                self._save_state()
                return
        plugins.append(plugin)
        self._save_state()

    def _remove_installed_plugin(self, name: str) -> None:
        plugins = self._state.get("installed_plugins", [])
        self._state["installed_plugins"] = [p for p in plugins if p.get("name") != name]
        self._save_state()

    def _set_plugin_enabled(self, name: str, enabled: bool) -> bool:
        """设置插件的启用/禁用状态."""
        plugins = self._state.get("installed_plugins", [])
        updated = False
        for p in plugins:
            if p.get("name") == name:
                p["enabled"] = bool(enabled)
                updated = True
                break
        if not updated:
            return False
        self._save_state()
        return True

    @staticmethod
    def _normalize_plugin(p: dict) -> dict:
        """规范化插件记录，补全 enabled 字段；移除已废弃的 version 持久化字段."""
        p.setdefault("enabled", True)
        p.pop("version", None)
        return p

    def _add_local_skill(self, skill: dict) -> None:
        local = self._state.setdefault("local_skills", [])
        # 更新已有记录
        for i, s in enumerate(local):
            if s.get("name") == skill.get("name"):
                local[i] = skill
                self._save_state()
                return
        local.append(skill)
        self._save_state()

    def _remove_local_skill(self, name: str) -> None:
        local = self._state.get("local_skills", [])
        self._state["local_skills"] = [s for s in local if s.get("name") != name]
        self._save_state()

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
