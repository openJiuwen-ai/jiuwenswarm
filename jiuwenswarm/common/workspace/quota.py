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
DEFAULT_USED_TTL_SECONDS = 5.0

# 策略 ``limit_bytes == -1`` 表示无限制；``0`` 表示零配额（满额阻断）。
UNLIMITED_LIMIT_BYTES = -1

QuotaStatus = Literal["ok", "warn", "block"]


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

# resolved root path -> (monotonic timestamp, used_bytes)
_USED_BYTES_CACHE: dict[str, tuple[float, int]] = {}
_USED_BYTES_CACHE_LOCK = threading.Lock()


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
    """用量 du 结果 TTL；``0`` 关闭缓存；非法值回落默认 5s。"""
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


def clear_used_bytes_cache() -> None:
    with _USED_BYTES_CACHE_LOCK:
        _USED_BYTES_CACHE.clear()


def _measure_used_bytes_cached(root: Path) -> int:
    """按 tenant root 缓存 ``measure_used_bytes``，避免热路径反复全量 du。"""
    key = str(Path(root).resolve())
    ttl = used_bytes_ttl_seconds()
    if ttl > 0:
        now = time.monotonic()
        with _USED_BYTES_CACHE_LOCK:
            hit = _USED_BYTES_CACHE.get(key)
            if hit is not None and (now - hit[0]) < ttl:
                return hit[1]
    used = max(0, int(measure_used_bytes(Path(root))))
    if ttl > 0:
        with _USED_BYTES_CACHE_LOCK:
            _USED_BYTES_CACHE[key] = (time.monotonic(), used)
    return used


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


def resolve_effective_quota(
    *,
    user_id: str = "",
    group_id: str = "",
    bot_id: str = "",
    used_bytes: int | None = None,
    tenant_root: Path | None = None,
) -> QuotaSnapshot:
    """选路 + du → 配额快照。未命中策略时回落环境变量默认限额。"""
    identity = _get_quota_identity()
    uid = (user_id or identity.get("user_id") or "").strip()
    gid = (group_id or identity.get("group_id") or "").strip()
    bid = (bot_id or identity.get("bot_id") or "").strip()

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

    used = (
        int(used_bytes)
        if used_bytes is not None
        else _measure_used_bytes_cached(Path(root))
    )

    policies = get_db_policy_cache() if db_policies_loaded() else []
    policy = _pick_policy(policies, user_id=uid, group_id=gid, bot_id=bid)
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
    "DEFAULT_USED_TTL_SECONDS",
    "FALLBACK_LIMIT_ENV",
    "FALLBACK_POLICY_ID",
    "QuotaGateDecision",
    "QuotaSnapshot",
    "QuotaStatus",
    "UNLIMITED_LIMIT_BYTES",
    "USED_TTL_ENV",
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
    "get_db_policy_cache",
    "reset_quota_identity",
    "resolve_effective_quota",
    "set_db_policy_cache",
    "snapshot_to_dict",
    "used_bytes_ttl_seconds",
]
