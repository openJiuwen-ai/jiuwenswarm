# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lean coding profile for Code mode (``modes.code.profile: lean``).

What this module does
    Holds the configuration schema and defaults of the lean profile and the
    ``LeanToolFilterRail`` that enforces it at model-call time. The lean
    profile keeps the per-call fixed prompt small for headless coding (the
    implement driver, benchmarks, CI): only the coding tools are shown to the
    model and prompt sections that describe interactive or team features are
    removed.

Who calls it
    * ``jiuwenswarm.agents.harness.code.spec.convert_code_config_to_deep_agent_spec``
      reads :func:`resolve_code_profile` to drop sub-agents, the sub-agent
      runtime, progressive tool disclosure, non-allowed configured tools and
      non-allowed dynamic rails from the agent spec.
    * ``JiuwenSwarmCodeAdapter._build_agent_rails`` skips the fixed rails listed
      in ``skip_rails`` and mounts :class:`LeanToolFilterRail` last.
    * ``code_prompt_builder.build_code_system_prompt(profile="lean")`` builds the
      shorter base system prompt.

What it does not do
    It never changes agent-core code or tool definitions; it only chooses what
    jiuwenswarm registers and filters the tool list / prompt sections through the
    public ``before_model_call`` rail hook. With the default profile
    (``modes.code.profile: default`` or the key absent) nothing here is active
    and Code mode behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

PROFILE_DEFAULT = "default"
PROFILE_LEAN = "lean"
_PROFILES = frozenset({PROFILE_DEFAULT, PROFILE_LEAN})

# Coding tools the model may see in the lean profile. The repo-aware tools
# (MR "repo-aware tools") and run_tests (MR "verification loop") are listed so a
# build that has them keeps them; names that are not registered are simply
# absent, which is harmless.
DEFAULT_TOOLS_ALLOW: tuple[str, ...] = (
    "read_file",
    "write_file",
    "edit_file",
    "bash",
    "grep",
    "glob",
    "run_tests",
    "code_search",
    "find_symbol",
    "symbol_outline",
)

# Dynamic rails (``modes.code.rails``) kept in the lean profile. SkillUseRail
# (skill_tool + skills catalogue) and WorktreeRail are interactive features.
DEFAULT_RAILS_ALLOW: tuple[str, ...] = ()

# Fixed rails of JiuwenSwarmCodeAdapter._build_agent_rails that the lean profile
# does not mount (attribute names of their _RailBuildInfo entries). Each one
# only adds interactive, team or delegation features and their prompt text:
DEFAULT_SKIP_RAILS: tuple[str, ...] = (
    "_skill_retrieval_prompt_rail",     # skill-retrieval prompt section
    "_symphony_orchestration_rail",     # multi-agent graph planning
    "_session_messaging_route_rail",    # team session messaging tools
    "_code_ask_user_rail",              # ask_user (no user in headless runs)
    "_code_task_planning_rail",         # todo_create/list/get/modify + todo prompt
    "_code_agent_rail",                 # code_agent delegation prompt
    "_code_plan_approval_rail",         # plan-approval interrupt (interactive)
    "_subagent_rail",                   # task_tool / subagent_* + delegation prompt
    "_coding_memory_rail",              # coding_memory_* tools + memory policy prompt
    "_lsp_rail",                        # deferred lsp tool (hidden anyway)
)

# Prompt sections removed before every model call. Names are the
# ``PromptSection.name`` keys of the agent's SystemPromptBuilder; unknown names
# are ignored. All of them describe tools or features absent in this profile.
DEFAULT_DROP_PROMPT_SECTIONS: tuple[str, ...] = (
    "skills",
    "todo",
    "task_tool",
    "subagent_tools",
    "session_tools",
    "progressive_tool_rules",
    "tool_navigation",
    "memory",
    "external_memory",
    "skill_creation_guidance",
    "skill_creation_nudge",
    "team_skill_creation_guidance",
    "team_skill_creation_nudge",
    "evolution_protocol",
    "evolution_team_protocol",
    "goal_protocol",
    "memory_forbidden",
)


# Sub-blocks of shared (all-mode) jiuwenswarm prompt sections that only describe
# features the lean profile does not expose. The rest of those sections stays:
# ``directory_boundaries`` also carries the "do not write into Agent internal
# data" rules, and ``input`` describes the user-message format.
# (section name, start marker, end marker or None = to the end of the section)
LEAN_PRUNE_BLOCKS: tuple[tuple[str, str, str | None], ...] = (
    ("directory_boundaries", "### Installed Extension Assets\n", "### Directory Usage Rules"),
    ("directory_boundaries", "### 已安装扩展资产\n", "### 目录使用规则"),
    ("input", "## System Messages\n", None),
    ("input", "## 系统消息\n", None),
)


def prune_block(text: str, start: str, end: str | None) -> str:
    """Remove ``start`` .. ``end`` (exclusive) from ``text``; unchanged if absent."""
    i = text.find(start)
    if i < 0:
        return text
    if end is None:
        return text[:i].rstrip() + "\n"
    j = text.find(end, i + len(start))
    if j < 0:
        return text
    return text[:i] + text[j:]


@dataclass(frozen=True)
class LeanProfileConfig:
    """Resolved ``modes.code.lean`` settings (only meaningful when enabled)."""

    enabled: bool = False
    tools_allow: tuple[str, ...] = DEFAULT_TOOLS_ALLOW
    rails_allow: tuple[str, ...] = DEFAULT_RAILS_ALLOW
    skip_rails: tuple[str, ...] = DEFAULT_SKIP_RAILS
    drop_prompt_sections: tuple[str, ...] = DEFAULT_DROP_PROMPT_SECTIONS
    disable_subagents: bool = True
    disable_progressive_tool: bool = True
    log_sections: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def _str_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(str(item).strip() for item in value if str(item).strip())


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def code_mode_config(config_base: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``modes.code`` from a config snapshot (empty dict when absent)."""
    if not isinstance(config_base, dict):
        return {}
    modes = config_base.get("modes")
    if not isinstance(modes, dict):
        return {}
    code = modes.get("code")
    return code if isinstance(code, dict) else {}


def resolve_code_profile(config_base: dict[str, Any] | None) -> LeanProfileConfig:
    """Resolve the Code profile from ``config.yaml``.

    ``modes.code.profile`` selects ``default`` (current behaviour) or ``lean``.
    Unknown values fall back to ``default`` with a warning so a typo can never
    silently strip tools from an interactive session.
    """
    code = code_mode_config(config_base)
    raw_profile = str(code.get("profile") or PROFILE_DEFAULT).strip().lower()
    if raw_profile not in _PROFILES:
        logger.warning(
            "[LeanProfile] unknown modes.code.profile=%r; using %r",
            raw_profile,
            PROFILE_DEFAULT,
        )
        raw_profile = PROFILE_DEFAULT
    if raw_profile != PROFILE_LEAN:
        return LeanProfileConfig(enabled=False)
    lean = code.get("lean")
    lean = lean if isinstance(lean, dict) else {}
    return LeanProfileConfig(
        enabled=True,
        tools_allow=_str_tuple(lean.get("tools_allow"), DEFAULT_TOOLS_ALLOW),
        rails_allow=_str_tuple(lean.get("rails_allow"), DEFAULT_RAILS_ALLOW),
        skip_rails=_str_tuple(lean.get("skip_rails"), DEFAULT_SKIP_RAILS),
        drop_prompt_sections=_str_tuple(
            lean.get("drop_prompt_sections"), DEFAULT_DROP_PROMPT_SECTIONS
        ),
        disable_subagents=_bool(lean.get("disable_subagents"), True),
        disable_progressive_tool=_bool(lean.get("disable_progressive_tool"), True),
        log_sections=_bool(lean.get("log_sections"), True),
    )


def filter_names(names: Iterable[str], allow: Iterable[str]) -> list[str]:
    """Keep ``names`` that are in ``allow``, preserving order."""
    allowed = set(allow)
    return [name for name in names if name in allowed]


def _tool_name(tool: Any) -> str:
    for attr in ("name",):
        value = getattr(tool, attr, None)
        if isinstance(value, str) and value:
            return value
    card = getattr(tool, "card", None)
    value = getattr(card, "name", None)
    if isinstance(value, str) and value:
        return value
    if isinstance(tool, dict):
        func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        value = func.get("name")
        if isinstance(value, str):
            return value
    return ""


class LeanToolFilterRail(DeepAgentRail):
    """Enforce the lean profile at every model call.

    Runs after every other rail (lowest priority) so it sees the final tool list
    and system-prompt sections, then keeps only allow-listed tools and removes
    the listed prompt sections. Tools stay registered; the model just does not
    see them, so any tool that another rail registers later is also covered.
    """

    priority = 1

    def __init__(self, config: LeanProfileConfig) -> None:
        super().__init__()
        self._config = config
        self._allow = frozenset(config.tools_allow)
        self._drop = tuple(config.drop_prompt_sections)
        self._logged = False
        self._tools_checked = False
        self.system_prompt_builder: Any = None

    def init(self, agent: Any) -> None:
        # Same pattern as the other Code rails: the builder is taken from the
        # agent at init time; ctx.agent at model-call time is the fallback.
        super().init(agent)
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self.system_prompt_builder = None
        super().uninit(agent)

    @property
    def config(self) -> LeanProfileConfig:
        return self._config

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        try:
            self._filter_tools(ctx)
            self._drop_sections(ctx)
        except Exception as exc:  # noqa: BLE001 - never break a turn over prompt shaping
            logger.warning("[LeanToolFilterRail] before_model_call failed: %s", exc)

    def _filter_tools(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not isinstance(tools, list):
            return
        kept = [tool for tool in tools if _tool_name(tool) in self._allow]
        if not self._tools_checked:
            self._tools_checked = True
            registered = {_tool_name(tool) for tool in tools}
            unmatched = sorted(name for name in self._allow if name not in registered)
            if unmatched:
                # Expected for optional tools absent from this build; a typo shows up here too.
                logger.warning(
                    "[LeanToolFilterRail] tools_allow names not registered in this agent: %s",
                    unmatched,
                )
        if tools and not kept:
            # A misspelled allow-list must not leave the model without any tools.
            logger.warning(
                "[LeanToolFilterRail] tools_allow matched none of %d registered tools; "
                "keeping the unfiltered tool list (check modes.code.lean.tools_allow)",
                len(tools),
            )
            return
        if len(kept) != len(tools):
            dropped = sorted(
                {_tool_name(tool) for tool in tools} - {_tool_name(tool) for tool in kept}
            )
            if not self._logged:
                logger.info(
                    "[LeanToolFilterRail] tools kept=%s dropped=%s",
                    sorted(_tool_name(tool) for tool in kept),
                    dropped,
                )
        inputs.tools = kept

    @staticmethod
    def _prune_shared_sections(builder: Any) -> None:
        get_section = getattr(builder, "get_section", None)
        if not callable(get_section):
            return
        for name, start, end in LEAN_PRUNE_BLOCKS:
            section = get_section(name)
            content = getattr(section, "content", None)
            if not isinstance(content, dict):
                continue
            for lang, text in list(content.items()):
                if isinstance(text, str):
                    content[lang] = prune_block(text, start, end)

    def _drop_sections(self, ctx: AgentCallbackContext) -> None:
        builder = self.system_prompt_builder or getattr(
            getattr(ctx, "agent", None), "system_prompt_builder", None
        )
        if builder is None:
            if not self._logged:
                self._logged = True
                logger.warning("[LeanToolFilterRail] no system_prompt_builder; prompt sections kept")
            return
        removed: list[str] = []
        for name in self._drop:
            if builder.has_section(name):
                builder.remove_section(name)
                removed.append(name)
        self._prune_shared_sections(builder)
        if self._logged:
            return
        self._logged = True
        if self._config.log_sections:
            try:
                sections = builder.get_all_sections()
                sizes = {
                    name: section.char_count("en") for name, section in sections.items()
                }
            except Exception:  # noqa: BLE001  # pragma: no cover - diagnostics only
                sizes = {}
            logger.info(
                "[LeanToolFilterRail] prompt sections removed=%s remaining(chars)=%s",
                removed,
                sizes,
            )


__all__ = [
    "DEFAULT_DROP_PROMPT_SECTIONS",
    "DEFAULT_RAILS_ALLOW",
    "DEFAULT_SKIP_RAILS",
    "DEFAULT_TOOLS_ALLOW",
    "LEAN_PRUNE_BLOCKS",
    "PROFILE_DEFAULT",
    "PROFILE_LEAN",
    "LeanProfileConfig",
    "LeanToolFilterRail",
    "code_mode_config",
    "filter_names",
    "resolve_code_profile",
]
