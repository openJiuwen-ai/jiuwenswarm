# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""External memory configuration helpers.

Reads `memory.external` from config.yaml. For provider=openjiuwen, also
maps the jiuwenclaw-shaped config (`memory.external.stores` + top-level
`embed`) into the config dict that OpenJiuwenMemoryProvider expects.
Concrete Store / Embedding instances are built inside the provider — not
here.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .config import _load_config, get_embed_config

logger = logging.getLogger(__name__)

_DEFAULT_USER = "__default__"
_DEFAULT_SCOPE = "__default__"
_LTM_SUBDIR = "memory/ltm"

_VALID_ENGINES = {"builtin", "external", "both", "none"}


def get_memory_engine(config: Optional[Dict[str, Any]] = None) -> str:
    """Return the memory engine policy: builtin | external | both | none.

    Controls which memory subsystems are allowed to mount.
    Default: builtin (backward-compatible with configs that predate this flag).
    """
    cfg = config if config is not None else _load_config()
    mem = (cfg or {}).get("memory", {}) if isinstance(cfg, dict) else {}
    value = str(mem.get("engine") or "builtin").strip().lower()
    return value if value in _VALID_ENGINES else "builtin"


def is_builtin_memory_allowed(config: Optional[Dict[str, Any]] = None) -> bool:
    """Engine-level gate for the built-in MemoryRail."""
    return get_memory_engine(config) in {"builtin", "both"}


def is_external_memory_allowed(config: Optional[Dict[str, Any]] = None) -> bool:
    """Engine-level gate for the ExternalMemoryRail."""
    return get_memory_engine(config) in {"external", "both"}


def get_external_memory_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the `memory.external` section with defaults filled in."""
    cfg = config if config is not None else _load_config()
    mem = (cfg or {}).get("memory", {}) if isinstance(cfg, dict) else {}
    ext = mem.get("external", {}) if isinstance(mem, dict) else {}
    if not isinstance(ext, dict):
        ext = {}

    return {
        "provider": (ext.get("provider") or "").strip(),
        "user_id": ext.get("user_id") or _DEFAULT_USER,
        "scope_id": ext.get("scope_id") or _DEFAULT_SCOPE,
        "allowed_plugins": ext.get("allowed_plugins") or [],
        "openjiuwen": ext.get("openjiuwen") or {},
        "mem0": ext.get("mem0") or {},
        "openviking": ext.get("openviking") or {},
    }


def is_external_memory_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return True iff external memory is both engine-allowed and has a provider."""
    if not is_external_memory_allowed(config):
        return False
    return bool(get_external_memory_config(config).get("provider"))


def _resolve_ltm_dir() -> Path:
    """Default LTM data dir under ~/.jiuwenclaw/memory/ltm."""
    base = Path.home() / ".jiuwenclaw" / _LTM_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def build_openjiuwen_provider_config(
    ext_cfg: Dict[str, Any],
    full_config: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Any]:
    """Map jiuwenclaw config into the dict OpenJiuwenMemoryProvider expects.

    Provider-expected shape:
        {
          "kv":        {"backend": "shelve|memory|sqlite", "path": "..."},
          "vector":    {"backend": "chroma", "persist_directory": "..."},
          "db":        {"backend": "sqlite", "path": "..."},
          "embedding": {"model_name": "...", "base_url": "...", "api_key": "..."},
        }

    Returns:
        (provider_config, scope_config)
    """
    oj_cfg = ext_cfg.get("openjiuwen") or {}
    ltm_dir = _resolve_ltm_dir()

    kv_backend = (oj_cfg.get("kv_type") or "shelve").lower()
    kv_path = oj_cfg.get("kv_path") or str(ltm_dir / "kv")

    vector_backend = (oj_cfg.get("vector_type") or "chroma").lower()
    vector_dir = oj_cfg.get("vector_persist_dir") or str(ltm_dir / "chroma")

    db_backend = (oj_cfg.get("db_type") or "sqlite").lower()
    db_path = oj_cfg.get("db_path") or str(ltm_dir / "ltm.db")

    embed_cfg = get_embed_config() or {}
    logger.info("[external_memory] get_embed_config() returned: %s", embed_cfg)
    embedding = {
        "model_name": embed_cfg.get("model") or os.getenv("EMBED_MODEL", ""),
        "base_url": embed_cfg.get("base_url") or os.getenv("EMBED_BASE_URL", ""),
        "api_key": embed_cfg.get("api_key") or os.getenv("EMBED_API_KEY", ""),
    }
    logger.info("[external_memory] resolved embedding: model_name=%s, base_url=%s, has_api_key=%s",
                embedding["model_name"], embedding["base_url"], bool(embedding["api_key"]))
    if not embedding["model_name"]:
        logger.warning(
            "[external_memory] Embedding not configured — OpenJiuwen LTM will skip vector search"
        )

    provider_config = {
        "kv": {"backend": kv_backend, "path": kv_path},
        "vector": {"backend": vector_backend, "persist_directory": vector_dir},
        "db": {"backend": db_backend, "path": db_path},
        "embedding": embedding,
    }
    logger.info("[external_memory] provider_config built: kv=(%s, %s), vector=(%s, %s), db=(%s, %s)",
                kv_backend, kv_path, vector_backend, vector_dir, db_backend, db_path)

    scope_config = _build_scope_config(full_config, embedding)

    return provider_config, scope_config


def _build_scope_config(
    full_config: Optional[Dict[str, Any]],
    embedding: Dict[str, Any],
) -> Any | None:
    from openjiuwen.core.memory.config.config import MemoryScopeConfig
    from openjiuwen.core.foundation.store.base_embedding import EmbeddingConfig
    from openjiuwen.core.foundation.llm.schema.config import (
        ModelRequestConfig,
        ModelClientConfig,
    )

    cfg = full_config if full_config is not None else _load_config()
    logger.info("[external_memory] _build_scope_config: full_config is None=%s, loaded cfg keys=%s",
                full_config is None, list(cfg.keys()) if isinstance(cfg, dict) else "N/A")

    models_cfg = (cfg or {}).get("models", {})
    logger.info("[external_memory] _build_scope_config: models_cfg=%s", models_cfg)
    default_model = models_cfg.get("default", {}) if isinstance(models_cfg, dict) else {}
    client_cfg = default_model.get("model_client_config", {}) if isinstance(default_model, dict) else {}
    model_obj_cfg = default_model.get("model_config_obj", {}) if isinstance(default_model, dict) else {}

    api_base = client_cfg.get("api_base", "")
    api_key = client_cfg.get("api_key", "")
    model_name = client_cfg.get("model_name", "")
    client_provider = client_cfg.get("client_provider", "OpenAI")
    verify_ssl = client_cfg.get("verify_ssl", True)
    timeout = client_cfg.get("timeout", 1800)
    logger.info("[external_memory] _build_scope_config: api_base=%s, model_name=%s, has_api_key=%s, client_provider=%s",
                api_base, model_name, bool(api_key), client_provider)

    if not api_key or not model_name or not api_base:
        logger.warning(
            "[external_memory] LLM not configured (models.default) — "
            "sync_turn (fact extraction) will not work"
        )
        return None

    model_cfg = ModelRequestConfig(
        model=model_name,
        temperature=model_obj_cfg.get("temperature", 0.2),
        top_p=model_obj_cfg.get("top_p", 0.7),
    )
    model_client_cfg = ModelClientConfig(
        client_provider=client_provider,
        api_key=api_key,
        api_base=api_base,
        verify_ssl=verify_ssl,
        timeout=timeout,
    )

    embed_model_name = embedding.get("model_name", "")
    embed_base_url = embedding.get("base_url", "")
    embed_api_key = embedding.get("api_key")
    embed_cfg_obj = None
    if embed_model_name and embed_base_url:
        embed_cfg_obj = EmbeddingConfig(
            model_name=embed_model_name,
            base_url=embed_base_url,
            api_key=embed_api_key,
        )
    else:
        logger.warning(
            "[external_memory] Embedding model_name or base_url missing — "
            "MemoryScopeConfig will not include embedding_cfg"
        )

    scope_config_kwargs = {
        "model_cfg": model_cfg,
        "model_client_cfg": model_client_cfg,
    }
    if embed_cfg_obj is not None:
        scope_config_kwargs["embedding_cfg"] = embed_cfg_obj

    scope_config = MemoryScopeConfig(**scope_config_kwargs)

    logger.info(
        "[external_memory] LLM config built for LTM: model=%s api_base=%s",
        model_name, api_base,
    )

    return scope_config
