# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project query Control Service. Uses the project state store."""

from __future__ import annotations

from jiuwenswarm.server.control.repositories.project_repository import (
    handle_project_request,
)

__all__ = ["handle_project_request"]
