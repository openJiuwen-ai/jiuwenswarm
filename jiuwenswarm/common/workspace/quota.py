# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作区配额：身份、策略缓存、状态计算与写盘门禁。"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jiuwenswarm.common.workspace.service import measure_used_bytes

logger = logging.getLogger(__name__)

WORKSPACE_QUOTA_EXCEEDED = "WORKSPACE_QUOTA_EXCEEDED"

DEFAULT_FALLBACK_LIMIT_BYTES = 10 * 1024**4
FALLBACK_POLICY_ID = "local_default"
FALLBACK_LIMIT_ENV = "AGENT_WORKSPACE_QUOTA_DEFAULT_LIMIT_BYTES"
USED_TTL_ENV = "AGENT_WORKSPACE_QUOTA_USED_TTL_SECONDS"
RECONCILE_INTERVAL_ENV = "AGENT_WORKSPACE_QUOTA_RECONCILE_INTERVAL_SECONDS"
FEATURE_ENABLED_ENV = "WORKSPACE_QUOTA_ENABLED"
# 同根两次校准最小间隔（近限同步 / 手动刷新不受此限）。
DEFAULT_USED_TTL_SECONDS = 5.0
# 后台定时全量 du 周期，默认 5 分钟。
DEFAULT_RECONCILE_INTERVAL_SECONDS = 300.0

# 策略 ``limit_bytes == -1`` 表示无限制；``0`` 表示零配额（满额阻断）。
UNLIMITED_LIMIT_BYTES = -1

QuotaStatus = Literal["ok", "warn", "block"]


def is_workspace_quota_enabled() -> bool:
    """用户空间配额特性开关；默认关闭。

    真值：``1/true/yes/on``；假值：``0/false/no/off/""``（未设置亦为假）。
    与 Manager 共用 ``WORKSPACE_QUOTA_ENABLED``（企业部署由同一 .env 注入）。
    """
    from jiuwenswarm.common.config import coerce_config_bool

    return coerce_config_bool(os.environ.get(FEATURE_ENABLED_ENV), False)


@dataclass(frozen=True)
class QuotaSnapshot:
    used_bytes: int
    limit_bytes: int
    percent: float
    status: QuotaStatus
    source_policy_id: str
    soft_percent: int = 80
    hard_percent: int = 100
    unlimited: bool = False


@dataclass(frozen=True)
class QuotaGateDecision:
    allowed: bool
    status: str
    detail: str | None = None
    snapshot: QuotaSnapshot | None = None


class WorkspaceQuotaExceeded(Exception):
    """写盘被配额拒绝；``detail`` 固定为 ``WORKSPACE_QUOTA_EXCEEDED``。"""

    def __init__(
        self,
        message: str = WORKSPACE_QUOTA_EXCEEDED,
        *,
        decision: QuotaGateDecision | None = None,
    ):
        super().__init__(message)
        self.detail = WORKSPACE_QUOTA_EXCEEDED
        self.decision = decision


_QUOTA_USER_CV: contextvars.ContextVar[str] = contextvars.ContextVar(
    "workspace_quota_user_id", default=""
)
_QUOTA_GROUP_CV: contextvars.ContextVar[str] = contextvars.ContextVar(
    "workspace_quota_group_id", default=""
)
_QUOTA_BOT_CV: contextvars.ContextVar[str] = contextvars.ContextVar(
    "workspace_quota_bot_id", default=""
)


def bind_quota_identity(
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    return (
        _QUOTA_USER_CV.set(str(user_id or "").strip()),
        _QUOTA_GROUP_CV.set(str(group_id or "").strip()),
        _QUOTA_BOT_CV.set(str(bot_id or "").strip()),
    )


def reset_quota_identity(
    tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token],
) -> None:
    _QUOTA_USER_CV.reset(tokens[0])
    _QUOTA_GROUP_CV.reset(tokens[1])
    _QUOTA_BOT_CV.reset(tokens[2])


def _get_quota_identity() -> dict[str, str]:
    return {
        "user_id": _QUOTA_USER_CV.get() or "",
        "group_id": _QUOTA_GROUP_CV.get() or "",
        "bot_id": _QUOTA_BOT_CV.get() or "",
    }


_DB_POLICIES: list[dict[str, Any]] = []
_DB_LOADED = False
_DB_LOADED_AT = 0.0


@dataclass
class UsageCacheEntry:
    """进程内用量缓存：仅保存最近一次 du 结果（无增量）。"""

    root: str
    used_bytes: int = 0
    reconciled_at: float = 0.0
    has_value: bool = False
    active: bool = False
    user_id: str = ""
    group_id: str = ""
    bot_id: str = ""


# resolved root path -> cache entry
_USAGE_CACHE: dict[str, UsageCacheEntry] = {}
_USAGE_CACHE_LOCK = threading.Lock()


def db_policies_loaded() -> bool:
    return _DB_LOADED


def db_policy_cache_loaded_at() -> float | None:
    if not _DB_LOADED:
        return None
    return _DB_LOADED_AT


def get_db_policy_cache() -> list[dict[str, Any]]:
    return list(_DB_POLICIES)


def set_db_policy_cache(rows: list[dict[str, Any]]) -> None:
    global _DB_POLICIES, _DB_LOADED, _DB_LOADED_AT
    _DB_POLICIES = [dict(row) for row in rows if isinstance(row, dict)]
    _DB_LOADED = True
    _DB_LOADED_AT = time.monotonic()


def used_bytes_ttl_seconds() -> float:
    """同一租户根两次后台校准的最小间隔；``0`` 表示不额外限频。非法值回落默认 5s。"""
    raw = os.getenv(USED_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_USED_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[workspace.quota] invalid %s=%r; using default %s",
            USED_TTL_ENV,
            raw,
            DEFAULT_USED_TTL_SECONDS,
        )
        return DEFAULT_USED_TTL_SECONDS
    if value < 0:
        logger.warning(
            "[workspace.quota] negative %s=%r; using default %s",
            USED_TTL_ENV,
            raw,
            DEFAULT_USED_TTL_SECONDS,
        )
        return DEFAULT_USED_TTL_SECONDS
    return value


def reconcile_interval_seconds() -> float:
    """后台校准循环休眠间隔；非法值回落默认 300s（5 分钟）。"""
    raw = os.getenv(RECONCILE_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[workspace.quota] invalid %s=%r; using default %s",
            RECONCILE_INTERVAL_ENV,
            raw,
            DEFAULT_RECONCILE_INTERVAL_SECONDS,
        )
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    if value <= 0:
        logger.warning(
            "[workspace.quota] non-positive %s=%r; using default %s",
            RECONCILE_INTERVAL_ENV,
            raw,
            DEFAULT_RECONCILE_INTERVAL_SECONDS,
        )
        return DEFAULT_RECONCILE_INTERVAL_SECONDS
    return value


def _root_key(root: Path | str) -> str:
    return str(Path(root).resolve())


def clear_used_bytes_cache() -> None:
    """清空用量缓存（测试 / 热重载）。"""
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE.clear()


def _touch_identity(
    entry: UsageCacheEntry,
    *,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
) -> None:
    uid = (user_id or "").strip()
    gid = (group_id or "").strip()
    bid = (bot_id or "").strip()
    if uid:
        entry.user_id = uid
    if gid:
        entry.group_id = gid
    if bid:
        entry.bot_id = bid
    if not (entry.user_id and entry.bot_id):
        identity = _get_quota_identity()
        if not entry.user_id:
            entry.user_id = (identity.get("user_id") or "").strip()
        if not entry.group_id:
            entry.group_id = (identity.get("group_id") or "").strip()
        if not entry.bot_id:
            entry.bot_id = (identity.get("bot_id") or "").strip()


def get_cached_used(tenant_root: Path | str) -> int:
    """热路径读缓存；无缓存返回 0（偏松，等后台 / 近限 / 手动刷新）。"""
    key = _root_key(tenant_root)
    with _USAGE_CACHE_LOCK:
        entry = _USAGE_CACHE.get(key)
        if entry is None or not entry.has_value:
            return 0
        return max(0, int(entry.used_bytes))


def set_cached_used(
    tenant_root: Path | str,
    used_bytes: int,
    *,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
) -> int:
    """写入最近一次 du 结果。"""
    key = _root_key(tenant_root)
    used = max(0, int(used_bytes))
    with _USAGE_CACHE_LOCK:
        entry = _USAGE_CACHE.get(key)
        if entry is None:
            entry = UsageCacheEntry(root=key)
            _USAGE_CACHE[key] = entry
        entry.used_bytes = used
        entry.has_value = True
        entry.reconciled_at = time.monotonic()
        entry.active = True
        _touch_identity(entry, user_id=user_id, group_id=group_id, bot_id=bot_id)
        return entry.used_bytes


def mark_usage_active(
    tenant_root: Path | str,
    *,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
) -> None:
    """登记活跃租户，供后台定时扫盘（不执行 du）。"""
    key = _root_key(tenant_root)
    with _USAGE_CACHE_LOCK:
        entry = _USAGE_CACHE.get(key)
        if entry is None:
            entry = UsageCacheEntry(root=key, active=True)
            _USAGE_CACHE[key] = entry
        else:
            entry.active = True
        _touch_identity(entry, user_id=user_id, group_id=group_id, bot_id=bot_id)


def measure_and_cache(
    tenant_root: Path | str,
    *,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
) -> int:
    """同步执行 du 并写入缓存（近限门禁 / 手动刷新 / 后台校准）。"""
    root = Path(tenant_root)
    used = max(0, int(measure_used_bytes(root)))
    return set_cached_used(
        root, used, user_id=user_id, group_id=group_id, bot_id=bot_id
    )


def list_usage_reconcile_targets(
    *,
    now: float | None = None,
    min_interval_seconds: float | None = None,
) -> list[UsageCacheEntry]:
    """选出需后台校准的活跃缓存快照。"""
    ts = time.monotonic() if now is None else float(now)
    min_interval = (
        reconcile_interval_seconds()
        if min_interval_seconds is None
        else float(min_interval_seconds)
    )
    # 与 USED_TTL 取较大者，避免过密扫盘。
    min_interval = max(min_interval, used_bytes_ttl_seconds())
    out: list[UsageCacheEntry] = []
    with _USAGE_CACHE_LOCK:
        for entry in _USAGE_CACHE.values():
            if not entry.active and entry.has_value:
                continue
            age = (
                (ts - entry.reconciled_at)
                if entry.reconciled_at > 0
                else float("inf")
            )
            if entry.has_value and age < min_interval:
                continue
            out.append(
                UsageCacheEntry(
                    root=entry.root,
                    used_bytes=entry.used_bytes,
                    reconciled_at=entry.reconciled_at,
                    has_value=entry.has_value,
                    active=entry.active,
                    user_id=entry.user_id,
                    group_id=entry.group_id,
                    bot_id=entry.bot_id,
                )
            )
    out.sort(key=lambda e: (e.has_value, e.reconciled_at))
    return out


def _is_unlimited_limit(limit_bytes: int) -> bool:
    try:
        return int(limit_bytes) == UNLIMITED_LIMIT_BYTES
    except (TypeError, ValueError):
        return False


def fallback_limit_bytes() -> int:
    """无策略时的默认配额。``0`` / 负数 / 非法值回落 10 TiB。"""
    raw = os.getenv(FALLBACK_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_FALLBACK_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[workspace.quota] invalid %s=%r; using default %s",
            FALLBACK_LIMIT_ENV,
            raw,
            DEFAULT_FALLBACK_LIMIT_BYTES,
        )
        return DEFAULT_FALLBACK_LIMIT_BYTES
    if value <= 0:
        logger.warning(
            "[workspace.quota] non-positive %s=%r; using default %s",
            FALLBACK_LIMIT_ENV,
            raw,
            DEFAULT_FALLBACK_LIMIT_BYTES,
        )
        return DEFAULT_FALLBACK_LIMIT_BYTES
    return value


def compute_quota_status(
    used_bytes: int,
    limit_bytes: int,
    *,
    soft_percent: int = 80,
    hard_percent: int = 100,
    source_policy_id: str = "",
) -> QuotaSnapshot:
    """由 used / limit / 阈值计算 ``ok`` / ``warn`` / ``block``。"""
    used = max(0, int(used_bytes))
    soft = int(soft_percent) if soft_percent is not None else 80
    hard = int(hard_percent) if hard_percent is not None else 100
    if hard <= soft:
        hard = soft + 1

    raw_limit = int(limit_bytes)
    if _is_unlimited_limit(raw_limit):
        return QuotaSnapshot(
            used_bytes=used,
            limit_bytes=UNLIMITED_LIMIT_BYTES,
            percent=0.0,
            status="ok",
            source_policy_id=str(source_policy_id or ""),
            soft_percent=soft,
            hard_percent=hard,
            unlimited=True,
        )

    limit = max(0, raw_limit)
    if limit <= 0:
        percent = 0.0 if used == 0 else 100.0
        status: QuotaStatus = "block"
    else:
        percent = (used / limit) * 100.0
        soft_bytes = limit * soft / 100.0
        hard_bytes = limit * hard / 100.0
        if used >= hard_bytes:
            status = "block"
        elif used >= soft_bytes:
            status = "warn"
        else:
            status = "ok"
    return QuotaSnapshot(
        used_bytes=used,
        limit_bytes=limit,
        percent=round(percent, 4),
        status=status,
        source_policy_id=str(source_policy_id or ""),
        soft_percent=soft,
        hard_percent=hard,
        unlimited=False,
    )


def snapshot_to_dict(
    snap: QuotaSnapshot,
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "group_id": group_id,
        "bot_id": bot_id,
        "used_bytes": snap.used_bytes,
        "limit_bytes": snap.limit_bytes,
        "percent": snap.percent,
        "status": snap.status,
        "source_policy_id": snap.source_policy_id,
        "unlimited": bool(snap.unlimited),
    }


def _additional_bytes_for_overwrite(*, old_size: int | None, new_size: int) -> int:
    new_n = max(0, int(new_size))
    if old_size is None:
        return new_n
    old_n = max(0, int(old_size))
    return max(0, new_n - old_n)


def _evaluate_write(
    *,
    used_bytes: int,
    limit_bytes: int,
    soft_percent: int = 80,
    hard_percent: int = 100,
    source_policy_id: str = "",
    additional_bytes: int,
) -> QuotaGateDecision:
    """纯判断：无限制 / 净增≤0 放行；block 或跨 hard 则拒绝。"""
    snap = compute_quota_status(
        used_bytes,
        limit_bytes,
        soft_percent=soft_percent,
        hard_percent=hard_percent,
        source_policy_id=source_policy_id,
    )
    if snap.unlimited:
        return QuotaGateDecision(allowed=True, status=snap.status, snapshot=snap)

    add = int(additional_bytes)
    if add <= 0:
        return QuotaGateDecision(allowed=True, status=snap.status, snapshot=snap)
    if snap.status == "block":
        return QuotaGateDecision(
            allowed=False,
            status=snap.status,
            detail=WORKSPACE_QUOTA_EXCEEDED,
            snapshot=snap,
        )
    hard_bytes = (
        0.0
        if snap.limit_bytes <= 0
        else snap.limit_bytes * snap.hard_percent / 100.0
    )
    if used_bytes + add >= hard_bytes:
        return QuotaGateDecision(
            allowed=False,
            status="block",
            detail=WORKSPACE_QUOTA_EXCEEDED,
            snapshot=snap,
        )
    return QuotaGateDecision(allowed=True, status=snap.status, snapshot=snap)


def _pick_policy(
    policies: list[dict[str, Any]],
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
) -> dict[str, Any] | None:
    from jiuwenswarm.gateway.config.enterprise.expressions import matches

    identity = {"user_id": user_id, "group_id": group_id, "bot_id": bot_id}
    hits: list[tuple[int, dict[str, Any]]] = []
    for row in policies:
        if not bool(row.get("enabled", True)):
            continue
        try:
            if not matches(row.get("match_expr"), identity):
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            priority = int(row.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        hits.append((priority, row))
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    return hits[0][1]


def resolve_tenant_root(tenant_root: Path | None = None) -> Path:
    """解析当前请求绑定的租户根目录。"""
    root = tenant_root
    if root is None:
        try:
            from jiuwenswarm.server.runtime.tenant_context import get_bound_tenant_root

            bound = get_bound_tenant_root()
            if bound is not None:
                root = bound
        except Exception:  # noqa: BLE001
            root = None
    if root is None:
        from jiuwenswarm.common.utils import get_multi_tenant_user_workspace_dir

        root = get_multi_tenant_user_workspace_dir()
    return Path(root)


def _snapshot_for_used(
    used: int,
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
) -> QuotaSnapshot:
    policies = get_db_policy_cache() if db_policies_loaded() else []
    policy = _pick_policy(policies, user_id=user_id, group_id=group_id, bot_id=bot_id)
    if policy is None:
        return compute_quota_status(
            used,
            fallback_limit_bytes(),
            soft_percent=80,
            hard_percent=100,
            source_policy_id=FALLBACK_POLICY_ID,
        )
    return compute_quota_status(
        used,
        int(policy.get("limit_bytes", 0)),
        soft_percent=int(policy.get("soft_percent") or 80),
        hard_percent=int(policy.get("hard_percent") or 100),
        source_policy_id=str(policy.get("policy_id") or ""),
    )


def resolve_effective_quota(
    *,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
    used_bytes: int | None = None,
    tenant_root: Path | None = None,
    force_refresh: bool = False,
    sync_on_near_limit: bool = True,
) -> QuotaSnapshot:
    """选路 + 用量 → 配额快照。

    - 默认只读缓存（不 du）；
    - ``force_refresh``：强制同步 du（页面刷新）；
    - ``sync_on_near_limit``：缓存已是 warn/block 时门禁路径同步校准（展示路径应关）。
    """
    identity = _get_quota_identity()
    uid = (user_id or identity.get("user_id") or "").strip()
    gid = (group_id or identity.get("group_id") or "").strip()
    bid = (bot_id or identity.get("bot_id") or "").strip()

    root = resolve_tenant_root(tenant_root)
    if used_bytes is not None:
        return _snapshot_for_used(
            int(used_bytes), user_id=uid, group_id=gid, bot_id=bid
        )

    mark_usage_active(root, user_id=uid, group_id=gid, bot_id=bid)
    used = get_cached_used(root)
    snap = _snapshot_for_used(used, user_id=uid, group_id=gid, bot_id=bid)
    need_du = bool(force_refresh) or (
        sync_on_near_limit and snap.status in ("warn", "block")
    )
    if need_du:
        used = measure_and_cache(
            root, user_id=uid, group_id=gid, bot_id=bid
        )
        snap = _snapshot_for_used(used, user_id=uid, group_id=gid, bot_id=bid)
    return snap


def check_workspace_write(
    *,
    additional_bytes: int,
    tenant_root: Path | None = None,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
    used_bytes: int | None = None,
) -> QuotaGateDecision:
    """写盘前检查。拒绝时抛 ``WorkspaceQuotaExceeded``。"""
    if not is_workspace_quota_enabled():
        return QuotaGateDecision(allowed=True, status="ok", detail=None, snapshot=None)
    snap = resolve_effective_quota(
        user_id=user_id,
        group_id=group_id,
        bot_id=bot_id,
        used_bytes=used_bytes,
        tenant_root=tenant_root,
    )
    decision = _evaluate_write(
        used_bytes=snap.used_bytes,
        limit_bytes=snap.limit_bytes,
        soft_percent=snap.soft_percent,
        hard_percent=snap.hard_percent,
        source_policy_id=snap.source_policy_id,
        additional_bytes=additional_bytes,
    )
    if not decision.allowed:
        raise WorkspaceQuotaExceeded(decision=decision)
    return decision


def estimate_text_write_additional(
    path: Path | str,
    content: str | bytes,
    *,
    encoding: str = "utf-8",
    append: bool = False,
) -> int:
    """估算写文件相对当前盘占用的净增字节。"""
    target = Path(path)
    if isinstance(content, bytes):
        new_payload = len(content)
    else:
        new_payload = len(str(content).encode(encoding, errors="replace"))
    if append:
        return max(0, new_payload)
    old_size: int | None = None
    try:
        if target.is_file():
            old_size = int(target.stat().st_size)
    except OSError:
        old_size = None
    return _additional_bytes_for_overwrite(old_size=old_size, new_size=new_payload)


def check_path_write(
    path: Path | str,
    content: str | bytes,
    *,
    encoding: str = "utf-8",
    append: bool = False,
    tenant_root: Path | None = None,
) -> QuotaGateDecision:
    add = estimate_text_write_additional(
        path, content, encoding=encoding, append=append
    )
    return check_workspace_write(additional_bytes=add, tenant_root=tenant_root)


__all__ = [
    "DEFAULT_FALLBACK_LIMIT_BYTES",
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "DEFAULT_USED_TTL_SECONDS",
    "FALLBACK_LIMIT_ENV",
    "FALLBACK_POLICY_ID",
    "FEATURE_ENABLED_ENV",
    "QuotaGateDecision",
    "QuotaSnapshot",
    "QuotaStatus",
    "RECONCILE_INTERVAL_ENV",
    "UNLIMITED_LIMIT_BYTES",
    "USED_TTL_ENV",
    "UsageCacheEntry",
    "WORKSPACE_QUOTA_EXCEEDED",
    "WorkspaceQuotaExceeded",
    "bind_quota_identity",
    "check_path_write",
    "check_workspace_write",
    "clear_used_bytes_cache",
    "compute_quota_status",
    "db_policies_loaded",
    "db_policy_cache_loaded_at",
    "estimate_text_write_additional",
    "fallback_limit_bytes",
    "get_cached_used",
    "get_db_policy_cache",
    "is_workspace_quota_enabled",
    "list_usage_reconcile_targets",
    "mark_usage_active",
    "measure_and_cache",
    "reconcile_interval_seconds",
    "reset_quota_identity",
    "resolve_effective_quota",
    "resolve_tenant_root",
    "set_cached_used",
    "set_db_policy_cache",
    "snapshot_to_dict",
    "used_bytes_ttl_seconds",
]
