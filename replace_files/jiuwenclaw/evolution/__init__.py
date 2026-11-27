# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuwenClaw Skills Online Self-Evolution Module.

Provides EvolutionService as the unified facade, backed by:
  - SignalDetector: rules-based signal extraction with evolution type classification
  - SkillEvolver: LLM-based experience generation with history dedup
  - EvolutionStore: pure IO layer for evolutions_desc.json / evolutions_body.json and SKILL.md
"""
from jiuwenclaw.evolution.schema import (
    EvolutionChange,
    EvolutionEntry,
    EvolutionFile,
    EvolutionSignal,
    EvolutionType,
    ExperienceTarget,
)
from jiuwenclaw.evolution.signal_detector import SignalDetector
from jiuwenclaw.evolution.evolver import SkillEvolver
from jiuwenclaw.evolution.store import EvolutionStore
from jiuwenclaw.evolution.service import EvolutionService

__all__ = [
    "EvolutionChange",
    "EvolutionEntry",
    "EvolutionFile",
    "EvolutionSignal",
    "EvolutionType",
    "ExperienceTarget",
    "SignalDetector",
    "SkillEvolver",
    "EvolutionStore",
    "EvolutionService",
]
