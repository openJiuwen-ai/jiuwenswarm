# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""按 ``is_enterprise()`` 装配 StorageContext（个人版/企业版）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.storage.backends.db.persistent_store import DbPersistentBackend
from jiuwenswarm.gateway.storage.backends.file_persistent import FilePersistentBackend
from jiuwenswarm.gateway.storage.backends.memory_ephemeral import MemoryEphemeralBackend
from jiuwenswarm.gateway.storage.backends.redis_ephemeral import RedisEphemeralBackend
from jiuwenswarm.gateway.storage.context import StorageContext
from jiuwenswarm.gateway.storage.errors import StorageUnavailableError
from jiuwenswarm.gateway.storage.protocols.ephemeral import EphemeralStore
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore
from jiuwenswarm.gateway.storage_assembly.db_connection import assert_replicas_db_compat
from jiuwenswarm.gateway.storage_assembly.layouts import build_gateway_store_registry

logger = logging.getLogger(__name__)


def _on_config_written(path: Path) -> None:
    try:
        from jiuwenswarm.common.config import clear_config_cache
        from jiuwenswarm.common.utils import get_config_file

        if path.resolve() == get_config_file().resolve():
            clear_config_cache()
    except Exception as exc:
        logger.warning("clear config cache after write failed: %s", exc)


def _redis_client() -> Any | None:
    try:
        from jiuwenswarm.extensions.redis.redis_runtime import get_gateway_redis_client

        client = get_gateway_redis_client()
    except Exception:
        client = None
    return client


def _create_persistent() -> PersistentStore:
    if is_enterprise():
        from jiuwenswarm.gateway.storage_assembly.db_connection import GatewayDbConnection

        assert_replicas_db_compat()
        return DbPersistentBackend(
            GatewayDbConnection(),
            build_gateway_store_registry(),
        )

    from jiuwenswarm.common.utils import (
        get_checkpoint_dir,
        get_config_file,
        get_user_workspace_dir,
        resolve_gateway_cron_jobs_path_template,
    )

    persistent_root = get_user_workspace_dir() / "gateway" / "persistent"
    config_file = get_config_file()
    session_map_file = get_checkpoint_dir() / "session_map.json"
    return FilePersistentBackend(
        registry=build_gateway_store_registry(
            persistent_root=persistent_root,
            config_file=config_file,
            session_map_file=session_map_file,
            cron_jobs_path_template=resolve_gateway_cron_jobs_path_template(),
        ),
        on_write=_on_config_written,
    )


def _require_redis_client() -> Any:
    client = _redis_client()
    if client is None:
        raise StorageUnavailableError(
            "enterprise edition requires Redis for ephemeral storage"
        )
    return client


def _create_ephemeral_factory():
    if not is_enterprise():
        def memory_factory(namespace: str) -> EphemeralStore:
            return MemoryEphemeralBackend(namespace)

        return memory_factory

    client = _require_redis_client()

    def factory(namespace: str) -> EphemeralStore:
        return RedisEphemeralBackend(
            namespace,
            client=client,
            key_prefix="gw:",
        )

    return factory


def resolve_storage_instance_id(cfg: dict[str, Any] | None = None) -> str:
    """每网关独立 DB，装配层固定传空 ``instance_id``（不再解析 Manager/env 身份）。"""
    _ = cfg
    return ""


def _storage_flag(
    cfg: dict[str, Any] | None,
    *,
    config_key: str,
    env_key: str,
    default: bool = True,
) -> bool:
    if cfg is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
    storage = (cfg.get("gateway") or {}).get("storage") or {}
    if config_key in storage:
        return bool(storage[config_key])
    env = os.getenv(env_key, "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return default


def is_storage_repositories_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """channel / permissions / logging / memory / cron 是否切到 PersistentStore。"""
    return _storage_flag(
        cfg,
        config_key="repositories",
        env_key="GATEWAY_STORAGE_REPOSITORIES",
        default=True,
    )


def is_session_map_repository_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """SessionMap 是否切到 PersistentStore Repository（其它域不受影响）。"""
    return _storage_flag(
        cfg,
        config_key="session_map_repository",
        env_key="GATEWAY_SESSION_MAP_REPOSITORY",
        default=True,
    )


def is_ephemeral_state_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """session_sharing / cron_scheduler 是否切到 EphemeralStore。"""
    return _storage_flag(
        cfg,
        config_key="ephemeral",
        env_key="GATEWAY_EPHEMERAL_STATE",
        default=True,
    )


def ensure_gateway_storage_context_for_ephemeral(
    cfg: dict[str, Any] | None = None,
    *,
    existing: StorageContext | None = None,
) -> StorageContext | None:
    """Ephemeral 需要 StorageContext；可与 Persistent 装配共用同一实例。"""
    if existing is not None:
        return existing
    if not is_ephemeral_state_enabled(cfg):
        return None
    return create_gateway_storage_context(cfg)


async def create_session_sharing_registry(
    ctx: StorageContext,
) -> Any:
    """装配 SessionSharingRegistry 并从 Ephemeral 恢复订阅（若有）。"""
    from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry

    registry = SessionSharingRegistry(ephemeral=ctx.ephemeral("session_sharing"))
    await registry.hydrate_from_ephemeral()
    return registry


def cron_run_ephemeral_store(ctx: StorageContext) -> Any:
    """Cron run 快照使用的 Ephemeral namespace。"""
    return ctx.ephemeral("cron_scheduler")


def _wire_personal_yaml_section_repositories(
    store: PersistentStore,
    cfg: dict[str, Any] | None,
) -> None:
    """personal-only：heartbeat / browser / locale / a2ui YAML overlay。"""
    from jiuwenswarm.gateway.config.a2ui.access import set_a2ui_config_repository
    from jiuwenswarm.gateway.config.browser.access import set_browser_config_repository
    from jiuwenswarm.gateway.config.heartbeat.access import (
        set_heartbeat_config_repository,
    )
    from jiuwenswarm.gateway.config.locale.access import (
        set_preferred_language_config_repository,
    )

    if is_enterprise():
        return

    instance_id = ""
    set_heartbeat_config_repository(
        create_heartbeat_config_repository(store, instance_id=instance_id)
    )
    set_browser_config_repository(
        create_browser_config_repository(store, instance_id=instance_id)
    )
    set_preferred_language_config_repository(
        create_preferred_language_config_repository(store, instance_id=instance_id)
    )
    set_a2ui_config_repository(
        create_a2ui_config_repository(store, instance_id=instance_id)
    )


def _wire_config_and_cron_repositories(
    store: PersistentStore,
    cfg: dict[str, Any] | None,
) -> None:
    """注入 channel / permissions / logging / memory Repository 与 cron PersistentStore。"""
    from jiuwenswarm.gateway.config.channel.access import set_channel_config_repository
    from jiuwenswarm.gateway.config.logging.access import set_logging_config_repository
    from jiuwenswarm.gateway.config.memory.access import set_memory_config_repository
    from jiuwenswarm.gateway.config.permissions.access import (
        set_permissions_config_repository,
    )
    from jiuwenswarm.gateway.cron.job_access import set_cron_persistent_store
    from jiuwenswarm.gateway.storage.access import set_persistent_store

    instance_id = ""
    set_channel_config_repository(
        create_channel_config_repository(store, instance_id=instance_id)
    )
    set_permissions_config_repository(
        create_permissions_config_repository(store, instance_id=instance_id)
    )
    set_logging_config_repository(
        create_logging_config_repository(store, instance_id=instance_id)
    )
    set_memory_config_repository(
        create_memory_config_repository(store, instance_id=instance_id)
    )
    set_persistent_store(store)
    set_cron_persistent_store(store)
    _wire_personal_yaml_section_repositories(store, cfg)


def _clear_config_and_cron_repositories() -> None:
    from jiuwenswarm.gateway.config.a2ui.access import clear_a2ui_config_repository
    from jiuwenswarm.gateway.config.browser.access import clear_browser_config_repository
    from jiuwenswarm.gateway.config.channel.access import clear_channel_config_repository
    from jiuwenswarm.gateway.config.enterprise.access import (
        clear_enterprise_record_repositories,
    )
    from jiuwenswarm.gateway.config.heartbeat.access import (
        clear_heartbeat_config_repository,
    )
    from jiuwenswarm.gateway.config.locale.access import (
        clear_preferred_language_config_repository,
    )
    from jiuwenswarm.gateway.config.logging.access import clear_logging_config_repository
    from jiuwenswarm.gateway.config.memory.access import clear_memory_config_repository
    from jiuwenswarm.gateway.config.permissions.access import (
        clear_permissions_config_repository,
    )
    from jiuwenswarm.gateway.cron.job_access import clear_cron_persistent_store
    from jiuwenswarm.gateway.storage.access import clear_persistent_store

    clear_channel_config_repository()
    clear_permissions_config_repository()
    clear_logging_config_repository()
    clear_memory_config_repository()
    clear_heartbeat_config_repository()
    clear_browser_config_repository()
    clear_preferred_language_config_repository()
    clear_a2ui_config_repository()
    clear_enterprise_record_repositories()
    clear_persistent_store()
    clear_cron_persistent_store()


async def setup_gateway_storage_repositories(
    cfg: dict[str, Any] | None = None,
) -> StorageContext | None:
    """按开关装配 SessionMap + overlay/cron Repository，返回共享 ``StorageContext``。

    - ``session_map_repository``：SessionMap
    - ``repositories``：channel / permissions / logging / memory / cron；
      personal 另注入 heartbeat / browser / locale / a2ui

    任一开启则创建 Context；全关返回 ``None``。须在 MessageHandler / CronTenantRegistry
    创建 tenant store 之前调用。
    """
    session_on = is_session_map_repository_enabled(cfg)
    repos_on = is_storage_repositories_enabled(cfg)
    if not session_on and not repos_on:
        return None
    if cfg is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()

    ctx = create_gateway_storage_context(cfg)
    store = await ctx.persistent()

    if session_on:
        from jiuwenswarm.gateway.routing.session_map_access import (
            set_session_map_repository,
        )

        set_session_map_repository(create_session_map_repository(store))

    if repos_on:
        _wire_config_and_cron_repositories(store, cfg)

    if is_enterprise():
        from jiuwenswarm.gateway.config.enterprise.access import (
            set_enterprise_record_repositories,
        )

        set_enterprise_record_repositories(
            create_enterprise_record_repositories(
                store,
                instance_id="",
            )
        )

    return ctx


async def teardown_gateway_storage_repositories(ctx: StorageContext) -> None:
    from jiuwenswarm.gateway.routing.session_map_access import clear_session_map_repository

    clear_session_map_repository()
    _clear_config_and_cron_repositories()
    await ctx.shutdown()


def ensure_enterprise_storage_context(
    cfg: dict[str, Any] | None = None,
    *,
    existing: StorageContext | None = None,
) -> StorageContext:
    """企业版至少持有 ``StorageContext``（企业配置写路径经 ``PersistentStore``）。"""
    if existing is not None:
        return existing
    if cfg is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
    if not is_enterprise():
        raise ValueError("ensure_enterprise_storage_context requires enterprise edition")
    return create_gateway_storage_context(cfg)


def wire_enterprise_persistent_repositories(
    ctx: StorageContext,
    cfg: dict[str, Any] | None = None,
) -> None:
    """同步包装：见 ``wire_enterprise_persistent_repositories_async``。"""
    from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

    run_awaitable(wire_enterprise_persistent_repositories_async(ctx, cfg))


async def wire_enterprise_persistent_repositories_async(
    ctx: StorageContext,
    cfg: dict[str, Any] | None = None,
) -> None:
    """企业版：注入 PersistentStore 与企业配置 / cron 等 Repository。"""
    if cfg is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
    if not is_enterprise():
        return
    store = await ctx.persistent()
    from jiuwenswarm.gateway.config.enterprise.access import (
        set_enterprise_record_repositories,
    )
    from jiuwenswarm.gateway.cron.job_access import (
        get_cron_persistent_store,
        set_cron_persistent_store,
    )
    from jiuwenswarm.gateway.storage.access import (
        get_persistent_store,
        set_persistent_store,
    )

    set_enterprise_record_repositories(
        create_enterprise_record_repositories(
            store,
            instance_id="",
        )
    )
    if get_persistent_store() is None:
        set_persistent_store(store)
    if get_cron_persistent_store() is None:
        set_cron_persistent_store(store)

    from jiuwenswarm.gateway.config.channel.access import get_channel_config_repository

    if get_channel_config_repository() is None:
        _wire_config_and_cron_repositories(store, cfg)


async def setup_session_map_repository(
    cfg: dict[str, Any] | None = None,
) -> StorageContext | None:
    """装配 SessionMap Repository（兼容旧入口；其它域见 ``setup_gateway_storage_repositories``）。"""
    if not is_session_map_repository_enabled(cfg):
        return None
    if cfg is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
    ctx = create_gateway_storage_context(cfg)
    store = await ctx.persistent()
    from jiuwenswarm.gateway.routing.session_map_access import set_session_map_repository

    set_session_map_repository(create_session_map_repository(store))
    return ctx


async def teardown_session_map_repository(ctx: StorageContext) -> None:
    from jiuwenswarm.gateway.routing.session_map_access import clear_session_map_repository

    clear_session_map_repository()
    await ctx.shutdown()


def create_gateway_storage_context(cfg: dict[str, Any] | None = None) -> StorageContext:
    """按 ``is_enterprise()`` 组装进程级 ``StorageContext``（持久化 + 临时态入口）。

    - 企业版/个人版由 ``is_enterprise()`` 判定
    - 选择 Persistent backend：personal → 文件；enterprise → DB
    - 选择 Ephemeral 工厂：personal → 内存；enterprise → Redis

    返回的 Context 提供 ``persistent()`` / ``ephemeral(ns)``。
    不创建 channel / logging 等 Repository（由 ``create_*_repository`` 负责）。
    """
    if cfg is None:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
    return StorageContext(
        _create_persistent(),
        ephemeral_factory=_create_ephemeral_factory(),
    )


def create_channel_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    """按 ``is_enterprise()`` 选 Codec，业务侧拿到的是同一个 Repository。"""
    from jiuwenswarm.gateway.config.channel import (
        ChannelConfigRepository,
        DbRowChannelCodec,
        YamlMapChannelCodec,
    )

    if is_enterprise():
        codec = DbRowChannelCodec(instance_id=instance_id)
    else:
        codec = YamlMapChannelCodec()
    return ChannelConfigRepository(store, codec)


def create_session_map_repository(store: PersistentStore):
    from jiuwenswarm.gateway.routing.session_map_repository import SessionMapRepository

    return SessionMapRepository(store)


def create_a2a_outbound_repository(
    store: PersistentStore,
):
    """Create the edition-specific outbound Repository."""
    from jiuwenswarm.gateway.a2a_manager.outbound import (
        A2AOutboundRepository,
        EnterpriseA2AProjection,
        JsonA2AOutboundRecordCodec,
    )

    if is_enterprise():
        return EnterpriseA2AProjection(
            store,
            templates=create_enterprise_record_repository(
                store, "a2a_outbound_template"
            ),
            user_states=create_enterprise_record_repository(
                store, "a2a_outbound_user_state"
            ),
            runtime_states=create_enterprise_record_repository(
                store, "a2a_outbound_runtime_state"
            ),
        )
    return A2AOutboundRepository(store, JsonA2AOutboundRecordCodec())


def create_permissions_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    """permissions 仓库：仅 yaml 段（企业策略走 permissions_template 槽位，不再落 permissions_config 表）。"""
    from jiuwenswarm.gateway.config.permissions import (
        PermissionsConfigRepository,
        YamlSectionCodec,
    )

    return PermissionsConfigRepository(
        store, YamlSectionCodec(), instance_id=instance_id
    )


def create_logging_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    from jiuwenswarm.gateway.config.logging import (
        LoggingConfigRepository,
        YamlSectionCodec,
        db_logging_codec,
    )

    codec = (
        db_logging_codec()
        if is_enterprise()
        else YamlSectionCodec()
    )
    return LoggingConfigRepository(store, codec, instance_id=instance_id)


def create_memory_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    from jiuwenswarm.gateway.config.memory import (
        DbBodySectionCodec,
        MemoryConfigRepository,
        YamlSectionCodec,
    )

    codec = (
        DbBodySectionCodec()
        if is_enterprise()
        else YamlSectionCodec()
    )
    return MemoryConfigRepository(store, codec, instance_id=instance_id)


def _require_personal_config_store(store_name: str) -> None:
    """heartbeat / browser / preferred_language / a2ui 无企业同构表。"""
    if is_enterprise():
        raise ValueError(
            f"{store_name} is personal-only (YAML overlay); "
            "no enterprise DB table"
        )


def create_heartbeat_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    from jiuwenswarm.gateway.config.heartbeat import (
        HeartbeatConfigRepository,
        YamlSectionCodec,
    )

    _require_personal_config_store("heartbeat_config")
    return HeartbeatConfigRepository(
        store, YamlSectionCodec(), instance_id=instance_id
    )


def create_browser_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    from jiuwenswarm.gateway.config.browser import (
        BrowserConfigRepository,
        YamlSectionCodec,
    )

    _require_personal_config_store("browser_config")
    return BrowserConfigRepository(
        store, YamlSectionCodec(), instance_id=instance_id
    )


def create_preferred_language_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    from jiuwenswarm.gateway.config.locale import (
        PreferredLanguageConfigRepository,
        YamlSectionCodec,
    )

    _require_personal_config_store("preferred_language_config")
    return PreferredLanguageConfigRepository(
        store, YamlSectionCodec(), instance_id=instance_id
    )


def create_a2ui_config_repository(
    store: PersistentStore,
    *,
    instance_id: str = "",
):
    from jiuwenswarm.gateway.config.a2ui import (
        A2uiConfigRepository,
        YamlSectionCodec,
    )

    _require_personal_config_store("a2ui_config")
    return A2uiConfigRepository(
        store, YamlSectionCodec(), instance_id=instance_id
    )


def create_enterprise_record_repository(
    store: PersistentStore,
    store_name: str,
    *,
    instance_id: str = "",
):
    """企业专属表通用 Repository（仅 DB）。"""
    from jiuwenswarm.gateway.config.enterprise import EnterpriseRecordRepository

    return EnterpriseRecordRepository(
        store,
        store_name,
        instance_id=instance_id,
    )


def create_enterprise_record_repositories(
    store: PersistentStore,
    *,
    instance_id: str = "",
) -> dict[str, Any]:
    """为全部企业专属 store name 创建 ``EnterpriseRecordRepository``。

    返回 ``{store_name: repo}``；企业启动时由
    ``set_enterprise_record_repositories`` / ``wire_enterprise_persistent_repositories_async`` 注入。
    """
    from jiuwenswarm.gateway.config.enterprise.catalog import (
        ENTERPRISE_RECORD_STORE_NAMES,
    )

    return {
        name: create_enterprise_record_repository(
            store, name, instance_id=instance_id
        )
        for name in ENTERPRISE_RECORD_STORE_NAMES
    }


__all__ = [
    "create_a2a_outbound_repository",
    "create_a2ui_config_repository",
    "create_browser_config_repository",
    "create_channel_config_repository",
    "create_enterprise_record_repository",
    "create_enterprise_record_repositories",
    "create_gateway_storage_context",
    "create_heartbeat_config_repository",
    "create_logging_config_repository",
    "create_memory_config_repository",
    "create_permissions_config_repository",
    "create_preferred_language_config_repository",
    "create_session_map_repository",
    "create_session_sharing_registry",
    "cron_run_ephemeral_store",
    "ensure_enterprise_storage_context",
    "ensure_gateway_storage_context_for_ephemeral",
    "is_ephemeral_state_enabled",
    "is_session_map_repository_enabled",
    "is_storage_repositories_enabled",
    "resolve_storage_instance_id",
    "setup_gateway_storage_repositories",
    "setup_session_map_repository",
    "teardown_gateway_storage_repositories",
    "teardown_session_map_repository",
    "wire_enterprise_persistent_repositories",
    "wire_enterprise_persistent_repositories_async",
]
