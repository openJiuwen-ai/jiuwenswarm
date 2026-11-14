# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted Discover SkillPack setting shared by product entry points."""

from typing import Any


def skillpack_productization_enabled(config: dict[str, Any] | None = None) -> bool:
    """Use the same setting as the existing frontend, including legacy YAML."""
    from jiuwenswarm.common.config import get_config, get_symphony_evolution_enabled

    resolved = get_config() if config is None else config
    return get_symphony_evolution_enabled(resolved)
