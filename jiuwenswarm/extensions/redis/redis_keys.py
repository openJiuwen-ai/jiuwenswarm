# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway Redis Key / 频道命名（gateway-reliability-enhancement-design §3.3.7）.

相对路径片段会与 ``RedisConfig.key_prefix`` 拼接为完整 Redis key 或 Pub/Sub 频道名。
模板字面量以 ``openjiuwen_protocol.redis_keys`` 为唯一 source of truth；本模块只做
prefix 归一化、空值校验与拼装。
"""

from __future__ import annotations

from openjiuwen_protocol.redis_keys import (
    GATEWAY_CRON_JOBS_HASH_TEMPLATE,
    GATEWAY_CRON_LIVE_CHANNEL_TEMPLATE,
    GATEWAY_CRON_LIVE_REPLAY_TEMPLATE,
    GATEWAY_CRON_PERMISSION_HOLD_BY_SESSION_TEMPLATE,
    GATEWAY_CRON_PERMISSION_HOLD_TEMPLATE,
    GATEWAY_LEADER_LOCK_TEMPLATE,
    GATEWAY_SESSION_MAP_HASH_TEMPLATE,
    REDIS_KEY_PREFIX_SEPARATOR,
)


def _normalize_prefix(key_prefix: str) -> str:
    p = (key_prefix or "").strip()
    if not p:
        return ""
    return p if p.endswith(REDIS_KEY_PREFIX_SEPARATOR) else f"{p}{REDIS_KEY_PREFIX_SEPARATOR}"


def session_map_hash_key(key_prefix: str, identity_key: str) -> str:
    """``{key_prefix}gateway:session_map:{identity_key}`` Hash。"""
    return _normalize_prefix(key_prefix) + GATEWAY_SESSION_MAP_HASH_TEMPLATE.format(
        identity_key=identity_key
    )


def cron_jobs_hash_rel(gateway_instance_id: str) -> str:
    """``gateway:cron_jobs:{gateway_instance_id}`` Hash（相对名，经 RedisClient 加 prefix）。"""
    iid = str(gateway_instance_id or "").strip()
    if not iid:
        raise ValueError("gateway_instance_id is required")
    return GATEWAY_CRON_JOBS_HASH_TEMPLATE.format(gateway_instance_id=iid)


def cron_jobs_hash_key(key_prefix: str, gateway_instance_id: str) -> str:
    """``{key_prefix}gateway:cron_jobs:{gateway_instance_id}`` Hash。"""
    return f"{_normalize_prefix(key_prefix)}{cron_jobs_hash_rel(gateway_instance_id)}"


def leader_lock_key(key_prefix: str, channel_id: str) -> str:
    """``{key_prefix}gateway:leader:{channel_id}`` String（带 TTL）。"""
    return _normalize_prefix(key_prefix) + GATEWAY_LEADER_LOCK_TEMPLATE.format(
        channel_id=channel_id
    )


def cron_live_channel_rel(session_id: str) -> str:
    """``gateway:cron_live:{session_id}`` Pub/Sub 频道（相对名，经 RedisClient 加 prefix）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return GATEWAY_CRON_LIVE_CHANNEL_TEMPLATE.format(session_id=sid)


def cron_live_replay_key_rel(session_id: str) -> str:
    """``gateway:cron_live_replay:{session_id}`` List（相对名；短 TTL 晚订回放）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return GATEWAY_CRON_LIVE_REPLAY_TEMPLATE.format(session_id=sid)


def cron_permission_hold_rel(job_id: str) -> str:
    """``gateway:cron_permission_hold:{job_id}`` String（相对名；TTL=审批窗口）。"""
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")
    return GATEWAY_CRON_PERMISSION_HOLD_TEMPLATE.format(job_id=jid)


def cron_permission_hold_by_session_rel(session_id: str) -> str:
    """``gateway:cron_permission_hold_by_session:{session_id}`` → job_id（跨进程释放）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return GATEWAY_CRON_PERMISSION_HOLD_BY_SESSION_TEMPLATE.format(session_id=sid)


def cron_active_session_rel(job_id: str) -> str:
    """``gateway:cron_active_session:{job_id}`` String：该 job 正在执行的会话号。

    每个 job 独立 key + 独立 TTL，避免短超时任务把整表 EXPIRE 盖掉长任务。
    企业多副本下列表可能打到非执行 Pod；前端靠这个字段发现到点会话并订 live。
    """
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")
    return f"gateway:cron_active_session:{jid}"


def cron_active_session_scan_rel() -> str:
    """``gateway:cron_active_session:*``，供 collect 扫描所有进行中会话。"""
    return "gateway:cron_active_session:*"


def cron_active_sessions_hash_rel() -> str:
    """旧版整表 Hash（已废弃）。

    曾用单 key + EXPIRE，后写入的短 TTL 会覆盖先写入的长任务。保留名仅用于迁移清理。
    """
    return "gateway:cron_active_sessions"
