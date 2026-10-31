# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway Redis Key / 频道命名（gateway-reliability-enhancement-design §3.3.7）.

相对路径片段会与 ``RedisConfig.key_prefix`` 拼接为完整 Redis key 或 Pub/Sub 频道名。
"""

from __future__ import annotations


def _normalize_prefix(key_prefix: str) -> str:
    p = (key_prefix or "").strip()
    if not p:
        return ""
    return p if p.endswith(":") else f"{p}:"


def session_map_hash_key(key_prefix: str, identity_key: str) -> str:
    """``{key_prefix}gateway:session_map:{identity_key}`` Hash。"""
    return f"{_normalize_prefix(key_prefix)}gateway:session_map:{identity_key}"


def cron_jobs_hash_rel(gateway_instance_id: str) -> str:
    """``gateway:cron_jobs:{gateway_instance_id}`` Hash（相对名，经 RedisClient 加 prefix）。"""
    iid = str(gateway_instance_id or "").strip()
    if not iid:
        raise ValueError("gateway_instance_id is required")
    return f"gateway:cron_jobs:{iid}"


def cron_jobs_hash_key(key_prefix: str, gateway_instance_id: str) -> str:
    """``{key_prefix}gateway:cron_jobs:{gateway_instance_id}`` Hash。"""
    return f"{_normalize_prefix(key_prefix)}{cron_jobs_hash_rel(gateway_instance_id)}"


def leader_lock_key(key_prefix: str, channel_id: str) -> str:
    """``{key_prefix}gateway:leader:{channel_id}`` String（带 TTL）。"""
    return f"{_normalize_prefix(key_prefix)}gateway:leader:{channel_id}"


def cron_live_channel_rel(session_id: str) -> str:
    """``gateway:cron_live:{session_id}`` Pub/Sub 频道（相对名，经 RedisClient 加 prefix）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return f"gateway:cron_live:{sid}"


def cron_live_replay_key_rel(session_id: str) -> str:
    """``gateway:cron_live_replay:{session_id}`` List（相对名；短 TTL 晚订回放）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return f"gateway:cron_live_replay:{sid}"


def cron_permission_hold_rel(job_id: str) -> str:
    """``gateway:cron_permission_hold:{job_id}`` String（相对名；TTL=审批窗口）。"""
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")
    return f"gateway:cron_permission_hold:{jid}"


def cron_permission_hold_by_session_rel(session_id: str) -> str:
    """``gateway:cron_permission_hold_by_session:{session_id}`` → job_id（跨进程释放）。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return f"gateway:cron_permission_hold_by_session:{sid}"
