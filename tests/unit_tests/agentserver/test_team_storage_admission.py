# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team storage params passthrough + non-swarmflow LLM admission."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.agents.harness.team.config_loader import _resolve_storage_config
from jiuwenswarm.agents.harness.team.rails import team_admission_rail as admission_mod
from jiuwenswarm.agents.harness.team.rails.team_admission_rail import (
    TeamAdmissionRail,
    build_team_admission_rail,
    reset_team_llm_semaphore_for_tests,
    resolve_team_admission_limit,
    resolve_team_section_for_admission,
)


def test_resolve_storage_config_merges_sqlite_pool_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path,
    )
    resolved = _resolve_storage_config(
        {
            "type": "sqlite",
            "params": {
                "db_timeout": 8,
                "read_pool_size": 12,
            },
        }
    )
    params = resolved["params"]
    assert params["db_timeout"] == 8
    assert params["read_pool_size"] == 12
    assert params["write_pool_size"] == 2
    assert params["db_enable_wal"] is True
    assert params["wal_checkpoint_interval_s"] == 30.0
    assert params["connection_string"] == str(tmp_path / "team.db")


def test_resolve_storage_config_keeps_explicit_connection_string(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path,
    )
    resolved = _resolve_storage_config(
        {
            "type": "sqlite",
            "params": {"connection_string": "custom.db"},
        }
    )
    assert resolved["params"]["connection_string"] == str(tmp_path / "custom.db")


def test_resolve_storage_config_memory_skips_sqlite_defaults():
    resolved = _resolve_storage_config(
        {
            "type": "memory",
            "params": {"connection_string": ":memory:"},
        }
    )
    params = resolved["params"]
    assert params == {"connection_string": ":memory:"}
    assert "db_enable_wal" not in params
    assert "read_pool_size" not in params


def test_resolve_storage_config_drops_unknown_sqlite_params(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.config_loader.get_agent_teams_home",
        lambda: tmp_path,
    )
    resolved = _resolve_storage_config(
        {
            "type": "sqlite",
            "params": {"db_timeout": 3, "not_a_real_knob": 1},
        }
    )
    assert "not_a_real_knob" not in resolved["params"]
    assert resolved["params"]["db_timeout"] == 3


@pytest.mark.parametrize(
    ("team_cfg", "expected"),
    [
        ({"enable_swarmflow": False}, 4),
        ({"enable_swarmflow": True}, 0),
        ({"enable_swarmflow": False, "resource_admission": {"max_concurrent_llm_calls": 2}}, 2),
        ({"enable_swarmflow": True, "resource_admission": {"max_concurrent_llm_calls": 3}}, 3),
        ({"enable_swarmflow": False, "resource_admission": {"max_concurrent_llm_calls": 0}}, 0),
        ({"enable_swarmflow": False, "resource_admission": {"max_concurrent_llm_calls": None}}, 4),
        # Invalid values fall back to swarmflow-aware default (not fail-open to 0).
        ({"enable_swarmflow": False, "resource_admission": {"max_concurrent_llm_calls": "x"}}, 4),
        ({"enable_swarmflow": True, "resource_admission": {"max_concurrent_llm_calls": "x"}}, 0),
        # YAML `.inf` → float('inf'); int(inf) raises OverflowError.
        ({"enable_swarmflow": False, "resource_admission": {"max_concurrent_llm_calls": float("inf")}}, 4),
    ],
)
def test_resolve_team_admission_limit(team_cfg, expected):
    assert resolve_team_admission_limit(team_cfg) == expected


def test_resolve_team_section_for_admission_prefers_jiuwen_team():
    section = resolve_team_section_for_admission(
        {
            "modes": {
                "team": {
                    "other": {"enable_swarmflow": True},
                    "jiuwen_team": {
                        "enable_swarmflow": False,
                        "resource_admission": {"max_concurrent_llm_calls": 2},
                    },
                }
            }
        }
    )
    assert section["enable_swarmflow"] is False
    assert resolve_team_admission_limit(section) == 2


def test_resolve_team_section_for_admission_uses_explicit_team_key():
    config = {
        "modes": {
            "team": {
                "jiuwen_team": {
                    "enable_swarmflow": False,
                    "resource_admission": {"max_concurrent_llm_calls": 2},
                },
                "oc_team_alpha": {
                    "enable_swarmflow": False,
                    "resource_admission": {"max_concurrent_llm_calls": 8},
                },
            }
        }
    }
    section = resolve_team_section_for_admission(config, team_key="oc_team_alpha")
    assert resolve_team_admission_limit(section) == 8
    # Session-scoped runtime name still binds the same template.
    scoped = resolve_team_section_for_admission(
        config, team_key="oc_team_alpha_sess_19f8"
    )
    assert resolve_team_admission_limit(scoped) == 8


def test_resolve_team_section_for_admission_refuses_first_dict_when_ambiguous():
    section = resolve_team_section_for_admission(
        {
            "modes": {
                "team": {
                    "oc_team_a": {
                        "enable_swarmflow": False,
                        "resource_admission": {"max_concurrent_llm_calls": 1},
                    },
                    "oc_team_b": {
                        "enable_swarmflow": False,
                        "resource_admission": {"max_concurrent_llm_calls": 9},
                    },
                }
            }
        }
    )
    # No team_key + no jiuwen_team → do not silently pick A or B by dict order.
    assert section == {}
    assert resolve_team_admission_limit(section) == 4


def test_build_team_admission_rail_disabled_when_zero():
    assert build_team_admission_rail({"enable_swarmflow": True}) is None
    assert build_team_admission_rail(max_concurrent_llm_calls=0) is None
    rail = build_team_admission_rail(max_concurrent_llm_calls=2)
    assert isinstance(rail, TeamAdmissionRail)


class _Ctx:
    def __init__(self) -> None:
        self.extra: dict = {}


def _sem_value(sem: asyncio.Semaphore) -> int:
    return int(getattr(sem, "_value"))


@pytest.mark.asyncio
async def test_team_admission_rail_caps_concurrent_model_calls():
    reset_team_llm_semaphore_for_tests()
    rail = TeamAdmissionRail(max_concurrent_llm_calls=2)
    active = 0
    peak = 0

    async def _one() -> None:
        nonlocal active, peak
        ctx = _Ctx()
        await rail.before_model_call(ctx)  # type: ignore[arg-type]
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        await rail.after_model_call(ctx)  # type: ignore[arg-type]

    await asyncio.gather(*[_one() for _ in range(6)])
    assert peak == 2
    reset_team_llm_semaphore_for_tests()


@pytest.mark.asyncio
async def test_team_admission_releases_on_model_exception():
    reset_team_llm_semaphore_for_tests()
    rail = TeamAdmissionRail(max_concurrent_llm_calls=1)
    ctx = _Ctx()
    await rail.before_model_call(ctx)  # type: ignore[arg-type]
    hold = ctx.extra[admission_mod._CTX_HOLD_KEY]
    assert _sem_value(hold.sem) == 0
    await rail.on_model_exception(ctx)  # type: ignore[arg-type]
    assert admission_mod._CTX_HOLD_KEY not in ctx.extra
    assert hold.released
    assert _sem_value(hold.sem) == 1
    reset_team_llm_semaphore_for_tests()


@pytest.mark.asyncio
async def test_team_admission_after_invoke_releases_on_cancel_path():
    """Cancel/interrupt may skip after_model_call; after_invoke must still free."""
    reset_team_llm_semaphore_for_tests()
    rail = TeamAdmissionRail(max_concurrent_llm_calls=1)
    ctx = _Ctx()
    await rail.before_model_call(ctx)  # type: ignore[arg-type]
    hold = ctx.extra[admission_mod._CTX_HOLD_KEY]
    assert _sem_value(hold.sem) == 0
    await rail.after_invoke(ctx)  # type: ignore[arg-type]
    assert admission_mod._CTX_HOLD_KEY not in ctx.extra
    assert hold.released
    assert _sem_value(hold.sem) == 1
    # Idempotent: second release is a no-op.
    await rail.after_invoke(ctx)  # type: ignore[arg-type]
    assert _sem_value(hold.sem) == 1
    reset_team_llm_semaphore_for_tests()


@pytest.mark.asyncio
async def test_team_admission_task_cancel_returns_permit_without_after_callbacks():
    """Simulate chat.interrupt: CancelledError skips after_* / on_model_exception.

    openjiuwen rail base finally gates after callbacks when is_cancelled; the
    permit must still return via Task done_callback or the process deadlocks
    after ``limit`` cancels (default 4).
    """
    reset_team_llm_semaphore_for_tests()
    rail = TeamAdmissionRail(max_concurrent_llm_calls=1)
    acquired = asyncio.Event()

    async def _in_flight_model_call() -> None:
        ctx = _Ctx()
        await rail.before_model_call(ctx)  # type: ignore[arg-type]
        acquired.set()
        # In-flight model call — no after_model_call / on_model_exception /
        # after_invoke (mirrors cancelled rail-base finally + killed invoke).
        await asyncio.Event().wait()

    task = asyncio.create_task(_in_flight_model_call())
    await asyncio.wait_for(acquired.wait(), timeout=1.0)
    assert admission_mod._sem is not None
    assert _sem_value(admission_mod._sem) == 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _sem_value(admission_mod._sem) == 1

    # Subsequent acquire must not hang (livelock regression).
    ctx2 = _Ctx()
    await asyncio.wait_for(rail.before_model_call(ctx2), timeout=1.0)  # type: ignore[arg-type]
    await rail.after_model_call(ctx2)  # type: ignore[arg-type]
    assert _sem_value(admission_mod._sem) == 1
    reset_team_llm_semaphore_for_tests()


@pytest.mark.asyncio
async def test_team_admission_four_cancels_do_not_exhaust_default_limit():
    """Default limit=4: four cancelled in-flight holds must fully reclaim."""
    reset_team_llm_semaphore_for_tests()
    rail = TeamAdmissionRail(max_concurrent_llm_calls=4)
    gate = asyncio.Event()
    started = 0

    async def _hold() -> None:
        nonlocal started
        ctx = _Ctx()
        await rail.before_model_call(ctx)  # type: ignore[arg-type]
        started += 1
        await gate.wait()

    tasks = [asyncio.create_task(_hold()) for _ in range(4)]
    for _ in range(50):
        if started == 4:
            break
        await asyncio.sleep(0.01)
    assert started == 4
    assert admission_mod._sem is not None
    assert _sem_value(admission_mod._sem) == 0

    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    assert _sem_value(admission_mod._sem) == 4
    reset_team_llm_semaphore_for_tests()


@pytest.mark.asyncio
async def test_team_admission_releases_acquired_sem_when_ctx_write_fails():
    reset_team_llm_semaphore_for_tests()
    rail = TeamAdmissionRail(max_concurrent_llm_calls=1)

    class _BrokenExtra(dict):
        def __setitem__(self, key, value):  # noqa: ANN001
            raise RuntimeError("ctx write failed")

    ctx = _Ctx()
    ctx.extra = _BrokenExtra()
    with pytest.raises(RuntimeError, match="ctx write failed"):
        await rail.before_model_call(ctx)  # type: ignore[arg-type]
    # Global sem was acquired then released by the BaseException path.
    assert admission_mod._sem is not None
    assert _sem_value(admission_mod._sem) == 1
    reset_team_llm_semaphore_for_tests()


@pytest.mark.asyncio
async def test_team_admission_release_targets_acquired_instance_after_limit_change():
    """_release must free the acquire'd Semaphore, not a rebuilt global _sem."""
    reset_team_llm_semaphore_for_tests()
    rail_a = TeamAdmissionRail(max_concurrent_llm_calls=1)
    ctx = _Ctx()
    await rail_a.before_model_call(ctx)  # type: ignore[arg-type]
    hold = ctx.extra[admission_mod._CTX_HOLD_KEY]
    old_sem = hold.sem
    assert old_sem is admission_mod._sem
    assert _sem_value(old_sem) == 0

    # Rebuild process-wide semaphore under a new limit (simulates limit/loop change).
    rail_b = TeamAdmissionRail(max_concurrent_llm_calls=2)
    new_sem = admission_mod._get_semaphore(rail_b._limit)
    assert new_sem is not old_sem
    assert _sem_value(new_sem) == 2

    await rail_a.after_model_call(ctx)  # type: ignore[arg-type]
    assert hold.released
    assert _sem_value(old_sem) == 1
    assert _sem_value(new_sem) == 2

    # Peak under the new limit remains constrained after the rebuild.
    active = 0
    peak = 0

    async def _one() -> None:
        nonlocal active, peak
        c = _Ctx()
        await rail_b.before_model_call(c)  # type: ignore[arg-type]
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        await rail_b.after_model_call(c)  # type: ignore[arg-type]

    await asyncio.gather(*[_one() for _ in range(5)])
    assert peak == 2
    reset_team_llm_semaphore_for_tests()
