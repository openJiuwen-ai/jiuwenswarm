# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for ``JiuwenSwarmFlashAdapter`` rail boundary and dreaming overrides.

Covers the review findings on PR6431:

- #1 ``_disabled_tools_rail`` is a security boundary (fail-closed). It is not in
  ``_FLASH_RAIL_KEEP`` (it's not built via keep/drop, the baseline appends it
  unconditionally), so it must live in ``_PROFILE_PROTECTED_RAILS`` so the
  keep-whitelist never drops it. Flash must apply the same
  ``react.disabled_tools`` policy as agent / code.
- #4 dynamic registration via ``_update_rails_for_mode`` must not penetrate the
  whitelist: rails outside ``_FLASH_RAIL_KEEP`` ∪ ``_PROFILE_PROTECTED_RAILS``
  (e.g. ``_context_assemble_rail``, ``_memory_rail``) are unregistered. The
  dead ``_ask_user_rail`` keep entry was removed (gated by ``agent`` prefix at
  build time, never built for flash).
- #6 flash does not start memory dreaming (``try_start_dreaming`` /
  ``try_stop_dreaming`` are no-ops).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _RailBuildInfo,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _info(attr_name: str) -> _RailBuildInfo:
    return _RailBuildInfo(attr_name=attr_name, build_func=lambda **_: None)


# ---------------------------------------------------------------------------
# #1 disabled_tools_rail is protected under the keep-whitelist
# ---------------------------------------------------------------------------


def test_disabled_tools_rail_is_protected():
    """``_disabled_tools_rail`` must survive the flash keep-whitelist filter.

    It is a security boundary (fail-closed): dropping it lets disabled tools
    advertise and execute. It is therefore in ``_PROFILE_PROTECTED_RAILS``,
    not in ``_FLASH_RAIL_KEEP``.
    """
    assert "_disabled_tools_rail" not in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert "_disabled_tools_rail" in JiuwenSwarmFlashAdapter._PROFILE_PROTECTED_RAILS


def test_keep_whitelist_keeps_protected_and_drops_others():
    """Whitelist keeps KEEP ∪ PROTECTED; everything else is dropped."""
    adapter = JiuwenSwarmFlashAdapter()
    rail_infos = [
        _info("_task_planning_rail"),          # in KEEP
        _info("_permission_rail"),              # in KEEP and PROTECTED
        _info("_disabled_tools_rail"),          # PROTECTED only
        _info("_skill_rail"),                   # in KEEP (skill execution)
        _info("_skill_credential_injection_rail"),  # in KEEP
        _info("_ask_user_rail"),                # in KEEP (interactive dynamic mount)
        _info("_context_assemble_rail"),        # neither (dynamic reg target)
        _info("_memory_rail"),                  # neither (dynamic reg target)
        _info("_symphony_orchestration_rail"),  # neither
    ]

    filtered = adapter._filter_rail_infos_by_keep(rail_infos)
    kept = {info.attr_name for info in filtered}

    assert kept == {
        "_task_planning_rail",
        "_permission_rail",
        "_disabled_tools_rail",
        "_skill_rail",
        "_skill_credential_injection_rail",
        "_ask_user_rail",
    }


def test_skill_execution_rails_are_in_keep():
    """#2 (corrected): flash must keep skill *execution*, not just search/install.

    ``SkillToolkit`` (registered in ``_get_tool_cards``, not rail-gated) only
    provides search_skill / install_skill / uninstall_skill. The actual skill
    execution tool (``SkillTool`` / ``ListSkillTool``) plus the skill-catalog
    prompt injection come from ``_skill_rail`` (SkillUseRail), and skill envs
    (``react.skill_envs`` credentials) from ``_skill_credential_injection_rail``.
    Both must be in the whitelist or flash is a half-state: can install but not
    run skills (background skill_turbo PPT jobs would fail). Skill *evolution*
    (SkillEvolutionRail / SkillCreateRail) stays dropped — that is the
    self-evolution flash excludes by design.
    """
    keep = JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert "_skill_rail" in keep
    assert "_skill_credential_injection_rail" in keep
    # evolution / create rails are NOT kept (self-evolution, excluded by design).
    assert "_skill_evolution_rail" not in keep
    assert "_skill_create_rail" not in keep


def test_ask_user_rail_is_in_keep_to_avoid_churn():
    """#4 (corrected): ``_ask_user_rail`` is kept so the per-request drop skips it.

    ``_set_user_interaction_enabled`` (called right after ``_update_rails_for_mode``)
    mounts ask_user for interactive requests and unmounts it for non-interactive
    ones. If the rail were outside the whitelist, the override would unregister it
    each request, then ``_set_user_interaction_enabled(True)`` would rebuild +
    re-register it — a per-request churn (with a misleading "dropped dynamic rails:
    ['StructuredAskUserRail']" log). Keeping it lets the drop step skip it; its
    lifecycle is fully owned by ``_set_user_interaction_enabled``.
    """
    assert "_ask_user_rail" in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP


# ---------------------------------------------------------------------------
# #4 _update_rails_for_mode does not penetrate the whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rails_for_mode_drops_non_whitelist_dynamic_rails():
    """Dynamic rails outside the whitelist are unregistered each request.

    The parent ``_update_rails_for_mode`` calls ``_update_agent_rails()`` which
    dynamically registers ``_context_assemble_rail`` / ``_memory_rail`` /
    ``_external_memory_rail`` — all outside the flash whitelist. Flash's
    override must unregister any such rail that slipped through (e.g. from a
    prior reload), and must NOT call ``_update_agent_rails``.
    """
    adapter = JiuwenSwarmFlashAdapter()

    # Simulate rails a prior reload / parent path may have registered. None of
    # these are in the flash whitelist, so the override must unregister each.
    # (``_ask_user_rail`` is NOT here — it is whitelisted (#4 corrected), so the
    # drop skips it and _set_user_interaction_enabled owns its lifecycle.)
    leaked = {
        "_context_assemble_rail": SimpleNamespace(name="context_assemble"),
        "_memory_rail": SimpleNamespace(name="memory"),
        "_external_memory_rail": SimpleNamespace(name="external_memory"),
        "_skill_create_rail": SimpleNamespace(name="skill_create"),
    }
    for attr, rail in leaked.items():
        setattr(adapter, attr, rail)

    unregister = AsyncMock()
    adapter._instance = SimpleNamespace(unregister_rail=unregister)
    # Whitelisted rails set to a sentinel must be left untouched — including
    # _ask_user_rail (kept, so dropped-skip applies to it too).
    adapter._task_planning_rail = SimpleNamespace(name="keep-me")
    adapter._context_processor_rail = SimpleNamespace(name="keep-me-2")
    adapter._skill_rail = SimpleNamespace(name="keep-me-skill")
    adapter._ask_user_rail = SimpleNamespace(name="keep-me-ask-user")

    await adapter._update_rails_for_mode("flash")

    # Each leaked rail was unregistered exactly once; whitelisted rails were not.
    unregistered = [call.args[0] for call in unregister.call_args_list]
    for rail in leaked.values():
        assert unregistered.count(rail) == 1
    assert adapter._task_planning_rail is not None
    assert adapter._context_processor_rail is not None
    assert adapter._skill_rail is not None
    assert adapter._ask_user_rail is not None  # kept, not churned
    assert all(getattr(adapter, a) is None for a in leaked)

    assert unregister.await_count == len(leaked)


@pytest.mark.asyncio
async def test_update_rails_for_mode_does_not_call_update_agent_rails():
    """Flash must not invoke the parent's full dynamic-registration path."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())

    with pytest.MonkeyPatch.context() as mp:
        called = {"n": 0}

        async def _nope():
            called["n"] += 1

        mp.setattr(adapter, "_update_agent_rails", _nope)
        await adapter._update_rails_for_mode("flash")

    assert called["n"] == 0


# ---------------------------------------------------------------------------
# #6 flash does not start dreaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_start_dreaming_is_noop():
    """Flash must not start memory dreaming (no self-evolution).

    The override shadows the parent's ``try_start_dreaming`` entirely: it must
    not import the dreaming module, must not flip ``_dreaming_started``, and
    must not require a ``busy_checker`` argument.
    """
    import jiuwenswarm.server.runtime.agent_adapter.interface_flash as mod

    adapter = JiuwenSwarmFlashAdapter()
    adapter._dreaming_started = False

    import jiuwenswarm.agents.harness.common.memory.dreaming as dreaming_mod

    start_spy = AsyncMock()
    # If the parent path were reached it would call start_dreaming; assert it
    # is never invoked.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dreaming_mod, "start_dreaming", start_spy)
        await adapter.try_start_dreaming(busy_checker=lambda: False)

    start_spy.assert_not_called()
    assert adapter._dreaming_started is False  # state untouched
    # The override is a true no-op, not delegating to super().
    assert mod.JiuwenSwarmFlashAdapter.try_start_dreaming is not (
        JiuWenSwarmDeepAdapter.try_start_dreaming
    )


@pytest.mark.asyncio
async def test_try_stop_dreaming_is_noop():
    adapter = JiuwenSwarmFlashAdapter()
    # Even if something set the flag, stop must be a harmless no-op.
    adapter._dreaming_started = True
    await adapter.try_stop_dreaming()
    # No-op: does not flip the flag via the parent path (no exception raised).
    # We only assert it returns without error; the parent path is unreachable
    # because the override never calls super().try_stop_dreaming().


def test_flash_overrides_are_present_on_subclass_not_inherited():
    """The overrides must be defined on FlashAdapter itself (not just inherited).

    Otherwise the parent's dreaming / dynamic-registration path would run.
    """
    assert "try_start_dreaming" in JiuwenSwarmFlashAdapter.__dict__
    assert "try_stop_dreaming" in JiuwenSwarmFlashAdapter.__dict__
    assert "_update_rails_for_mode" in JiuwenSwarmFlashAdapter.__dict__
    assert "_drop_non_whitelist_dynamic_rails" in JiuwenSwarmFlashAdapter.__dict__
    # Sanity: the parent still has the real dreaming impl flash is shadowing.
    assert "try_start_dreaming" in JiuWenSwarmDeepAdapter.__dict__


# ---------------------------------------------------------------------------
# #3a resolve_agent_request_mode: flash injection / direct dispatch
# ---------------------------------------------------------------------------


def _set_flash_enabled(monkeypatch, enabled):
    """Patch the config read inside _shared.resolve_agent_request_mode."""
    import jiuwenswarm.server.handlers._shared as shared

    cfg = {"flash": {"enabled": enabled}}
    monkeypatch.setattr(shared, "get_config", lambda: cfg)


def test_resolve_mode_disabled_keeps_agent(monkeypatch):
    """flash.enabled=false (default) → agent requests stay agent."""
    from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

    _set_flash_enabled(monkeypatch, False)
    assert resolve_agent_request_mode("agent") == ("agent", None, "agent")
    assert resolve_agent_request_mode("plan") == ("agent", None, "agent")
    assert resolve_agent_request_mode("agent.fast") == ("agent", None, "agent")


def test_resolve_mode_enabled_injects_agent_to_flash(monkeypatch):
    """flash.enabled=true → agent/plan/fast requests rewritten to flash."""
    from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

    _set_flash_enabled(monkeypatch, True)
    assert resolve_agent_request_mode("agent") == ("flash", None, "flash")
    assert resolve_agent_request_mode("plan") == ("flash", None, "flash")
    assert resolve_agent_request_mode("agent.fast") == ("flash", None, "flash")


def test_resolve_mode_enabled_does_not_touch_code_or_team(monkeypatch):
    """flash.enabled=true only injects agent paths; code/team unaffected."""
    from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

    _set_flash_enabled(monkeypatch, True)
    # code via work_mode is not rewritten to flash
    assert resolve_agent_request_mode("agent", work_mode="code") == (
        "code",
        "normal",
        "code.normal",
    )
    # explicit code stays code
    assert resolve_agent_request_mode("code") == ("code", "normal", "code.normal")
    # team stays team
    assert resolve_agent_request_mode("team")[0] == "team"


def test_resolve_mode_guard_covers_agent_family_not_enumerated(monkeypatch):
    """The guard matches the ``agent`` family by first segment, not a hard enum.

    Any ``agent.*`` sub-mode (e.g. ``agent.team``, ``agent.custom``) is an
    agent-family request and injects to flash when the switch is on — a hard
    enum of ``agent.plan`` / ``agent.fast`` would miss new sub-modes and leave
    the same mode family inconsistent (PR6431 review, 次要项). Bare ``plan`` /
    ``fast`` (legacy cron data) also inject.
    """
    from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

    _set_flash_enabled(monkeypatch, True)
    # agent.* family all inject
    assert resolve_agent_request_mode("agent.team") == ("flash", None, "flash")
    assert resolve_agent_request_mode("agent.custom") == ("flash", None, "flash")
    # bare plan/fast inject (legacy cron job data)
    assert resolve_agent_request_mode("fast") == ("flash", None, "flash")


def test_resolve_mode_explicit_flash_dispatches_directly(monkeypatch):
    """Explicit mode=flash bypasses the config switch (works with switch off)."""
    from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

    _set_flash_enabled(monkeypatch, False)
    assert resolve_agent_request_mode("flash") == ("flash", None, "flash")
    _set_flash_enabled(monkeypatch, True)
    assert resolve_agent_request_mode("flash") == ("flash", None, "flash")


def test_resolve_mode_missing_config_is_safe(monkeypatch):
    """Config read failure / missing flash key must NOT inject (fail-open safe)."""
    from jiuwenswarm.server.handlers import _shared

    monkeypatch.setattr(_shared, "get_config", lambda: None)
    assert _shared.resolve_agent_request_mode("agent") == ("agent", None, "agent")

    monkeypatch.setattr(_shared, "get_config", lambda: {})  # no flash key
    assert _shared.resolve_agent_request_mode("agent") == ("agent", None, "agent")

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(_shared, "get_config", _boom)
    assert _shared.resolve_agent_request_mode("agent") == ("agent", None, "agent")


# ---------------------------------------------------------------------------
# #3c reload keeps enable_task_loop=false
# ---------------------------------------------------------------------------


def test_flash_override_keeps_task_loop_false_against_global_skill_create():
    """Flash react override must keep ``_resolve_enable_task_loop`` False.

    Even when the global config enables ``skill_create`` / ``review_trigger``
    (which would otherwise force task_loop=True), flash's overlay of
    ``evolution.skill_create=false`` / ``review_trigger=false`` /
    ``enable_task_loop=false`` wins because the merge runs before
    ``_resolve_enable_task_loop`` reads them.
    """
    adapter = JiuwenSwarmFlashAdapter()
    # Global config has skill_create / review_trigger enabled (would force True).
    config_base = {
        "react": {
            "enable_task_loop": True,  # global default
            "evolution": {
                "skill_create": True,
                "review_trigger": True,
                "skill_evolution": True,
            },
        }
    }

    merged = adapter._apply_flash_react_override(config_base)
    react = merged["react"]

    assert react["enable_task_loop"] is False
    assert react["evolution"]["skill_create"] is False
    assert react["evolution"]["review_trigger"] is False
    assert react["evolution"]["skill_evolution"] is False
    # With the overlay applied, task_loop resolves False (no force-revive).
    assert JiuWenSwarmDeepAdapter._resolve_enable_task_loop(react, merged) is False


def test_flash_override_is_idempotent_across_reload():
    """Re-applying the overlay (reload re-merge) keeps task_loop False.

    The merge is deep and idempotent: a second pass (simulating
    ``_apply_reload_config_snapshot`` re-applying flash override after the
    parent resets caches to global react) must not resurrect task_loop=True.
    """
    adapter = JiuwenSwarmFlashAdapter()
    config_base = {
        "react": {
            "enable_task_loop": True,
            "evolution": {"skill_create": True, "review_trigger": True},
        }
    }

    once = adapter._apply_flash_react_override(config_base)
    # Parent reload resets to global react, then flash re-merges — simulate that.
    twice = adapter._apply_flash_react_override(
        {"react": dict(config_base["react"])}
    )

    assert once["react"]["enable_task_loop"] is False
    assert twice["react"]["enable_task_loop"] is False
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(twice["react"], twice)
        is False
    )
