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
from .config import _load_config, get_embed_config

logger = logging.getLogger(__name__)

_BUILTIN_PROVIDERS = {"openjiuwen", "mem0", "openviking"}


def build_external_memory_rail(
    config: Optional[Dict[str, Any]] = None,
    workspace_dir: str = ".",
) -> Optional[Any]:
    """Build an ExternalMemoryRail from config, or None if disabled/failed."""
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
        elif provider_name == "jiuwenmemory":
            provider = _build_jiuwen_provider(ext_cfg, config)
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

    try:
        rail = ExternalMemoryRail(
            provider,
            user_id=ext_cfg.get("user_id", "__default__"),
            scope_id=ext_cfg.get("scope_id", "__default__"),
        )
        logger.info(
            "[ExternalMemoryBuilder] ExternalMemoryRail built (provider=%s)",
            provider_name,
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


def _build_jiuwen_provider(ext_cfg: Dict[str, Any], full_config: Optional[Dict[str, Any]] = None):
    """Build Jiuwen (agent-memory) external memory provider — server or sdk.

    Two modes, picked by ``memory.external.jiuwen.mode``:

    - ``server`` (default): talks to a remote ``agent-memory`` HTTP service over
      httpx — ``POST /v1/<verb>``. No local engine; just a network client.
      ``httpx`` is a jiuwenswarm core dependency.
    - ``sdk``: builds the ``agent-memory`` kernel **in-process** via
      ``from api import assemble`` and calls it directly — no HTTP hop. The
      ``jiuwen.sdk`` config block is the agent-memory two-level namespace dict
      (globals + kv_store/fulltext_store/vector_store/llm/embedder/tokenizer);
      credentials (llm_api_key / embedder_api_key) live under ``globals`` because
      that's where agent-memory's build functions read them. Requires
      ``agent-memory`` installed (``pip install JiuwenMemory``).

    Common config (both modes):
        mode:                str   # server | sdk (default server)
        tenant_id:           str   # org axis of Scope (default 'default')
        infer_turns:         bool  # distill user turns into facts (default True)
        save_assistant_turns: bool # also persist assistant reply (default False)

    server-only (memory.external.jiuwen.server):
        base_url:     str   # agent-memory server URL (default http://127.0.0.1:8137)
        api_key:      str   # optional bearer token
        read_timeout:  float
        write_timeout: float

    sdk-only (memory.external.jiuwen.sdk): the agent-memory two-level namespace
    dict (globals + per-namespace target/params), passed straight to
    ``JiuwenMemoryProvider(config_dict=...)``.

    user_id is normally resolved at initialize() time from the rail (top-level
    memory.external.user_id maps to mem2's scope); an explicit override is
    accepted for parity with the other providers.
    """
    from openjiuwen.core.memory.external.jiuwen_memory_provider import (
        JiuwenMemoryProvider,
    )

    j_cfg = ext_cfg.get("jiuwen") or {}
    mode = str(j_cfg.get("mode") or os.environ.get("JIUWEN_MEMORY_MODE", "server")).strip().lower()
    tenant_id = j_cfg.get("tenant_id") or os.environ.get("JIUWEN_MEMORY_TENANT_ID", "default")
    user_id = j_cfg.get("user_id") or os.environ.get("JIUWEN_MEMORY_USER_ID", "")
    infer_turns = bool(j_cfg.get("infer_turns", True))
    save_assistant_turns = bool(j_cfg.get("save_assistant_turns", False))

    if mode == "sdk":
        # jiuwen.sdk holds the flat kv/vector/db backend selection; LLM and
        # embedder are reused from jiuwenswarm's own config (models.defaults +
        # embed section). _build_jiuwen_sdk_config_dict stitches these into the
        # agent-memory two-level namespace dict.
        sdk_cfg = j_cfg.get("sdk") or {}
        config_dict = _build_jiuwen_sdk_config_dict(sdk_cfg, full_config)
        provider = JiuwenMemoryProvider(
            mode="sdk",
            tenant_id=tenant_id,
            user_id=user_id,
            config_dict=config_dict,
            infer_turns=infer_turns,
            save_assistant_turns=save_assistant_turns,
        )
        logger.info(
            "[ExternalMemoryBuilder] Jiuwen provider built (mode=sdk, tenant=%s)",
            tenant_id,
        )
        return provider

    # ---- server mode ---- #
    server_cfg = j_cfg.get("server") or {}
    # Backward compat: read base_url/api_key/timeouts from the server sub-block,
    # falling back to the legacy top-level keys / env vars.
    base_url = server_cfg.get("base_url") or j_cfg.get("base_url") or os.environ.get(
        "JIUWEN_MEMORY_BASE_URL", "http://127.0.0.1:8137"
    )
    api_key = server_cfg.get("api_key") or j_cfg.get("api_key") or os.environ.get(
        "JIUWEN_MEMORY_API_KEY", ""
    )
    # `or` would swallow an explicit 0 (falsy) into the fallback; use max()
    # so 0/negative (illegal) falls back to the default instead of hanging requests.
    read_timeout = max(float(server_cfg.get("read_timeout") or j_cfg.get("read_timeout") or 30.0), 30.0)
    write_timeout = max(float(server_cfg.get("write_timeout") or j_cfg.get("write_timeout") or 120.0), 120.0)

    provider = JiuwenMemoryProvider(
        mode="server",
        base_url=base_url,
        api_key=api_key,
        tenant_id=tenant_id,
        user_id=user_id,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
        infer_turns=infer_turns,
        save_assistant_turns=save_assistant_turns,
    )
    if not provider.is_available():
        logger.warning("[ExternalMemoryBuilder] Jiuwen unavailable (no base_url)")
        return None

    logger.info(
        "[ExternalMemoryBuilder] Jiuwen provider built (mode=server, base_url=%s, tenant=%s)",
        base_url, tenant_id,
    )
    return provider


def _build_jiuwen_sdk_config_dict(
    sdk_cfg: Dict[str, Any],
    full_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stitch the flat jiuwen.sdk backend selection + jiuwenswarm's LLM/embedder
    config into the agent-memory two-level namespace dict.

    Backend selection (from jiuwen.sdk):
        kv_type / kv_url       → kv_store.default        (sqlite | redis | memory)
        vector_type / vector_url → vector_store.default  (milvus | memory)
        db_type / db_url       → fulltext_store.default  (elasticsearch | memory)

    LLM credentials are reused from jiuwenswarm's ``models.defaults[is_default=true]
    .model_client_config`` (api_base/api_key/model_name/client_provider); the
    agent-memory target is inferred from the client_provider/base_url
    (DashScope-style → "dashscope", otherwise "openai"). Embedder is reused from
    jiuwenswarm's ``embed`` section.

    rerank/graph/enable_thinking/embedder_ssl_verify are pinned here (not exposed
    in config.yaml): rerank off (no external reranker), graph off, thinking off.
    """
    # ---- backend type/url (flat → agent-memory target/params) ---- #
    kv_type = str(sdk_cfg.get("kv_type") or "sqlite").lower()
    kv_url = sdk_cfg.get("kv_url") or ""
    vector_type = str(sdk_cfg.get("vector_type") or "milvus").lower()
    vector_url = sdk_cfg.get("vector_url") or ""
    db_type = str(sdk_cfg.get("db_type") or "elasticsearch").lower()
    db_url = sdk_cfg.get("db_url") or ""
    embedder_dim = int(sdk_cfg.get("embedder_dim") or 1024)

    kv_store = _kv_spec(kv_type, kv_url)
    vector_store = _vector_spec(vector_type, vector_url, embedder_dim)
    fulltext_store = _fulltext_spec(db_type, db_url)

    # ---- LLM (reuse jiuwenswarm models.defaults) ---- #
    llm_creds = _jiuwenswarm_llm_creds(full_config)
    embed_creds = get_embed_config() or {}

    globals_: Dict[str, Any] = {
        "vector_enabled": True,
        "graph_enabled": False,        # pinned off
        "rerank_enabled": False,       # pinned off (no external reranker)
        "embedder_dim": embedder_dim,
        "chunk_size": 512,
        "enable_thinking": "false",          # pinned off
        "embedder_ssl_verify": "false",      # pinned off
    }
    # LLM credentials → globals (openai_llm/dashscope_llm read from globals)
    if llm_creds.get("api_key") and llm_creds.get("model") and llm_creds.get("base_url"):
        globals_["llm_api_key"] = llm_creds["api_key"]
        globals_["llm_model"] = llm_creds["model"]
        globals_["llm_base_url"] = llm_creds["base_url"]
    # Embedder credentials → globals (openai_embedder reads from globals)
    if embed_creds.get("api_key") and embed_creds.get("base_url") and embed_creds.get("model"):
        globals_["embedder_api_key"] = embed_creds["api_key"]
        # jiuwenswarm's embed.embed_base_url often ends in /embeddings (its own
        # client appends nothing), but agent-memory's openai_embedder uses the
        # OpenAI client which appends /embeddings itself — so strip a trailing
        # /embeddings here to avoid a doubled .../v1/embeddings/embeddings path.
        globals_["embedder_base_url"] = _strip_embeddings_suffix(embed_creds["base_url"])
        globals_["embedder_model"] = embed_creds["model"]

    cfg: Dict[str, Any] = {
        "globals": globals_,
        "tokenizer": {"default": {"target": "jieba"}},   # Chinese BM25
        "kv_store": kv_store,
        "vector_store": vector_store,
        "fulltext_store": fulltext_store,
    }
    # LLM target + embedder target only when creds are present
    if llm_creds.get("api_key") and llm_creds.get("model") and llm_creds.get("base_url"):
        cfg["llm"] = {"default": {"target": llm_creds["target"]}}
    if embed_creds.get("api_key") and embed_creds.get("base_url") and embed_creds.get("model"):
        cfg["embedder"] = {"default": {"target": "openai"}}
    return cfg


def _kv_spec(kv_type: str, kv_url: str) -> Dict[str, Any]:
    """Flat kv_type/kv_url → agent-memory kv_store.default spec."""
    if kv_type == "redis":
        return {"default": {"target": "redis", "params": {"url": kv_url or "redis://localhost:6379/0"}}}
    if kv_type == "memory":
        return {"default": {"target": "memory"}}
    # default sqlite: kv_url is a file path. If the user left kv_url at its
    # redis-style default (redis://...) while keeping kv_type=sqlite, that URL
    # isn't a valid file path — fall back to a sane default db file.
    if kv_url and "://" in kv_url:
        kv_url = "agent_memory.db"
    return {"default": {"target": "sqlite", "params": {"db_path": kv_url or "agent_memory.db"}}}


def _vector_spec(vector_type: str, vector_url: str, dim: int) -> Dict[str, Any]:
    """Flat vector_type/vector_url → agent-memory vector_store.default spec."""
    if vector_type == "memory":
        return {"default": {"target": "memory"}}
    # default milvus
    return {
        "default": {
            "target": "milvus",
            "params": {
                "uri": vector_url or "http://localhost:19530",
                "collection": "agent_memory_vectors",
                "dim": dim,
                "metric_type": "COSINE",
            },
        }
    }


def _fulltext_spec(db_type: str, db_url: str) -> Dict[str, Any]:
    """Flat db_type/db_url → agent-memory fulltext_store.default spec."""
    if db_type == "memory":
        return {"default": {"target": "memory"}}
    # default elasticsearch
    return {"default": {"target": "elasticsearch", "params": {"hosts": db_url or "http://localhost:9200"}}}


def _jiuwenswarm_llm_creds(full_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull the default LLM credentials from jiuwenswarm's models.defaults.

    Returns api_key/model/base_url/target (target inferred from client_provider
    or base_url: dashscope-style → "dashscope", otherwise "openai").
    """
    cfg = full_config if full_config is not None else _load_config()
    models_cfg = (cfg or {}).get("models", {}) if isinstance(cfg, dict) else {}
    defaults = models_cfg.get("defaults") if isinstance(models_cfg, dict) else None
    default_model: Dict[str, Any] = {}
    if isinstance(defaults, list):
        for entry in defaults:
            if isinstance(entry, dict) and entry.get("is_default"):
                default_model = entry
                break
        if not default_model and defaults:
            default_model = defaults[0] if isinstance(defaults[0], dict) else {}
    elif isinstance(defaults, dict):
        default_model = defaults

    client_cfg = default_model.get("model_client_config", {}) if isinstance(default_model, dict) else {}
    api_base = client_cfg.get("api_base", "")
    api_key = client_cfg.get("api_key", "")
    model_name = client_cfg.get("model_name", "")
    client_provider = str(client_cfg.get("client_provider", "OpenAI"))

    # Infer agent-memory llm target: DashScope-style endpoint/provider → dashscope,
    # otherwise generic OpenAI-compatible → openai.
    is_dashscope = "dashscope" in (api_base + client_provider).lower()
    target = "dashscope" if is_dashscope else "openai"
    return {"api_key": api_key, "model": model_name, "base_url": api_base, "target": target}


def _strip_embeddings_suffix(base_url: str) -> str:
    """Strip a trailing ``/embeddings`` (case-insensitive) from an embedder URL.

    jiuwenswarm's embed section stores the full ``.../v1/embeddings`` URL (its own
    client appends nothing). agent-memory's openai_embedder uses the OpenAI
    client, which appends ``/embeddings`` itself — feeding it a URL already
    ending in ``/embeddings`` yields ``.../v1/embeddings/embeddings`` (404).
    """
    if not base_url:
        return base_url
    s = base_url.rstrip("/")
    lower = s.lower()
    for suffix in ("/embeddings", "/embedding"):
        if lower.endswith(suffix):
            return s[: -len(suffix)].rstrip("/")
    return base_url


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
