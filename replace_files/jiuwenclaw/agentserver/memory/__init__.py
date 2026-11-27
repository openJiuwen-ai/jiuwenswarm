# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Memory system for JiuWenClaw."""

from .types import (
    MemorySearchResult,
    MemoryFileEntry,
    MemoryChunk,
    MemorySource,
    FileEntry,
)
from .manager import MemoryIndexManager, get_memory_manager, clear_memory_manager_cache
from .config import (
    MemorySettings,
    create_memory_settings,
    is_memory_enabled,
    get_memory_mode,
    get_embed_config,
    DEFAULT_WORKSPACE_DIR,
)
from .embeddings import EmbeddingProvider, create_embedding_provider
from .external_memory_config import (
    get_external_memory_config,
    is_external_memory_enabled,
    build_openjiuwen_provider_config,
    get_memory_engine,
    is_builtin_memory_allowed,
    is_external_memory_allowed,
)
from .external_memory_builder import build_external_memory_rail
from .internal import (
    estimate_tokens,
    ensure_dir,
    list_memory_files,
    build_file_entry,
    chunk_markdown,
    hash_text,
    build_fts_query,
    bm25_rank_to_score,
    is_memory_path,
    normalize_extra_memory_paths,
)

__all__ = [
    "MemoryIndexManager",
    "MemorySettings",
    "get_memory_manager",
    "clear_memory_manager_cache",
    "EmbeddingProvider",
    "create_embedding_provider",
    "MemorySearchResult",
    "MemoryFileEntry",
    "MemoryChunk",
    "MemorySource",
    "FileEntry",
    "ensure_dir",
    "list_memory_files",
    "build_file_entry",
    "chunk_markdown",
    "hash_text",
    "build_fts_query",
    "bm25_rank_to_score",
    "is_memory_path",
    "normalize_extra_memory_paths",
    "create_memory_settings",
    "is_memory_enabled",
    "get_memory_mode",
    "get_embed_config",
    "DEFAULT_WORKSPACE_DIR",
    "estimate_tokens",
    "get_external_memory_config",
    "is_external_memory_enabled",
    "build_openjiuwen_provider_config",
    "build_external_memory_rail",
    "get_memory_engine",
    "is_builtin_memory_allowed",
    "is_external_memory_allowed",
]
