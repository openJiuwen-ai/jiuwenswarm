# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Named parameter bundles for team session scope and runtime attachment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TeamBootstrapParams:
    """Optional context for distributed ``TeamManager.create_team`` paths."""

    request_id: str | None = None
    channel_id: str | None = None
    request_metadata: dict[str, Any] | None = None
    sessions_root: str | Path | None = None


@dataclass(frozen=True)
class TeamMonitorAttachOptions:
    """Options when attaching team/workflow monitor handlers to a runtime."""

    hide_dm: bool = False
    enable_swarmflow: bool = False
    sessions_root: str | Path | None = None
