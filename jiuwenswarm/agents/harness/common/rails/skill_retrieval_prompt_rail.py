"""Prompt boundary for Symphony-managed runtime Skill discovery."""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail

from openjiuwen.symphony.discovery import (
    DiscoverySettings,
    SkillFS,
    SkillPromptBranch,
    SkillPromptEntry,
    SkillPromptSnapshot,
    consume_incremental_skill_reminder,
    initialize_incremental_skill_notice_state,
)

from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
    SkillRetrievalToolkit,
    build_discovery_settings,
    is_skill_retrieval_enabled,
)

_LEGACY_LIST_SKILL_TOOL_NAMES = frozenset({"list_skill", "list_skills"})
_SKILL_INDEX_TOOL_NAME = "skill_index"
_RUNTIME_SKILL_ATTACHMENT_SECTION = "skills.runtime_changes"
_SYMPHONY_RUNTIME_TOOL_NAMES = frozenset(
    {
        "skill_index",
        "symphony_compose_score",
        "symphony_read_score",
        "symphony_refresh_score",
        # JiuwenSwarm's current orchestration compatibility surface.
        "symphony_compose_graph",
        "symphony_read_graph",
        "symphony_refresh_graph",
    }
)
logger = logging.getLogger(__name__)


class SkillRetrievalPromptRail(DeepAgentRail):
    """Make Symphony the sole Skill-discovery surface while preserving SkillTool."""

    # Higher priorities run first. Hide the native section after SkillUseRail
    # refreshes it for the current model call.
    priority = SkillUseRail.priority - 1
    SECTION_NAME = "skill_retrieval"
    CANDIDATE_SECTION_NAME = "skill_retrieval.session_candidates"
    ATTACHMENT_SOURCE = "jiuwenswarm.skill_retrieval_prompt_rail"
    # Keep the inventory-dependent appendix after the reusable system/tool
    # prefix. Its content is frozen once in ``init``.
    CANDIDATE_SECTION_PRIORITY = 10_000

    def __init__(
        self,
        *,
        toolkit: SkillRetrievalToolkit | None = None,
        session_scope: str = "default",
        incremental_notice_max_chars: int | None = None,
        discovery_settings: DiscoverySettings | None = None,
        environment: SkillFS | None = None,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        settings = (
            discovery_settings
            or (toolkit.settings if toolkit is not None else None)
            or build_discovery_settings(config_base)
        )
        notice_max_chars = (
            incremental_notice_max_chars
            if incremental_notice_max_chars is not None
            else settings.incremental_notice_max_chars
        )
        if notice_max_chars <= 0:
            raise ValueError("incremental_notice_max_chars must be positive")
        self._session_scope = session_scope
        self._source_toolkit = toolkit
        self._incremental_notice_max_chars = notice_max_chars
        self._discovery_settings = settings
        self._prompt_skillfs: SkillFS | None = environment or (
            toolkit.environment if toolkit is not None else None
        )
        self._config_base = config_base
        self._session_enabled = is_skill_retrieval_enabled(config_base)
        self._agent = None
        self.system_prompt_builder = None
        self._hidden_legacy_abilities: dict[str, Any] = {}
        self._hidden_skills_section: PromptSection | None = None
        self.attachment_manager = None
        self._frozen_prompt_snapshot: SkillPromptSnapshot | None = None

    def init(self, agent: Any) -> None:
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self.attachment_manager = getattr(
            agent,
            "prompt_attachment_manager",
            None,
        )
        try:
            toolkit = self._toolkit()
            selection_cards = (
                toolkit.frozen_selection_cards
                if toolkit is not None
                else self._scan_selection_cards()
            )
            self._frozen_prompt_snapshot = (
                toolkit.frozen_prompt_snapshot
                if toolkit is not None
                else self._prompt_skillfs.prompt_snapshot()
                if self._prompt_skillfs is not None
                else self._empty_prompt_snapshot()
            )
            initialize_incremental_skill_notice_state(
                self._session_scope,
                selection_cards,
                discovery_tool_name=_SKILL_INDEX_TOOL_NAME,
            )
        except Exception:
            self._frozen_prompt_snapshot = self._empty_prompt_snapshot()
            logger.warning(
                "Unable to initialize Symphony Skill prompt/delta baseline",
                exc_info=True,
            )

    def uninit(self, agent: Any) -> None:
        self._restore_legacy_list_skill(agent)
        self._restore_native_skills_section()
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            self.system_prompt_builder.remove_section(self.CANDIDATE_SECTION_NAME)
        self.system_prompt_builder = None
        self.attachment_manager = None
        self._prompt_skillfs = None
        self._source_toolkit = None
        self._frozen_prompt_snapshot = None
        self._agent = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Synchronize only after the model tool list has been populated.

        ``before_invoke`` intentionally stays inherited as a no-op: its inputs
        do not yet contain tools, so treating that temporary absence as a
        disabled index would append remove/add deltas on every user turn.
        """

        await self._sync_prompt_attachment(ctx)

    async def _sync_prompt_attachment(self, ctx: AgentCallbackContext) -> None:
        """Keep discovery guidance out of the cache-stable system prefix."""
        agent = getattr(ctx, "agent", None)
        if agent is not None:
            self._agent = agent
            if self.system_prompt_builder is None:
                self.system_prompt_builder = getattr(
                    agent, "system_prompt_builder", None
                )
            if self.attachment_manager is None:
                self.attachment_manager = getattr(
                    agent, "prompt_attachment_manager", None
                )

        if not self._session_enabled or not self._has_skill_index(ctx):
            await self._clear_prompt_attachments(ctx)
            restored_legacy = self._disable_agentic_prompt(ctx)
            self._restore_legacy_list_skill_in_model_inputs(ctx, restored_legacy)
            return

        if self.system_prompt_builder is None:
            return

        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        self._hide_legacy_list_skill()
        self._filter_legacy_list_skill_from_model_inputs(ctx)
        self._hide_native_skills_section()
        await self._clear_runtime_skill_attachment(ctx)
        try:
            snapshot = self._prompt_snapshot()
        except Exception:
            logger.warning(
                "Unable to build the Symphony Skill prompt snapshot",
                exc_info=True,
            )
            snapshot = self._empty_prompt_snapshot()
        candidate_appendix = self._build_candidate_appendix(language, snapshot)
        manager = self.attachment_manager
        if manager is None:
            self._add_prompt_builder_section(language, candidate_appendix)
            return

        self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder.remove_section(self.CANDIDATE_SECTION_NAME)
        writer = manager.bind_context(ctx)
        try:
            await writer.add_section(
                section=self.CANDIDATE_SECTION_NAME,
                content=candidate_appendix,
                kind=PromptAttachmentKind.SKILL,
                source=self.ATTACHMENT_SOURCE,
                priority=self.CANDIDATE_SECTION_PRIORITY,
                content_kind="text/markdown",
            )
        except ValueError as exc:
            logger.warning(
                "[SkillRetrievalPromptRail] attachment write failed: %s", exc
            )
            await self._clear_prompt_attachments(ctx)
            self._add_prompt_builder_section(language, candidate_appendix)

    def _add_prompt_builder_section(
        self,
        language: str,
        candidate_appendix: str,
    ) -> None:
        if self.system_prompt_builder is None:
            return
        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.CANDIDATE_SECTION_NAME,
                content={language: candidate_appendix},
                priority=self.CANDIDATE_SECTION_PRIORITY,
            )
        )

    async def _clear_prompt_attachments(self, ctx: AgentCallbackContext) -> None:
        manager = self.attachment_manager
        if manager is None:
            return
        writer = manager.bind_context(ctx)
        try:
            await writer.clear_section(self.SECTION_NAME)
            await writer.clear_section(self.CANDIDATE_SECTION_NAME)
        except ValueError as exc:
            logger.warning(
                "[SkillRetrievalPromptRail] attachment clear failed: %s", exc
            )

    def _disable_agentic_prompt(self, ctx: AgentCallbackContext) -> tuple[Any, ...]:
        restored = self._restore_legacy_list_skill(getattr(ctx, "agent", None))
        self._restore_native_skills_section()
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            self.system_prompt_builder.remove_section(self.CANDIDATE_SECTION_NAME)
        return restored

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        _ = ctx

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self._session_enabled or getattr(ctx, "exception", None) is not None:
            return
        inputs = getattr(ctx, "inputs", None)
        tool_name = str(
            getattr(inputs, "tool_name", None)
            or getattr(getattr(inputs, "tool_call", None), "name", None)
            or ""
        ).strip()
        if tool_name not in _SYMPHONY_RUNTIME_TOOL_NAMES:
            return
        # The session-owned command toolkit refreshes the SkillFS and consumes
        # the shared notice cursor as part of its result. Avoid a second scan.
        if tool_name == _SKILL_INDEX_TOOL_NAME:
            return
        try:
            reminder = consume_incremental_skill_reminder(
                self._session_scope,
                self._scan_selection_cards(),
                max_chars=self._incremental_notice_max_chars,
                discovery_tool_name=_SKILL_INDEX_TOOL_NAME,
            )
        except Exception:
            logger.warning(
                "Unable to consume Symphony Skill delta",
                exc_info=True,
            )
            return
        if reminder:
            self._append_tool_result_reminder(inputs, reminder)

    def _scan_selection_cards(self) -> dict[str, dict[str, str]]:
        if self._prompt_skillfs is None:
            return {}
        return self._prompt_skillfs.selection_cards()

    @staticmethod
    def _append_tool_result_reminder(
        inputs: Any,
        reminder: str,
    ) -> None:
        result = getattr(inputs, "tool_result", None)
        if isinstance(result, dict):
            updated = dict(result)
            for key in ("output", "result"):
                value = updated.get(key)
                if isinstance(value, str):
                    updated[key] = f"{value}\n\n{reminder}" if value else reminder
                    inputs.tool_result = updated
                    return
            updated["system_reminder"] = reminder
            inputs.tool_result = updated
            return
        if isinstance(result, str):
            inputs.tool_result = f"{result}\n\n{reminder}" if result else reminder

    async def _clear_runtime_skill_attachment(
        self,
        ctx: AgentCallbackContext,
    ) -> None:
        manager = self.attachment_manager
        if manager is None:
            return
        writer = manager.bind_context(ctx)
        if writer.session_id:
            await writer.clear_section(_RUNTIME_SKILL_ATTACHMENT_SECTION)

    def _hide_legacy_list_skill(self) -> None:
        ability_manager = getattr(self._agent, "ability_manager", None)
        if ability_manager is None:
            return
        get_ability = getattr(ability_manager, "get", None)
        remove_ability = getattr(ability_manager, "remove", None)
        if not callable(get_ability) or not callable(remove_ability):
            return

        for name in _LEGACY_LIST_SKILL_TOOL_NAMES:
            if name in self._hidden_legacy_abilities:
                continue
            card = get_ability(name)
            if card is None:
                continue
            removed = remove_ability(name)
            if removed is not None:
                self._hidden_legacy_abilities[name] = removed

    def _restore_legacy_list_skill(
        self,
        agent: Any | None = None,
    ) -> tuple[Any, ...]:
        if agent is not None:
            self._agent = agent
        ability_manager = getattr(self._agent, "ability_manager", None)
        if ability_manager is None or not self._hidden_legacy_abilities:
            return ()
        get_ability = getattr(ability_manager, "get", None)
        add_ability = getattr(ability_manager, "add", None)
        if not callable(get_ability) or not callable(add_ability):
            return ()

        restored: list[Any] = []
        for name, card in list(self._hidden_legacy_abilities.items()):
            current = get_ability(name)
            if current is None:
                add_ability(card)
                current = card
            restored.append(current)
            self._hidden_legacy_abilities.pop(name, None)
        return tuple(restored)

    def _restore_legacy_list_skill_in_model_inputs(
        self,
        ctx: AgentCallbackContext,
        restored_cards: tuple[Any, ...],
    ) -> None:
        """Expose a restored legacy ToolCard in the current model call."""

        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not restored_cards or inputs is None:
            return
        if tools is not None and not isinstance(tools, list):
            return

        restored = list(tools or [])
        original_count = len(restored)
        visible_names = {self._model_tool_name(tool) for tool in restored}
        for card in restored_cards:
            name = self._model_tool_name(card)
            if name in visible_names:
                continue
            to_tool_info = getattr(card, "tool_info", None)
            if not callable(to_tool_info):
                continue
            restored.append(to_tool_info())
        if len(restored) > original_count:
            inputs.tools = restored

    def _hide_native_skills_section(self) -> None:
        if self.system_prompt_builder is None:
            return
        if self._hidden_skills_section is None:
            self._hidden_skills_section = self.system_prompt_builder.get_section(
                SectionName.SKILLS
            )
        self.system_prompt_builder.remove_section(SectionName.SKILLS)

    def _restore_native_skills_section(self) -> None:
        if self.system_prompt_builder is None or self._hidden_skills_section is None:
            return
        if not self.system_prompt_builder.has_section(SectionName.SKILLS):
            self.system_prompt_builder.add_section(self._hidden_skills_section)
        self._hidden_skills_section = None

    def _prompt_snapshot(self) -> SkillPromptSnapshot:
        if self._frozen_prompt_snapshot is None:
            toolkit = self._toolkit()
            self._frozen_prompt_snapshot = (
                toolkit.frozen_prompt_snapshot
                if toolkit is not None
                else self._prompt_skillfs.prompt_snapshot()
                if self._prompt_skillfs is not None
                else self._empty_prompt_snapshot()
            )
        return self._frozen_prompt_snapshot

    def _toolkit(self) -> SkillRetrievalToolkit | None:
        environment = self._prompt_skillfs
        if environment is None:
            return None
        # The rail intentionally keeps only the environment public; use the
        # constructor-owned toolkit when available without introducing a
        # second live inventory scan.
        toolkit = getattr(self, "_source_toolkit", None)
        return toolkit if isinstance(toolkit, SkillRetrievalToolkit) else None

    def _empty_prompt_snapshot(self) -> SkillPromptSnapshot:
        return SkillPromptSnapshot(
            mode="small",
            total_count=0,
            entries=(),
            estimated_candidate_tokens=0,
            candidate_budget_tokens=(self._discovery_settings.candidate_budget_tokens),
            index_state="missing",
        )

    def _build_candidate_appendix(
        self,
        language: str,
        snapshot: SkillPromptSnapshot,
    ) -> str:
        english = str(language).lower().startswith("en")
        complete = snapshot.all_candidates_included
        lines = ["## Installed Skills" if english else "## 已安装 Skill"]

        if snapshot.branches:
            lines.extend(
                [
                    "",
                    "Categories:" if english else "分类：",
                    *_render_prompt_branches(snapshot.branches),
                ]
            )
            if snapshot.omitted_branch_count:
                lines.append(
                    f"- … {snapshot.omitted_branch_count} more categories."
                    if english
                    else f"- … 另有 {snapshot.omitted_branch_count} 个分类。"
                )

        if snapshot.entries:
            lines.extend(
                [
                    "",
                    ("Skills:" if complete else "Examples:")
                    if english
                    else ("Skill：" if complete else "示例："),
                    *_render_prompt_entries(snapshot.entries, empty_text=""),
                ]
            )
        elif not snapshot.branches:
            lines.extend(
                [
                    "",
                    (
                        "No available Skills."
                        if complete
                        else "No examples shown; use skill_index to find installed Skills."
                    )
                    if english
                    else (
                        "当前没有可用 Skill。"
                        if complete
                        else "未展示示例；可使用 skill_index 检索已安装 Skill。"
                    ),
                ]
            )

        return "\n".join(lines)

    @staticmethod
    def _filter_legacy_list_skill_from_model_inputs(ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not tools:
            return

        filtered = []
        for tool in tools:
            name = SkillRetrievalPromptRail._model_tool_name(tool)
            if name not in _LEGACY_LIST_SKILL_TOOL_NAMES:
                filtered.append(tool)
        if len(filtered) != len(tools):
            inputs.tools = filtered

    @staticmethod
    def _model_tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict):
                return str(function.get("name", "") or "")
            return str(tool.get("name", "") or "")
        return str(getattr(tool, "name", "") or "")

    @classmethod
    def _has_skill_index(cls, ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        tools = getattr(inputs, "tools", None)
        if not tools:
            return False
        return any(
            cls._model_tool_name(tool) == _SKILL_INDEX_TOOL_NAME for tool in tools
        )


def _render_prompt_entries(
    entries: tuple[SkillPromptEntry, ...],
    *,
    empty_text: str,
) -> list[str]:
    if not entries:
        return [empty_text]
    return [
        f"- `{_compact_prompt_text(entry.worker_id)}`: "
        f"{_compact_prompt_text(entry.description)}"
        for entry in entries
    ]


def _render_prompt_branches(
    branches: tuple[SkillPromptBranch, ...],
) -> list[str]:
    return [
        f"- `{_compact_prompt_text(branch.label)}`: "
        f"{_compact_branch_description(branch.description, branch.label)}"
        for branch in branches
    ]


def _compact_branch_description(description: str, fallback: str) -> str:
    primary = ""
    select_when = ""
    for paragraph in str(description or "").split("\n\n"):
        semantic_lines: list[str] = []
        for raw_line in paragraph.splitlines():
            line = raw_line.strip()
            lowered = line.casefold()
            if lowered.startswith("select when:"):
                if not select_when:
                    select_when = line
                continue
            if lowered.startswith(("covers ", "representative ", "don't select when:")):
                continue
            if line:
                semantic_lines.append(line)
        if semantic_lines and not primary:
            primary = " ".join(semantic_lines)
    return _compact_prompt_text(
        " ".join(part for part in (primary, select_when) if part) or fallback
    )


def _compact_prompt_text(value: str) -> str:
    return " ".join(str(value or "").replace("`", "\\`").split())


__all__ = ["SkillRetrievalPromptRail"]
