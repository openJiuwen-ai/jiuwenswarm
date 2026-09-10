# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业租户已安装技能账本：Agent 写库 / Gateway 只读 list。

权威表：``installed_skill``（Gateway DB）。有行即安装成功并可启用。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "installed_skill"
SOURCE_PREBUILT = "prebuilt"
SOURCE_USER = "user"

SKILL_SOURCE_PREFIX_WEB = "web:"
SKILL_SOURCE_PREFIX_SKILLNET = "skillnet:"
SKILL_SOURCE_PREFIX_CLAWHUB = "clawhub:"

# D13 / §6.2 decide_user_reinstall 结果
DECISION_INSTALL = "install"
DECISION_UPGRADE = "upgrade"
DECISION_ALREADY_INSTALLED = "already_installed"
DECISION_PREBUILT = "prebuilt"
DECISION_BLOCKED = "blocked"

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def skill_versions_equal(left: str | None, right: str | None) -> bool:
    """D13：仅去首尾空白后的字符串全等（不做 semver / 不剥 v 前缀）。"""
    return str(left or "").strip() == str(right or "").strip()


def decide_user_reinstall(
    existing: dict[str, Any] | None,
    *,
    new_version: str | None,
) -> str:
    """用户自装撞名决策：install / upgrade / already_installed / prebuilt / blocked。"""
    if existing is None:
        return DECISION_INSTALL
    st = str(existing.get("source_type") or "").strip()
    if st == SOURCE_PREBUILT:
        return DECISION_PREBUILT
    if st != SOURCE_USER:
        return DECISION_BLOCKED
    if skill_versions_equal(existing.get("skill_version"), new_version):
        return DECISION_ALREADY_INSTALLED
    return DECISION_UPGRADE


def format_user_skill_source(channel: str, identifier: str) -> str:
    """渠道前缀 + 正文；渠道仅供展示/审计，不参与启用/卸载守卫。"""
    body = str(identifier or "").strip()
    ch = str(channel or "").strip().lower()
    if ch == "web":
        return f"{SKILL_SOURCE_PREFIX_WEB}{body}"
    if ch == "clawhub":
        return f"{SKILL_SOURCE_PREFIX_CLAWHUB}{body}"
    return f"{SKILL_SOURCE_PREFIX_SKILLNET}{body}"


def verify_skill_download_hmac(content: bytes, signature: str, *, secret: str | None = None) -> bool:
    """HMAC-SHA256(content) hex；密钥来自 ``SKILL_DOWNLOAD_HMAC_SECRET``。"""
    key = (secret if secret is not None else os.getenv("SKILL_DOWNLOAD_HMAC_SECRET", "")).strip()
    expected = str(signature or "").strip().lower()
    if not key or not expected or not content:
        return False
    digest = hmac.new(key.encode("utf-8"), content, hashlib.sha256).hexdigest().lower()
    return hmac.compare_digest(digest, expected)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, dict):
            return dumped
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, dict):
            return dumped
    fields = getattr(row, "__dataclass_fields__", None) or getattr(row, "__annotations__", None)
    if fields:
        return {k: getattr(row, k) for k in fields if not str(k).startswith("_")}
    keys = (
        "id",
        "group_id",
        "bot_id",
        "user_id",
        "service_id",
        "agent_id",
        "skill_name",
        "source_type",
        "skill_source",
        "skill_version",
        "skill_id",
        "installed_at",
        "updated_at",
        "data",
    )
    return {k: getattr(row, k) for k in keys if hasattr(row, k)}


async def _handler() -> Any:
    from jiuwenswarm.infrastructure.db.database import Database

    # AgentServer 无 PersistentStore；直连 Database 单例。
    return await Database.current().ensure_ready(log_prefix=TABLE)


async def list_installed_skills(
    *,
    service_id: str,
    agent_id: str,
    source_type: str | None = None,
) -> list[dict[str, Any]]:
    """按最终 ``service_id`` + ``agent_id`` 列出成功安装行。"""
    sid = str(service_id or "").strip()
    aid = str(agent_id or "").strip()
    if not sid or not aid:
        return []

    filters: dict[str, Any] = {
        "service_id": sid,
        "agent_id": aid,
    }
    if source_type:
        filters["source_type"] = str(source_type).strip()

    handler = await _handler()
    rows = await handler.list_records(
        TABLE,
        filters,
        limit=10_000,
        offset=0,
        order_by=[("updated_at", True)],
    )
    out: list[dict[str, Any]] = []
    for row in rows or []:
        data = _row_to_dict(row)
        name = str(data.get("skill_name") or "").strip()
        if name:
            out.append(data)
    return out


async def list_enabled_skill_names(*, service_id: str, agent_id: str) -> list[str]:
    rows = await list_installed_skills(service_id=service_id, agent_id=agent_id)
    seen: set[str] = set()
    names: list[str] = []
    for row in rows:
        name = str(row.get("skill_name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


async def get_installed_skill(
    *,
    service_id: str,
    agent_id: str,
    skill_name: str,
) -> dict[str, Any] | None:
    sid = str(service_id or "").strip()
    aid = str(agent_id or "").strip()
    name = str(skill_name or "").strip()
    if not sid or not aid or not name:
        return None
    handler = await _handler()
    row = await handler.get(
        TABLE,
        {
            "service_id": sid,
            "agent_id": aid,
            "skill_name": name,
        },
    )
    return _row_to_dict(row) if row is not None else None


async def upsert_installed_skill(
    *,
    service_id: str,
    agent_id: str,
    skill_name: str,
    source_type: str,
    skill_source: str | None = None,
    skill_version: str | None = None,
    skill_id: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Agent 写库：按唯一键 upsert。失败抛异常（调用方回滚盘）。"""
    sid = str(service_id or "").strip()
    aid = str(agent_id or "").strip()
    name = str(skill_name or "").strip()
    st = str(source_type or "").strip()
    if not sid or not aid or not name:
        raise ValueError("service_id, agent_id and skill_name are required")
    if st not in (SOURCE_PREBUILT, SOURCE_USER):
        raise ValueError(f"invalid source_type: {source_type}")

    now = _utc_now()
    handler = await _handler()
    identity = {
        "service_id": sid,
        "agent_id": aid,
        "skill_name": name,
    }
    existing = await handler.get(TABLE, identity)

    row_data: dict[str, Any] = {
        "service_id": sid,
        "agent_id": aid,
        "skill_name": name,
        "source_type": st,
        "skill_source": (str(skill_source).strip() if skill_source is not None else None) or None,
        "skill_version": (str(skill_version).strip() if skill_version is not None else None) or None,
        "skill_id": (str(skill_id).strip() if skill_id is not None else None) or None,
        "group_id": (str(group_id).strip() if group_id else None) or None,
        "bot_id": (str(bot_id).strip() if bot_id else None) or None,
        "user_id": (str(user_id).strip() if user_id else None) or None,
        "updated_at": now,
        "data": data,
    }

    if existing is not None:
        existing_map = _row_to_dict(existing)
        installed_at = existing_map.get("installed_at") or now
        # 预置项 user_id 可空：更新时若未传则保留原值（抬升时清空）
        if user_id is None and st == SOURCE_USER:
            row_data["user_id"] = existing_map.get("user_id")
        update_payload = dict(row_data)
        update_payload.pop("service_id", None)
        update_payload.pop("agent_id", None)
        update_payload.pop("skill_name", None)
        result = await handler.update(TABLE, identity, update_payload)
        if result is None:
            raise RuntimeError(f"failed to update installed_skill: {name}")
        out = dict(row_data)
        out["installed_at"] = installed_at
        return out

    row_data["installed_at"] = now
    created = await handler.create(TABLE, row_data)
    if created is None:
        raise RuntimeError(f"failed to insert installed_skill: {name}")
    return row_data


async def delete_installed_skill(
    *,
    service_id: str,
    agent_id: str,
    skill_name: str,
) -> bool:
    sid = str(service_id or "").strip()
    aid = str(agent_id or "").strip()
    name = str(skill_name or "").strip()
    if not sid or not aid or not name:
        return False
    handler = await _handler()
    return bool(
        await handler.delete(
            TABLE,
            {
                "service_id": sid,
                "agent_id": aid,
                "skill_name": name,
            },
        )
    )


def _json_safe_dt(value: Any) -> Any:
    """将 DB ``datetime`` 转为 ISO 字符串，避免 WebSocket JSON 序列化失败。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def row_public_view(row: dict[str, Any]) -> dict[str, Any]:
    """Gateway list / Web 展示字段。"""
    source_type = str(row.get("source_type") or "").strip()
    return {
        "skill_name": row.get("skill_name"),
        "source_type": source_type,
        "skill_source": row.get("skill_source"),
        "skill_version": row.get("skill_version"),
        "skill_id": row.get("skill_id"),
        "user_id": row.get("user_id"),
        "group_id": row.get("group_id"),
        "bot_id": row.get("bot_id"),
        "service_id": row.get("service_id"),
        "agent_id": row.get("agent_id"),
        "installed_at": _json_safe_dt(row.get("installed_at")),
        "updated_at": _json_safe_dt(row.get("updated_at")),
        "removable": source_type == SOURCE_USER,
    }


async def list_installed_skills_for_gateway(
    *,
    service_id: str,
    agent_id: str,
) -> list[dict[str, Any]]:
    rows = await list_installed_skills(service_id=service_id, agent_id=agent_id)
    return [row_public_view(r) for r in rows]


def resolve_final_tenant_ids(
    *,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
    service_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[str, str]:
    """逻辑 ID → MD5 最终 ID（与 RuntimeManagement ``_SessionRequest`` 一致）。

    若 ``service_id`` / ``agent_id`` 均已是 32 位 hex 最终 ID，则原样返回（§7）。
    """
    g = str(group_id or "").strip()
    b = str(bot_id or "").strip()
    u = str(user_id or "").strip()
    svc = str(service_id or "").strip()
    ag = str(agent_id or "").strip()
    if _HEX32_RE.fullmatch(svc) and _HEX32_RE.fullmatch(ag):
        return svc, ag
    if not svc or not ag:
        if g and b and u:
            try:
                from openjiuwen_runtime_management_extension.invoke_ids import (  # type: ignore
                    _default_invoke_ids,
                )

                svc, ag = _default_invoke_ids(g, b, u)
            except Exception as exc:  # noqa: BLE001 — extension optional; fallback below
                logger.debug(
                    "[InstalledSkill] _default_invoke_ids failed for g=%r b=%r u=%r: %s",
                    g,
                    b,
                    u,
                    exc,
                )
                routed = b
                svc = svc or f"{g}{routed}"
                ag = ag or f"{g}{routed}{u}"
        else:
            svc = svc or "default_service_id"
            ag = ag or "default_agent_id"
    if _HEX32_RE.fullmatch(svc) and _HEX32_RE.fullmatch(ag):
        return svc, ag
    return (
        hashlib.md5(svc.encode("utf-8")).hexdigest(),
        hashlib.md5(ag.encode("utf-8")).hexdigest(),
    )


__all__ = (
    "SOURCE_PREBUILT",
    "SOURCE_USER",
    "SKILL_SOURCE_PREFIX_WEB",
    "SKILL_SOURCE_PREFIX_SKILLNET",
    "SKILL_SOURCE_PREFIX_CLAWHUB",
    "TABLE",
    "DECISION_INSTALL",
    "DECISION_UPGRADE",
    "DECISION_ALREADY_INSTALLED",
    "DECISION_PREBUILT",
    "DECISION_BLOCKED",
    "decide_user_reinstall",
    "delete_installed_skill",
    "format_user_skill_source",
    "get_installed_skill",
    "list_enabled_skill_names",
    "list_installed_skills",
    "list_installed_skills_for_gateway",
    "resolve_final_tenant_ids",
    "row_public_view",
    "skill_versions_equal",
    "upsert_installed_skill",
    "verify_skill_download_hmac",
)
