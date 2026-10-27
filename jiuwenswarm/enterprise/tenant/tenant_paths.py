# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""多租户工作区路径与 tenant_key 工具.

``tenant_key = md5("{group_id}::{bot_id}::{user_id}")``，避免路径/日志直接暴露身份字段。
个人版路径门面仍在 ``jiuwenswarm.common.utils``；本模块承载纯多租户语义。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from jiuwenswarm.enterprise.edition import is_enterprise

logger = logging.getLogger(__name__)

_TENANT_KEY_SEP = "::"

__all__ = [
    "normalize_tenant_key_part",
    "build_tenant_key",
    "get_agent_workspace_relative_dir",
    "get_agent_root_relative_dir",
    "get_agent_sessions_relative_dir",
    "get_multi_tenant_user_workspace_dir",
    "seed_tenant_agent_workspace",
]


def normalize_tenant_key_part(value: str | None, *, default: str = "default") -> str:
    text = default if value is None else str(value).strip() or default
    return text


def build_tenant_key(
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Compose a stable opaque ``tenant_key``.

    先归一三元组并按 ``group_id::bot_id::user_id`` 拼接，再做 MD5 hex，
    用作池缓存键与企业版 ``workspace_{tenant_key}`` 目录名，避免明文落盘/打日志。
    """
    raw = _TENANT_KEY_SEP.join(
        (
            normalize_tenant_key_part(group_id),
            normalize_tenant_key_part(bot_id),
            normalize_tenant_key_part(user_id),
        )
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def get_agent_workspace_relative_dir() -> Path:
    """Relative agent workspace under a tenant root."""
    from jiuwenswarm.common.utils import get_agent_workspace_dirname

    return Path("agent") / get_agent_workspace_dirname()


def get_agent_root_relative_dir() -> Path:
    return Path("agent")


def get_agent_sessions_relative_dir() -> Path:
    return Path("agent") / "sessions"


def _require_tenant_key(tenant_key: str | None) -> str:
    text = str(tenant_key or "").strip()
    if not text:
        raise ValueError(f"tenant_key required: tenant_key={tenant_key!r}")
    if any(token in text for token in ("\x00", "/", "\\")) or text in {".", ".."}:
        raise ValueError(f"tenant_key must not contain path syntax: {text!r}")
    return text


def _effective_tenant_key(tenant_key: str | None = None) -> str:
    """Explicit tenant_key > bound TenantContext > personal default."""
    if tenant_key is not None:
        return _require_tenant_key(tenant_key)
    try:
        from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

        bound = TenantContext.current_tenant_key()
        if bound:
            return _require_tenant_key(bound)
    except ImportError:
        pass
    return build_tenant_key()


def get_multi_tenant_user_workspace_dir(tenant_key: str | None = None) -> Path:
    """Tenant workspace root.

    - Enterprise: ``~/.jiuwenswarm/workspace_{tenant_key}``
    - Personal: develop layout unchanged (``~/.jiuwenswarm``)
    """
    from jiuwenswarm.common.utils import get_user_workspace_dir

    if is_enterprise():
        tk = _effective_tenant_key(tenant_key)
        return get_user_workspace_dir() / f"workspace_{tk}"
    return get_user_workspace_dir()


def seed_tenant_agent_workspace(tenant_root: Path) -> None:
    """为租户工作区补种 DeepAgent 标准模板文件（幂等，已存在一律跳过）。

    ``prepare_workspace`` 只初始化默认工作区（个人版 ``~/.jiuwenswarm``），
    企业版各租户目录（``workspace_{tenant_key}``）由 ``TenantAgentPool`` 懒创建，
    此前从未播种 AGENT/SOUL/IDENTITY/HEARTBEAT/USER/MEMORY.md 等模板。
    首次进入某租户请求时调用本函数补齐，使 write_memory/read_memory 等记忆工具
    有完整的工作区骨架。
    """
    from jiuwenswarm.common.utils import (
        _find_package_root,
        _resolve_preferred_language,
        get_config_file,
    )

    workspace = Path(tenant_root) / get_agent_workspace_relative_dir()
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    package_root = _find_package_root()
    if package_root is None:
        logger.warning("seed_tenant_agent_workspace: package root not found, skip seeding")
        return
    template_workspace = package_root / "resources" / "agent" / "workspace"
    if not template_workspace.is_dir():
        logger.warning(
            "seed_tenant_agent_workspace: template missing: %s, skip seeding",
            template_workspace,
        )
        return

    resolved_lang = _resolve_preferred_language(get_config_file(), None)
    suffix = "_ZH" if resolved_lang == "zh" else "_EN"
    # (模板内相对路径, 落盘相对路径)，多语言文件去掉 _ZH/_EN 后缀
    entries: list[tuple[str, str]] = [
        (f"AGENT{suffix}.md", "AGENT.md"),
        (f"HEARTBEAT{suffix}.md", "HEARTBEAT.md"),
        (f"IDENTITY{suffix}.md", "IDENTITY.md"),
        (f"SOUL{suffix}.md", "SOUL.md"),
        ("USER.md", "USER.md"),
        (f"memory/MEMORY{suffix}.md", "memory/MEMORY.md"),
    ]
    added: list[str] = []
    for src_rel, dst_rel in entries:
        src = template_workspace / src_rel
        if not src.is_file():
            continue
        dst = workspace / dst_rel
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            added.append(dst_rel)
        except OSError as e:
            logger.warning("seed_tenant_agent_workspace: copy %s failed: %s", dst_rel, e)
    if added:
        logger.info("seed_tenant_agent_workspace: %s -> %s", ", ".join(added), workspace)
