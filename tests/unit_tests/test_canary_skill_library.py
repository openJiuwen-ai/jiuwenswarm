# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the opt-in canary skill library (no network)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.canary_skill_library import (
    CanaryLibrary,
    CanarySkillRail,
    attach_canary_library,
    get_canary_skill_config,
)


def _task_ctx(task_id, skills, passed, base_rate=None):
    """Fake callback context shaped like AgentCallbackContext."""
    return SimpleNamespace(
        success=passed,
        inputs=SimpleNamespace(
            task_id=task_id, canary_skills=skills, canary_base_rate=base_rate
        ),
    )


def test_default_config_disabled():
    cfg = get_canary_skill_config(None)
    assert cfg["enabled"] is False
    assert cfg["core_strikes"] == 2


def test_enabled_true_only_on_explicit_true():
    cfg = get_canary_skill_config({"react": {"evolution": {"canary": {"enabled": True}}}})
    assert cfg["enabled"] is True
    assert get_canary_skill_config({"react": {"evolution": {"canary": {"enabled": "true"}}}})["enabled"] is False


def test_config_garbage_degrades_to_defaults_instead_of_raising():
    cfg = get_canary_skill_config({"react": {"evolution": {"canary": {
        "enabled": True, "core_strikes": "abc", "promote_after": None,
        "attribution_floor": "oops",
    }}}})
    assert cfg["core_strikes"] == 2
    assert cfg["promote_after"] == 2
    assert cfg["attribution_floor"] == 0.5


def test_config_values_are_clamped():
    cfg = get_canary_skill_config({"react": {"evolution": {"canary": {
        "enabled": True, "core_strikes": 0, "promote_after": -3,
        "attribution_floor": 1.5,
    }}}})
    assert cfg["core_strikes"] == 1
    assert cfg["promote_after"] == 1
    assert cfg["attribution_floor"] == 1.0


def _store_rail():
    """Stub matching the real SkillEvolutionRail surface: store, no on_skill_written."""

    class _Store:
        def __init__(self):
            self.written = []

        async def write_skill_content(self, name, content):
            self.written.append(name)
            return True

    class _Rail:
        def __init__(self):
            self.store = _Store()

    return _Rail()


def test_store_write_path_admits_skills():
    rail = _store_rail()
    lib = CanaryLibrary()
    attach_canary_library(rail, lib)
    ok = asyncio.run(rail.store.write_skill_content("learned_t17", "# skill"))
    assert ok is True
    assert "learned_t17" in lib.entries
    assert "learned_t17" in rail.store.written  # native write untouched


def test_store_admission_wrap_is_idempotent():
    rail = _store_rail()
    lib = CanaryLibrary()
    attach_canary_library(rail, lib)
    attach_canary_library(rail, lib)  # second bind must not double-wrap
    asyncio.run(rail.store.write_skill_content("learned_x", "# x"))
    assert lib.entries["learned_x"].name == "learned_x"  # admitted exactly once


def test_attach_without_any_hook_warns_but_stays_safe():
    class _BareRail:
        pass

    lib = CanaryLibrary()
    attach_canary_library(_BareRail(), lib)  # must not raise
    assert lib.entries == {}


def test_probation_evicted_on_first_attributed_failure():
    lib = CanaryLibrary()
    lib.admit("s1")
    dec = lib.record_task(["s1"], passed=False, base_rate=0.9)
    assert dec.attributed and "s1" in dec.evicted
    assert "s1" not in lib.entries


def test_low_base_rate_not_attributed_and_not_credited():
    lib = CanaryLibrary()
    lib.admit("s1")
    dec = lib.record_task(["s1"], passed=False, base_rate=0.2)
    assert not dec.attributed
    assert dec.evicted == []
    assert "s1" in lib.entries
    # An unattributed failure is neither a strike nor a clean task: a skill
    # must never reach core on the back of failed tasks.
    assert lib.entries["s1"].clean_tasks == 0


def test_unattributed_failures_do_not_promote():
    lib = CanaryLibrary(promote_after=1)
    lib.admit("s1")
    for _ in range(3):
        lib.record_task(["s1"], passed=False, base_rate=0.1)
    assert lib.entries["s1"].tier == "probation"
    dec = lib.record_task(["s1"], passed=True, base_rate=0.1)
    assert dec.promoted == ["s1"]


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


def test_canary_skill_rail_records_attributed_failure():
    lib = CanaryLibrary()
    lib.admit("s1")
    rail = CanarySkillRail(lib)
    asyncio.run(rail.after_invoke(_task_ctx("t1", ["s1"], passed=False, base_rate=0.9)))
    assert "s1" not in lib.entries  # probation: first attributed strike evicts


def test_canary_skill_rail_defaults_to_inputs_attribution():
    lib = CanaryLibrary(core_strikes=2, promote_after=2)
    lib.admit("s1")
    rail = CanarySkillRail(lib)
    asyncio.run(rail.after_task_iteration(_task_ctx("t1", "s1", passed=True)))
    assert lib.entries["s1"].clean_tasks == 1  # string form accepted


def test_canary_skill_rail_ignores_unattributed_tasks():
    lib = CanaryLibrary()
    lib.admit("s1")
    rail = CanarySkillRail(lib)
    asyncio.run(rail.after_invoke(_task_ctx("t1", [], passed=False, base_rate=0.9)))
    assert "s1" in lib.entries
    assert lib.entries["s1"].strikes == 0
    assert lib.entries["s1"].clean_tasks == 0


def test_canary_skill_rail_ignores_non_bool_success():
    lib = CanaryLibrary()
    lib.admit("s1")
    rail = CanarySkillRail(lib)
    ctx = SimpleNamespace(success="yes", inputs=SimpleNamespace(task_id="t1", canary_skills=["s1"]))
    asyncio.run(rail.after_invoke(ctx))
    assert lib.entries["s1"].strikes == 0


def test_canary_skill_rail_dedupes_same_task():
    lib = CanaryLibrary(core_strikes=2, promote_after=1)
    lib.admit("s1")
    lib.record_task(["s1"], passed=True, base_rate=0.9)  # promote to core
    assert lib.entries["s1"].tier == "core"
    rail = CanarySkillRail(lib)
    asyncio.run(rail.after_task_iteration(_task_ctx("t1", ["s1"], passed=False, base_rate=0.9)))
    asyncio.run(rail.after_invoke(_task_ctx("t1", ["s1"], passed=False, base_rate=0.9)))
    # DeepAgent fires both hooks for one task; one strike, not two
    # (a double strike would evict a core skill).
    assert "s1" in lib.entries
    assert lib.entries["s1"].strikes == 1


def test_canary_skill_rail_ignores_unknown_skill_names():
    lib = CanaryLibrary()
    rail = CanarySkillRail(lib)
    asyncio.run(rail.after_invoke(_task_ctx("t1", ["ghost"], passed=False, base_rate=0.9)))
    assert lib.entries == {}


def test_invalid_params():
    with pytest.raises(ValueError):
        CanaryLibrary(core_strikes=0)
    with pytest.raises(ValueError):
        CanaryLibrary(promote_after=0)
    with pytest.raises(ValueError):
        CanaryLibrary(attribution_floor=1.5)
