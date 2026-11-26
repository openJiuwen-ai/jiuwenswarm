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
  are unregistered; workspace context and memory rails are retained. The
  dead ``_ask_user_rail`` keep entry was removed (gated by ``agent`` prefix at
  build time, never built for flash).
- #6 flash does not start memory dreaming (``try_start_dreaming`` /
  ``try_stop_dreaming`` are no-ops).
"""

import asyncio
import sys
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from openjiuwen.extensions.observability.demand import get_trajectory_span_processor
from openjiuwen.harness.rails.evolution import EvolutionInterruptRail, SkillEvolutionRail
from openjiuwen.harness.rails.evolution.configuration import configure_skill_evolution_runtime
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _RailBuildInfo,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
    JiuwenSwarmFlashAdapter,
    _FLASH_OWNED_CONFIG_KEYS,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import _resolve_enable_task_loop


def _span_context(*, trace_id: int, span_id: int) -> SpanContext:
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
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
        _info("_model_routing_rail"),          # in KEEP (auto model routing)
        _info("_permission_rail"),              # in KEEP and PROTECTED
        _info("_disabled_tools_rail"),          # PROTECTED only
        _info("_skill_rail"),                   # in KEEP (skill execution)
        _info("_skill_evolution_rail"),        # in KEEP (restricted evolution)
        _info("_evolution_interrupt_rail"),    # in KEEP (restricted evolution)
        _info("_skill_sleep_rail"),            # in KEEP (restricted evolution)
        _info("_skill_credential_injection_rail"),  # in KEEP
        _info("_skill_active_state_rail"),      # in KEEP（本仓决策：技能链对等保留）
        _info("_skill_authorization_rail"),     # in KEEP（本仓决策：授权门禁保留）
        _info("_memory_rail"),                  # in KEEP（本仓决策：统一 memory 工具）
        _info("_ask_user_rail"),                # in KEEP (interactive dynamic mount)
        _info("_context_assemble_rail"),        # in KEEP (workspace identity)
        _info("_external_memory_rail"),         # in KEEP (config-driven)
        _info("_office_ace_user_profile_rail"),  # in KEEP (config-driven)
        _info("_symphony_orchestration_rail"),  # neither
    ]

    filtered = adapter._filter_rail_infos_by_keep(rail_infos)
    kept = {info.attr_name for info in filtered}

    assert kept == {
        "_task_planning_rail",
        "_model_routing_rail",
        "_permission_rail",
        "_disabled_tools_rail",
        "_skill_rail",
        "_skill_evolution_rail",
        "_evolution_interrupt_rail",
        "_skill_sleep_rail",
        "_skill_credential_injection_rail",
        "_skill_active_state_rail",
        "_skill_authorization_rail",
        "_memory_rail",
        "_context_assemble_rail",
        "_external_memory_rail",
        "_office_ace_user_profile_rail",
        "_ask_user_rail",
    }


def test_context_overflow_recovery_rail_is_in_flash_keep():
    """Flash must keep the reactive context-overflow recovery rail.

    Huawei MaaS returns ``ModelArts.81001 input token limit is ...`` when the
    prompt exceeds the model window. Without this rail flash cannot force a
    compaction and the raw error reaches the user.
    """
    assert "_context_overflow_recovery_rail" in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP


def test_llm_retry_rail_is_in_flash_keep():
    """Without it the factory falls back to the stock rail, which never reports exhaustion."""
    assert "_llm_retry_rail" in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP


def test_model_routing_rail_is_in_flash_keep():
    """Flash must retain ModelRoutingRail for auto-gear routing.

    The rail is feature-gated by ``model_routing.enabled`` at build time and
    by request-level ``enable_routing`` at invoke time, so including it in the
    profile whitelist does not force routing for explicit model gears. Keeping
    it is required for auto to consume the routing flag and switch models.
    """
    adapter = JiuwenSwarmFlashAdapter()
    assert "_model_routing_rail" in adapter._FLASH_RAIL_KEEP

    filtered = adapter._filter_rail_infos_by_keep([_info("_model_routing_rail")])
    assert [info.attr_name for info in filtered] == ["_model_routing_rail"]


def test_skill_execution_rails_are_in_keep():
    """#2 (corrected): flash must keep skill *execution*, not just search/install.

    ``SkillToolkit`` (registered in ``_get_tool_cards``, not rail-gated) only
    provides search_skill / install_skill / uninstall_skill. The actual skill
    execution tool (``SkillTool`` / ``ListSkillTool``) plus the skill-catalog
    prompt injection come from ``_skill_rail`` (SkillUseRail), and skill envs
    (``react.skill_envs`` credentials) from ``_skill_credential_injection_rail``.
    Both must be in the whitelist or flash is a half-state: can install but not
    run skills (background skill_turbo PPT jobs would fail). Restricted skill
    self-evolution is kept, while SkillCreate stays outside the Flash profile.
    """
    keep = JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert "_skill_rail" in keep
    assert "_skill_credential_injection_rail" in keep
    assert "_skill_evolution_rail" in keep
    assert "_evolution_interrupt_rail" in keep
    assert "_skill_sleep_rail" in keep
    # SkillCreate remains excluded from Flash.
    assert "_skill_create_rail" not in keep


def test_skill_protocol_prompt_rail_is_in_flash_keep():
    """技能执行规范（skill_protocol 段，含「执行 skill 前必须建 todo」）必须进 flash。

    父类 ``_build_agent_rails`` 对 ``SkillProtocolPromptRail`` 用
    ``mode.startswith("agent")`` 门控，flash 模式拿不到；flash 适配器在
    ``_instantiate_rails`` 显式补装配（见 test_flash_artifact_post_process 的
    ``flash_skill_protocol_builder.assert_called_once``）。进 keep 保证 reload
    一致性（``_rail_snapshot``/``_profile_fingerprint``）与白名单过滤都覆盖到它。
    """
    assert "_skill_protocol_prompt_rail" in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP


def test_flash_prompt_static_rails_are_in_keep():
    """A1 三 rail（段序收口 / 最后审计 / 静态正文 composer）必须进白名单并冷启动装配。

    漏掉任何一个：FlashSectionOrderRail 段序收口与剥除不执行、FlashPromptAuditRail
    每轮校验不执行、FlashPromptComposerRail（persona/safety/tools/response 四域重组
    的唯一入口）整段静默丢失——sections.py 已写明「审计对账」会误导后续维护者。
    """
    keep = JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert "_flash_section_order_rail" in keep
    assert "_flash_prompt_audit_rail" in keep
    assert "_flash_prompt_composer_rail" in keep


def test_static_prompt_text_rails_use_one_composer():
    """静态提示词转换只注册一个 Composer，不再分散成四个后处理 Rail。"""
    keep = JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP
    assert "_flash_prompt_composer_rail" in keep
    assert "_flash_persona_rail" not in keep
    assert "_flash_safety_rail" not in keep
    assert "_flash_tool_guide_rail" not in keep
    assert "_flash_response_rail" not in keep


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


def test_multimodal_image_rail_is_in_keep():
    """上传图片必须由 MultimodalImageRail 直接送入主模型，而不是退化成 read_file。"""
    adapter = JiuwenSwarmFlashAdapter()
    assert "_multimodal_image_rail" in adapter._FLASH_RAIL_KEEP
    filtered = adapter._filter_rail_infos_by_keep([_info("_multimodal_image_rail")])
    assert [info.attr_name for info in filtered] == ["_multimodal_image_rail"]


# ---------------------------------------------------------------------------
# #4 _update_rails_for_mode does not penetrate the whitelist
# ---------------------------------------------------------------------------
# #4 _update_rails_for_mode does not penetrate the whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rails_for_mode_drops_non_whitelist_dynamic_rails():
    """Dynamic rails outside the whitelist are unregistered each request.

    Memory rails have their own config-driven lifecycle. Other dynamic rails
    outside the flash profile must still be removed.
    """
    adapter = JiuwenSwarmFlashAdapter()

    # Simulate rails a prior reload / parent path may have registered. None of
    # these are in the flash whitelist, so the override must unregister each.
    # (``_ask_user_rail`` and ``_memory_rail`` are NOT here — both are
    # whitelisted (#4 corrected / unified-memory design), so the drop skips
    # them; ask_user's lifecycle is owned by _set_user_interaction_enabled,
    # memory's by _handle_memory_rail_by_config via the agent-profile gate.)
    leaked = {"_skill_create_rail": SimpleNamespace(name="skill_create")}
    for attr, rail in leaked.items():
        setattr(adapter, attr, rail)

    unregister = AsyncMock()
    adapter._instance = SimpleNamespace(unregister_rail=unregister)
    # Whitelisted rails set to a sentinel must be left untouched — including
    # _ask_user_rail (kept, so dropped-skip applies to it too) and _memory_rail
    # (kept; flash mounts the unified memory rail itself before the drop).
    adapter._task_planning_rail = SimpleNamespace(name="keep-me")
    adapter._context_assemble_rail = SimpleNamespace(name="keep-context")
    adapter._context_processor_rail = SimpleNamespace(name="keep-me-2")
    adapter._skill_rail = SimpleNamespace(name="keep-me-skill")
    adapter._ask_user_rail = SimpleNamespace(name="keep-me-ask-user")
    adapter._memory_rail = SimpleNamespace(name="keep-me-memory")
    adapter._external_memory_rail = SimpleNamespace(name="keep-me-external")
    adapter._office_ace_user_profile_rail = SimpleNamespace(name="keep-me-profile")

    # 统一 memory 工具的挂/卸走 agent 档闸门，这里 mock 掉以隔离 drop 逻辑本身。
    handle_memory = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(adapter, "_handle_memory_rail_by_config", handle_memory)
        mp.setattr(adapter, "_handle_external_memory_rail_by_config", AsyncMock())
        mp.setattr(adapter, "_handle_office_ace_user_profile_rail_by_config", AsyncMock())
        await adapter._update_rails_for_mode("flash")

    handle_memory.assert_awaited_once_with("agent")

    # Each leaked rail was unregistered exactly once; whitelisted rails were not.
    unregistered = [call.args[0] for call in unregister.call_args_list]
    for rail in leaked.values():
        assert unregistered.count(rail) == 1
    assert adapter._task_planning_rail is not None
    assert adapter._context_assemble_rail is not None
    assert adapter._context_processor_rail is not None
    assert adapter._skill_rail is not None
    assert adapter._ask_user_rail is not None  # kept, not churned
    assert adapter._memory_rail is not None  # kept, not churned
    assert adapter._external_memory_rail is not None
    assert adapter._office_ace_user_profile_rail is not None
    assert all(getattr(adapter, a) is None for a in leaked)

    assert unregister.await_count == len(leaked)


@pytest.mark.asyncio
async def test_update_rails_for_mode_does_not_call_update_agent_rails():
    """Flash must not invoke the parent's full dynamic-registration path."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock())

    with pytest.MonkeyPatch.context() as mp:
        called = {"n": 0}

        async def _nope():
            called["n"] += 1

        mp.setattr(adapter, "_update_agent_rails", _nope)
        mp.setattr(adapter, "_rebind_late_read_rails_to_local", Mock())
        await adapter._update_rails_for_mode("flash")

    assert called["n"] == 0


@pytest.mark.asyncio
async def test_flash_reconcile_registers_active_evolution_when_enabled(monkeypatch):
    """Flash mounts restricted online evolution rails only when opted in."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": True, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {"evolution": {"skill_evolution": True}}
    adapter._instance = SimpleNamespace(register_rail=AsyncMock())
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    reconcile_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_awaited_once()
    unconfigure.assert_not_awaited()
    reconcile_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_reconcile_defaults_keep_skill_sleep_without_online_evolution(monkeypatch):
    """Flash defaults are closed online evolution and resident SkillSleep."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {}
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    adapter._skill_evolution_rail = object()
    adapter._evolution_interrupt_rail = object()
    adapter._skill_sleep_rail = object()
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    reconcile_sleep = AsyncMock()
    unconfigure_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)
    monkeypatch.setattr(adapter, "_unconfigure_skill_sleep_rail", unconfigure_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_not_awaited()
    unconfigure.assert_awaited_once()
    reconcile_sleep.assert_awaited_once()
    unconfigure_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_flash_reconcile_does_not_inherit_global_skill_evolution_switch(monkeypatch):
    """Flash online evolution is opt-in and never falls back to react.evolution."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "react": {"evolution": {"enabled": True, "skill_evolution": True}},
        "flash": {"evolution": {"enabled": False, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {"evolution": {"enabled": True, "skill_evolution": True}}
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    adapter._skill_evolution_rail = object()
    adapter._evolution_interrupt_rail = object()
    adapter._skill_sleep_rail = object()
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    reconcile_sleep = AsyncMock()
    unconfigure_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)
    monkeypatch.setattr(adapter, "_unconfigure_skill_sleep_rail", unconfigure_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_not_awaited()
    unconfigure.assert_awaited_once()
    reconcile_sleep.assert_awaited_once()
    unconfigure_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_flash_reconcile_can_disable_skill_sleep_independently(monkeypatch):
    """A Flash-only SkillSleep switch can unload sleep without online rails."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": True, "skill_sleep": {"enabled": False}}},
    }
    adapter._config_cache = {}
    adapter._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock())
    adapter._skill_evolution_rail = object()
    adapter._skill_sleep_rail = object()
    ensure = AsyncMock()
    unconfigure_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_skill_sleep_rail", unconfigure_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_awaited_once()
    unconfigure_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_reconcile_unmounts_evolution_when_disabled(monkeypatch):
    """A disabled online switch unmounts only online evolution rails."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": False, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {"evolution": {"enabled": True}}
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    adapter._skill_evolution_rail = object()
    adapter._evolution_interrupt_rail = object()
    adapter._skill_sleep_rail = object()
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    reconcile_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_not_awaited()
    unconfigure.assert_awaited_once()
    reconcile_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_reconcile_keeps_cold_started_skill_sleep_by_default(monkeypatch):
    """A cold-mounted SkillSleep rail remains resident by Flash default."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": False, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {}
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    adapter._skill_sleep_rail = object()
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    reconcile_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_not_awaited()
    unconfigure.assert_not_awaited()
    reconcile_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_reconcile_unmounts_cold_started_skill_sleep_when_disabled(monkeypatch):
    """An explicit ``skill_sleep.enabled=false`` must remove a cold-mounted rail."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": False, "skill_sleep": {"enabled": False}}},
    }
    adapter._config_cache = {}
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    adapter._skill_sleep_rail = object()
    ensure = AsyncMock()
    unconfigure = AsyncMock()
    unconfigure_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_unconfigure_skill_sleep_rail", unconfigure_sleep)

    await adapter._reconcile_flash_evolution_rails()

    ensure.assert_not_awaited()
    unconfigure.assert_not_awaited()
    unconfigure_sleep.assert_awaited_once()


def test_flash_review_agent_restores_when_only_flash_evolution_enabled(monkeypatch):
    """Flash reload must not require the inherited global evolution switch."""

    class _SkillEvolutionRail:
        def __init__(self):
            self.registered_agent = None

        def _register_evolution_review_agent(self, agent):
            self.registered_agent = agent

    class _EvolutionInterruptRail:
        pass

    class _SubagentRail:
        pass

    class _Instance:
        def __init__(self, rails):
            self.rails = rails

        def find_rails_by_type(self, rail_types):
            return [rail for rail in self.rails if isinstance(rail, rail_types)]

    rail = _SkillEvolutionRail()
    interrupt_rail = _EvolutionInterruptRail()
    subagent_rail = _SubagentRail()
    instance = _Instance([subagent_rail, interrupt_rail, rail])
    adapter = JiuwenSwarmFlashAdapter()
    adapter._instance = instance
    adapter._skill_evolution_rail = rail
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": True, "skill_sleep": {"enabled": True}}},
        "react": {"evolution": {"enabled": False}},
    }
    adapter._config_cache = adapter._config_base_cache["react"]

    refresh_refs = Mock()
    monkeypatch.setattr(adapter, "_refresh_active_evolution_rail_refs", refresh_refs)

    adapter._sync_active_evolution_review_agent_after_reload()

    assert rail.registered_agent is instance
    refresh_refs.assert_called_once_with()


@pytest.mark.asyncio
async def test_flash_reconcile_isolates_unmount_failures(monkeypatch):
    """A failed active-rail unmount must not block SkillSleep reconciliation."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": False, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {}
    adapter._instance = SimpleNamespace(unregister_rail=AsyncMock())
    adapter._skill_evolution_rail = object()
    adapter._skill_sleep_rail = object()
    reconcile_sleep = AsyncMock()

    async def fail_active() -> None:
        raise RuntimeError("active rail unregister failed")

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", AsyncMock())
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", fail_active)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_flash_evolution_rails()

    reconcile_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_reload_reconcile_does_not_mount_ttse(monkeypatch):
    """Hot reload keeps Flash on restricted evolution rails and skips TTSE."""
    adapter = JiuwenSwarmFlashAdapter()
    restricted = AsyncMock()
    ensure_ttse = AsyncMock()
    reconcile_sleep = AsyncMock()

    monkeypatch.setattr(adapter, "_reconcile_flash_evolution_rails", restricted)
    monkeypatch.setattr(adapter, "_ensure_ttse_rail_registered", ensure_ttse)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_evolution_rails()

    restricted.assert_awaited_once()
    ensure_ttse.assert_not_awaited()


@pytest.mark.asyncio
async def test_flash_reconcile_serializes_concurrent_rail_updates(monkeypatch):
    """Registration and unregister await points must not interleave in Flash."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": True, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {"evolution": {"enabled": True}}
    adapter._instance = SimpleNamespace(register_rail=AsyncMock())
    active = 0
    observed_max = 0

    async def ensure():
        nonlocal active, observed_max
        active += 1
        observed_max = max(observed_max, active)
        await asyncio.sleep(0)
        active -= 1

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", ensure)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", AsyncMock())
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", AsyncMock())

    await asyncio.gather(
        adapter._reconcile_flash_evolution_rails(),
        adapter._reconcile_flash_evolution_rails(),
    )

    assert observed_max == 1


def test_flash_rail_snapshot_is_stable_and_fingerprint_is_stable():
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._task_planning_rail = object()
    adapter._memory_rail = object()
    adapter._context_assemble_rail = object()
    adapter._skill_evolution_rail = object()
    adapter._evolution_interrupt_rail = object()
    adapter._skill_sleep_rail = object()

    snapshot = adapter._rail_snapshot()
    fingerprint = adapter._profile_fingerprint()

    assert snapshot == (
        "task_planning_rail,context_assemble_rail,memory_rail,"
        "skill_evolution_rail,evolution_interrupt_rail,skill_sleep_rail"
    )
    assert fingerprint == adapter._profile_fingerprint()
    assert len(fingerprint) == 12


def test_rail_snapshot_failure_degrades_silently(monkeypatch):
    adapter = object.__new__(JiuwenSwarmFlashAdapter)

    def fail_snapshot():
        raise RuntimeError("rail snapshot unavailable")

    monkeypatch.setattr(adapter, "_rail_observability_snapshot", fail_snapshot)
    adapter._refresh_rail_observability_snapshot()


def test_rail_snapshot_binds_attributes_to_current_trace(monkeypatch):
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._task_planning_rail = object()
    adapter._memory_rail = object()
    span_registry = SimpleNamespace(bind_trace_attributes=Mock())
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.get_telemetry_runtime",
        lambda: SimpleNamespace(
            is_unified_active=lambda: True,
            span_registry=span_registry,
        ),
    )
    context = _span_context(trace_id=0xABC, span_id=0x123)
    with trace.use_span(NonRecordingSpan(context), end_on_exit=False):
        adapter._refresh_rail_observability_snapshot()

    span_registry.bind_trace_attributes.assert_called_once_with(
        0xABC,
        {
            "jiuwenclaw.agent.rails": "task_planning_rail,memory_rail",
            "jiuwenclaw.agent.profile": f"flash:{adapter._profile_fingerprint()}",
        },
    )


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
# config namespace dual-read: flash.* 优先、react.* 兜底
# ---------------------------------------------------------------------------


def _adapter_with_config(flash_section=None, react_cache=None):
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._config_base_cache = {"flash": flash_section or {}, "react": react_cache or {}}
    adapter._config_cache = react_cache or {}
    return adapter


def test_flash_config_prefers_flash_namespace():
    """flash.<key> 命中时优先于 react.<key>。"""
    adapter = _adapter_with_config(
        flash_section={"persona": {"enabled": False}},
        react_cache={"persona": {"enabled": True}},
    )
    assert adapter._flash_config("persona") == {"enabled": False}


def test_flash_config_falls_back_to_react():
    """flash.<key> 缺省时回退 react.<key>，迁移期旧配置仍生效。"""
    adapter = _adapter_with_config(
        flash_section={},
        react_cache={"composer": {"verify_only": True}},
    )
    assert adapter._flash_config("composer") == {"verify_only": True}


def test_flash_config_returns_default_when_both_absent():
    """两个命名空间都没有时返回传入 default。"""
    adapter = _adapter_with_config(flash_section={}, react_cache={})
    assert adapter._flash_config("relay_merge", {"enabled": True}) == {"enabled": True}


def test_flash_config_excludes_shared_react_keys():
    """共享 react 键（skill_turbo / disabled_tools）不进入 flash 双读键表。

    _flash_config 只用于 _FLASH_OWNED_CONFIG_KEYS 内的 flash 专属键；与 Deep 共享的
    react.* 键仍走 react.* 单一语义，避免 flash.* 下同名键顶掉共享配置。
    """
    assert "skill_turbo" not in _FLASH_OWNED_CONFIG_KEYS
    assert "disabled_tools" not in _FLASH_OWNED_CONFIG_KEYS


def test_flash_owned_config_keys_cover_static_rail_switches():
    """静态区三 rail 及其四域开关都在 flash.* 命名空间内（_flash_config 双读范围）。"""
    owned = interface_flash._FLASH_OWNED_CONFIG_KEYS
    for key in ("composer", "section_order", "audit", "persona", "safety", "tool_guide", "response"):
        assert key in owned


def test_flash_owned_config_keys_covers_all_rail_switches():
    """所有 flash 专属 rail 开关都在双读键表内，缺一即漏改。"""
    assert _FLASH_OWNED_CONFIG_KEYS == {
        "persona",
        "safety",
        "tool_guide",
        "response",
        "composer",
        "repeat_call_guard",
        "skill_catalog",
        "state",
        "pressure",
        "relay_merge",
        "section_order",
        "audit",
    }


def test_flash_config_keys_declared_in_template_and_survive_merge():
    """评审 #1：flash 配置键须在 config.yaml 模板声明，用户 YAML 覆盖后保留。

    模板合并（merge_template_with_override）会删除「override 有、模板没有」的键；若
    这些键未在模板声明，audit.fail_closed=true / persona.enabled=false / 线 B 的
    pressure.warning_ratio 等都会被静默丢弃。
    """
    import yaml

    from jiuwenswarm.common.utils import (
        merge_template_with_override,
        resolve_shipped_template_config_path,
    )

    template = yaml.safe_load(
        resolve_shipped_template_config_path().read_text(encoding="utf-8")
    )
    assert isinstance(template.get("flash"), dict)
    for key in (
        "audit", "persona", "safety", "tool_guide", "response", "composer", "section_order",
        "repeat_call_guard", "skill_catalog", "state", "pressure", "relay_merge",
    ):
        assert key in template["flash"], f"flash.{key} 未在 config.yaml 模板声明"

    override = {
        "flash": {
            "audit": {"fail_closed": True},
            "persona": {"enabled": False},
            "safety": {"enabled": False},
            "tool_guide": {"enabled": False},
            "response": {"enabled": False},
            "composer": {"takeover_domains": [], "verify_only": True},
            "section_order": {"enabled": False},
            "repeat_call_guard": {"thresholds": [2, 4], "exclude": ["bash"]},
            "skill_catalog": {"enabled": False, "catalog_max_entries": 30},
            "state": {"enabled": False},
            "pressure": {"warning_ratio": 0.5, "critical_ratio": 0.8},
            "relay_merge": {"enabled": False},
        }
    }
    merged = merge_template_with_override(template, override)
    flash = merged["flash"]
    assert flash["audit"]["fail_closed"] is True
    assert flash["persona"]["enabled"] is False
    assert flash["safety"]["enabled"] is False
    assert flash["composer"]["takeover_domains"] == []
    assert flash["composer"]["verify_only"] is True
    assert flash["section_order"]["enabled"] is False
    assert flash["repeat_call_guard"]["thresholds"] == [2, 4]
    assert flash["repeat_call_guard"]["exclude"] == ["bash"]
    assert flash["skill_catalog"]["enabled"] is False
    assert flash["skill_catalog"]["catalog_max_entries"] == 30
    assert flash["state"]["enabled"] is False
    assert flash["pressure"]["warning_ratio"] == 0.5
    assert flash["pressure"]["critical_ratio"] == 0.8
    assert flash["relay_merge"]["enabled"] is False


# ---------------------------------------------------------------------------
# E：默认生产接管装配（adapter 缺省 takeover_domains = safety/response）
# ---------------------------------------------------------------------------


def _fake_composer_module():
    """给 adapter 懒加载的 composer 打桩，捕获 FlashPromptComposerRail 构造入参。

    composer 在 ``_build_flash_prompt_composer_rail`` 内懒 import，本地无 agent_shield
    也能测 adapter 的缺省接线（不必真正 import composer/rails 链）。
    """
    import types

    composer_mod = types.ModuleType("jiuwenswarm.agents.harness.flash.prompt.composer")
    composer_mod.TAKEOVERABLE_DOMAINS = frozenset({"safety", "response"})
    composer_mod.coerce_takeover_domains = lambda domains: frozenset(domains)
    captured = {}

    def _fake_rail(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    composer_mod.FlashPromptComposerRail = _fake_rail
    return composer_mod, captured


def test_adapter_default_takeover_domains_are_safety_response(monkeypatch):
    """生产缺省：provider 返回 safety+response（Composer 构造器缺省为空集）。

    Composer 单元测试里的默认实例 takeover_domains 为空，但生产路径的默认接管来自
    adapter——缺了这条测试，改掉 adapter 缺省值不会在单测层暴露。
    """
    composer_mod, captured = _fake_composer_module()
    adapter = _adapter_with_config()  # flash/ 与 react/ 均无 composer 配置

    monkeypatch.setitem(
        sys.modules, "jiuwenswarm.agents.harness.flash.prompt.composer", composer_mod
    )
    adapter._build_flash_prompt_composer_rail()

    assert captured["config_provider"]()["takeover_domains"] == frozenset({"safety", "response"})


def test_adapter_explicit_takeover_domains_pass_through(monkeypatch):
    """显式配置接管域时，provider 原样返回（不强制默认 safety/response）。"""
    composer_mod, captured = _fake_composer_module()
    adapter = _adapter_with_config(
        flash_section={"composer": {"takeover_domains": ["response"]}},
    )

    monkeypatch.setitem(
        sys.modules, "jiuwenswarm.agents.harness.flash.prompt.composer", composer_mod
    )
    adapter._build_flash_prompt_composer_rail()

    assert captured["config_provider"]()["takeover_domains"] == frozenset({"response"})


def test_adapter_explicit_empty_takeover_disables_all(monkeypatch):
    """显式空列表 = 全部关闭接管，provider 不退回默认 safety/response。"""
    composer_mod, captured = _fake_composer_module()
    adapter = _adapter_with_config(
        flash_section={"composer": {"takeover_domains": []}},
    )

    monkeypatch.setitem(
        sys.modules, "jiuwenswarm.agents.harness.flash.prompt.composer", composer_mod
    )
    adapter._build_flash_prompt_composer_rail()

    assert captured["config_provider"]()["takeover_domains"] == frozenset()


# ---------------------------------------------------------------------------
# B：Todo disabled_tools 热更新同步（reload 后 FlashTodoRail 快照不陈旧）
# ---------------------------------------------------------------------------


def test_sync_flash_todo_disabled_tools_resyncs_from_disabled_rail():
    """reload 后 FlashTodoRail._disabled_tools 与 DisabledToolsRail 保持一致。"""
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._disabled_tools_rail = SimpleNamespace(_disabled_tools=["todo", "bash"])
    received = {}
    adapter._task_planning_rail = SimpleNamespace(
        update_disabled_tools=lambda d: received.update(disabled_tools=d)
    )

    adapter._sync_flash_todo_disabled_tools()

    assert received["disabled_tools"] == ["todo", "bash"]


def test_sync_flash_todo_disabled_tools_clears_when_no_disabled_rail():
    """disabled_tools 清空（DisabledToolsRail 为 None）→ todo rail 快照清空。"""
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._disabled_tools_rail = None
    received = {}
    adapter._task_planning_rail = SimpleNamespace(
        update_disabled_tools=lambda d: received.update(disabled_tools=d)
    )

    adapter._sync_flash_todo_disabled_tools()

    assert received["disabled_tools"] == []


def test_sync_flash_todo_disabled_tools_noop_without_todo_rail():
    """todo rail 未装配时同步是安全 no-op。"""
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._disabled_tools_rail = SimpleNamespace(_disabled_tools=["todo"])
    adapter._task_planning_rail = None
    adapter._sync_flash_todo_disabled_tools()  # 不抛异常


def test_sync_flash_todo_disabled_tools_noop_without_update_method():
    """todo rail 无 update_disabled_tools 入口时跳过（不抛异常）。"""
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._disabled_tools_rail = SimpleNamespace(_disabled_tools=["todo"])
    adapter._task_planning_rail = SimpleNamespace()
    adapter._sync_flash_todo_disabled_tools()  # 不抛异常


def test_reload_resyncs_todo_disabled_tools_after_disabled_rail_rebuild(monkeypatch):
    """reload 入口必须在 DisabledToolsRail 重建后同步 todo rail 的 disabled_tools。

    上面 4 个用例只测了 helper 自身分支；本用例覆盖真正要保证的契约——reload 路径
    （``_get_current_agent_rails``）会恰好调用一次同步，且入参是重建后的列表。若接线
    漏调或提前调用（在 ``_disabled_tools_rail`` 重建前同步），todo 被禁后仍会注入
    430 todo 纪律段（提示词要求调 todo、工具面却没有），而前 4 个用例会静默通过。
    """
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._disabled_tools_rail = SimpleNamespace(_disabled_tools=["todo", "bash"])
    calls = []
    adapter._task_planning_rail = SimpleNamespace(
        update_disabled_tools=lambda d: calls.append(list(d))
    )
    adapter._flash_skill_selection_rail = None

    # 隔离出 reload 的同步逻辑：父类 rail 表返回空、skill_selection 返回 None。
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter, "_get_current_agent_rails",
        lambda self, config, config_base: ([], []),
    )
    monkeypatch.setattr(
        JiuwenSwarmFlashAdapter, "_build_flash_skill_selection_rail",
        lambda self, config_base: None,
    )

    adapter._get_current_agent_rails({}, {})

    # 恰好调用一次，且入参是 DisabledToolsRail 重建后的列表。
    assert calls == [["todo", "bash"]]


# ---------------------------------------------------------------------------
# 评审 #1/#3：flash 配置热更新改为每轮 config_provider 现读；fallback 目录兜底
# ---------------------------------------------------------------------------


def test_read_fallback_skills_filters_disabled():
    """fallback 目录来自实际加载技能，并过滤 list_execution_disabled_skills（评审 #3）。"""
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._skill_rail = SimpleNamespace(
        skills_meta=[
            SimpleNamespace(name="a", description="A"),
            SimpleNamespace(name="b", description="B"),
        ],
    )
    adapter._skill_manager = SimpleNamespace(list_execution_disabled_skills=lambda: {"b"})

    assert adapter._read_fallback_skills() == [("a", "A")]


def test_read_fallback_skills_empty_without_skill_rail():
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._skill_rail = None
    adapter._skill_manager = None

    assert adapter._read_fallback_skills() == []


def test_read_disabled_tools_from_config_cache():
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    adapter._config_cache = {"disabled_tools": ["todo", "bash"]}

    assert adapter._read_disabled_tools() == ["todo", "bash"]


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
    """flash.enabled=true selects Flash while preserving agent canonical mode."""
    from jiuwenswarm.server.handlers._shared import resolve_agent_request_mode

    _set_flash_enabled(monkeypatch, True)
    assert resolve_agent_request_mode("agent") == ("flash", None, "agent")
    assert resolve_agent_request_mode("plan") == ("flash", None, "agent")
    assert resolve_agent_request_mode("agent.fast") == ("flash", None, "agent")


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
    assert resolve_agent_request_mode("agent.team") == ("flash", None, "agent")
    assert resolve_agent_request_mode("agent.custom") == ("flash", None, "agent")
    # bare plan/fast inject (legacy cron job data)
    assert resolve_agent_request_mode("fast") == ("flash", None, "agent")


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
    assert react["evolution"]["skill_evolution"] is True
    # With the overlay applied, task_loop resolves False (no force-revive).
    assert _resolve_enable_task_loop(react, merged) is False


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
        _resolve_enable_task_loop(twice["react"], twice)
        is False
    )


# ---------------------------------------------------------------------------
# Evolution rail trigger drift: unbound prewarm vs bound chat turn
# ---------------------------------------------------------------------------
# Contract source: openjiuwen/harness/rails/evolution/configuration.py
# ``_validate_evolution_rail_config`` raises ``RuntimeError`` when an existing
# SkillEvolutionRail's signal/review trigger differs from the requested one and
# tells the caller to ``unconfigure_skill_evolution()`` first.
#
# OfficeClaw 1.9.0 prewarms a session unbound (``user_id=null``). Unbound,
# ``get_local_config`` falls through to ``os.environ`` and a legacy
# ``EVOLUTION_AUTO_SCAN=false`` from .env yields ``signal_trigger=False``. The
# first bound chat turn seals the overlay (missing key == unset) and resolves
# ``signal_trigger=True`` from config. The tests below run the *real* harness
# configure/unconfigure helpers against a storage-only agent stub, so the
# production error is reproduced rather than imitated.


class _EvolutionAgentStub:
    """DeepAgent stand-in exposing the rail storage harness evolution helpers use."""

    def __init__(self) -> None:
        self.rails: list[Any] = []
        self._pending_rails: list[Any] = []
        self.registered: list[Any] = []

    def add_rail(self, rail: Any) -> None:
        self._pending_rails.append(rail)

    async def register_rail(self, rail: Any) -> None:
        self._pending_rails.remove(rail)
        self.rails.append(rail)
        self.registered.append(rail)

    async def unregister_rail(self, rail: Any) -> None:
        if rail in self.rails:
            self.rails.remove(rail)

    def find_rails_by_type(self, types: tuple[type, ...]) -> list[Any]:
        return [rail for rail in self.rails if isinstance(rail, types)]

    def strip_rails_by_type(self, types: tuple[type, ...]) -> int:
        removed = [rail for rail in self.rails if isinstance(rail, types)]
        self.rails = [rail for rail in self.rails if not isinstance(rail, types)]
        return len(removed)


async def _configure_evolution_rail(
    agent: _EvolutionAgentStub,
    tmp_path,
    *,
    signal_trigger: bool,
    review_trigger: bool = False,
) -> SkillEvolutionRail:
    """Mount rails through the real harness entry point the adapter calls."""
    await configure_skill_evolution_runtime(
        agent,
        skills_dir=[str(tmp_path)],
        llm=None,
        model="test-model",
        trajectory_span_processor=get_trajectory_span_processor(),
        review_trigger=review_trigger,
        signal_trigger=signal_trigger,
        auto_save=True,
        language="cn",
    )
    return next(rail for rail in agent.rails if isinstance(rail, SkillEvolutionRail))


async def _deep_adapter_with_cached_evolution_rail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    signal_trigger: bool,
    review_trigger: bool = False,
) -> tuple[JiuWenSwarmDeepAdapter, SkillEvolutionRail]:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {}
    adapter._instance = _EvolutionAgentStub()
    adapter._default_model_name = "test-model"
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda extra_skill_dir=None: [str(tmp_path)])
    rail = await _configure_evolution_rail(
        adapter._instance,
        tmp_path,
        signal_trigger=signal_trigger,
        review_trigger=review_trigger,
    )
    adapter._refresh_active_evolution_rail_refs()
    assert adapter._skill_evolution_rail is rail
    return adapter, rail


@pytest.mark.parametrize(
    ("cached", "desired"),
    [
        # 1.9.0 incident: unbound prewarm mounted False, bound turn wants True.
        ({"signal_trigger": False, "review_trigger": False}, {"signal_trigger": True, "review_trigger": False}),
        ({"signal_trigger": True, "review_trigger": False}, {"signal_trigger": False, "review_trigger": False}),
        ({"signal_trigger": True, "review_trigger": False}, {"signal_trigger": True, "review_trigger": True}),
    ],
)
@pytest.mark.asyncio
async def test_ensure_active_evolution_rails_rebuilds_on_trigger_drift(
    monkeypatch,
    tmp_path,
    cached,
    desired,
):
    """A cached rail with different triggers is rebuilt, not left to raise."""
    adapter, stale_rail = await _deep_adapter_with_cached_evolution_rail(
        monkeypatch,
        tmp_path,
        signal_trigger=cached["signal_trigger"],
        review_trigger=cached["review_trigger"],
    )
    monkeypatch.setattr(
        deep_module,
        "get_passive_skill_evolution_triggers",
        lambda _config: dict(desired),
    )
    stale_interrupt = adapter._evolution_interrupt_rail

    await adapter._ensure_active_evolution_rails_registered()

    current = adapter._skill_evolution_rail
    assert current is not None
    assert current is not stale_rail
    assert current.signal_trigger is desired["signal_trigger"]
    assert current.review_trigger is desired["review_trigger"]
    assert adapter._instance.find_rails_by_type((SkillEvolutionRail,)) == [current]
    assert stale_rail not in adapter._instance.rails
    # The stale interrupt rail is torn down with its evolution rail: exactly one remains.
    interrupt_rails = adapter._instance.find_rails_by_type((EvolutionInterruptRail,))
    assert len(interrupt_rails) == 1
    assert interrupt_rails[0] is not stale_interrupt
    assert adapter._evolution_interrupt_rail is interrupt_rails[0]


@pytest.mark.parametrize(
    ("signal_trigger", "review_trigger"),
    [(True, False), (False, False), (False, True)],
)
@pytest.mark.asyncio
async def test_ensure_active_evolution_rails_keeps_rail_when_triggers_match(
    monkeypatch,
    tmp_path,
    signal_trigger,
    review_trigger,
):
    """Matching triggers keep the mounted rail object: no needless rebuild."""
    adapter, rail = await _deep_adapter_with_cached_evolution_rail(
        monkeypatch,
        tmp_path,
        signal_trigger=signal_trigger,
        review_trigger=review_trigger,
    )
    monkeypatch.setattr(
        deep_module,
        "get_passive_skill_evolution_triggers",
        lambda _config: {
            "signal_trigger": signal_trigger,
            "review_trigger": review_trigger,
        },
    )
    unconfigure = AsyncMock(wraps=adapter._unconfigure_active_evolution_rails)
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)

    await adapter._ensure_active_evolution_rails_registered()

    unconfigure.assert_not_awaited()
    assert adapter._skill_evolution_rail is rail


@pytest.mark.asyncio
async def test_ensure_active_evolution_rails_retries_when_cached_rail_is_missing(
    monkeypatch,
    tmp_path,
):
    """A stale cache hides the mounted rail until the SDK detects the mismatch."""
    adapter, stale_rail = await _deep_adapter_with_cached_evolution_rail(
        monkeypatch,
        tmp_path,
        signal_trigger=False,
    )

    adapter._skill_evolution_rail = None
    monkeypatch.setattr(
        deep_module,
        "get_passive_skill_evolution_triggers",
        lambda _config: {"signal_trigger": True, "review_trigger": False},
    )
    configure = AsyncMock(wraps=configure_skill_evolution_runtime)
    monkeypatch.setattr(deep_module, "configure_skill_evolution_runtime", configure)

    await adapter._ensure_active_evolution_rails_registered()

    assert configure.await_count == 2
    assert adapter._skill_evolution_rail.signal_trigger is True
    assert adapter._instance.find_rails_by_type((SkillEvolutionRail,)) == [adapter._skill_evolution_rail]
    assert stale_rail not in adapter._instance.rails


_RAIL_MISMATCH = (
    "Evolution rail config mismatch. Existing configuration differs: "
    "signal_trigger: False != True. Use unconfigure_skill_evolution() first to remove the existing rail."
)


@pytest.mark.asyncio
async def test_flash_reconcile_survives_evolution_registration_failure(monkeypatch):
    """An auxiliary evolution rail failure must not abort the chat turn."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": True, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {}
    adapter._instance = SimpleNamespace(register_rail=AsyncMock())
    configure = AsyncMock(side_effect=RuntimeError(_RAIL_MISMATCH))
    unconfigure = AsyncMock()
    reconcile_sleep = AsyncMock()
    monkeypatch.setattr(deep_module, "configure_skill_evolution_runtime", configure)
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [])
    monkeypatch.setattr(adapter, "_unconfigure_active_evolution_rails", unconfigure)
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_flash_evolution_rails()

    assert configure.await_count == 2
    # Clean up before the retry and again after the second failure.
    assert unconfigure.await_count == 2
    reconcile_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_flash_reconcile_survives_skill_sleep_registration_failure(monkeypatch):
    """SkillSleep is auxiliary too: its failure must not abort the chat turn."""
    adapter = JiuwenSwarmFlashAdapter()
    adapter._config_base_cache = {
        "flash": {"evolution": {"enabled": True, "skill_sleep": {"enabled": True}}},
    }
    adapter._config_cache = {}
    adapter._instance = SimpleNamespace(register_rail=AsyncMock())
    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", AsyncMock())
    reconcile_sleep = AsyncMock(side_effect=RuntimeError("SkillSleep rail registration failed"))
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    await adapter._reconcile_flash_evolution_rails()

    reconcile_sleep.assert_awaited_once()


@pytest.mark.parametrize("entrypoint", ["chat", "slash"])
@pytest.mark.asyncio
async def test_deep_evolution_retry_failure_by_entrypoint(monkeypatch, entrypoint):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {}
    adapter._instance = _EvolutionAgentStub()
    configure = AsyncMock(side_effect=[RuntimeError(_RAIL_MISMATCH), RuntimeError("retry failed")])
    reconcile_sleep = AsyncMock()
    monkeypatch.setattr(deep_module, "get_evolution_enabled", lambda _config: True)
    monkeypatch.setattr(deep_module, "get_ttse_enabled", lambda _config: False)
    monkeypatch.setattr(deep_module, "configure_skill_evolution_runtime", configure)
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [])
    monkeypatch.setattr(adapter, "_reconcile_skill_sleep_rail", reconcile_sleep)

    if entrypoint == "chat":
        await adapter._reconcile_evolution_rails()
        reconcile_sleep.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="retry failed"):
            await adapter._ensure_evolution_rail_for_slash("agent")

    assert configure.await_count == 2
