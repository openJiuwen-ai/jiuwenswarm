# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Host memory services used by extensions."""

from typing import Any

from jiuwenswarm.agents.harness.common.memory import (
    MemoryIndexManager,
    MemorySettings,
    bm25_rank_to_score,
)


def get_embed_config() -> dict[str, str | None]:
    from jiuwenswarm.agents.harness.common.memory import get_embed_config as read_config

    return read_config()


def create_memory_settings(*, workspace_dir: str, **overrides: Any) -> MemorySettings:
    from jiuwenswarm.agents.harness.common.memory import (
        create_memory_settings as create_settings,
    )

    return create_settings(workspace_dir=workspace_dir, **overrides)


async def get_memory_manager(
    *, agent_id: str, workspace_dir: str, settings: MemorySettings
) -> MemoryIndexManager | None:
    from jiuwenswarm.agents.harness.common.memory import (
        get_memory_manager as get_manager,
    )

    return await get_manager(agent_id=agent_id, workspace_dir=workspace_dir, settings=settings)

__all__ = [
    "bm25_rank_to_score",
    "create_memory_settings",
    "get_embed_config",
    "get_memory_manager",
]
