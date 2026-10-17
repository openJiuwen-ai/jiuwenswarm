# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Workspace paths for extensions."""

from pathlib import Path


def get_user_workspace_dir() -> Path:
    from jiuwenswarm.common.utils import get_user_workspace_dir as get_dir

    return get_dir()


def get_agent_workspace_dir() -> Path:
    from jiuwenswarm.common.utils import get_agent_workspace_dir as get_dir

    return get_dir()


def get_agent_sessions_dir() -> Path:
    from jiuwenswarm.common.utils import get_agent_sessions_dir as get_dir

    return get_dir()
