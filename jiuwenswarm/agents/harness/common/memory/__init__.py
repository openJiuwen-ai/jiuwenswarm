# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Memory system for JiuWenSwarm."""

from .config import (
    is_memory_enabled,
    get_memory_mode,
    get_embed_config,
    DEFAULT_WORKSPACE_DIR,
)
from .external_memory_config import (
    get_external_memory_config,
    is_external_memory_enabled,
    build_openjiuwen_provider_config,
    get_memory_engine,
    is_external_memory_allowed,
)
from .external_memory_builder import build_external_memory_rail

__all__ = [
    "is_memory_enabled",
    "get_memory_mode",
    "get_embed_config",
    "DEFAULT_WORKSPACE_DIR",
    "get_external_memory_config",
    "is_external_memory_enabled",
    "build_openjiuwen_provider_config",
    "build_external_memory_rail",
    "get_memory_engine",
    "is_external_memory_allowed",
]
