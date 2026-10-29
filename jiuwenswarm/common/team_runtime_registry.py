# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Non-creating access to the process's existing Team execution owner."""

from __future__ import annotations

import sys
from typing import Any


def get_existing_team_manager() -> Any | None:
    """Query cancellation/read ports without activating an unused capability.

    TeamManager publishes its singleton after construction. A Team execution
    request still uses its normal factory; ordinary UI cleanup cannot create it.
    """
    module = sys.modules.get("jiuwenswarm.agents.harness.team.team_manager")
    return getattr(module, "_team_manager", None) if module is not None else None
