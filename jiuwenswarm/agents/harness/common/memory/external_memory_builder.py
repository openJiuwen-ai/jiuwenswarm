# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Builder for ExternalMemoryRail — single entry point, config-driven.

Dispatches on `memory.external.provider`:
  - openjiuwen  -> OpenJiuwenMemoryProvider (builds its own KV/Vector/DB from config)
  - mem0        -> Mem0MemoryProvider
  - openviking  -> OpenVikingMemoryProvider
  - <plugin>    -> user-installed plugin from ~/.jiuwenswarm/plugins/memory/
  - ""          -> disabled (returns None)

Any failure returns None — the main flow is never blocked.
"""

import logging
import os
from typing import Any, Dict, Optional

from .external_memory_config import (
    build_openjiuwen_provider_config,
    get_external_memory_config,
)

logger = logging.getLogger(__name__)

_BUILTIN_PROVIDERS = {"openjiuwen", "mem0", "openviking", "officeace_cloud"}


def build_external_memory_rail(
    config: Optional[Dict[str, Any]] = None,
    workspace_dir: str = ".",
    session_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Optional[Any]:
    """Build an ExternalMemoryRail from config, or None if disabled/failed.

    Args:
        config: Full config dict (memory.external.* selects the provider).
        workspace_dir: Agent workspace directory.
        session_id: relay-claw runtime session id (thread_id 的 sha256 哈希)。
            officeace_cloud 用它做云端 memory session；其他 provider 忽略。
        thread_id: 业务对话 ID（前端 thread_<ts+random>）。PC 端 OfficeAce 记忆
            sync_turn 上报 pc-threads/{thread_id}/messages 用它。云端 provider 不消费。
    """
    try:
        from openjiuwen.harness.rails import ExternalMemoryRail
    except Exception as exc:
        logger.warning("[ExternalMemoryBuilder] ExternalMemoryRail import failed: %s", exc)
        return None

    ext_cfg = get_external_memory_config(config)
    provider_name = ext_cfg.get("provider", "")
    if not provider_name:
        return None

    provider = None
    try:
        if provider_name == "openjiuwen":
            provider = _build_openjiuwen_provider(ext_cfg, config)
        elif provider_name == "mem0":
            provider = _build_mem0_provider(ext_cfg)
        elif provider_name == "openviking":
            provider = _build_openviking_provider(ext_cfg)
        elif provider_name == "lakebase":
            provider = _build_lakebase_provider(ext_cfg)
        elif provider_name == "officeace_cloud":
            provider = _build_officeace_cloud_provider(ext_cfg)
        else:
            provider = _load_plugin_provider(provider_name, ext_cfg.get("allowed_plugins") or None)
    except Exception as exc:
        logger.warning(
            "[ExternalMemoryBuilder] build provider '%s' failed: %s",
            provider_name, exc,
        )
        return None

    if provider is None:
        return None

    # Only officeace_cloud consumes the per-session session_id (for its cloud
    # memory session). Other providers (openjiuwen/mem0/openviking/lakebase)
    # must keep their original "__default__" session_id — passing a real
    # session id would change how they tag/scope stored messages, breaking
    # the "do not affect other providers" requirement.
    if provider_name == "officeace_cloud" and session_id:
        rail_session_id = session_id
    else:
        rail_session_id = "__default__"

    # thread_id 仅 officeace_cloud PC 端消费（sync_turn 上报）。
    rail_thread_id = thread_id if provider_name == "officeace_cloud" else None

    try:
        rail = ExternalMemoryRail(
            provider,
            user_id=ext_cfg.get("user_id", "__default__"),
            scope_id=ext_cfg.get("scope_id", "__default__"),
            session_id=rail_session_id,
            thread_id=rail_thread_id,
        )
        logger.info(
            "[ExternalMemoryBuilder] ExternalMemoryRail built (provider=%s, session_id=%s, thread_id=%s)",
            provider_name,
            (rail_session_id or "default"),
            (rail_thread_id or "(none)"),
        )
        return rail
    except Exception as exc:
        logger.warning("[ExternalMemoryBuilder] rail construction failed: %s", exc)
        return None


def _build_openjiuwen_provider(ext_cfg: Dict[str, Any], full_config: Optional[Dict[str, Any]] = None):
    from openjiuwen.core.memory.external.openjiuwen_memory_provider import (
        OpenJiuwenMemoryProvider,
    )
    provider_config, scope_config = build_openjiuwen_provider_config(ext_cfg, full_config)
    return OpenJiuwenMemoryProvider(config=provider_config, scope_config=scope_config)


def _build_mem0_provider(ext_cfg: Dict[str, Any]):
    from openjiuwen.core.memory.external.mem0_provider import Mem0MemoryProvider

    mem0_cfg = ext_cfg.get("mem0") or {}
    api_key = mem0_cfg.get("api_key") or os.environ.get("MEM0_API_KEY", "")
    user_id = mem0_cfg.get("user_id") or os.environ.get("MEM0_USER_ID", "jiuwenswarm-user")
    agent_id = mem0_cfg.get("agent_id") or os.environ.get("MEM0_AGENT_ID", "jiuwenswarm")
    rerank = bool(mem0_cfg.get("rerank", True))

    provider = Mem0MemoryProvider(
        api_key=api_key,
        user_id=user_id,
        agent_id=agent_id,
        rerank=rerank,
    )
    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] Mem0 unavailable (no API key)")
        return None
    return provider


def _build_openviking_provider(ext_cfg: Dict[str, Any]):
    from openjiuwen.core.memory.external.openviking_memory_provider import (
        OpenVikingMemoryProvider,
    )

    vk_cfg = ext_cfg.get("openviking") or {}
    endpoint = vk_cfg.get("endpoint") or os.environ.get("OPENVIKING_ENDPOINT", "")
    api_key = vk_cfg.get("api_key") or os.environ.get("OPENVIKING_API_KEY", "")
    account = vk_cfg.get("account") or os.environ.get("OPENVIKING_ACCOUNT", "root")
    user = vk_cfg.get("user") or os.environ.get("OPENVIKING_USER", "default")

    provider = OpenVikingMemoryProvider(
        endpoint=endpoint,
        api_key=api_key,
        account=account,
        user=user,
    )
    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] OpenViking unavailable (no endpoint)")
        return None
    return provider


def _build_lakebase_provider(ext_cfg: Dict[str, Any]):
    """Build LakeBase (DBay) external memory provider.

    LakeBase provides:
    - Semantic memory storage and retrieval via pgvector
    - Multiple memory types (fact, episode, procedural, etc.)
    - Trait extraction via digest API
    - Multi-workspace support via base switching

    Config shape (memory.external.lakebase):
        api_key: str       # LakeBase API key (required)
        base_url: str      # LakeBase API endpoint (default: localhost:8080)
        base_id: str       # Memory base ID (workspace)
        database_id: str   # Database ID for branching
        timeout: float     # HTTP request timeout
    """
    from openjiuwen.core.memory.external.lakebase_memory_provider import (
        LakeBaseMemoryProvider,
    )

    lb_cfg = ext_cfg.get("lakebase") or {}
    api_key = lb_cfg.get("api_key") or os.environ.get("LAKEBASE_API_KEY", "")
    base_url = lb_cfg.get("base_url") or os.environ.get(
        "LAKEBASE_API_URL", "http://localhost:8080/api/v1"
    )
    base_id = lb_cfg.get("base_id") or os.environ.get("LAKEBASE_MEM_BASE_ID", "mem_default")
    database_id = lb_cfg.get("database_id") or os.environ.get(
        "LAKEBASE_DATABASE_ID", "db_agent_memory"
    )
    timeout = float(lb_cfg.get("timeout") or 60.0)

    if not api_key:
        logger.warning("[ExternalMemoryBuilder] LakeBase unavailable (no api_key)")
        return None

    provider = LakeBaseMemoryProvider(
        api_key=api_key,
        base_url=base_url,
        base_id=base_id,
        database_id=database_id,
        timeout=timeout,
    )

    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] LakeBase unavailable (config incomplete)")
        return None

    logger.info(
        "[ExternalMemoryBuilder] LakeBase provider built: base_url=%s, base_id=%s",
        base_url, base_id,
    )
    return provider


def _is_cloud_deployment() -> bool:
    """True if running in cloud deployment form.

    云端（``OFFICE_ACE_DEPLOYMENT=cloud``）与 PC 端（``pc`` 或缺省）的区分：
    * 云端：记忆搜索走 AgentArts SDK，sync_turn 不上报（由 relay-claw 直报）。
    * PC 端：记忆搜索走 chat-service appapi，sync_turn 走 pc-threads 上报。
    """
    return os.environ.get("OFFICE_ACE_DEPLOYMENT", "pc").strip().lower() == "cloud"


def _build_officeace_cloud_provider(
    ext_cfg: Dict[str, Any],
):
    """Build OfficeAce memory provider (cloud or PC form).

    OfficeAce memory is a long-term memory service shared by cloud and PC
    deployments. 凭据/endpoint 直接读 config/env（无 pre-session 级别）——
    relay-claw 不再经 chat.send params 下发 per-session 凭据，provider 构造时
    一次性绑定静态配置。

    Deployment dispatch:
        cloud → :class:`OfficeAceMemoryCloudProvider` (AgentArts SDK search,
            sync_turn no-op; relay-claw reports conversations directly).
        pc    → :class:`OfficeAceMemoryPcProvider` (chat-service appapi search
            + pc-threads messages sync_turn).

    Args:
        ext_cfg: ``memory.external`` config slice (contains the
            ``officeace_cloud`` sub-section).

    Config shape (memory.external.officeace_cloud):
        base_url: str    # OfficeAce memory endpoint
        api_key: str     # 凭据（config 或 AGENTARTS_MEMORY_API_KEY 环境变量）
        space_id: str    # space/library id（云端消费，PC 端不用）
    """
    oa_cfg = ext_cfg.get("officeace_cloud") or {}
    base_url = oa_cfg.get("base_url") or os.environ.get("AGENTARTS_MEMORY_BASE_URL", "")
    api_key = oa_cfg.get("api_key") or os.environ.get("AGENTARTS_MEMORY_API_KEY", "")
    space_id = oa_cfg.get("space_id") or os.environ.get("AGENTARTS_MEMORY_SPACE_ID", "")
    actor_id = ext_cfg.get("user_id") or ""

    if _is_cloud_deployment():
        from openjiuwen.core.memory.external.office_ace_memory_cloud_provider import (
            OfficeAceMemoryCloudProvider,
        )

        provider = OfficeAceMemoryCloudProvider(
            base_url=base_url or None,
            api_key=api_key,
            space_id=space_id,
            actor_id=actor_id,
        )
        logger.info(
            "[ExternalMemoryBuilder] OfficeAce cloud provider built: "
            "base_url=%s, api_key=%s, space_id=%s",
            base_url, bool(api_key), bool(space_id),
        )
        return provider

    from openjiuwen.core.memory.external.office_ace_memory_pc_provider import (
        OfficeAceMemoryPcProvider,
    )

    provider = OfficeAceMemoryPcProvider(
        base_url=base_url or None,
        api_key=api_key,
        actor_id=actor_id,
    )
    logger.info(
        "[ExternalMemoryBuilder] OfficeAce pc provider built: "
        "base_url=%s, api_key=%s, actor_id=%s",
        base_url, bool(api_key), actor_id or "(none)",
    )
    return provider


def _load_plugin_provider(name: str, allowed: Optional[list] = None):
    try:
        from .plugin_discovery import load_memory_plugin
    except ImportError:
        logger.warning(
            "[ExternalMemoryBuilder] plugin '%s' requested but plugin_discovery not yet available",
            name,
        )
        return None
    return load_memory_plugin(name, allowed_plugins=allowed)
