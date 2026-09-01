# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the swarm provider-based team assembly framework.

These tests exercise the config-sourced assembly path end to end without ever
touching a real LLM, the network, or a live ``DeepAgent``:

* provider/rail-type registration is idempotent and lands in openjiuwen's
  registries,
* ``build_member_capability_specs`` emits the expected ``swarm.*`` rail/tool
  references per role (purely from a config dict),
* ``enrich_team_spec_for_swarm`` rewrites a real ``TeamAgentSpec`` in place,
  attaches a ``SwarmBuildContext``, and keeps the parent-free contract,
* the enriched spec serializes cleanly with the live build context excluded,
* individual providers degrade gracefully (return ``[]`` / ``None``) when their
  config gate is closed.
"""

from __future__ import annotations

import inspect
import json
import logging
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openjiuwen.agent_teams.rails.builtin_elements import SKILL_USE as CORE_SKILL_USE
from openjiuwen.harness.schema import deep_agent_spec as das
from openjiuwen.agent_teams.harness.manifest import get_catalog, resolve_factory
from openjiuwen.agent_teams.schema.blueprint import (
    LeaderSpec,
    TeamAgentSpec,
    TransportSpec,
)
from openjiuwen.agent_teams.schema.deep_agent_spec import (
    BuiltinToolSpec,
    DeepAgentSpec,
    RailSpec,
    TeamModelConfig,
    WorkspaceSpec,
    register_rail_provider,
)
from openjiuwen.core.foundation.llm import ModelClientConfig
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    AgentRail,
    ToolCallInputs,
)
from openjiuwen.harness.tools.worktree import WorktreeConfig
from openjiuwen.harness.prompts.builder import SystemPromptBuilder
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager
from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import (
    HEARTBEAT_TOOL_NAMES,
)
from jiuwenswarm.agents.harness.code.rails.heartbeat_rail import HeartbeatRail
from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.agents.swarm import (
    SwarmBuildContext,
    enrich_team_spec_for_swarm,
    preflight_team_mcps,
    register_swarm_providers,
)
from openjiuwen.agent_teams.rails.elements import TEAM_SKILL_USE

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import (
    build_member_capability_specs,
    build_member_deep_agent_spec,
    build_member_subagent_specs,
)
from jiuwenswarm.agents.swarm.providers import (
    code_rails,
    evolution_rails,
    member_rails,
    runtime_tools,
    tools,
)
from jiuwenswarm.agents.swarm.providers.code_subagents import (
    SWARM_BROWSER_AGENT,
    _PARENT_MODEL_EXTRAS_KEY,
    _browser_key,
    build_swarm_browser_agent,
)
from jiuwenswarm.common.coding_memory_paths import (
    resolve_project_coding_memory_dir,
)
from jiuwenswarm.common.config import get_config

logger = logging.getLogger(__name__)


@pytest.mark.parametrize("mode", ["team", "team.plan.normal", "code.team", "team.plan.code"])
def test_member_runtime_prompt_rail_binds_request_identity(mode: str) -> None:
    context = SwarmBuildContext(session_id="session-123", mode=mode)

    rail = member_rails._build_runtime_prompt_rail({}, context)

    assert rail._session_id == "session-123"
    assert rail._mode == mode


def test_team_heartbeat_provider_mounts_new_job_rail_once() -> None:
    service = object()
    context = SwarmBuildContext(
        session_id="session-123",
        channel_id="web",
        user_id="user-1",
        request_metadata={"mode": "team.work.normal"},
        mode="team.work.normal",
        member_card_id="leader-card",
        heartbeat_job_service=service,
    )

    rail = member_rails._build_heartbeat_rail({}, context)

    assert isinstance(rail, HeartbeatRail)
    assert rail._runtime._service is service
    assert rail._context.session_id == "session-123"
    assert rail._context.user_id == "user-1"

    registered_tools = {}

    class AbilityManager:
        @staticmethod
        def add_ability(card, tool) -> None:  # noqa: ANN001
            registered_tools[card.name] = tool

    rail.init(SimpleNamespace(ability_manager=AbilityManager()))
    assert set(registered_tools) == HEARTBEAT_TOOL_NAMES


@pytest.mark.parametrize(
    "mode",
    [
        "team",
        "team.plan.normal",
        "code.team",
        "team.plan.code",
        "team.work.normal",
        "team.work.plan",
        "team.code.normal",
        "team.code.plan",
    ],
)
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_all_team_modes_declare_exactly_one_new_heartbeat_rail(
    mode: str,
    role: str,
) -> None:
    rails, _ = build_member_capability_specs({}, mode, role)

    assert registry.HEARTBEAT == "swarm.heartbeat"
    assert [spec.type for spec in rails].count(registry.HEARTBEAT) == 1


@pytest.mark.parametrize("mode", ["team.code.normal", "team.code.plan"])
def test_canonical_code_team_modes_use_code_profile(mode: str) -> None:
    rails, _ = build_member_capability_specs({}, mode, "leader")
    rail_types = {spec.type for spec in rails}

    assert registry.CODE_RUNTIME_PROMPT in rail_types
    assert registry.RUNTIME_PROMPT not in rail_types

# Rail provider names shared by both roles (no role-specific evolution rails).
# Harness todo planning is teammate-only; leaders use the team task board instead.
# Sourced from the registry symbols so the test tracks renames automatically.
_TEAM_SHARED_RAIL_NAMES: frozenset[str] = frozenset(
    {
        registry.RUNTIME_PROMPT,
        registry.TEAM_SKILL_STORAGE_POLICY,
        registry.TEAM_SKILL_LIBRARY_RELOAD,
        registry.RESPONSE_PROMPT,
        registry.SYS_OPERATION,
        registry.STREAM_EVENT,
        registry.SECURITY,
        registry.MODEL_ANOMALY_DETECTION,
        registry.HEARTBEAT,
        registry.AVATAR_PROMPT,
        registry.MULTIMODAL_IMAGE,
        registry.TEAM_WORKSPACE_REPORT_PATH,
        registry.CONTEXT_PROCESSOR,
        registry.PLUGIN_RAILS,
        registry.SKILL_RETRIEVAL_PROMPT,
        registry.SYMPHONY_ORCHESTRATION_PROMPT,
        registry.MEMBER_SKILL_TOOLKIT,
        TEAM_SKILL_USE,
    }
)

# Provider names from config ``_COMMON_RAIL_NAMES`` (includes TASK_PLANNING;
# excludes MEMBER_SKILL_TOOLKIT, which is appended during team build).
_COMMON_RAIL_PROVIDER_NAMES: frozenset[str] = _TEAM_SHARED_RAIL_NAMES | {
    registry.TASK_PLANNING,
} - {registry.MEMBER_SKILL_TOOLKIT, TEAM_SKILL_USE}

_COMMON_TOOL_NAMES: frozenset[str] = frozenset(
    {
        registry.WEB_SEARCH,
        registry.WEB_FETCH,
        registry.WEB_PAID_SEARCH,
        registry.VISION,
        registry.AUDIO,
        # SKILL_TOOLKIT is no longer declared as a tool; the
        # MEMBER_SKILL_TOOLKIT rail is the sole registrar of skill tools.
        # Skill retrieval is a separate self-gated tool provider.
        registry.SKILL_RETRIEVAL,
        registry.SYMPHONY_TOOLKIT,
        registry.USER_TODOS,
        registry.VIDEO,
        registry.IMAGE_GEN,
        registry.XIAOYI_PHONE,
        registry.CRON_TOOLS,
        registry.SEND_FILE,
    }
)


def _make_team_spec() -> TeamAgentSpec:
    """Build a minimal, valid two-role team spec for enrichment tests.

    Uses bare ``DeepAgentSpec`` members (no model / card) so the spec stays
    free of any live runtime dependency; enrichment only reads/writes rails,
    tools and ``build_context``.

    Returns:
        A ``TeamAgentSpec`` with ``leader`` and ``teammate`` members.
    """
    return TeamAgentSpec(
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
        team_name="unit_team",
        leader=LeaderSpec(member_name="team_leader"),
    )


def _agentic_retrieval_config(enabled: bool = True) -> dict:
    return {"symphony": {"skill_retrieval": {"enabled": enabled}}}


def _skill_evolution_config(*, enabled: bool = True, auto_save: bool = False) -> dict:
    return {
        "react": {
            "evolution": {
                "skill_evolution": enabled,
                "auto_save": auto_save,
            }
        }
    }


@pytest.fixture(autouse=True)
def _enable_canonical_skill_evolution_for_assembly_tests(monkeypatch: pytest.MonkeyPatch):
    """Keep enrichment tests on the explicitly enabled product path."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config",
        lambda: _skill_evolution_config(),
    )


class _FakeEvolutionInterruptRail:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeEvolutionRail:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.swarm_context = {}
        self.review_feedback_config = None
        self.approval_submission_service = object()

    def bind_swarm_context(self, **kwargs) -> None:
        self.swarm_context.update(kwargs)

    def configure_review_feedback_evolution(self, **kwargs) -> None:
        self.review_feedback_config = kwargs


class _FakeMemberSkillEvolutionRail(_FakeEvolutionRail):
    pass


def _assert_evolution_approval_stack(
    built: list[object],
    rail_type: type,
    *,
    auto_save: bool,
    language: str,
):
    assert len(built) == 2
    interrupt_rail, rail = built
    assert isinstance(rail, rail_type)
    assert isinstance(interrupt_rail, _FakeEvolutionInterruptRail)
    assert "review_runtime" in rail.kwargs
    assert rail.kwargs["review_runtime"] is not None
    assert "fuzzy_review" not in rail.kwargs
    assert interrupt_rail.kwargs == {
        "review_runtime": rail.kwargs["review_runtime"],
        "submission_service": rail.approval_submission_service,
        "auto_save": auto_save,
        "language": language,
    }
    return rail


def test_register_swarm_providers_is_idempotent() -> None:
    """Two consecutive registrations must not raise and stay consistent."""
    register_swarm_providers()
    rail_after_first = {
        k for k in das._RAIL_PROVIDER_REGISTRY if k.startswith("swarm.")
    }
    tool_after_first = {
        k for k in das._TOOL_PROVIDER_REGISTRY if k.startswith("swarm.")
    }

    register_swarm_providers()
    rail_after_second = {
        k for k in das._RAIL_PROVIDER_REGISTRY if k.startswith("swarm.")
    }
    tool_after_second = {
        k for k in das._TOOL_PROVIDER_REGISTRY if k.startswith("swarm.")
    }

    assert rail_after_first == rail_after_second
    assert tool_after_first == tool_after_second
    logger.info(
        "idempotent registration: %d rail providers, %d tool providers",
        len(rail_after_second),
        len(tool_after_second),
    )


def test_register_swarm_providers_populates_registries() -> None:
    """Registration installs every common ``swarm.*`` provider name."""
    register_swarm_providers()

    rail_providers = set(das._RAIL_PROVIDER_REGISTRY)
    tool_providers = set(das._TOOL_PROVIDER_REGISTRY)

    # The legacy class-type registry is gone; every rail (including the unified
    # class rails) is now provider-backed.
    for name in _COMMON_RAIL_PROVIDER_NAMES:
        assert name in rail_providers, name
    for name in _COMMON_TOOL_NAMES:
        assert name in tool_providers, name

    # Role-specific evolution rails are provider-backed.
    assert registry.TEAM_SKILL_EVOLUTION in rail_providers
    assert registry.TEAM_SKILL_CREATE in rail_providers
    assert registry.MEMBER_SKILL_EVOLUTION in rail_providers


def test_runtime_prompt_rail_resolves_via_registry() -> None:
    """A ``RailSpec`` referencing a swarm provider builds a live rail.

    Proves the registration is wired through openjiuwen's resolution path (not
    just present as a dict key) using a fake per-member context.
    """
    register_swarm_providers()
    fake_ctx = SwarmBuildContext(language="cn", channel="web")

    rail = RailSpec(type=registry.RUNTIME_PROMPT).build(language="cn", context=fake_ctx)

    assert rail is not None
    assert rail.__class__.__name__ == "RuntimePromptRail"


@pytest.mark.asyncio
async def test_team_skill_storage_policy_rail_resolves_and_injects_paths(tmp_path: Path) -> None:
    """The team skill storage policy should inject concrete team-level paths.

    Only team-level paths belong here: they are identical for every member, so
    the team shares one cacheable prefix. The member's own workspace is
    per-member and openjiuwen's team rail tells the member about it as part of
    its identity, so this rail must not carry it in any lane.
    """
    register_swarm_providers()
    global_skills_dir = str(tmp_path / "agent" / "workspace" / "skills")
    team_ws_root = str(tmp_path / ".agent_teams" / "unit" / "team-workspace")
    team_visibility_path = str(
        tmp_path / ".agent_teams" / "unit" / "team-workspace" / "skills-visibility.json"
    )
    member_workspace_root = str(
        tmp_path / ".agent_teams" / "unit" / "workspaces" / "member_workspace"
    )
    fake_ctx = SwarmBuildContext(
        language="cn",
        global_skills_dir=global_skills_dir,
        team_ws_root=team_ws_root,
        team_skill_visibility_path=team_visibility_path,
        workspace=types.SimpleNamespace(root_path=member_workspace_root),
    )

    rail = RailSpec(type=registry.TEAM_SKILL_STORAGE_POLICY).build(
        language="cn",
        context=fake_ctx,
    )
    builder = SystemPromptBuilder(language="cn")
    manager = PromptAttachmentManager()
    rail.init(
        types.SimpleNamespace(
            system_prompt_builder=builder,
            prompt_attachment_manager=manager,
        )
    )

    session = types.SimpleNamespace(get_session_id=lambda: "sess-1")
    await rail.before_model_call(AgentCallbackContext(agent=None, inputs=None, session=session))

    content = builder.build()
    assert f"{global_skills_dir}/<skill-name>/SKILL.md" in content
    assert team_ws_root in content
    # The team declares visibility only; it owns no skills/ directory any more.
    assert team_visibility_path in content
    assert "skill-creator" not in content
    # The per-member path is not this rail's business any more.
    assert member_workspace_root not in content
    assert await manager.list_by_filter(session_id="sess-1") == []


@pytest.mark.asyncio
async def test_team_skill_library_reload_rail_reloads_agent_skill_views(
    tmp_path: Path,
) -> None:
    """The library reload rail should reload the member's own Skill views.

    Skills now live in one physical library and per-member visibility is
    metadata, so a library write only has to make the member re-read it. No
    team manager and no session id are involved any more.
    """
    register_swarm_providers()
    global_skills_dir = tmp_path / "agent" / "workspace" / "skills"
    skill_dir = global_skills_dir / "new-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\ndescription: test\n---\n", encoding="utf-8")

    reloaded: list[str] = []

    class _FakeSkillRail:
        async def reload_skills(self) -> None:
            reloaded.append("reloaded")

    fake_agent = SimpleNamespace(find_rails_by_type=lambda types_: [_FakeSkillRail()])
    fake_ctx = SwarmBuildContext(
        language="cn",
        session_id="session-1",
        channel="web",
        global_skills_dir=str(global_skills_dir),
    )

    rail = RailSpec(type=registry.TEAM_SKILL_LIBRARY_RELOAD).build(
        language="cn",
        context=fake_ctx,
    )

    await rail.after_tool_call(
        AgentCallbackContext(
            agent=fake_agent,
            inputs=ToolCallInputs(
                tool_name="write_file",
                tool_args={"file_path": str(skill_file)},
            ),
            session=None,
        )
    )

    assert reloaded == ["reloaded"]
    logger.info("skill library reload rail reloaded %d view(s)", len(reloaded))


@pytest.mark.asyncio
async def test_team_skill_library_reload_rail_ignores_writes_outside_library(
    tmp_path: Path,
) -> None:
    """Writes outside the single physical library must not trigger a reload."""
    register_swarm_providers()
    global_skills_dir = tmp_path / "agent" / "workspace" / "skills"
    global_skills_dir.mkdir(parents=True)
    outside_file = tmp_path / "notes" / "memo.md"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("hello", encoding="utf-8")

    reloaded: list[str] = []

    class _FakeSkillRail:
        async def reload_skills(self) -> None:
            reloaded.append("reloaded")

    fake_agent = SimpleNamespace(find_rails_by_type=lambda types_: [_FakeSkillRail()])
    fake_ctx = SwarmBuildContext(
        language="cn",
        session_id="session-1",
        channel="web",
        global_skills_dir=str(global_skills_dir),
    )

    rail = RailSpec(type=registry.TEAM_SKILL_LIBRARY_RELOAD).build(
        language="cn",
        context=fake_ctx,
    )

    await rail.after_tool_call(
        AgentCallbackContext(
            agent=fake_agent,
            inputs=ToolCallInputs(
                tool_name="write_file",
                tool_args={"file_path": str(outside_file)},
            ),
            session=None,
        )
    )

    assert reloaded == []


def test_unknown_swarm_rail_type_raises() -> None:
    """An unregistered ``swarm.*`` rail type surfaces a clear ``ValueError``."""
    register_swarm_providers()
    fake_ctx = SwarmBuildContext(language="cn", channel="web")

    with pytest.raises(ValueError):
        RailSpec(type="swarm.__does_not_exist__").build(
            language="cn",
            context=fake_ctx,
        )


@pytest.mark.parametrize(
    ("role", "extra_rails"),
    [
        (
            "leader",
            {
                registry.STRUCTURED_ASK_USER,
                registry.TEAM_SKILL_EVOLUTION,
                registry.TEAM_SKILL_CREATE,
            },
        ),
        (
            "teammate",
            {
                registry.MEMBER_SKILL_EVOLUTION,
            },
        ),
    ],
)
def test_build_member_capability_specs_rail_names(
    role: str,
    extra_rails: set[str],
) -> None:
    """Each role gets the common rails plus its role-specific extra rails."""
    config = {
        "agents": {
            "leader": {"skills": ["alpha"]},
            "teammate": {"skills": ["beta"]},
        },
        **_skill_evolution_config(),
    }

    rails_specs, _ = build_member_capability_specs(config, "team", role)
    rail_names = {spec.type for spec in rails_specs}

    expected = _TEAM_SHARED_RAIL_NAMES | extra_rails
    if role == "teammate":
        expected = expected | {registry.TASK_PLANNING}

    assert _TEAM_SHARED_RAIL_NAMES <= rail_names
    assert extra_rails <= rail_names
    assert len(_TEAM_SHARED_RAIL_NAMES) == 18
    assert rail_names == expected
    # No DeepAgent is involved; every entry is a plain declarative RailSpec.
    assert all(isinstance(spec, RailSpec) for spec in rails_specs)
    logger.info("%s rails: %s", role, sorted(rail_names))


@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_build_member_capability_specs_tool_names(role: str) -> None:
    """Both roles declare the common tool set (base / cron / send_file)."""
    config = {"agents": {"leader": {"skills": []}, "teammate": {"skills": []}}}

    _, tool_specs = build_member_capability_specs(config, "team", role)
    tool_names = {spec.type for spec in tool_specs}

    assert tool_names == _COMMON_TOOL_NAMES
    assert all(isinstance(spec, BuiltinToolSpec) for spec in tool_specs)


def test_role_skills_seed_only_the_team_skill_rail() -> None:
    """The role's skill selection reaches the single seeder and nothing else.

    ``core.team.skill_use`` owns the visibility document, so it is the only
    rail that may carry the seed allow-list. The toolkit rail used to carry a
    copy of it and seed the same file a second time.
    """
    config = {
        "agents": {
            "leader": {"skills": ["alpha", "  ", "beta"]},
            "teammate": {"skills": []},
        }
    }

    leader_rails, _ = build_member_capability_specs(config, "team", "leader")
    skill_rail = next(
        spec for spec in leader_rails if spec.type == TEAM_SKILL_USE
    )
    toolkit = next(
        spec for spec in leader_rails if spec.type == registry.MEMBER_SKILL_TOOLKIT
    )

    # Blank entries are stripped; order is preserved.
    assert skill_rail.params["bootstrap_allow"] == ["alpha", "beta"]
    assert not (toolkit.params or {})


def test_swarm_skill_retrieval_tools_use_live_context_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Skill retrieval receives the live library and visibility providers."""
    captured: dict[str, object] = {}

    class FakeToolkit:
        def __init__(
            self,
            *,
            skill_directories: object,
            source_by_name: object,
            visible_skill_names: object,
            disabled_skills: object = None,
            session_scope: str = "default",
            config_base: dict[str, Any] | None = None,
            **_kwargs: object,
        ) -> None:
            captured["skill_directories"] = skill_directories
            captured["disabled_skills"] = disabled_skills
            captured["source_by_name"] = source_by_name
            captured["visible_skill_names"] = visible_skill_names
            captured["session_scope"] = session_scope
            captured["config_base"] = config_base

        @staticmethod
        def get_tools() -> list:
            return []

    monkeypatch.setattr(
        tools,
        "is_skill_retrieval_enabled",
        lambda _config_base=None: True,
    )
    monkeypatch.setattr(tools, "SkillRetrievalToolkit", FakeToolkit)

    factory = resolve_factory(get_catalog()[registry.SKILL_RETRIEVAL].factory_ref)
    global_skills_dir = tmp_path / "global-skills"
    global_skills_dir.mkdir()
    built = factory(
        {},
        SwarmBuildContext(global_skills_dir=str(global_skills_dir)),
    )

    assert built == []
    directories = captured["skill_directories"]
    disabled = captured["disabled_skills"]
    sources = captured["source_by_name"]
    visible = captured["visible_skill_names"]
    assert callable(directories)
    assert callable(sources)
    assert callable(visible)
    assert directories() == [str(global_skills_dir)]
    assert disabled is tools.load_execution_disabled_skills
    assert isinstance(sources(), dict)
    assert visible() == set()
    assert captured["session_scope"] == "default:team:member"
    assert captured["config_base"] is None


def _install_library_skill(library_dir: Path, name: str) -> None:
    """Create a minimal Skill entity inside the single physical library."""
    skill_dir = library_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}", encoding="utf-8")


def test_swarm_list_skill_sees_whole_library_without_visibility_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No metadata document means "inherit the whole library", not "deny all"."""
    from jiuwenswarm.agents.swarm.providers import skills as skills_provider

    library = tmp_path / "library"
    for name in ("alpha", "beta"):
        _install_library_skill(library, name)
    member_root = tmp_path / "workspaces" / "coder_workspace"
    member_root.mkdir(parents=True)
    monkeypatch.setattr(skills_provider, "_load_global_disabled_skills", list)

    ctx = SwarmBuildContext(
        mode="team",
        team_id="unit-team",
        member_name="coder",
        global_skills_dir=str(library),
        workspace=SimpleNamespace(root_path=str(member_root)),
    )

    assert tools.visible_skill_names_for_list_skill(ctx) == {"alpha", "beta"}


def test_swarm_list_skill_composes_member_team_and_global_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_skill`` filters the one library by the composed visibility documents."""
    from openjiuwen.agent_teams.skill import SCOPE_MEMBER, SCOPE_TEAM, set_skill_visibility

    from jiuwenswarm.agents.swarm.providers import skills as skills_provider

    library = tmp_path / "library"
    for name in ("alpha", "beta", "gamma", "blocked"):
        _install_library_skill(library, name)
    member_root = tmp_path / "workspaces" / "coder_workspace"
    member_root.mkdir(parents=True)
    team_root = tmp_path / "team-workspace"
    team_root.mkdir(parents=True)

    set_skill_visibility(
        member_root / "skills-visibility.json",
        scope=SCOPE_MEMBER,
        entity_id="coder",
        allow=["alpha", "blocked"],
        deny=["gamma"],
    )
    set_skill_visibility(
        team_root / "skills-visibility.json",
        scope=SCOPE_TEAM,
        entity_id="unit-team",
        allow=["beta"],
        deny=[],
    )
    monkeypatch.setattr(
        skills_provider,
        "_load_global_disabled_skills",
        lambda: ["blocked"],
    )

    ctx = SwarmBuildContext(
        mode="team",
        team_id="unit-team",
        member_name="coder",
        global_skills_dir=str(library),
        team_skill_visibility_path=str(team_root / "skills-visibility.json"),
        workspace=SimpleNamespace(root_path=str(member_root)),
    )

    # allow is the union of both documents; deny (member + global) always wins.
    assert tools.visible_skill_names_for_list_skill(ctx) == {"alpha", "beta"}


def test_swarm_skill_retrieval_prompt_uses_same_live_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The retrieval prompt uses the same live inventory as the retrieval tool."""
    monkeypatch.setenv(
        "SYMPHONY_SKILL_RETRIEVAL_ROOT",
        str(tmp_path / "skillfs-artifacts"),
    )
    workspace_root = str(tmp_path / "member-workspace")
    global_skills_dir = tmp_path / "global-skills"
    skill_dir = global_skills_dir / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Demo Skill\ndescription: live demo\n---\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits.is_skill_retrieval_enabled",
        lambda _config_base=None: True,
    )

    factory = resolve_factory(get_catalog()[registry.SKILL_RETRIEVAL_PROMPT].factory_ref)
    rail = factory(
        {},
        SwarmBuildContext(
            global_skills_dir=str(global_skills_dir),
            workspace=types.SimpleNamespace(root_path=workspace_root),
        ),
    )

    assert rail is not None
    assert rail._prompt_skillfs is not None
    assert set(rail._prompt_skillfs.selection_cards()) == {"demo-skill"}


@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_team_member_deep_agent_spec_uses_agentic_skill_disclosure(role: str) -> None:
    """Chat-team members avoid the core full-skill discovery path."""
    base = DeepAgentSpec(enable_skill_discovery=False)

    spec = build_member_deep_agent_spec(_agentic_retrieval_config(), "team", role, base)
    skill_rails = [rail for rail in (spec.rails or []) if rail.type == TEAM_SKILL_USE]

    assert spec.enable_skill_discovery is False
    assert len(skill_rails) == 1
    assert skill_rails[0].params["skill_mode"] == SkillUseRail.SKILL_MODE_AUTO_LIST
    assert skill_rails[0].params["include_tools"] is False


@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_team_member_deep_agent_spec_normalizes_existing_skill_use_rail(role: str) -> None:
    """Chat-team members normalize an existing skill rail to auto-list mode."""
    base = DeepAgentSpec(
        enable_skill_discovery=False,
        rails=[
            RailSpec(
                type="SkillUseRail",
                params={"skill_mode": SkillUseRail.SKILL_MODE_ALL},
            )
        ],
    )

    spec = build_member_deep_agent_spec(_agentic_retrieval_config(), "team", role, base)
    skill_rails = [rail for rail in (spec.rails or []) if rail.type in {CORE_SKILL_USE, "SkillUseRail"}]

    assert spec.enable_skill_discovery is False
    assert len(skill_rails) == 1
    assert skill_rails[0].type == "SkillUseRail"
    assert skill_rails[0].params["skill_mode"] == SkillUseRail.SKILL_MODE_AUTO_LIST
    assert skill_rails[0].params["include_tools"] is False


@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_team_member_deep_agent_spec_keeps_core_skill_discovery_when_retrieval_disabled(role: str) -> None:
    """Chat-team members keep the original skill discovery path when retrieval is disabled."""
    base = DeepAgentSpec(enable_skill_discovery=False)

    spec = build_member_deep_agent_spec(_agentic_retrieval_config(False), "team", role, base)

    assert spec.enable_skill_discovery is True


@pytest.mark.parametrize("mode", ["code.team", "team.plan.code"])
def test_code_member_deep_agent_spec_keeps_skill_use_rail_when_retrieval_enabled(mode: str) -> None:
    """Code profiles keep skill_tool access without all-mode skill injection."""
    base = DeepAgentSpec(enable_skill_discovery=False)

    spec = build_member_deep_agent_spec(_agentic_retrieval_config(), mode, "leader", base)
    skill_rails = [rail for rail in (spec.rails or []) if rail.type == TEAM_SKILL_USE]

    assert spec.enable_skill_discovery is False
    assert len(skill_rails) == 1
    assert skill_rails[0].params["skill_mode"] == SkillUseRail.SKILL_MODE_AUTO_LIST


@pytest.mark.parametrize("mode", ["code.team", "team.plan.code"])
def test_code_member_deep_agent_spec_keeps_skill_use_rail_when_retrieval_disabled(mode: str) -> None:
    """Code profiles keep their explicit SkillUseRail provider when retrieval is disabled."""
    base = DeepAgentSpec(enable_skill_discovery=False)

    spec = build_member_deep_agent_spec(_agentic_retrieval_config(False), mode, "leader", base)
    rail_names = {rail.type for rail in (spec.rails or [])}

    assert spec.enable_skill_discovery is False
    assert TEAM_SKILL_USE in rail_names


def test_member_deep_agent_spec_merges_config_mcp_configs() -> None:
    """Member specs inherit MCP declarations from config while preserving base MCPs."""
    base_mcp = McpServerConfig(
        server_id="base-id",
        server_name="base_mcp",
        server_path="stdio://base_mcp",
        client_type="stdio",
        params={"command": "python"},
    )
    config_mcp = McpServerConfig(
        server_id="config-id",
        server_name="config_mcp",
        server_path="stdio://config_mcp",
        client_type="stdio",
        params={"command": "node"},
    )
    base = DeepAgentSpec(enable_skill_discovery=False, mcps=[base_mcp])

    spec = build_member_deep_agent_spec(
        _agentic_retrieval_config(),
        "team",
        "leader",
        base,
        mcp_configs=[config_mcp],
    )

    assert [cfg.server_name for cfg in (spec.mcps or [])] == ["base_mcp", "config_mcp"]
    assert spec.mcps[1] is not config_mcp


def test_member_deep_agent_spec_keeps_base_mcp_on_duplicate_name() -> None:
    """An explicitly declared member MCP wins over config with the same server name."""
    base_mcp = McpServerConfig(
        server_id="base-id",
        server_name="shared_mcp",
        server_path="stdio://shared_mcp",
        client_type="stdio",
        params={"command": "python"},
    )
    config_mcp = McpServerConfig(
        server_id="config-id",
        server_name="shared_mcp",
        server_path="stdio://shared_mcp",
        client_type="stdio",
        params={"command": "node"},
    )

    spec = build_member_deep_agent_spec(
        _agentic_retrieval_config(),
        "team",
        "leader",
        DeepAgentSpec(mcps=[base_mcp]),
        mcp_configs=[config_mcp],
    )

    assert [cfg.server_id for cfg in (spec.mcps or [])] == ["base-id"]


def test_enrich_team_spec_for_swarm_has_no_deep_agent_param() -> None:
    """The enrichment seam must never accept a pre-built DeepAgent."""
    params = set(inspect.signature(enrich_team_spec_for_swarm).parameters)

    assert "deep_agent" not in params
    assert "deep_agent_spec" not in params
    # The exact public surface is the session/request descriptors only.
    assert params == {
        "spec",
        "session_id",
        "mode",
        "project_dir",
        "trusted_dirs",
        "request_id",
        "user_id",
        "channel_id",
        "request_metadata",
        "agent_group_name",
    }


def test_enrich_team_spec_for_swarm_rewrites_spec_in_place() -> None:
    """Enrichment rewrites member rails and attaches the build context."""
    spec = _make_team_spec()

    result = enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="team",
        channel_id="web",
    )

    # Mutates in place and returns nothing.
    assert result is None

    leader_rail_names = {rail.type for rail in (spec.agents["leader"].rails or [])}
    teammate_rail_names = {rail.type for rail in (spec.agents["teammate"].rails or [])}
    assert any(name.startswith("swarm.") for name in leader_rail_names)
    assert registry.TEAM_SKILL_EVOLUTION in leader_rail_names
    assert registry.MEMBER_SKILL_EVOLUTION in teammate_rail_names

    # Build context carries the per-team handles.
    assert isinstance(spec.build_context, SwarmBuildContext)
    assert spec.build_context.session_id == "s"
    assert spec.build_context.team_id == "unit_team"

    # The parent-free contract: openjiuwen removed the imperative customizer
    # hook entirely, so the field no longer exists on the spec.
    assert not hasattr(spec, "agent_customizer")


@pytest.mark.parametrize(
    ("channel_id", "port_env", "port", "expected_url"),
    [
        ("web", "WEB_PORT", "29000", "ws://127.0.0.1:29000/ws"),
        ("tui", "GATEWAY_PORT", "29001", "ws://127.0.0.1:29001/tui"),
    ],
)
def test_enrich_team_spec_configures_external_cli_event_relay(
    channel_id: str,
    port_env: str,
    port: str,
    expected_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External CLI events must return through the active client channel."""
    monkeypatch.delenv("TEAM_EVENT_GATEWAY_WS_URL", raising=False)
    monkeypatch.setenv(port_env, port)
    spec = _make_team_spec()

    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="team",
        channel_id=channel_id,
    )

    assert spec.external_transport is not None
    assert spec.external_transport.type == "hybrid"
    transport = spec.external_transport.build()
    assert transport.team_name == spec.team_name
    assert transport.external_publish_url == expected_url


def test_enrich_team_spec_preserves_explicit_external_transport() -> None:
    """Deployments may provide their own cross-process team transport."""
    spec = _make_team_spec()
    configured_transport = TransportSpec(
        type="hybrid",
        params={"external_publish_url": "wss://relay.example.test/team-events"},
    )
    spec.external_transport = configured_transport

    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="team",
        channel_id="web",
    )

    assert spec.external_transport is configured_transport


def test_enrich_team_spec_resolves_team_visibility_path_without_writing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assembly injects the team document path but is not its seeder.

    The team-scope ``skills-visibility.json`` has exactly one writer,
    ``TeamWorkspaceManager.initialize``. Assembly only resolves where that
    document lives so the rails can read it; a missing file reads back as "no
    restriction", so there is nothing to create here.
    """
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_agent_skills_dir",
        lambda: tmp_path / "global-skills",
    )
    team_ws_root = tmp_path / "team-workspace"
    spec = _make_team_spec()
    spec.workspace = WorkspaceSpec(root_path=str(team_ws_root))

    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    visibility_path = team_ws_root / "skills-visibility.json"
    assert spec.build_context.team_skill_visibility_path == str(visibility_path)
    assert not visibility_path.exists()


def test_enrich_team_spec_points_member_cwd_at_project_dir() -> None:
    """Core receives project-rooted member cwd, not a rewritten workspace.

    cwd and workspace are separate layers: members run in the project
    directory while keeping their own workspace for artifacts.
    """
    spec = _make_team_spec()
    spec.worktree = WorktreeConfig(enabled=True)

    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/project",
        channel_id="web",
    )

    for role in ("leader", "teammate"):
        assert spec.agents[role].cwd == "/tmp/project"
        assert spec.agents[role].project_root == "/tmp/project"
        assert spec.agents[role].workspace is None


def test_enrich_team_spec_leaves_workspace_when_worktree_disabled() -> None:
    """Non-worktree teams keep their existing member workspace semantics."""
    spec = _make_team_spec()

    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/project",
        channel_id="web",
    )

    assert spec.agents["leader"].workspace is None
    assert spec.agents["teammate"].workspace is None
    # cwd is seeded regardless of worktree isolation.
    assert spec.agents["leader"].cwd == "/tmp/project"
    assert spec.agents["teammate"].cwd == "/tmp/project"


def test_enrich_team_spec_preserves_explicit_member_workspace() -> None:
    """A configured member workspace survives; only cwd is seeded."""
    spec = _make_team_spec()
    spec.worktree = WorktreeConfig(enabled=True)
    spec.agents["leader"].workspace = WorkspaceSpec(root_path="/tmp/custom")

    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/project",
        channel_id="web",
    )

    assert spec.agents["leader"].workspace.root_path == "/tmp/custom"
    assert spec.agents["teammate"].workspace is None
    assert spec.agents["leader"].cwd == "/tmp/project"
    assert spec.agents["teammate"].cwd == "/tmp/project"


def test_enrich_team_spec_appends_after_existing_rails(monkeypatch) -> None:
    """Provider rails are appended after a member's pre-existing rails."""
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.config_specs._retrieval_enabled",
        lambda config=None: False,
    )
    spec = _make_team_spec()
    # Seed the leader with a non-swarm rail to prove ordering is preserved.
    spec.agents["leader"].rails = [RailSpec(type="skill_use")]

    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    leader_rail_types = [rail.type for rail in (spec.agents["leader"].rails or [])]
    assert leader_rail_types[0] == "skill_use"
    assert leader_rail_types.count("skill_use") == 1
    assert len(leader_rail_types) > 1


@pytest.mark.parametrize("mode", ["team", "team.plan.normal", "code.team", "team.plan.code"])
def test_enrich_team_spec_for_swarm_injects_config_mcp_servers(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Enabled config MCP servers are mounted on every declarative team member."""
    config = {
        "mcp": {
            "servers": [
                {
                    "name": "local_tool",
                    "enabled": True,
                    "transport": "stdio",
                    "command": "python",
                    "args": ["server.py"],
                    "cwd": str(tmp_path),
                },
                {
                    "name": "disabled_tool",
                    "enabled": False,
                    "transport": "stdio",
                    "command": "python",
                },
                {
                    "name": "invalid_tool",
                    "enabled": True,
                    "transport": "stdio",
                },
            ],
        },
    }
    monkeypatch.setattr("jiuwenswarm.agents.swarm.assembly.get_config", lambda: config)
    # extract_enabled_mcp_server_entries reads get_mcp_servers() (which merges
    # config.yaml + state.json from disk) rather than the passed config_base,
    # so mock it at the source to inject the test's servers.
    test_servers = [s for s in config["mcp"]["servers"] if isinstance(s, dict)]
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_mcp_servers",
        lambda: test_servers,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_agent_skills_dir",
        lambda: tmp_path / "global-skills",
    )
    spec = _make_team_spec()

    enrich_team_spec_for_swarm(spec, session_id="s", mode=mode, channel_id="web")

    leader_mcps = spec.agents["leader"].mcps or []
    teammate_mcps = spec.agents["teammate"].mcps or []
    assert [cfg.server_name for cfg in leader_mcps] == ["local_tool"]
    assert [cfg.server_name for cfg in teammate_mcps] == ["local_tool"]
    assert leader_mcps[0].server_id == teammate_mcps[0].server_id
    assert leader_mcps[0].client_type == "stdio"
    assert leader_mcps[0].params == {
        "command": "python",
        "args": ["server.py"],
        "cwd": str(tmp_path),
    }


@pytest.mark.asyncio
async def test_preflight_team_mcps_drops_unreachable_and_degrades_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad HTTP MCP is dropped from every member + degraded to disconnected;
    the good MCP stays; the team assembly doesn't raise."""
    good_mcp = McpServerConfig(
        server_id="good-sid",
        server_name="good_http",
        server_path="https://good.example.com/mcp",
        client_type="streamable-http",
        auth_headers={"Authorization": "Bearer real-token"},
    )
    bad_mcp = McpServerConfig(
        server_id="bad-sid",
        server_name="bad_http",
        server_path="https://bad.example.com/mcp",
        client_type="streamable-http",
        auth_headers={"Authorization": "Bearer ${GITHUB_TOKEN}"},
    )
    spec = TeamAgentSpec(
        agents={
            "leader": DeepAgentSpec(mcps=[good_mcp, bad_mcp]),
            "teammate": DeepAgentSpec(mcps=[good_mcp, bad_mcp]),
        },
        team_name="probe_team",
        leader=LeaderSpec(member_name="team_leader"),
    )

    # Probe: good → reachable, bad → 401. Names drive the verdict so the
    # shared-config dedup path (by server_id) is also exercised.
    async def fake_probe(cfg: McpServerConfig, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        if cfg.server_name == "bad_http":
            return False, "http 401 from server"
        return True, ""

    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.preflight_mcp_server_reachable",
        fake_probe,
    )
    degraded: list[str] = []
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state",
        lambda name, *, state: degraded.append(name),
    )

    dropped = await preflight_team_mcps(spec)

    assert dropped == ["bad_http"]
    assert degraded == ["bad_http"]
    leader_names = [c.server_name for c in (spec.agents["leader"].mcps or [])]
    teammate_names = [c.server_name for c in (spec.agents["teammate"].mcps or [])]
    assert leader_names == ["good_http"]
    assert teammate_names == ["good_http"]


@pytest.mark.asyncio
async def test_preflight_team_mcps_keeps_stdio_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdio MCPs report reachable (no HTTP probe) and are kept as-is."""
    stdio_mcp = McpServerConfig(
        server_id="stdio-sid",
        server_name="local_tool",
        server_path="stdio://local_tool",
        client_type="stdio",
        params={"command": "python"},
    )
    spec = TeamAgentSpec(
        agents={"leader": DeepAgentSpec(mcps=[stdio_mcp])},
        team_name="stdio_team",
        leader=LeaderSpec(member_name="team_leader"),
    )

    probe_calls: list[str] = []
    real_probe = __import__(
        "jiuwenswarm.common.mcp_config", fromlist=["preflight_mcp_server_reachable"]
    ).preflight_mcp_server_reachable

    async def spy(cfg: McpServerConfig, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        probe_calls.append(cfg.server_name)
        return await real_probe(cfg)

    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.preflight_mcp_server_reachable",
        spy,
    )

    dropped = await preflight_team_mcps(spec)

    assert dropped == []
    assert (spec.agents["leader"].mcps or [])[0].server_name == "local_tool"
    # stdio is reported reachable without a real HTTP probe (no network).
    assert probe_calls == ["local_tool"]


def test_build_enabled_mcp_server_configs_resolves_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_credentials=True substitutes ${VAR} from CredentialStore."""
    entry = {
        "name": "github_http",
        "enabled": True,
        "transport": "streamable-http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
    }
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_mcp_servers",
        lambda: [entry],
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.mcp_config.CredentialStore.get_all",
        lambda self, name: {"GITHUB_TOKEN": "ghp_real_123"} if name == "github_http" else {},
    )

    from jiuwenswarm.common.mcp_config import build_enabled_mcp_server_configs

    configs = build_enabled_mcp_server_configs(
        {}, server_id_scope="team:unit_team", resolve_credentials=True
    )
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.server_name == "github_http"
    # auth_headers carry the resolved real token, not the literal placeholder.
    assert cfg.auth_headers["Authorization"] == "Bearer ghp_real_123"


def test_enrich_skips_absent_roles_gracefully() -> None:
    """A team without a teammate role is enriched without error."""
    spec = TeamAgentSpec(
        agents={"leader": DeepAgentSpec()},
        team_name="solo_team",
        leader=LeaderSpec(member_name="team_leader"),
    )

    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    assert "teammate" not in spec.agents
    leader_rail_names = {rail.type for rail in (spec.agents["leader"].rails or [])}
    assert registry.TEAM_SKILL_EVOLUTION in leader_rail_names


def test_enriched_spec_serialization_round_trip() -> None:
    """The enriched spec dumps to JSON with the build context excluded."""
    spec = _make_team_spec()
    enrich_team_spec_for_swarm(spec, session_id="s", mode="team", channel_id="web")

    dumped = spec.model_dump_json()
    data = json.loads(dumped)

    # ``build_context`` (live carrier) is excluded; ``agent_customizer`` was
    # removed from the spec entirely (F_32) and must not reappear.
    assert "build_context" not in data
    assert "agent_customizer" not in data

    # Rails serialize as {type, params} declarative references.
    leader_rails = data["agents"]["leader"]["rails"]
    assert leader_rails, "expected leader rails in the dumped spec"
    for rail in leader_rails:
        assert set(rail.keys()) == {"type", "params"}
        assert isinstance(rail["type"], str) and rail["type"]
    # The member profile mixes swarm-owned (swarm.*) and openjiuwen-provided
    # (bare name) rails after the element unification.
    rail_types = {rail["type"] for rail in leader_rails}
    assert any(name.startswith("swarm.") for name in rail_types)
    assert any(not name.startswith("swarm.") for name in rail_types)


def test_enrich_applies_agent_group_as_hybrid_member_snapshots(monkeypatch) -> None:
    """AgentGroup prompts stay Team-owned while capabilities use snapshots."""
    from jiuwenswarm.server.runtime import extension_package_manager as package_manager

    resources = package_manager.get_equipment_resources_agent_groups_dir()
    assert resources is not None
    monkeypatch.setattr(
        package_manager,
        "resolve_agent_group_dir",
        lambda _name: resources / "sample-expert-group",
    )
    spec = _make_team_spec()
    spec.leader.prompt = "existing leader agreement"

    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="team",
        channel_id="web",
        agent_group_name="sample-expert-group",
    )

    assert spec.team_mode == "hybrid"
    assert [member.member_name for member in spec.predefined_members] == [
        "member1",
        "member2",
    ]
    assert set(spec.agents) >= {"leader", "teammate", "member1", "member2"}

    assert spec.leader.prompt.startswith("existing leader agreement\n\n")
    assert "# Expert Group Leader" in spec.leader.prompt
    assert "# 专家团负责人" in spec.leader.prompt
    assert "Leader 负责理解用户目标" in spec.leader.prompt

    predefined = {member.member_name: member for member in spec.predefined_members}
    assert "# 方案分析专家" in predefined["member1"].prompt
    assert "# 风险与质量复核专家" in predefined["member2"].prompt
    for member in predefined.values():
        assert "Leader 负责理解用户目标" in member.prompt

    for name in ("leader", "member1", "member2"):
        member_spec = spec.agents[name]
        snapshot = member_spec.agent_template_spec
        assert snapshot is not None
        assert snapshot["agent_card"]["id"] == name
        assert snapshot["prompt_sections"] == []
        assert "skill_name_1" in (member_spec.skills or [])

    # The generic teammate remains an unbound fallback; exact member names win
    # in AgentConfigurator.resolve_agent_spec.
    assert getattr(spec.agents["teammate"], "agent_template_spec", None) is None

    # This JiuwenSwarm PR is paired with the AgentTemplate snapshot change in
    # agent-core.  Keep the assembly assertions runnable against the preceding
    # core baseline used by independent CI, while exercising JSON round-trip as
    # soon as that paired schema field is available.
    if "agent_template_spec" in DeepAgentSpec.model_fields:
        restored = TeamAgentSpec.model_validate_json(spec.model_dump_json())
        assert restored.agents["member1"].agent_template_spec is not None
        restored_members = {
            member.member_name: member for member in restored.predefined_members
        }
        assert restored_members["member1"].prompt == predefined["member1"].prompt
        assert restored.leader.prompt == spec.leader.prompt


def test_send_file_returns_empty_without_request_id() -> None:
    """The send-file provider skips (returns []) when request_id is missing."""
    ctx = SwarmBuildContext(
        session_id="s",
        request_id=None,
        channel_id="web",
        config={},
    )

    assert runtime_tools.build_send_file_tools({}, ctx) == []


def test_send_file_returns_empty_without_channel_id() -> None:
    """The send-file provider skips (returns []) when channel_id is missing."""
    ctx = SwarmBuildContext(
        session_id="s",
        request_id="r",
        channel_id=None,
        config={},
    )

    assert runtime_tools.build_send_file_tools({}, ctx) == []


def test_send_file_gating_defaults_by_channel() -> None:
    """File sending defaults on for ``web`` and off for other channels."""
    assert runtime_tools._is_send_file_enabled(None, "web")
    assert not runtime_tools._is_send_file_enabled(None, "feishu")
    # Explicit config switch overrides the default.
    disabled = {"channels": {"web": {"send_file_allowed": False}}}
    assert not runtime_tools._is_send_file_enabled(disabled, "web")


def test_cron_tools_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cron provider builds the member-scoped toolkit via CronRuntimeBridge."""

    class _FakeCronBridge:
        def build_tools(self, *, context, agent_id, language="cn"):
            return [
                types.SimpleNamespace(
                    card=types.SimpleNamespace(
                        name="cron_list_jobs", id=f"cron_{agent_id}"
                    )
                )
            ]

    monkeypatch.setattr(runtime_tools, "CronRuntimeBridge", _FakeCronBridge)
    ctx = SwarmBuildContext(member_card_id="m1", channel_id="web", session_id="s")

    built = runtime_tools.build_cron_tools({}, ctx)

    assert [tool.card.name for tool in built] == ["cron_list_jobs"]


def test_context_processor_returns_none_when_engine_disabled() -> None:
    """The context-processor provider returns None when the engine is off.

    The enabled flag is a config-derived attribute baked into ``params`` by
    config_specs; the provider gates on it directly.
    """
    ctx = SwarmBuildContext()

    assert (
        member_rails._build_context_processor({"context_engine_enabled": False}, ctx)
        is None
    )


def test_model_anomaly_detection_provider_wires_tool_loop_compact() -> None:
    """Swarm provider mounts configured ModelAnomalyDetectionRail with compact on."""
    from openjiuwen.harness.rails import ModelAnomalyDetectionRail

    ctx = SwarmBuildContext()
    rail = member_rails._build_model_anomaly_detection(
        {
            "rail_config": {
                "enabled": True,
                "tool_loop_compact": {"enabled": True, "consecutive_threshold": 4},
            }
        },
        ctx,
    )
    assert isinstance(rail, ModelAnomalyDetectionRail)
    assert rail._tool_loop_compact.enabled is True  # pylint: disable=protected-access


def test_model_anomaly_detection_params_from_execution_guard() -> None:
    """config_specs forwards execution_guard.model_anomaly_detection_rail section."""
    from jiuwenswarm.agents.swarm.config_specs import _model_anomaly_detection_params

    params = _model_anomaly_detection_params(
        {
            "execution_guard": {
                "model_anomaly_detection_rail": {
                    "enabled": True,
                    "tool_loop_compact": {"enabled": True},
                }
            }
        }
    )
    assert params["rail_config"]["enabled"] is True
    assert params["rail_config"]["tool_loop_compact"]["enabled"] is True


def test_team_workspace_report_path_returns_none_without_root() -> None:
    """The report-path rail is skipped when no team workspace root is set."""
    ctx = SwarmBuildContext(team_ws_root=None)

    assert member_rails._build_team_workspace_report_path_rail({}, ctx) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "code.team"])
async def test_team_workspace_policy_keeps_project_deliverables_in_project(
    mode: str,
    tmp_path: Path,
) -> None:
    """Both team profiles separate project files from collaboration artifacts."""
    project_dir = str(tmp_path / "project")
    team_ws_root = str(tmp_path / "team-workspace")
    context = SwarmBuildContext(
        mode=mode,
        project_dir=project_dir,
        team_id="unit-team",
        team_ws_root=team_ws_root,
        language="cn",
    )

    rail = member_rails._build_team_workspace_report_path_rail({}, context)
    assert rail is not None
    assert rail._project_dir == project_dir

    builder = SystemPromptBuilder(language="cn")
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    await rail.before_model_call(
        AgentCallbackContext(agent=None, inputs=None, session=SimpleNamespace())
    )

    content = builder.build()
    assert f"User project root: `{project_dir}`" in content
    assert f"Team collaboration workspace: `{team_ws_root}`" in content
    assert "Source code, tests, configuration" in content
    assert "When worktree isolation is active" in content
    assert "Do not place final project files in the team collaboration workspace" in content
    assert "Use the internal mount path only" not in content


@pytest.mark.asyncio
async def test_team_workspace_policy_does_not_fallback_project_files_to_team_workspace(
    tmp_path: Path,
) -> None:
    """Missing project identity must not turn the team workspace into a project cwd."""
    team_ws_root = str(tmp_path / "team-workspace")
    context = SwarmBuildContext(
        mode="team",
        team_id="unit-team",
        team_ws_root=team_ws_root,
        language="cn",
    )

    rail = member_rails._build_team_workspace_report_path_rail({}, context)
    assert rail is not None
    builder = SystemPromptBuilder(language="cn")
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    await rail.before_model_call(
        AgentCallbackContext(agent=None, inputs=None, session=SimpleNamespace())
    )

    content = builder.build()
    assert "User project root: unavailable" in content
    assert "Do not silently use the team collaboration workspace" in content


@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_disabled_team_specs_keep_mount_context_provider_without_evolution_rails(
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled team still mounts the non-evolution context lifecycle rail."""
    config = _skill_evolution_config(enabled=False)
    rails, _ = build_member_capability_specs(config, "team", role)
    rail_types = {rail.type for rail in rails}

    assert registry.TEAM_WORKSPACE_REPORT_PATH in rail_types
    assert all("evolution" not in rail_type for rail_type in rail_types)

    context = SwarmBuildContext(
        config=config,
        role=role,
        member_name=f"{role}-member",
        member_card_id=f"{role}-card",
        session_id="disabled-session",
        channel="web",
        team_id="disabled-team",
        team_ws_root="/tmp/disabled-team",
        project_dir="/tmp/user-project",
        team_skill_visibility_path="/tmp/disabled-team/skills-visibility.json",
        trajectory_span_processor=object(),
    )
    rail = member_rails._build_team_workspace_report_path_rail({}, context)
    assert rail is not None

    captured: dict[str, object] = {}

    class _RecorderTeamManager:
        def get_team_rail_context(self, session_id):
            return None

        def register_team_rail_context(self, session_id, mount_context) -> None:
            captured["leader"] = mount_context

        def register_team_member_rail_context(self, session_id, mount_context) -> None:
            captured["teammate"] = mount_context

    import jiuwenswarm.agents.harness.team.team_manager as team_manager_module

    monkeypatch.setattr(
        team_manager_module,
        "get_team_manager",
        lambda channel=None: _RecorderTeamManager(),
    )
    rail.init(types.SimpleNamespace(card=types.SimpleNamespace(name=f"{role}-member")))

    mount_context = captured[role]
    assert mount_context.member_info.role == role
    assert mount_context.team_workspace.team_id == "disabled-team"
    assert mount_context.team_workspace.project_dir == "/tmp/user-project"
    assert mount_context.team_workspace.config == config


# ---------------------------------------------------------------------------
# Multimodal / xiaoyi tool gating (config-sourced base tools)
# ---------------------------------------------------------------------------


def test_xiaoyi_phone_tools_gated_by_config() -> None:
    """xiaoyi phone tools are built only when the channel switch is on."""
    enabled = SwarmBuildContext(
        config={"channels": {"xiaoyi": {"phone_tools_enabled": True}}},
    )
    disabled = SwarmBuildContext(config={})

    assert len(tools._build_xiaoyi_phone_tools(enabled)) == 27
    assert tools._build_xiaoyi_phone_tools(disabled) == []


def test_video_tool_gated_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The video tool is built only when models.video is configured."""
    ctx = SwarmBuildContext(config={})
    # Gate closed: empty config has no dedicated video model.
    assert tools._build_video_tools(ctx) == []

    # Gate open: complete config reported + VIDEO_API_KEY present.
    monkeypatch.setattr(tools, "apply_video_model_config_from_yaml", lambda cfg: None)
    monkeypatch.setattr(
        tools, "complete_multimodal_model_configured", lambda cfg, kind: True
    )
    monkeypatch.setattr(tools, "multimodal_model_enabled", lambda cfg, kind: True)
    monkeypatch.setenv("VIDEO_API_KEY", "k")
    monkeypatch.setenv("VIDEO_API_BASE", "https://video.example/v1")
    monkeypatch.setenv("VIDEO_MODEL_NAME", "video-model")
    built = tools._build_video_tools(ctx)
    assert [tool.card.name for tool in built] == ["video_understanding"]


def test_image_gen_tool_gated_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Image/video generation tools are built when media-gen credentials exist."""
    ctx = SwarmBuildContext(config={})
    monkeypatch.setattr(
        tools, "apply_image_gen_model_config_from_yaml", lambda cfg: None
    )
    monkeypatch.setattr(
        tools, "apply_video_gen_model_config_from_yaml", lambda cfg: None
    )
    monkeypatch.delenv("IMAGE_GEN_API_KEY", raising=False)
    monkeypatch.delenv("VIDEO_GEN_API_KEY", raising=False)
    assert tools._build_image_gen_tools(ctx) == []

    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.delenv("VIDEO_GEN_API_KEY", raising=False)
    built = tools._build_image_gen_tools(ctx)
    assert [tool.card.name for tool in built] == ["generate_image"]

    monkeypatch.delenv("IMAGE_GEN_API_KEY", raising=False)
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "vk")
    built_video = tools._build_image_gen_tools(ctx)
    assert [tool.card.name for tool in built_video] == ["generate_video"]

    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "vk")
    built_both = tools._build_image_gen_tools(ctx)
    assert [tool.card.name for tool in built_both] == [
        "generate_image",
        "generate_video",
    ]


def test_symphony_toolkit_is_leader_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symphony tools are built only for the team leader."""
    seen_configs: list[dict] = []
    fake_tool = types.SimpleNamespace(
        card=types.SimpleNamespace(name="symphony_compose_graph")
    )

    class FakeSymphonyToolkit:
        @staticmethod
        def get_tools(config_base):
            seen_configs.append(config_base)
            return [fake_tool] if config_base["symphony"]["enabled"] else []

    monkeypatch.setattr(tools, "SymphonyToolkit", FakeSymphonyToolkit)
    monkeypatch.setattr(
        tools,
        "get_config",
        lambda: {"symphony": {"enabled": True}},
    )

    leader = SwarmBuildContext(role="leader")
    teammate = SwarmBuildContext(role="teammate")

    built = tools.build_symphony_toolkit({}, leader)

    assert [tool.card.name for tool in built] == ["symphony_compose_graph"]
    assert tools.build_symphony_toolkit({}, teammate) == []
    assert seen_configs == [{"symphony": {"enabled": True}}]


def test_symphony_toolkit_respects_disabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider delegates symphony.enabled gating to SymphonyToolkit."""
    monkeypatch.setattr(
        tools,
        "get_config",
        lambda: {"symphony": {"enabled": False}},
    )

    assert tools.build_symphony_toolkit({}, SwarmBuildContext(role="leader")) == []


def test_symphony_orchestration_prompt_provider_is_leader_only() -> None:
    """Only the leader gets the Symphony orchestration prompt rail."""
    factory = resolve_factory(
        get_catalog()[registry.SYMPHONY_ORCHESTRATION_PROMPT].factory_ref,
    )

    leader_rail = factory({}, SwarmBuildContext(role="leader"))
    teammate_rail = factory({}, SwarmBuildContext(role="teammate"))

    assert type(leader_rail).__name__ == "SymphonyOrchestrationRail"
    assert teammate_rail is None


def test_team_leader_spec_includes_symphony_orchestration_prompt() -> None:
    """The team leader spec declares the Symphony orchestration prompt provider."""
    spec = build_member_deep_agent_spec(
        {"symphony": {"enabled": True}},
        "team",
        "leader",
        DeepAgentSpec(),
    )

    assert any(
        rail.type == registry.SYMPHONY_ORCHESTRATION_PROMPT
        for rail in (spec.rails or [])
    )


def test_vision_model_config_params_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vision config params are empty without a dedicated model, filled when complete."""
    assert tools.vision_model_config_params({}) == {}

    monkeypatch.setattr(
        tools, "complete_multimodal_model_configured", lambda cfg, kind: True
    )
    monkeypatch.setattr(tools, "multimodal_model_enabled", lambda cfg, kind: True)
    monkeypatch.setattr(tools, "apply_vision_model_config_from_yaml", lambda cfg: None)
    monkeypatch.setenv("VISION_API_KEY", "key")
    monkeypatch.setenv("VISION_BASE_URL", "https://vision.example")
    monkeypatch.setenv("VISION_MODEL", "vlm-1")

    params = tools.vision_model_config_params({})
    assert params["api_key"] == "key"
    assert params["base_url"] == "https://vision.example"
    assert params["model"] == "vlm-1"


def test_vision_audio_config_params_empty_when_unconfigured() -> None:
    """Vision/audio config params are empty when no dedicated model is configured.

    The vision/audio tools themselves are openjiuwen elements (``core.vision`` /
    ``core.audio``); swarm only fills their config, so an unconfigured member
    yields empty params (the core element then builds nothing).
    """
    assert tools.vision_model_config_params({}) == {}
    assert tools.audio_model_config_params({}) == {}
    assert tools.audio_dedicated_configured({}) is False


# ---------------------------------------------------------------------------
# Evolution hot-reload: full TeamWorkspaceInfo on the registered rail context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auto_save", [False, True])
def test_team_skill_evolution_provider_passes_review_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_save: bool,
) -> None:
    monkeypatch.setattr(
        evolution_rails,
        "SwarmTeamSkillEvolutionRail",
        _FakeEvolutionRail,
    )
    monkeypatch.setattr(evolution_rails, "EvolutionInterruptRail", _FakeEvolutionInterruptRail)
    monkeypatch.setattr(
        evolution_rails,
        "_build_evolution_llm_from",
        lambda config: (object(), "model"),
    )
    monkeypatch.setattr(evolution_rails, "load_execution_disabled_skills", lambda: [])

    ctx = SwarmBuildContext(
        language="cn",
        role="leader",
        session_id="sess",
        channel="web",
        team_id="t",
        team_ws_root=str(tmp_path),
        team_skill_visibility_path=str(tmp_path / "skills-visibility.json"),
        global_skills_dir=str(tmp_path / "global-skills"),
        trajectory_span_processor=object(),
        config=_skill_evolution_config(auto_save=auto_save),
    )

    built = evolution_rails.build_team_skill_evolution_rail(
        {
            "evolution_model_config": {},
            "auto_save": auto_save,
        },
        ctx,
    )

    _assert_evolution_approval_stack(
        built,
        _FakeEvolutionRail,
        auto_save=auto_save,
        language="cn",
    )
    rail = built[1]
    # Single physical library: the rail is rooted at the global library alone.
    assert rail.args[0] == str(tmp_path / "global-skills")
    assert rail.kwargs["signal_trigger"] is False
    assert rail.kwargs["auto_save"] is auto_save
    assert rail.kwargs["review_trigger"] is True
    assert rail.review_feedback_config["session_id"] == "sess"
    assert rail.review_feedback_config["team_id"] == "t"
    assert rail.review_feedback_config["min_confidence"] == 0.7


def test_swarm_team_skill_evolution_registration_retries_deferred_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    create_rail = object()

    class _FakeManager:
        @staticmethod
        def register_team_live_rail(session_id, agent, rail) -> None:
            calls.append(f"live:{session_id}")

        @staticmethod
        def register_team_skill_rail(session_id, rail) -> None:
            calls.append(f"skill:{session_id}")

        @staticmethod
        def get_team_skill_create_rail(session_id):
            calls.append(f"create:{session_id}")
            return create_rail

        @staticmethod
        def consume_team_evolution_watcher_deferred(session_id) -> bool:
            calls.append(f"consume:{session_id}")
            return True

    manager = _FakeManager()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_team_manager",
        lambda channel: manager,
    )
    monkeypatch.setattr(evolution_rails.TeamSkillEvolutionRail, "init", lambda self, agent: None)
    monkeypatch.setattr(evolution_rails, "_register_team_rail_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.team_helpers.ensure_team_evolution_watcher",
        lambda channel, session_id, *, source: calls.append(f"watcher:{channel}:{session_id}:{source}"),
    )

    rail = evolution_rails.SwarmTeamSkillEvolutionRail.__new__(
        evolution_rails.SwarmTeamSkillEvolutionRail
    )
    rail.bind_swarm_context(
        channel="web",
        session_id="sess-1",
        team_ws_root=None,
        skills_dir="/tmp/global-skills",
        team_id="team-1",
        config={},
    )
    rail.init(SimpleNamespace(card=SimpleNamespace(name="leader")))

    assert rail._review_feedback_skill_create_rail is create_rail
    assert calls == [
        "live:sess-1",
        "skill:sess-1",
        "create:sess-1",
        "consume:sess-1",
        "watcher:web:sess-1:rail_registered",
    ]


def test_member_skill_evolution_provider_passes_review_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evolution_rails,
        "SwarmMemberSkillEvolutionRail",
        _FakeMemberSkillEvolutionRail,
    )
    monkeypatch.setattr(evolution_rails, "EvolutionInterruptRail", _FakeEvolutionInterruptRail)
    monkeypatch.setattr(
        evolution_rails,
        "_build_evolution_llm_from",
        lambda config: (object(), "model"),
    )
    monkeypatch.setattr(evolution_rails, "load_execution_disabled_skills", lambda: [])

    processor_obj = object()
    ctx = SwarmBuildContext(
        language="en",
        role="teammate",
        session_id="sess",
        channel="web",
        team_id="t",
        team_skill_visibility_path=str(tmp_path / "skills-visibility.json"),
        global_skills_dir=str(tmp_path / "global-skills"),
        trajectory_span_processor=processor_obj,
        config=_skill_evolution_config(),
    )

    built = evolution_rails.build_member_skill_evolution_rail(
        {"evolution_model_config": {}},
        ctx,
    )

    assert len(built) == 1
    rail = built[0]
    assert isinstance(rail, _FakeMemberSkillEvolutionRail)
    # Single physical library: the rail is rooted at the global library alone.
    assert rail.args[0] == str(tmp_path / "global-skills")
    assert rail.kwargs["language"] == "en"
    assert rail.kwargs["signal_trigger"] is True
    assert rail.kwargs["review_trigger"] is False
    assert rail.kwargs["auto_save"] is True
    assert rail.kwargs["trajectory_span_processor"] is processor_obj


def test_rail_spec_build_flattens_single_and_list_provider_returns() -> None:
    """Declarative rail providers may return either one rail or a rail stack."""
    single_rail = AgentRail()
    first_stack_rail = AgentRail()
    second_stack_rail = AgentRail()

    register_rail_provider("swarm.test_single_rail", lambda params, ctx: single_rail)
    register_rail_provider(
        "swarm.test_rail_stack",
        lambda params, ctx: [first_stack_rail, second_stack_rail],
    )

    spec = DeepAgentSpec(
        rails=[
            RailSpec(type="swarm.test_single_rail"),
            RailSpec(type="swarm.test_rail_stack"),
        ],
    )

    parts = spec.resolve_parts(context=SwarmBuildContext(language="cn"))

    assert parts.rails[:3] == [
        single_rail,
        first_stack_rail,
        second_stack_rail,
    ]


def test_team_skill_create_rail_registers_full_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader create rail registers a fully-populated ``TeamWorkspaceInfo``.

    An empty workspace info previously broke ``update_evolution_config`` rebuilds
    (``build_member_rails`` skips the leader branch without ``skills_dir``). This
    asserts the rail-mount context now carries root_dir / skills_dir / team_id /
    config / trajectory_span_processor from the build context.
    """
    from openjiuwen.agent_evolving.trajectory.processor import TrajectorySpanProcessor

    register_swarm_providers()
    processor_obj = TrajectorySpanProcessor()
    config = _skill_evolution_config()
    ctx = SwarmBuildContext(
        language="cn",
        role="leader",
        member_card_id="t_leader",
        session_id="sess",
        channel="web",
        team_id="t",
        team_ws_root="/tmp/team-x",
        team_skill_visibility_path="/tmp/team-x/skills-visibility.json",
        global_skills_dir="/tmp/global",
        trajectory_span_processor=processor_obj,
        config=config,
    )

    rail = evolution_rails.build_team_skill_create_rail({}, ctx)
    assert rail is not None

    captured: dict[str, object] = {}
    team_rail = SimpleNamespace(
        bind_review_feedback_skill_create_rail=MagicMock(),
    )

    class _RecorderTeamManager:
        def register_team_live_rail(self, session_id, agent, registered_rail) -> None:
            captured["live_rail"] = (session_id, registered_rail)

        def register_team_skill_create_rail(self, session_id, registered_rail) -> None:
            captured["create_rail"] = (session_id, registered_rail)

        def get_team_skill_rail(self, session_id):
            return team_rail

        def get_team_rail_context(self, session_id):
            return None

        def register_team_rail_context(self, session_id, context) -> None:
            captured["rail_context"] = context

    import jiuwenswarm.agents.harness.team.team_manager as tm_mod

    monkeypatch.setattr(
        tm_mod, "get_team_manager", lambda channel=None: _RecorderTeamManager()
    )

    fake_agent = types.SimpleNamespace(
        card=types.SimpleNamespace(name="leader", id="t_leader"),
    )
    rail.init(fake_agent)

    team_rail.bind_review_feedback_skill_create_rail.assert_called_once_with(rail)
    workspace = captured["rail_context"].team_workspace
    assert workspace.root_dir == "/tmp/team-x"
    # The team owns no skills/ directory: the library is the global one.
    assert workspace.skills_dir == "/tmp/global"
    assert workspace.team_id == "t"
    assert workspace.config == config
    assert isinstance(workspace.trajectory_span_processor, TrajectorySpanProcessor)
    assert workspace.trajectory_span_processor is processor_obj


# ---------------------------------------------------------------------------
# code.team / team.plan.code declarative profile
# ---------------------------------------------------------------------------

_EXPECTED_CODE_RAIL_NAMES_LEADER: frozenset[str] = frozenset(
    {
        registry.CODE_RUNTIME_PROMPT,
        registry.RESPONSE_PROMPT,
        registry.STREAM_EVENT,
        registry.SECURITY,
        registry.MODEL_ANOMALY_DETECTION,
        registry.CODE_LSP,
        registry.CODE_PROJECT_MEMORY,
        registry.SYS_OPERATION,
        registry.CODE_CODING_MEMORY,
        registry.CODE_AGENT_MODE,
        registry.STRUCTURED_ASK_USER,
        registry.CONTEXT_PROCESSOR,
        registry.CODE_AGENT_RAIL,
        registry.USER_HOOKS,
        TEAM_SKILL_USE,
        registry.SKILL_RETRIEVAL_PROMPT,
        registry.SYMPHONY_ORCHESTRATION_PROMPT,
        registry.CODE_CONFIRM_INTERRUPT,
        registry.MEMBER_SKILL_TOOLKIT,
        registry.TEAM_WORKSPACE_REPORT_PATH,
        registry.PLUGIN_RAILS,
    }
)

_EXPECTED_CODE_RAIL_NAMES_TEAMMATE: frozenset[str] = _EXPECTED_CODE_RAIL_NAMES_LEADER | {
    registry.CODE_TASK_PLANNING,
}


@pytest.mark.parametrize("mode", ["code.team", "team.plan.code"])
def test_code_capability_specs_rail_and_tool_names(mode: str) -> None:
    """Code modes emit the code rail/tool profile (not the chat-team common rails)."""
    register_swarm_providers()
    rails_specs, tool_specs = build_member_capability_specs(
        _skill_evolution_config(), mode, "leader"
    )
    rail_names = {spec.type for spec in rails_specs}
    rail_params = {spec.type: spec.params for spec in rails_specs}
    tool_names = {spec.type for spec in tool_specs}

    expected_rails = _EXPECTED_CODE_RAIL_NAMES_LEADER
    if mode == "team.plan.code":
        expected_rails = expected_rails - {registry.CODE_CONFIRM_INTERRUPT}
    assert expected_rails <= rail_names
    assert registry.CODE_TASK_PLANNING not in rail_names
    assert registry.TEAM_SKILL_EVOLUTION in rail_names
    assert registry.STRUCTURED_ASK_USER in rail_names
    if mode == "team.plan.code":
        assert registry.TEAM_PLAN_APPROVAL in rail_names
        assert registry.CODE_CONFIRM_INTERRUPT not in rail_names
    else:
        assert registry.TEAM_PLAN_APPROVAL not in rail_names
        assert registry.CODE_CONFIRM_INTERRUPT in rail_names
        assert rail_params[registry.CODE_CONFIRM_INTERRUPT]["tool_names"] == [
            "switch_mode",
            "exit_plan_mode",
        ]
    # The chat-team common runtime prompt is replaced by the code variant.
    assert registry.RUNTIME_PROMPT not in rail_names
    assert tool_names == {
        registry.WEB_SEARCH,
        registry.WEB_FETCH,
        registry.WEB_PAID_SEARCH,
        registry.VISION,
        registry.AUDIO,
        # SKILL_TOOLKIT moved to the MEMBER_SKILL_TOOLKIT rail (see common set).
        registry.SKILL_RETRIEVAL,
        registry.USER_TODOS,
        registry.VIDEO,
        registry.IMAGE_GEN,
        registry.XIAOYI_PHONE,
        registry.SYMPHONY_TOOLKIT,
        registry.CODE_EXTRA_TOOLS,
        registry.CRON_TOOLS,
        registry.SEND_FILE,
    }


def test_code_team_plan_approval_only_mounts_on_leader() -> None:
    """Only team.plan.code leader uses the plan approval interrupt."""
    register_swarm_providers()
    leader_rails, _ = build_member_capability_specs({}, "team.plan.code", "leader")
    teammate_rails, _ = build_member_capability_specs({}, "team.plan.code", "teammate")
    code_team_rails, _ = build_member_capability_specs({}, "code.team", "leader")

    assert registry.TEAM_PLAN_APPROVAL in {spec.type for spec in leader_rails}
    assert registry.TEAM_PLAN_APPROVAL not in {spec.type for spec in teammate_rails}
    assert registry.TEAM_PLAN_APPROVAL not in {spec.type for spec in code_team_rails}
    assert registry.CODE_CONFIRM_INTERRUPT not in {spec.type for spec in leader_rails}
    assert registry.CODE_CONFIRM_INTERRUPT in {spec.type for spec in teammate_rails}


def test_normal_team_plan_leader_uses_deepagent_plan_profile() -> None:
    """Normal Team Plan adds Work plan rails without mounting Code profile rails."""
    register_swarm_providers()
    leader_rails, _ = build_member_capability_specs(
        {}, "team.plan.normal", "leader"
    )
    teammate_rails, _ = build_member_capability_specs(
        {}, "team.plan.normal", "teammate"
    )

    leader_types = {spec.type for spec in leader_rails}
    teammate_types = {spec.type for spec in teammate_rails}

    assert registry.RUNTIME_PROMPT in leader_types
    assert registry.CODE_AGENT_MODE in leader_types
    assert registry.TEAM_PLAN_APPROVAL in leader_types
    assert registry.CODE_RUNTIME_PROMPT not in leader_types
    assert registry.CODE_CODING_MEMORY not in leader_types
    assert registry.CODE_AGENT_MODE not in teammate_types
    assert registry.TEAM_PLAN_APPROVAL not in teammate_types


def test_team_work_plan_leader_uses_deepagent_plan_profile() -> None:
    """The new Team Plan canonical mounts the plan mechanics on the Leader."""
    register_swarm_providers()
    leader_rails, _ = build_member_capability_specs({}, "team.work.plan", "leader")
    teammate_rails, _ = build_member_capability_specs({}, "team.work.plan", "teammate")

    leader_types = {spec.type for spec in leader_rails}
    teammate_types = {spec.type for spec in teammate_rails}

    assert registry.CODE_AGENT_MODE in leader_types
    assert registry.TEAM_PLAN_APPROVAL in leader_types
    assert registry.CODE_AGENT_MODE not in teammate_types
    assert registry.TEAM_PLAN_APPROVAL not in teammate_types


def test_normal_team_plan_agent_mode_provider_builds_work_rail() -> None:
    """The normal Team Plan leader receives WorkAgentModeRail with Team semantics."""
    register_swarm_providers()
    ctx = SwarmBuildContext(
        mode="team.plan.normal",
        role="leader",
        config={"preferred_language": "zh"},
    )

    rail = code_rails.build_code_agent_mode({}, ctx)

    from jiuwenswarm.agents.harness.work.rails.work_agent_mode_rail import (
        WorkAgentModeRail,
    )

    assert isinstance(rail, WorkAgentModeRail)
    assert code_rails.build_code_agent_mode(
        {}, SwarmBuildContext(mode="team.plan.normal", role="teammate")
    ) is None


def test_normal_team_plan_teammate_spec_equals_plain_team() -> None:
    """Normal Team Plan does not change the ordinary DeepAgent teammate spec."""
    base = DeepAgentSpec(system_prompt="base teammate prompt")

    plain = build_member_deep_agent_spec({}, "team", "teammate", base)
    planned = build_member_deep_agent_spec(
        {}, "team.plan.normal", "teammate", base
    )

    assert planned.model_dump() == plain.model_dump()


def test_code_team_plan_teammate_spec_equals_code_team() -> None:
    """Code Team Plan changes only the Leader; its teammate matches code.team."""
    base = DeepAgentSpec(system_prompt="base teammate prompt")

    plain = build_member_deep_agent_spec({}, "code.team", "teammate", base)
    planned = build_member_deep_agent_spec(
        {}, "team.plan.code", "teammate", base
    )

    assert planned.system_prompt == plain.system_prompt
    assert [rail.model_dump() for rail in planned.rails or []] == [
        rail.model_dump() for rail in plain.rails or []
    ]
    assert [tool.model_dump() for tool in planned.tools or []] == [
        tool.model_dump() for tool in plain.tools or []
    ]
    assert [
        (sub.factory_name, sub.factory_kwargs, getattr(sub, "subagent_type", None))
        for sub in planned.subagents or []
    ] == [
        (sub.factory_name, sub.factory_kwargs, getattr(sub, "subagent_type", None))
        for sub in plain.subagents or []
    ]


def test_leader_omits_harness_todo_planning_rails() -> None:
    """Leaders use the team task board; harness todo rails stay on teammates."""
    register_swarm_providers()
    leader_rails, _ = build_member_capability_specs({}, "team", "leader")
    teammate_rails, _ = build_member_capability_specs({}, "team", "teammate")
    code_leader_rails, _ = build_member_capability_specs({}, "code.team", "leader")
    code_teammate_rails, _ = build_member_capability_specs({}, "code.team", "teammate")

    leader_types = {spec.type for spec in leader_rails}
    teammate_types = {spec.type for spec in teammate_rails}
    code_leader_types = {spec.type for spec in code_leader_rails}
    code_teammate_types = {spec.type for spec in code_teammate_rails}

    assert registry.TASK_PLANNING not in leader_types
    assert registry.TASK_PLANNING in teammate_types
    assert registry.CODE_TASK_PLANNING not in code_leader_types
    assert registry.CODE_TASK_PLANNING in code_teammate_types


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan.normal", "team.plan.code"])
def test_leader_deep_agent_spec_forces_enable_task_planning_off(mode: str) -> None:
    """Leader must not rely on agent-core auto-inject when YAML enables the flag."""
    register_swarm_providers()
    base = DeepAgentSpec(enable_task_planning=True)

    leader_spec = build_member_deep_agent_spec({}, mode, "leader", base)
    teammate_spec = build_member_deep_agent_spec({}, mode, "teammate", base)

    assert leader_spec.enable_task_planning is False
    assert teammate_spec.enable_task_planning is True


def test_code_subagent_specs_use_factory_names() -> None:
    """Code modes declare explore/plan (+ gated code) sub-agents via factory_name."""
    register_swarm_providers()
    config = {"react": {"subagents": {"code_agent": {"enabled": True}}}}

    subs = build_member_subagent_specs(config, "code.team", "leader")
    factory_names = [spec.factory_name for spec in subs]

    assert registry.EXPLORE_AGENT in factory_names
    assert registry.PLAN_AGENT in factory_names
    assert registry.CODE_AGENT in factory_names
    # Team mode keeps only the built-in status-line setup subagent.
    team_subs = build_member_subagent_specs({}, "team", "leader")
    assert [sub.agent_card.name for sub in team_subs] == ["statusline-setup"]


def test_code_member_deep_spec_dedupes_base_explore_agent() -> None:
    """A base team spec's explore_agent gives way to the one we declare."""
    from openjiuwen.agent_teams.schema.deep_agent_spec import SubAgentSpec
    from openjiuwen.core.single_agent import AgentCard

    base = DeepAgentSpec(
        subagents=[
            SubAgentSpec(
                agent_card=AgentCard(name="explore_agent"),
                system_prompt="",
                factory_name=registry.EXPLORE_AGENT,
            )
        ]
    )

    spec = build_member_deep_agent_spec({}, "code.team", "leader", base)
    names = [sub.agent_card.name for sub in spec.subagents or []]

    assert names == ["statusline-setup", "explore_agent", "plan_agent"]


def test_code_runtime_language_by_mode_and_role() -> None:
    """Only the team.plan.code leader uses configured language; code is else English."""
    plan_leader = SwarmBuildContext(
        mode="team.plan.code",
        role="leader",
        config={"preferred_language": "zh"},
    )
    plan_teammate = SwarmBuildContext(mode="team.plan.code", role="teammate", config={})
    code_team = SwarmBuildContext(mode="code.team", role="leader", config={})

    assert code_rails.code_runtime_language(plan_leader) in {"cn", "zh"}
    assert code_rails.code_runtime_language(plan_teammate) == "en"
    assert code_rails.code_runtime_language(code_team) == "en"


def test_code_runtime_prompt_provider_carries_project_dir(tmp_path: Path) -> None:
    """code.team runtime prompts keep the user's project scope visible."""
    register_swarm_providers()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    ctx = SwarmBuildContext(
        mode="code.team",
        role="leader",
        session_id="sess-code-team-runtime",
        channel="tui",
        project_dir=str(project_dir),
    )

    rail = RailSpec(type=registry.CODE_RUNTIME_PROMPT).build(
        language="cn",
        context=ctx,
    )

    assert getattr(rail, "_project_dir") == str(project_dir)
    assert getattr(rail, "_cwd") == str(project_dir)
    assert getattr(rail, "_mode") == "code.team"
    assert getattr(rail, "_session_id") == "sess-code-team-runtime"


def test_team_plan_approval_provider_builds_only_for_leader() -> None:
    """The approval rail is scoped to both normal/code Team Plan leaders."""
    register_swarm_providers()
    plan_leader = SwarmBuildContext(mode="team.plan.code", role="leader")
    normal_plan_leader = SwarmBuildContext(mode="team.plan.normal", role="leader")
    plan_teammate = SwarmBuildContext(mode="team.plan.code", role="teammate")
    code_team_leader = SwarmBuildContext(mode="code.team", role="leader")

    rail = RailSpec(type=registry.TEAM_PLAN_APPROVAL).build(
        language="cn",
        context=plan_leader,
    )

    from jiuwenswarm.agents.harness.code.rails import PlanApprovalInterruptRail

    assert isinstance(rail, PlanApprovalInterruptRail)
    assert isinstance(
        RailSpec(type=registry.TEAM_PLAN_APPROVAL).build(
            language="cn",
            context=normal_plan_leader,
        ),
        PlanApprovalInterruptRail,
    )
    assert RailSpec(type=registry.TEAM_PLAN_APPROVAL).build(
        language="cn",
        context=plan_teammate,
    ) is None
    assert RailSpec(type=registry.TEAM_PLAN_APPROVAL).build(
        language="cn",
        context=code_team_leader,
    ) is None


@pytest.mark.asyncio
async def test_team_plan_approval_reuses_code_plan_copy(
    tmp_path: Path,
) -> None:
    """team.plan reuses the same raw approval copy as code.plan."""
    register_swarm_providers()
    plan_leader = SwarmBuildContext(mode="team.plan.code", role="leader")
    rail = RailSpec(type=registry.TEAM_PLAN_APPROVAL).build(
        language="cn",
        context=plan_leader,
    )
    plan_file = tmp_path / "team-plan.md"
    plan_file.write_text("## 团队计划\n\n- T1", encoding="utf-8")
    rail.init(
        SimpleNamespace(
            system_prompt_builder=SimpleNamespace(language="cn"),
            get_plan_file_path=lambda _session: plan_file,
        )
    )

    decision = await rail.resolve_interrupt(
        SimpleNamespace(session=SimpleNamespace(), extra={}),
        SimpleNamespace(name="exit_plan_mode", arguments="{}"),
        None,
    )
    message = decision.request.message

    assert "**计划审批**" in message
    assert "Agent 已完成计划制定，等待你审批：" in message
    assert "## 团队计划" in message
    assert "请选择：" in message
    assert "- **批准**" in message
    assert "- **拒绝**" in message


@pytest.mark.asyncio
async def test_code_plan_approval_still_contains_inline_choices(tmp_path: Path) -> None:
    """The original code.plan rail copy stays unchanged."""
    from jiuwenswarm.agents.harness.code.rails import PlanApprovalInterruptRail

    plan_file = tmp_path / "code-plan.md"
    plan_file.write_text("## 计划\n\n- T1", encoding="utf-8")
    rail = PlanApprovalInterruptRail()
    rail.init(
        SimpleNamespace(
            system_prompt_builder=SimpleNamespace(language="cn"),
            get_plan_file_path=lambda _session: plan_file,
        )
    )
    decision = await rail.resolve_interrupt(
        SimpleNamespace(session=SimpleNamespace(), extra={}),
        SimpleNamespace(name="exit_plan_mode", arguments="{}"),
        None,
    )
    message = decision.request.message

    assert "请选择：" in message
    assert "- **批准**" in message
    assert "- **拒绝**" in message


def test_team_plan_leader_code_agent_mode_has_team_exit_notification(monkeypatch) -> None:
    """Approved team plans should resume the Leader into team execution semantics.

    ``team.plan.code`` leader routes through ``CodeAgentModeRail`` (code profile
    prompts + team exit notification); ``code.team`` leader keeps the code
    profile prompts without an exit notification. The fake rail records every
    kwarg passed by either caller so we can assert on both shapes.
    """
    from jiuwenswarm.agents.swarm.providers.code_rails import (
        _TEAM_PLAN_EXIT_NOTIFICATION_EN,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        _PLAN_MODE_SYSTEM_NOTE,
        _code_enter_plan_instructions,
    )

    plan_leader = SwarmBuildContext(mode="team.plan.code", role="leader")
    code_team_leader = SwarmBuildContext(mode="code.team", role="leader")
    captured_configs: list[dict[str, object]] = []

    class FakeCodeAgentModeRail:
        def __init__(self, **kwargs: object) -> None:
            captured_configs.append(dict(kwargs))

    # build_code_agent_mode lazy-imports CodeAgentModeRail at call time, so
    # patching the module attribute is enough to capture both callers — both
    # team.plan.code and code.team leaders resolve to the code rail branch.
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail.CodeAgentModeRail",
        FakeCodeAgentModeRail,
    )
    code_rails.build_code_agent_mode({}, plan_leader)
    code_rails.build_code_agent_mode({}, code_team_leader)

    team_config, code_config = captured_configs
    # team.plan.code leader goes through CodeAgentModeRail with the code
    # profile prompts plus a user-facing team execution notification.
    assert team_config["plan_mode_system_note"] == _PLAN_MODE_SYSTEM_NOTE
    assert "plan_mode_attachment_note" not in team_config
    assert team_config["enter_plan_instructions"] == _code_enter_plan_instructions(
        plan_leader.config
    )
    assert team_config["exit_plan_notification"] == _TEAM_PLAN_EXIT_NOTIFICATION_EN
    assert "Team Leader" in team_config["exit_plan_notification"]
    for internal_name in (
        "build_team",
        "create_task",
        "spawn_teammate",
        "send_message",
    ):
        assert internal_name not in team_config["exit_plan_notification"]
    assert "ask_user" in team_config["allowed_tools"]
    # code.team leader stays on the code profile prompts with no exit
    # notification (only Team Plan leaders receive the Team Leader reminder).
    assert code_config["plan_mode_system_note"] == _PLAN_MODE_SYSTEM_NOTE
    assert "plan_mode_attachment_note" not in code_config
    assert code_config["enter_plan_instructions"] == _code_enter_plan_instructions(
        code_team_leader.config
    )
    assert code_config["exit_plan_notification"] is None
    assert "ask_user" in code_config["allowed_tools"]


def test_team_plan_leader_structured_ask_user_provider_builds() -> None:
    """team.plan leader keeps code-mode structured ask_user clarification."""
    register_swarm_providers()
    plan_leader = SwarmBuildContext(mode="team.plan.code", role="leader")
    code_team_leader = SwarmBuildContext(mode="code.team", role="leader")

    assert type(
        RailSpec(type=registry.STRUCTURED_ASK_USER).build(
            language="cn",
            context=plan_leader,
        )
    ).__name__ == "StructuredAskUserRail"
    assert type(
        RailSpec(type=registry.STRUCTURED_ASK_USER).build(
            language="cn",
            context=code_team_leader,
        )
    ).__name__ == "StructuredAskUserRail"


def test_non_interactive_team_omits_structured_ask_user_provider() -> None:
    context = SwarmBuildContext(
        mode="team",
        role="leader",
        request_metadata={"supports_user_interaction": False},
    )

    assert code_rails.build_structured_ask_user({}, context) is None


def test_structured_ask_user_language_uses_team_leader_preferred_language() -> None:
    team_leader = SwarmBuildContext(
        mode="team",
        role="leader",
        config={"preferred_language": "zh"},
    )
    team_teammate = SwarmBuildContext(
        mode="team",
        role="teammate",
        config={"preferred_language": "zh"},
    )

    assert code_rails.structured_ask_user_language(team_leader) == "cn"
    assert code_rails.structured_ask_user_language(team_teammate) == "en"


@pytest.mark.asyncio
async def test_team_plan_leader_permission_rail_skips_exit_plan_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan approval owns exit_plan_mode; permission rail still checks other tools."""
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers

    calls: list[str] = []
    created: list[object] = []

    class FakePermissionRail:
        priority = 90

        def get_callbacks(self) -> dict[AgentCallbackEvent, object]:
            return {AgentCallbackEvent.BEFORE_TOOL_CALL: self.before_tool_call}

        async def before_tool_call(self, ctx: object) -> None:
            calls.append(ctx.inputs.tool_name)

    def fake_build_permission_rail(**_kwargs: object) -> FakePermissionRail:
        rail = FakePermissionRail()
        created.append(rail)
        return rail

    monkeypatch.setattr(interrupt_helpers, "build_permission_rail", fake_build_permission_rail)

    plan_rail = code_rails.build_permission_interrupt(
        {"permissions_config": {"enabled": True}, "model_name": "gpt-4"},
        SwarmBuildContext(mode="team.plan.code", role="leader"),
    )
    code_rail = code_rails.build_permission_interrupt(
        {"permissions_config": {"enabled": True}, "model_name": "gpt-4"},
        SwarmBuildContext(mode="code.team", role="leader"),
    )

    assert plan_rail is not created[0]
    assert code_rail is created[1]
    assert plan_rail.get_callbacks()[AgentCallbackEvent.BEFORE_TOOL_CALL] == plan_rail.before_tool_call

    await plan_rail.before_tool_call(types.SimpleNamespace(inputs=types.SimpleNamespace(tool_name="exit_plan_mode")))
    await plan_rail.before_tool_call(types.SimpleNamespace(inputs=types.SimpleNamespace(tool_name="bash")))

    assert calls == ["bash"]


def test_code_extra_tools_gated_by_config() -> None:
    """The code-exclusive tool provider only builds when ``acp_agents`` is configured."""
    register_swarm_providers()
    enabled = SwarmBuildContext(config={"acp_agents": {"demo": {}}})
    disabled = SwarmBuildContext(config={})

    assert tools.build_code_extra_tools({}, disabled) == []
    assert isinstance(tools.build_code_extra_tools({}, enabled), list)


def test_code_coding_memory_provider_keeps_storage_outside_workspace_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider stores memory globally without creating project workspace nodes."""
    register_swarm_providers()

    import jiuwenswarm.server.runtime.agent_adapter.interface_code as interface_code

    project_dir = tmp_path / "project"
    workspace_root = tmp_path / "member-workspace"
    project_dir.mkdir()
    workspace_root.mkdir()
    monkeypatch.setattr(code_rails, "get_agent_workspace_dir", lambda: workspace_root)

    created: dict[str, Any] = {}
    rail = object()

    def _fake_create_coding_memory_rail(
        *,
        project_dir: str | None,
        agent_workspace_dir: str,
        config: dict[str, Any] | None,
    ) -> object:
        created["project_dir"] = project_dir
        created["agent_workspace_dir"] = agent_workspace_dir
        created["config"] = config
        return rail

    monkeypatch.setattr(
        interface_code,
        "create_coding_memory_rail",
        _fake_create_coding_memory_rail,
    )

    class Workspace:
        def __init__(self, root_path: Path) -> None:
            self.root_path = str(root_path)
            self.directories: list[dict[str, Any]] = []

        def set_directory(self, directory: dict[str, Any]) -> None:
            self.directories.append(directory)

    workspace = Workspace(workspace_root)
    ctx = SwarmBuildContext(
        mode="code.team",
        project_dir=str(project_dir),
        workspace=workspace,
        config={},
    )

    built = code_rails.build_code_coding_memory({"embed_config": {}}, ctx)

    assert built is rail
    assert ctx.extras[code_rails.CODING_MEMORY_EXTRAS_KEY] is rail
    assert created == {
        "project_dir": str(project_dir),
        "agent_workspace_dir": str(workspace_root),
        "config": {"embed": {}},
    }
    assert workspace.directories == []


def test_code_coding_memory_provider_falls_back_to_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing project_dir must not collapse distinct workspaces into default."""
    register_swarm_providers()

    import jiuwenswarm.server.runtime.agent_adapter.interface_code as interface_code

    workspace_root = tmp_path / "frontend"
    agent_workspace = tmp_path / "agent-workspace"
    workspace_root.mkdir()
    agent_workspace.mkdir()
    monkeypatch.setattr(code_rails, "get_agent_workspace_dir", lambda: agent_workspace)
    created: dict[str, Any] = {}
    rail = object()

    def _fake_create_coding_memory_rail(
        *,
        project_dir: str | None,
        agent_workspace_dir: str,
        config: dict[str, Any] | None,
    ) -> object:
        created["project_dir"] = project_dir
        created["agent_workspace_dir"] = agent_workspace_dir
        return rail

    monkeypatch.setattr(
        interface_code,
        "create_coding_memory_rail",
        _fake_create_coding_memory_rail,
    )

    class Workspace:
        def __init__(self, root_path: Path) -> None:
            self.root_path = str(root_path)
            self.directories: list[dict[str, Any]] = []

        def set_directory(self, directory: dict[str, Any]) -> None:
            self.directories.append(directory)

    workspace = Workspace(workspace_root)
    ctx = SwarmBuildContext(
        mode="code.team",
        project_dir=None,
        workspace=workspace,
        config={},
    )

    assert code_rails.build_code_coding_memory({"embed_config": {}}, ctx) is rail
    assert created == {
        "project_dir": str(workspace_root),
        "agent_workspace_dir": str(agent_workspace),
    }
    assert workspace.directories == []


def test_code_member_builds_declaratively_without_post_processing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code.team member builds through ``spec.build`` with zero post-processing.

    This is the crux of the "no customizer / no rail.init hack" contract: the
    code rails, sub-agents and system prompt materialize through openjiuwen's
    normal declarative build, and ``configure_code_team_member_agent`` is never
    invoked.
    """
    register_swarm_providers()
    config = get_config()
    base = DeepAgentSpec(
        model=TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_provider="OpenAI",
                api_key="test-key",
                api_base="https://example.test/v1",
                verify_ssl=False,
            )
        ),
        workspace=WorkspaceSpec(root_path=str(tmp_path), language="en"),
    )
    spec = build_member_deep_agent_spec(config, "code.team", "leader", base)

    import jiuwenswarm.server.runtime.agent_adapter.interface_code as interface_code

    post_processing_calls: list[int] = []
    monkeypatch.setattr(
        interface_code,
        "configure_code_team_member_agent",
        lambda *args, **kwargs: post_processing_calls.append(1),
    )

    ctx = SwarmBuildContext(
        mode="code.team",
        role="leader",
        member_name="leader",
        member_card_id="t_leader",
        session_id="s",
        channel="web",
        team_id="t",
        project_dir=str(tmp_path),
        team_ws_root=str(tmp_path),
        team_skill_visibility_path=str(tmp_path / "skills-visibility.json"),
        global_skills_dir=str(tmp_path / "global"),
        trajectory_span_processor=object(),
        config=config,
    )
    agent_workspace_dir = tmp_path / "agent-workspace"
    agent_workspace_dir.mkdir()
    monkeypatch.setattr(
        code_rails,
        "get_agent_workspace_dir",
        lambda: agent_workspace_dir,
    )
    agent = spec.build(context=ctx)

    rails = list(getattr(agent, "_pending_rails", [])) + list(
        getattr(agent, "_registered_rails", [])
    )
    rail_types = {rail.__class__.__name__ for rail in rails}

    # Zero post-processing: the legacy code customizer is never invoked.
    assert post_processing_calls == []
    # Code-specific rails materialized via the normal declarative spec.build.
    for expected in (
        "CodeAgentModeRail",
        "StructuredAskUserRail",
    ):
        assert expected in rail_types, (expected, sorted(rail_types))
    # Leaders use the team task board; harness code todo stays on teammates.
    assert "CodeTaskPlanningRail" not in rail_types
    assert "WorktreeRail" not in rail_types
    # The code system prompt is set declaratively on the spec.
    assert agent.deep_config.system_prompt
    # CodingMemoryRail materializes through the derived build context.
    assert "CodingMemoryRail" in rail_types
    coding_memory_dir = resolve_project_coding_memory_dir(
        agent_workspace_dir=str(agent_workspace_dir),
        project_dir=str(tmp_path),
    )
    assert Path(coding_memory_dir).is_dir()
    assert not (tmp_path / "coding_memory").exists()
    assert not any(
        node.get("name") == "coding_memory"
        for node in agent.deep_config.workspace.directories
    )
    logger.info("code member declarative build rail types: %s", sorted(rail_types))


# ---------------------------------------------------------------------------
# Serializable build-context seed (spawned / distributed member rebuild)
# ---------------------------------------------------------------------------


def test_swarm_build_context_seed_round_trip() -> None:
    """``to_seed`` exports the serializable fields; ``from_seed`` restores them."""
    base = SwarmBuildContext(
        session_id="s1",
        request_id="r1",
        channel_id="c1",
        channel="web",
        request_metadata={"mode": "code.team"},
        mode="code.team",
        project_dir="/tmp/proj",
        disable_teammate_worktree=True,
        team_id="t1",
        team_ws_root="/tmp/ws",
        team_skill_visibility_path="/tmp/ws/skills-visibility.json",
        global_skills_dir="/tmp/global",
        trajectory_span_processor=object(),
        config={"team": {}},
    )
    seed = base.to_seed()

    # Per-member and non-serializable fields stay out of the seed.
    for excluded in (
        "member_name",
        "role",
        "language",
        "workspace",
        "member_card_id",
        "config",
        "trajectory_span_processor",
    ):
        assert excluded not in seed

    processor_obj = object()
    restored = SwarmBuildContext.from_seed(
        seed,
        config={"k": "v"},
        trajectory_span_processor=processor_obj,
    )
    assert restored.session_id == "s1"
    assert restored.mode == "code.team"
    assert restored.project_dir == "/tmp/proj"
    assert restored.disable_teammate_worktree is True
    assert restored.team_id == "t1"
    assert restored.team_ws_root == "/tmp/ws"
    assert restored.request_metadata == {"mode": "code.team"}
    # Non-serializable handles are sourced from the receiver, not the seed.
    assert restored.config == {"k": "v"}
    assert restored.trajectory_span_processor is processor_obj


def test_build_context_factory_rebuilds_and_shares_processor() -> None:
    """The registered factory rebuilds contexts with the process-level processor."""
    from openjiuwen.agent_teams.schema.build_context import build_context_from_seed

    register_swarm_providers()
    seed = {"session_id": "s", "team_id": "t", "mode": "team", "project_dir": "/p"}

    ctx = build_context_from_seed(seed)
    assert isinstance(ctx, SwarmBuildContext)
    assert ctx.mode == "team"
    assert ctx.project_dir == "/p"
    # config is sourced locally (this process's config.yaml), not from the seed.
    assert ctx.config is not None
    assert ctx.trajectory_span_processor is not None

    # The process-level processor is shared by every team context.
    again = build_context_from_seed(seed)
    assert again.trajectory_span_processor is ctx.trajectory_span_processor
    other = build_context_from_seed(
        {"session_id": "s", "team_id": "t2", "mode": "team"}
    )
    assert other.trajectory_span_processor is ctx.trajectory_span_processor


def test_enrich_sets_serializable_build_context_seed() -> None:
    """Enrichment leaves a serializable seed mirroring the live build context."""
    spec = _make_team_spec()
    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/proj",
        channel_id="web",
    )
    assert spec.build_context_seed is not None
    assert spec.build_context_seed["mode"] == "code.team"
    assert spec.build_context_seed["project_dir"] == "/tmp/proj"
    assert spec.build_context_seed["disable_teammate_worktree"] is True
    assert spec.build_context_seed["team_id"] == spec.team_name
    # The seed equals what the live context exports.
    assert spec.build_context_seed == spec.build_context.to_seed()


def test_distributed_member_rebuild_reconstructs_build_context() -> None:
    """A serialized member spec rebuilds its provider context from the seed.

    Simulates the spawn / distributed boundary without ZMQ: ``model_dump`` drops
    the live ``build_context`` but carries ``build_context_seed``;
    ``materialize_build_context`` reconstructs the context from it via the
    registered factory.
    """
    register_swarm_providers()
    spec = _make_team_spec()
    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/proj",
        channel_id="web",
    )

    payload = spec.model_dump(mode="json")
    # build_context (live) is excluded; the seed survives the round-trip.
    assert "build_context" not in payload
    assert payload["build_context_seed"]["mode"] == "code.team"

    rebuilt = TeamAgentSpec.model_validate(payload)
    assert rebuilt.build_context is None
    rebuilt.materialize_build_context()

    assert isinstance(rebuilt.build_context, SwarmBuildContext)
    assert rebuilt.build_context.mode == "code.team"
    assert rebuilt.build_context.project_dir == "/tmp/proj"
    assert rebuilt.build_context.config is not None
    assert rebuilt.build_context.trajectory_span_processor is not None
    # Idempotent: a second call does not replace the rebuilt context.
    existing = rebuilt.build_context
    rebuilt.materialize_build_context()
    assert rebuilt.build_context is existing


def test_rebuilt_member_spec_keeps_provider_declarations() -> None:
    """Provider rail / sub-agent declarations and the code prompt survive the round-trip.

    Complements the build-context reconstruction: the existing crown-jewel proves the
    declarative build itself, so here we prove the *serialized* code.team spec still
    carries its ``swarm.*`` rail / sub-agent references and code system prompt — so the
    post-round-trip build yields the same capabilities with no post-processing. (We do
    not build here: a second in-process ``spec.build`` would collide on the global tool
    registry, which never happens in production where each member builds in its own
    process.)
    """
    register_swarm_providers()
    spec = _make_team_spec()
    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/proj",
        channel_id="web",
    )

    rebuilt = TeamAgentSpec.model_validate(spec.model_dump(mode="json"))
    teammate = rebuilt.agents["teammate"]

    rail_types = {rail.type for rail in teammate.rails}
    assert registry.CODE_TASK_PLANNING in rail_types
    assert registry.CODE_AGENT_MODE in rail_types
    assert registry.CODE_WORKTREE not in rail_types
    # The code runtime prompt replaces the chat-team common runtime prompt.
    assert registry.CODE_RUNTIME_PROMPT in rail_types
    assert registry.RUNTIME_PROMPT not in rail_types
    # Sub-agents are declared by ``factory_name`` and survive serialization.
    leader_factory_names = {
        sub.factory_name for sub in rebuilt.agents["leader"].subagents
    }
    assert registry.EXPLORE_AGENT in leader_factory_names
    assert registry.PLAN_AGENT in leader_factory_names
    # The code system prompt is carried declaratively on the spec.
    assert teammate.system_prompt


def test_swarm_assembly_hint_from_seed_and_legacy() -> None:
    """The bootstrap envelope hint carries mode/project_dir; empty for legacy."""
    from jiuwenswarm.agents.harness.team import remote_member_bootstrap as rmb

    spec = _make_team_spec()
    enrich_team_spec_for_swarm(
        spec,
        session_id="s",
        mode="code.team",
        project_dir="/tmp/proj",
        channel_id="web",
    )
    leader = types.SimpleNamespace(spec=spec)
    assert rmb._swarm_assembly_hint(leader) == {
        "mode": "code.team",
        "project_dir": "/tmp/proj",
    }

    # Legacy spec (no seed, no live context) yields no hint -> teammate stays legacy.
    legacy = types.SimpleNamespace(spec=_make_team_spec())
    assert rmb._swarm_assembly_hint(legacy) == {}
    # Defensive: a team agent without a spec yields no hint.
    assert rmb._swarm_assembly_hint(types.SimpleNamespace()) == {}


def test_browser_key_derivation() -> None:
    """_browser_key composes session+member into a unique, stable key."""
    # Normal: session_id + member_name → "sess-alice"
    assert _browser_key("sess", "alice", "teammate") == "sess-alice"
    # Leader also keyed by member_name, not role
    assert _browser_key("sess", "leader", "leader") == "sess-leader"
    # Different sessions never collide on the same member_name
    assert _browser_key("s1", "alice", "teammate") != _browser_key("s2", "alice", "teammate")
    # Different member_names in same session are distinct
    assert _browser_key("sess", "alice", "teammate") != _browser_key("sess", "bob", "teammate")
    # Empty member_name falls back to role
    assert _browser_key("sess", "", "leader") == "sess-leader"
    # Both empty → empty (preserves shared-browser legacy behaviour)
    assert _browser_key("", "", "") == ""
    # No session → just the discriminator
    assert _browser_key("", "alice", "teammate") == "alice"


def test_browser_subagent_spec_included_when_enabled() -> None:
    """browser_agent SubAgentSpec uses SWARM_BROWSER_AGENT factory when enabled."""
    register_swarm_providers()
    config = {"react": {"subagents": {"browser_agent": {"enabled": True}}}}
    subs = build_member_subagent_specs(config, "code.team", "leader")
    factory_names = [s.factory_name for s in subs]
    assert SWARM_BROWSER_AGENT in factory_names
    browser_spec = next(s for s in subs if s.factory_name == SWARM_BROWSER_AGENT)
    assert (
        browser_spec.factory_kwargs["max_iterations"]
        == DEFAULT_BROWSER_AGENT_MAX_ITERATIONS
    )


def test_browser_subagent_spec_honors_explicit_iteration_budget() -> None:
    """An explicit browser budget wins over the generous browser default."""
    config = {
        "react": {
            "max_iterations": 9,
            "subagents": {
                "browser_agent": {
                    "enabled": True,
                    "max_iterations": 23,
                },
            },
        },
    }

    subs = build_member_subagent_specs(config, "code.team", "leader")
    browser_spec = next(s for s in subs if s.factory_name == SWARM_BROWSER_AGENT)

    assert browser_spec.factory_kwargs["max_iterations"] == 23


def test_browser_subagent_spec_excluded_when_disabled() -> None:
    """browser_agent is absent from SubAgentSpecs when disabled or absent."""
    register_swarm_providers()
    subs_disabled = build_member_subagent_specs(
        {"react": {"subagents": {"browser_agent": {"enabled": False}}}},
        "code.team",
        "leader",
    )
    subs_absent = build_member_subagent_specs({}, "code.team", "leader")
    for subs in (subs_disabled, subs_absent):
        assert all(s.factory_name != SWARM_BROWSER_AGENT for s in subs)


def test_browser_subagent_provider_skips_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_swarm_browser_agent returns None when no parent model is on the context."""
    register_swarm_providers()
    ctx = SwarmBuildContext(
        mode="code.team",
        role="leader",
        member_name="leader",
        session_id="s",
        config={},
    )
    # No _parent_model in ctx.extras → provider must short-circuit to None.
    result = build_swarm_browser_agent({}, ctx)
    assert result is None


def test_browser_subagent_provider_passes_correct_browser_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_swarm_browser_agent passes the per-member browser_key to agent-core."""
    from jiuwenswarm.agents.swarm.providers import code_subagents as _cs

    captured: list[dict] = []

    def _fake_build(*args, **kwargs):
        captured.append(kwargs)
        spec = MagicMock()
        spec.factory_kwargs = {}
        return spec

    monkeypatch.setattr(_cs, "build_browser_agent_config", _fake_build)

    fake_model = object()
    ctx = SwarmBuildContext(
        mode="code.team",
        role="teammate",
        member_name="browser-usd-sgd",
        session_id="sess42",
        config={},
    )
    ctx.extras[_PARENT_MODEL_EXTRAS_KEY] = fake_model

    result = build_swarm_browser_agent({}, ctx)

    assert result is not None
    assert len(captured) == 1
    assert captured[0]["browser_key"] == "sess42-browser-usd-sgd"
    assert (
        captured[0]["max_iterations"]
        == DEFAULT_BROWSER_AGENT_MAX_ITERATIONS
    )


def test_browser_subagent_teammates_get_distinct_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two teammates in the same session never share a browser_key."""
    from unittest.mock import MagicMock
    from jiuwenswarm.agents.swarm.providers import code_subagents as _cs

    keys: list[str] = []

    def _fake_build(*args, **kwargs):
        keys.append(kwargs.get("browser_key", ""))
        spec = MagicMock()
        spec.factory_kwargs = {}
        return spec

    monkeypatch.setattr(_cs, "build_browser_agent_config", _fake_build)
    fake_model = object()

    for name in ("browser-usd-sgd", "browser-eur-usd"):
        ctx = SwarmBuildContext(
            mode="code.team",
            role="teammate",
            member_name=name,
            session_id="sess42",
            config={},
        )
        ctx.extras[_PARENT_MODEL_EXTRAS_KEY] = fake_model
        build_swarm_browser_agent({}, ctx)

    assert len(keys) == 2
    assert keys[0] != keys[1], "Teammates must not share a browser_key"


def test_team_mode_deep_spec_replaces_shared_browser_agent() -> None:
    """shared browser_agent is replaced by SWARM_BROWSER_AGENT for each member."""
    register_swarm_providers()
    from openjiuwen.agent_teams.schema.deep_agent_spec import SubAgentSpec
    from openjiuwen.core.single_agent import AgentCard

    shared_browser = SubAgentSpec(
        agent_card=AgentCard(name="browser_agent"),
        system_prompt="",
        subagent_type="browser_agent",
    )
    base = DeepAgentSpec(
        subagents=[shared_browser],
        workspace=WorkspaceSpec(root_path="/tmp/ws"),
    )
    config = {"react": {"subagents": {"browser_agent": {"enabled": True}}}}
    spec = build_member_deep_agent_spec(config, "team", "leader", base)

    # The shared entry must be gone and exactly one SWARM_BROWSER_AGENT present.
    subagent_factories = [
        getattr(s, "factory_name", None) for s in (spec.subagents or [])
    ]
    shared_entries = []
    for s in (spec.subagents or []):
        if getattr(s, "factory_name", None) is None and getattr(s, "subagent_type", None) == "browser_agent":
            shared_entries.append(s)
    assert not shared_entries, "shared playwright_official_stdio entry must be removed"
    assert subagent_factories.count(SWARM_BROWSER_AGENT) == 1
