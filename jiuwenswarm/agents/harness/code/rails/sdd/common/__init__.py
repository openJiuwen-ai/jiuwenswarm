# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared infrastructure for SDD-family rails.

  * ``rail_state_machine`` — ``RailStateMachineBase`` (shared pattern: in-memory
    state + advance-tool registration + skill-methodology injection +
    artifacts gate). NOT a rail itself; SDD-family rails (design_rail,
    future implement_rail / project_analysis_rail) inherit it.
"""
from __future__ import annotations

from jiuwenswarm.agents.harness.code.rails.sdd.common.rail_state_machine import (
    RailStateMachineBase,
)

__all__ = ["RailStateMachineBase"]
