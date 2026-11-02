"""Memory / ExternalMemory / OfficeAce 用户画像 rail 的构建与热切换。"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
from openjiuwen.harness.rails import MemoryRail

from jiuwenswarm.agents.harness.common.memory.config import (
    get_embed_config,
    get_memory_mode,
    is_memory_enabled,
    is_proactive_memory,
)
from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
    is_builtin_memory_allowed,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.model_factory import (
    _embedding_config_fingerprint,
)

logger = logging.getLogger(__name__)


def _schedule_memory_reindex(adapter) -> None:
    """延时后对记忆重新索引（debounce：多次触发只跑最后一次）。

    前提：调用前 MemoryRail 已按新 embedding 配置重建（_embedding_config 已刷新），
    且 openjiuwen lite 的 INDEX_CACHE 已清（aclose_memory_manager_cache）。
    这样延时到期时 init_memory_manager_async 会用新配置建新 manager + 新 provider。
    """
    workspace = os.path.normcase(os.path.abspath(adapter._workspace_dir or ""))
    reindex_key = (workspace, adapter._memory_embedding_fingerprint)
    with adapter._MEMORY_REINDEX_KEYS_LOCK:
        if reindex_key in adapter._MEMORY_REINDEX_KEYS:
            logger.info(
                "[JiuWenSwarmDeepAdapter] memory reindex coalesced: workspace=%s",
                workspace,
            )
            return
        adapter._MEMORY_REINDEX_KEYS.add(reindex_key)
    if adapter._memory_reindex_task is not None and not adapter._memory_reindex_task.done():
        adapter._memory_reindex_task.cancel()
    adapter._memory_reindex_task = asyncio.create_task(
        adapter._do_memory_reindex(reindex_key)
    )


async def _do_memory_reindex(adapter, reindex_key: tuple[str, str]) -> None:
    try:
        await asyncio.sleep(adapter._MEMORY_REINDEX_DELAY_SECONDS)
        rail = adapter._memory_rail
        if rail is None:
            return
        manager = await adapter._get_current_memory_manager()
        if manager is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] memory reindex skipped: manager unavailable"
            )
            return
        await manager.sync(reason="embed_config_changed", force=True)
        logger.info(
            "[JiuWenSwarmDeepAdapter] memory reindexed after embedding config change"
        )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("[JiuWenSwarmDeepAdapter] memory reindex failed: %s", e)
    finally:
        with adapter._MEMORY_REINDEX_KEYS_LOCK:
            adapter._MEMORY_REINDEX_KEYS.discard(reindex_key)


async def _get_current_memory_manager(adapter):
    """获取当前 MemoryRail 对应的 memory manager（必要时按新配置重建）。

    rail 重建后其 _embedding_config 已是最新值；但 manager 在 rail 首次 invoke
    前可能尚未 init，这里主动调 init_memory_manager_async 触发初始化/复用。
    """
    from openjiuwen.core.memory.lite.memory_tools import init_memory_manager_async

    rail = adapter._memory_rail
    if rail is None:
        return None
    embedding_config = getattr(rail, "_embedding_config", None)
    workspace = adapter._get_memory_workspace()
    agent_id = getattr(getattr(adapter._instance, "card", None), "id", None) or "default"
    try:
        return await init_memory_manager_async(
            workspace=workspace,
            agent_id=agent_id,
            embedding_config=embedding_config,
            sys_operation=adapter._sys_operation,
        )
    except Exception as e:
        logger.warning("[JiuWenSwarmDeepAdapter] init memory manager failed: %s", e)
        return None


def _get_memory_workspace(adapter):
    """构造记忆用的 Workspace 对象（与 _make_deep_agent_config 中构造方式一致）。"""
    resolved_language = getattr(adapter, "_resolved_language", None) or "zh"
    return adapter._build_agent_workspace(language=resolved_language)


def _build_memory_rail(adapter, mode: str) -> MemoryRail | None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod
    try:
        config = (
            adapter._startup_config_base
            if isinstance(adapter._startup_config_base, dict)
            else deep_mod.get_config()
        )
        embed_config = get_embed_config()
        has_api_key = embed_config.get("api_key") if isinstance(embed_config, dict) else None
        has_base_url = embed_config.get("base_url") if isinstance(embed_config, dict) else None
        has_model = embed_config.get("model") if isinstance(embed_config, dict) else None
        if not all([has_api_key, has_base_url, has_model]):
            embed_cfg = config.get("embed") if isinstance(config, dict) else None
            embed_cfg = embed_cfg if isinstance(embed_cfg, dict) else {}
            embed_config = {
                "api_key": embed_cfg.get("embed_api_key"),
                "base_url": embed_cfg.get("embed_base_url") or embed_cfg.get("embed_api_base"),
                "model": embed_cfg.get("embed_model"),
            }
            has_api_key = embed_config.get("api_key")
            has_base_url = embed_config.get("base_url")
            has_model = embed_config.get("model")
        if not all([has_api_key, has_base_url, has_model]):
            logger.warning(
                "[JiuWenSwarmDeepAdapter] MemoryRail create failed: No available embedding config"
            )
        adapter._is_proactive_memory = is_proactive_memory(mode, config)
        memory_rail = MemoryRail(
            embedding_config=EmbeddingConfig(
                model_name=embed_config.get("model"),
                base_url=embed_config.get("base_url"),
                api_key=embed_config.get("api_key"),
            ),
            is_proactive=adapter._is_proactive_memory,
        )
        logger.info("[JiuWenSwarmDeepAdapter] MemoryRail create success")
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] MemoryRail create failed: %s", exc)
        memory_rail = None
    return memory_rail


def shutdown_context_session_memory(context_processor_rail: ContextProcessorRail) -> bool:
    """Stop overlapping semantic session memory through a stable Adapter API.

    三态区分（CR-5），保证日志不失实：

    - manager 在位：shutdown 并置 None，返回 True；
    - 属性为 None：未配置（框架默认，仅在
      session_memory_compressor_config.enabled=true 时创建）或已
      关闭的合法幂等态，debug 即可，不告警；
    - 属性缺失（agent-core 私有属性被改名 / rail 类型不符）：
      保留 CR-2 加固的 WARNING——SessionMemoryManager 可能仍在
      eternal conversation 旁 unnoticed 运行（双记忆后端风险）。

    上游 02957a531 对两种非在位状态都是静默 False；缺失属性告警
    是本地有意的加强。
    """

    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _MISSING_SESSION_MEMORY_MGR,
    )

    session_memory_manager = getattr(
        context_processor_rail, "_session_memory_mgr", _MISSING_SESSION_MEMORY_MGR
    )
    if session_memory_manager is _MISSING_SESSION_MEMORY_MGR:
        # _session_memory_mgr is a private agent-core attribute; a renamed
        # attribute or a wrong rail type would otherwise fail silently
        # and leave SessionMemoryManager writing beside eternal conversation.
        logger.warning(
            "[JiuWenSwarmDeepAdapter] shutdown_context_session_memory found no "
            "_session_memory_mgr attribute on %r; the framework may have renamed "
            "it and SessionMemoryManager may keep running beside eternal "
            "conversation (double memory backend)",
            type(context_processor_rail).__name__,
        )
        return False
    if session_memory_manager is None:
        # 合法幂等态：未配置或已成功关闭。上游在 None 时也静默返回
        # False；这里只留 debug 便于排障，不打 WARNING（CR-5）。
        logger.debug(
            "[JiuWenSwarmDeepAdapter] shutdown_context_session_memory: "
            "no active SessionMemoryManager on %r (not configured or "
            "already shut down)",
            type(context_processor_rail).__name__,
        )
        return False
    session_memory_manager.shutdown()
    setattr(context_processor_rail, "_session_memory_mgr", None)
    return True


async def _handle_memory_rail_by_config(adapter, mode: str):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod
    config = (
        adapter._startup_config_base
        if isinstance(adapter._startup_config_base, dict)
        else deep_mod.get_config()
    )
    if get_memory_mode(config) == "local":
        # 引擎门禁：memory.engine 未放行内置时，等同于禁用
        builtin_on = (
            not getattr(adapter, "_eternal_conversation_enabled", False)
            and is_builtin_memory_allowed(config)
            and is_memory_enabled(mode, config)
        )
        if builtin_on:
            # 开启记忆
            new_embed_fp = _embedding_config_fingerprint(config)
            previous_embed_fp = adapter._memory_embedding_fingerprint
            if adapter._memory_rail is not None:
                cur_memory_type = is_proactive_memory(mode, config)
                if adapter._is_proactive_memory != cur_memory_type:
                    # 当前记忆类型（主动/被动）和之前注册的不一致，重新注册
                    await adapter._instance.unregister_rail(adapter._memory_rail)
                    adapter._memory_rail = None
                elif adapter._memory_embedding_fingerprint != new_embed_fp:
                    # 记忆类型没变，但 embedding 配置（base_url/api_key/model）变了：
                    # 必须重建 rail 才能刷新 MemoryRail._embedding_config，否则换 endpoint
                    # 时 rail 仍持旧配置，新 manager 仍用旧 provider。
                    logger.info(
                        "[JiuWenSwarmDeepAdapter] embedding config changed, rebuilding MemoryRail"
                    )
                    await adapter._instance.unregister_rail(adapter._memory_rail)
                    adapter._memory_rail = None
                else:
                    # 已经注册，且记忆类型与 embedding 配置均未变，无需其他操作
                    return
            if adapter._memory_rail is None:
                adapter._memory_rail = adapter._build_memory_rail(mode)
            if adapter._memory_rail is not None:
                # 重建（或首次注册）后记录新指纹，作为下次比对的基线
                adapter._memory_embedding_fingerprint = new_embed_fp
                try:
                    await adapter._instance.register_rail(adapter._memory_rail)
                except Exception as e:
                    # register_rail 失败：回滚指纹与 rail，避免留下"指纹已是新值、
                    # 但 rail 未成功注册"的不一致状态——否则下次比对会认为"没变"而
                    # 走 return，让这个孤儿 rail 既不注册也不重建，记忆静默失效。
                    adapter._memory_embedding_fingerprint = ""
                    adapter._memory_rail = None
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] register MemoryRail failed: %s", e
                    )
                else:
                    logger.info(f"[JiuWenSwarmDeepAdapter] MemoryRail registered for {mode} mode")
                    # 重建后触发延时重索引（debounce），使新 embedding 配置对历史记忆文件生效
                    if previous_embed_fp and previous_embed_fp != new_embed_fp:
                        adapter._schedule_memory_reindex()
        elif not builtin_on and adapter._memory_rail is not None:
            await adapter._instance.unregister_rail(adapter._memory_rail)
            adapter._memory_rail = None
            logger.info(f"[JiuWenSwarmDeepAdapter] MemoryRail unregistered for {mode} mode")


def _build_external_memory_rail(adapter):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod
    from jiuwenswarm.agents.harness.common.memory.external_memory_builder import (
        build_external_memory_rail,
    )

    return build_external_memory_rail(
        config=deep_mod.get_config(),
        workspace_dir=adapter._workspace_dir,
        session_id=adapter._parent_session_id,
        thread_id=adapter._parent_thread_id,
    )


async def _handle_external_memory_rail_by_config(adapter):
    """Register / unregister ExternalMemoryRail based on config.

    External memory is mode-independent — configured once and active in
    the merged agent mode. `_external_memory_rail_registered` dedups
    repeated calls from `_update_agent_rails()`.
    Not part of `_get_current_agent_rails()`, so it is not torn down on
    config hot-reload (preserves prefetch cache + circuit breaker state).
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
        is_external_memory_enabled,
    )

    config = deep_mod.get_config()
    if is_external_memory_enabled(config) and not getattr(
        adapter, "_eternal_conversation_enabled", False
    ):
        if adapter._external_memory_rail_registered:
            return
        if adapter._external_memory_rail is None:
            adapter._external_memory_rail = adapter._build_external_memory_rail()
        if adapter._external_memory_rail is None:
            return
        try:
            await adapter._instance.register_rail(adapter._external_memory_rail)
            adapter._external_memory_rail_registered = True
            logger.info("[JiuWenSwarmDeepAdapter] ExternalMemoryRail registered")
        except Exception as exc:
            logger.error("[JiuWenSwarmDeepAdapter] ExternalMemoryRail register failed: %s", exc)
            adapter._external_memory_rail = None
    elif adapter._external_memory_rail is not None and adapter._external_memory_rail_registered:
        # Call on_session_end BEFORE unregister_rail: unregister -> uninit()
        # is sync, and run_coroutine_threadsafe from the same event loop
        # thread would deadlock.
        provider = getattr(adapter._external_memory_rail, "_provider", None)
        if provider is not None and hasattr(provider, "on_session_end"):
            try:
                await provider.on_session_end()
            except Exception as exc:
                logger.debug("[JiuWenSwarmDeepAdapter] on_session_end failed: %s", exc)
        try:
            await adapter._instance.unregister_rail(adapter._external_memory_rail)
            logger.info("[JiuWenSwarmDeepAdapter] ExternalMemoryRail unregistered")
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] ExternalMemoryRail unregister failed: %s", exc
            )
        adapter._external_memory_rail = None
        adapter._external_memory_rail_registered = False


def _build_office_ace_user_profile_rail():
    """Build OfficeAceUserProfileRail from config, or None if disabled/failed.

    启用条件由 external memory 开关 + provider=officeace_cloud + 凭证齐全隐含控制
    （``get_office_ace_user_profile_config`` 综合判定，``cfg["enabled"]`` 为结果）。
    不满足 → 返回 None（rail 不挂载，零残留）。
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
        get_office_ace_user_profile_config,
    )
    from jiuwenswarm.agents.harness.common.rails.office_ace_user_profile import (
        OfficeAceUserProfileRail,
        UserProfileConfig,
    )

    cfg = get_office_ace_user_profile_config(deep_mod.get_config())
    if not cfg["enabled"]:
        logger.info(
            "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail disabled "
            "(external memory off / provider not officeace_cloud / credentials incomplete)",
        )
        return None
    try:
        rail = OfficeAceUserProfileRail(UserProfileConfig(**cfg))
        logger.info(
            "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail create success "
            "(user=%s, endpoint=%s)",
            cfg["user_id"],
            cfg["endpoint"],
        )
        return rail
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail create failed: %s",
            exc,
        )
        return None


async def _handle_office_ace_user_profile_rail_by_config(adapter):
    """Register / unregister OfficeAceUserProfileRail based on config.

    Mode-independent（同 ExternalMemoryRail），不随 config 热重载销毁。
    画像按需拉取：rail 的 before_model_call 在本地缓存失效时才 fetch
    （per-agent user_id）。
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
        is_office_ace_user_profile_enabled,
    )

    config = deep_mod.get_config()
    if is_office_ace_user_profile_enabled(config):
        if adapter._office_ace_user_profile_rail_registered:
            return
        if adapter._office_ace_user_profile_rail is None:
            adapter._office_ace_user_profile_rail = (
                adapter._build_office_ace_user_profile_rail()
            )
        if adapter._office_ace_user_profile_rail is None:
            return
        try:
            await adapter._instance.register_rail(adapter._office_ace_user_profile_rail)
            adapter._office_ace_user_profile_rail_registered = True
            logger.info(
                "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail registered",
            )
        except Exception as exc:
            logger.error(
                "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail register failed: %s",
                exc,
            )
            adapter._office_ace_user_profile_rail = None
    elif (
        adapter._office_ace_user_profile_rail is not None
        and adapter._office_ace_user_profile_rail_registered
    ):
        try:
            await adapter._instance.unregister_rail(
                adapter._office_ace_user_profile_rail,
            )
            adapter._office_ace_user_profile_rail_registered = False
            logger.info(
                "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail unregistered",
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] OfficeAceUserProfileRail unregister failed: %s",
                exc,
            )
        adapter._office_ace_user_profile_rail = None
