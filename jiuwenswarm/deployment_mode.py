# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 部署模式集中定义与判断 helper。

三种模式：
- ``standalone``：单机（默认）。不连 Redis，Session/Cron 本地文件。
- ``active-standby``：主备。连接 Redis + LeaderElection 选主，仅 PRIMARY 处理。
- ``distributed``：多副本同时在线。连接 Redis 共享 Session（内存缓存 + Redis，
  跨实例靠 lazy get 回填），无选主；Cron 默认关闭；仅 web/tui 通道。

各模块（redis_runtime / session_map / channel_config_overlay / app_gateway 等）
统一引用本模块，避免散落的字符串比较。
"""

from __future__ import annotations

from typing import Literal

MODE_STANDALONE = "standalone"
MODE_ACTIVE_STANDBY = "active-standby"
MODE_DISTRIBUTED = "distributed"

VALID_DEPLOYMENT_MODES: tuple[str, ...] = (
    MODE_STANDALONE,
    MODE_ACTIVE_STANDBY,
    MODE_DISTRIBUTED,
)

SessionStorageBackend = Literal["local", "redis"]
HistoryStorageBackend = Literal["memory", "mysql"]


def normalize_deployment_mode(raw: object) -> str:
    """归一化 deployment_mode；非法/空值回退 ``standalone``。"""
    mode = str(raw or "").strip().lower()
    if mode in VALID_DEPLOYMENT_MODES:
        return mode
    return MODE_STANDALONE


def uses_gateway_redis(mode: str) -> bool:
    """该模式是否需要连接 Gateway Redis（active-standby / distributed）。"""
    return normalize_deployment_mode(mode) in (MODE_ACTIVE_STANDBY, MODE_DISTRIBUTED)


def uses_leader_election(mode: str) -> bool:
    """仅 active-standby 需要 LeaderElection；distributed 多副本同时处理，不选主。"""
    return normalize_deployment_mode(mode) == MODE_ACTIVE_STANDBY


def session_storage_backend(mode: str) -> SessionStorageBackend:
    """SessionMap 存储后端：standalone 本地文件；其余为内存缓存 + Redis。"""
    if uses_gateway_redis(mode):
        return "redis"
    return "local"


def history_storage_backend(mode: str) -> HistoryStorageBackend:
    """Web 会话历史默认存储后端。

    - standalone：纯内存（个人版不落库；web 数据库为企业版特性）
    - active-standby / distributed：MySQL 独立库 ``web``（删 Pod 不丢历史）

    实际运行时仍以 ``WEB_DB_TYPE`` / ``WEB_DB_HOST`` 等环境变量优先
    （见 ``resolve_history_db_type``）；本函数表达「部署模式推荐默认值」。
    sqlite 已废弃（两端均不支持）。
    """
    if normalize_deployment_mode(mode) in (MODE_ACTIVE_STANDBY, MODE_DISTRIBUTED):
        return "mysql"
    return "memory"


def default_cron_enabled(mode: str) -> bool:
    """Cron 默认开关：distributed 默认关闭（多副本无选主，避免重复调度）。"""
    return normalize_deployment_mode(mode) != MODE_DISTRIBUTED


def channel_config_overlay_default(mode: str) -> bool:
    """channel_config DB overlay 是否启用：仅 active-standby（企业/K8s 主备）。

    distributed 与 standalone 一样直接读 ``config.yaml`` 的 ``channels``。
    """
    return normalize_deployment_mode(mode) == MODE_ACTIVE_STANDBY


def distributed_channel_whitelist() -> frozenset[str]:
    """distributed 模式允许启动的通道（tui 由 /tui 路由独立注册，不走 channels 段）。"""
    return frozenset({"web", "tui"})
