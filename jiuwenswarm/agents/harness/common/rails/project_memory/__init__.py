# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project memory helpers (file discovery + PromptSection factory)."""
from jiuwenswarm.agents.harness.common.rails.project_memory.files import (
    ADDITIONAL_DIRECTORIES_ENV,
    DEFAULT_MAX_CHARS,
    LOCAL_MEMORY_FILES,
    LoadedMemoryFile,
    MANAGED_MEMORY_FILES,
    MANAGED_MEMORY_GLOBS,
    MAX_MEMORY_CHARACTER_COUNT,
    PROJECT_MEMORY_FILES,
    PROJECT_MEMORY_GLOBS,
    PROJECT_ROOT_MARKERS,
    USER_MEMORY_FILES,
    USER_MEMORY_GLOBS,
    clear_project_memory_cache,
    discover_and_load_memory_files,
    find_project_root,
    get_large_memory_files,
    merge_memory_content,
)
from jiuwenswarm.agents.harness.common.rails.project_memory.section import (
    SECTION_NAME,
    build_project_memory_section,
)

__all__ = [
    "ADDITIONAL_DIRECTORIES_ENV",
    "LoadedMemoryFile",
    "discover_and_load_memory_files",
    "find_project_root",
    "get_large_memory_files",
    "MAX_MEMORY_CHARACTER_COUNT",
    "merge_memory_content",
    "PROJECT_ROOT_MARKERS",
    "PROJECT_MEMORY_FILES",
    "PROJECT_MEMORY_GLOBS",
    "LOCAL_MEMORY_FILES",
    "USER_MEMORY_FILES",
    "USER_MEMORY_GLOBS",
    "MANAGED_MEMORY_FILES",
    "MANAGED_MEMORY_GLOBS",
    "clear_project_memory_cache",
    "DEFAULT_MAX_CHARS",
    "SECTION_NAME",
    "build_project_memory_section",
]
