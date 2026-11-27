# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def long_horizon_now(monkeypatch):
    """Use the same scenario time for drafting, scheduling and overdue delivery."""
    from jiuwenswarm.agents.harness.common.long_horizon import core, models, schedule_port, tools

    instant = datetime(2026, 9, 16, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

    class ScenarioDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone().replace(tzinfo=None)
            return instant.astimezone(tz)

    for module in (core, models, schedule_port, tools):
        monkeypatch.setattr(module, "datetime", ScenarioDateTime)
    return instant
