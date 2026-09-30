# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the opt-in canary skill library (no network)."""
from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.canary_skill_library import (
    CanaryLibrary,
    CanarySkillRail,
    attach_canary_library,
    get_canary_skill_config,
)


def test_default_config_disabled():
    cfg = get_canary_skill_config(None)
    assert cfg["enabled"] is False
    assert cfg["core_strikes"] == 2


def test_enabled_true_only_on_explicit_true():
    cfg = get_canary_skill_config({"react": {"evolution": {"canary": {"enabled": True}}}})
    assert cfg["enabled"] is True
    assert get_canary_skill_config({"react": {"evolution": {"canary": {"enabled": "true"}}}})["enabled"] is False


def test_probation_evicted_on_first_attributed_failure():
    lib = CanaryLibrary()
    lib.admit("s1")
    dec = lib.record_task(["s1"], passed=False, base_rate=0.9)
    assert dec.attributed and "s1" in dec.evicted
    assert "s1" not in lib.entries


def test_low_base_rate_not_attributed():
    lib = CanaryLibrary()
    lib.admit("s1")
    dec = lib.record_task(["s1"], passed=False, base_rate=0.2)
    assert not dec.attributed
    assert dec.evicted == []
    assert "s1" in lib.entries


def test_core_needs_two_strikes():
    lib = CanaryLibrary(core_strikes=2, promote_after=1)
    lib.admit("s1")
    lib.record_task(["s1"], passed=True, base_rate=0.9)
    dec = lib.record_task(["s1"], passed=False, base_rate=0.9)
    assert dec.evicted == []
    dec = lib.record_task(["s1"], passed=False, base_rate=0.9)
    assert dec.evicted == ["s1"]


def test_attach_wraps_on_skill_written():
    class _Rail:
        def __init__(self):
            self.names = []

        def on_skill_written(self, name):
            self.names.append(name)
            return name

    lib = CanaryLibrary()
    rail = _Rail()
    attach_canary_library(rail, lib)
    rail.on_skill_written("learned_d1")
    assert "learned_d1" in lib.entries
    assert rail._canary_library is lib


def test_canary_skill_rail_holds_library():
    lib = CanaryLibrary()
    rail = CanarySkillRail(lib)
    assert rail.library is lib


def test_invalid_params():
    with pytest.raises(ValueError):
        CanaryLibrary(core_strikes=0)
    with pytest.raises(ValueError):
        CanaryLibrary(promote_after=0)
    with pytest.raises(ValueError):
        CanaryLibrary(attribution_floor=1.5)
