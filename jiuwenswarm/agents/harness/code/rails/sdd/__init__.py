# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDD-family state-machine rails — shared base + independent rails.

This package groups the SDD (Spec-Driven Development) family of rails under
one namespace:

  * ``rail_state_machine`` — ``RailStateMachineBase`` (shared pattern: in-memory
    state + advance-tool registration + skill-methodology injection +
    artifacts gate). NOT a rail itself; subclasses inherit it.
  * ``design_rail`` — DesignRail (wave-1: requirements analysis + design).
  * (future) ``implement_rail`` — ImplementRail (reads design_rail's RDS).
  * (future) ``project_analysis_rail`` — ProjectAnalysisRail (independent).

Each rail owns its own ``ADVANCE_TOOL`` + ``stages`` + ``SKILLS_DIR``;
they share the IMPLEMENTATION PATTERN via ``RailStateMachineBase`` (not a
shared tool or state machine).
"""
from __future__ import annotations

from jiuwenswarm.agents.harness.code.rails.sdd.common.rail_state_machine import (
    RailStateMachineBase,
)

__all__ = ["RailStateMachineBase"]
