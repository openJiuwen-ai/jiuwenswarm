"""JiuwenSwarm adaptation for Symphony's session capability discovery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.symphony import SymphonyRuntime
from openjiuwen.symphony.config import RetrievalConfig
from openjiuwen.symphony.discovery import (
    CapabilityCandidate,
    SkillFS,
    SkillIndexSnapshot,
    SkillPromptBranch,
    SkillPromptEntry,
    SkillPromptSnapshot,
    SkillRecord,
)

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.context_window import resolve_context_window_tokens
from jiuwenswarm.common.symphony_config import configured_skill_roots, load_symphony_config

logger = logging.getLogger(__name__)

SkillDirectoriesProvider = Sequence[str] | Callable[[], Sequence[str]]
DisabledSkillsProvider = Iterable[str] | Callable[[], Iterable[str]] | None
SkillSourcesProvider = Mapping[str, str] | Callable[[], Mapping[str, str]] | None
VisibleSkillsProvider = (
    set[str] | frozenset[str] | Callable[[], set[str] | frozenset[str] | None] | None
)

SKILL_RETRIEVAL_SESSION_PROFILE_VERSION = 2
_PROFILE_SETTINGS_FIELDS = {"prompt", "output", "use_existing_index"}


def _settings_from_profile(profile: Mapping[str, Any] | None) -> RetrievalConfig | None:
    raw = profile.get("settings") if isinstance(profile, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        data = dict(raw)
        if data.keys() != _PROFILE_SETTINGS_FIELDS:
            return None
        restored = RetrievalConfig.model_validate(data)
        if restored.model_dump(mode="json", include=_PROFILE_SETTINGS_FIELDS) != data:
            return None
        return restored
    except (TypeError, ValueError):
        return None


def _snapshot_from_profile(
    profile: Mapping[str, Any] | None,
) -> SkillPromptSnapshot | None:
    raw = profile.get("prompt_snapshot") if isinstance(profile, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    raw_entries = raw.get("entries")
    raw_branches = raw.get("branches", ())
    if not isinstance(raw_entries, (list, tuple)) or not isinstance(
        raw_branches, (list, tuple)
    ):
        return None
    try:
        mode = str(raw.get("mode") or "")
        index_state = str(raw.get("index_state") or "")
        total_count = int(raw.get("total_count"))
        estimated_tokens = int(raw.get("estimated_candidate_tokens"))
        budget_tokens = int(raw.get("candidate_budget_tokens"))
        omitted_count = int(raw.get("omitted_branch_count") or 0)
        if mode not in {"small", "large-flat", "indexed", "indexed-stale"}:
            return None
        if index_state not in {"missing", "fresh", "stale"}:
            return None
        if min(total_count, estimated_tokens, omitted_count) < 0 or budget_tokens < 1:
            return None
        return SkillPromptSnapshot(
            mode=mode,
            total_count=total_count,
            entries=tuple(
                SkillPromptEntry(
                    worker_id=str(entry.get("worker_id") or ""),
                    description=str(entry.get("description") or ""),
                    source=str(entry.get("source") or ""),
                )
                for entry in raw_entries
                if isinstance(entry, Mapping) and str(entry.get("worker_id") or "")
            ),
            estimated_candidate_tokens=estimated_tokens,
            candidate_budget_tokens=budget_tokens,
            index_state=index_state,
            branches=tuple(
                SkillPromptBranch(
                    path=str(branch.get("path") or ""),
                    label=str(branch.get("label") or ""),
                    description=str(branch.get("description") or ""),
                )
                for branch in raw_branches
                if isinstance(branch, Mapping) and str(branch.get("path") or "")
            ),
            omitted_branch_count=omitted_count,
        )
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid persisted Skill retrieval prompt snapshot")
        return None


def is_valid_skill_retrieval_session_profile(
    profile: Mapping[str, Any] | None,
) -> bool:
    """Validate the persisted configuration profile before it can affect a session."""

    if not isinstance(profile, Mapping) or profile.get("schema_version") != SKILL_RETRIEVAL_SESSION_PROFILE_VERSION:
        return False
    if profile.get("enabled") is False:
        return True
    if not isinstance(profile.get("index_enabled"), bool):
        return False
    if not isinstance(profile.get("selection_cards"), Mapping):
        return False
    if _settings_from_profile(profile) is None:
        return False
    if _snapshot_from_profile(profile) is None:
        return False
    raw_index_snapshot = profile.get("index_snapshot")
    expert_snapshot = profile.get("expert_index_snapshot")
    if expert_snapshot is not None:
        try:
            expert = SkillIndexSnapshot.from_dict(expert_snapshot)
        except (TypeError, ValueError, OverflowError):
            return False
        if not isinstance(profile.get("expert_taxonomy_root"), str):
            return False
        if raw_index_snapshot is not None and expert.fingerprint != profile.get("pinned_index_revision"):
            return False
    if raw_index_snapshot is None:
        return not str(profile.get("pinned_index_revision") or "")
    try:
        snapshot = SkillIndexSnapshot.from_dict(raw_index_snapshot)
    except (TypeError, ValueError, OverflowError):
        return False
    return snapshot.fingerprint == str(profile.get("pinned_index_revision") or "")


def _manager_items(manager: Any, method_name: str) -> list[Any]:
    method = getattr(manager, method_name, None)
    if not callable(method):
        return []
    try:
        items = method()
    except Exception:
        return []
    return items if isinstance(items, list) else []


def skill_sources_from_manager(manager: Any) -> dict[str, str]:
    """Map installation identities (directory IDs, not display names) to provenance."""

    sources: dict[str, str] = {}
    for method_name in ("get_local_skills", "get_installed_plugins"):
        for item in _manager_items(manager, method_name):
            if not isinstance(item, dict):
                continue
            name = str(item.get("worker_id") or item.get("dir_name") or item.get("name") or "").strip()
            source = str(item.get("source") or item.get("marketplace") or "").strip()
            if name and source:
                sources.setdefault(name, source)
    return sources


def skill_sources_from_directories(directories: Sequence[str | Path]) -> dict[str, str]:
    """Read collection-local provenance using the same IDs as Core inventory."""
    from jiuwenswarm.server.runtime.skill.skilldev.state_utils import load_skill_state

    sources: dict[str, str] = {}
    for directory in directories:
        state = load_skill_state(skills_dir=directory)
        for key in ("local_skills", "installed_plugins"):
            items = state.get(key)
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                identifier = str(item.get("worker_id") or item.get("dir_name") or item.get("name") or "").strip()
                source = str(item.get("source") or item.get("marketplace") or "").strip()
                if identifier and source:
                    sources.setdefault(identifier, source)
    return sources


def _config(config_base: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(config_base, dict):
        return config_base
    value = get_config() or {}
    return value if isinstance(value, dict) else {}


def is_skill_retrieval_enabled(config_base: dict[str, Any] | None = None) -> bool:
    return load_symphony_config(_config(config_base)).retrieval.enabled


def build_model_discovery_settings(config_base: dict[str, Any] | None = None, *, model: Any = None) -> RetrievalConfig:
    config = _config(config_base)
    context_window_tokens = None
    if model is not None:
        request = getattr(model, "model_config", None)
        model_name = str(getattr(request, "model_name", None) or getattr(request, "model", "") or "").strip()
        react = config.get("react")
        context_window_tokens = resolve_context_window_tokens(
            model_name=model_name or None,
            context_engine_config=react if isinstance(react, dict) else {},
            model_config_obj=request,
            model_context_window_override=getattr(model, "_agentos_ctx_window", None),
        )
    return load_symphony_config(config, context_window_tokens=context_window_tokens).retrieval


def skill_retrieval_artifact_root(config_base: dict[str, Any] | None = None) -> Path:
    return SymphonyRuntime(None, load_symphony_config(_config(config_base))).retrieval.artifact_root


def disabled_skills_from_directories(directories: Sequence[Path]) -> list[str]:
    from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

    return sorted({name for root in directories for name in load_execution_disabled_skills(skills_dir=root)})


class SkillRetrievalToolkit:
    """Bind the agent's Runtime search and Skill prompt to the same capability view."""

    def __init__(self, *, skill_directories: SkillDirectoriesProvider,
                 disabled_skills: DisabledSkillsProvider = None, source_by_name: SkillSourcesProvider = None,
                 visible_skill_names: VisibleSkillsProvider = None, session_scope: str = "default",
                 config_base: dict[str, Any] | None = None, settings: RetrievalConfig | None = None,
                 model: Any = None, auto_build_index: bool = False,
                 frozen_profile: Mapping[str, Any] | None = None) -> None:
        self._config_base = deepcopy(_config(config_base))
        self._config = load_symphony_config(self._config_base)
        self._model = model
        self._disabled_skills = disabled_skills
        self._source_by_name = source_by_name
        self._session_scope = session_scope
        self._runtime = SymphonyRuntime(model, self._config)
        self._has_expert_sources = any(source.type == "expert" for source in self._config.capability.sources)
        self._close_task: asyncio.Task | None = None
        self._closed = False
        self._ability_manager: Callable[[], Any] | None = None
        if frozen_profile is not None and not is_valid_skill_retrieval_session_profile(frozen_profile):
            logger.warning("Ignoring invalid persisted Skill retrieval profile")
            frozen_profile = None
        configured = (_settings_from_profile(frozen_profile) or settings
                      or build_model_discovery_settings(self._config_base, model=model)).model_copy(deep=True)
        restored_snapshot = _snapshot_from_profile(frozen_profile)
        restored_index = None
        if frozen_profile is not None and frozen_profile.get("index_snapshot") is not None:
            restored_index = SkillIndexSnapshot.from_dict(frozen_profile["index_snapshot"])
        self._index_enabled = self._config.retrieval.enabled and configured.use_existing_index
        flat_config = configured.model_copy(update={"use_existing_index": False})
        self._environment = self.runtime.retrieval.bind_search(
            session_scope=session_scope, capability_directories=skill_directories,
            visible_capability_ids=visible_skill_names, disabled_capability_ids=disabled_skills,
            source_by_id=source_by_name, config=flat_config,
            catalog_candidates=self._catalog_candidates,
        )
        flat_snapshot = self._environment.prompt_snapshot(refresh=False)
        baseline = restored_snapshot or flat_snapshot
        self._candidate_scale = "small" if baseline.all_candidates_included else "large"
        self._estimated_candidate_tokens = baseline.estimated_candidate_tokens
        self._candidate_budget_tokens = baseline.candidate_budget_tokens
        consume_index = self._index_enabled and (self._candidate_scale == "large" or self._has_expert_sources)
        self._settings = configured.model_copy(update={"use_existing_index": consume_index})
        if consume_index:
            pinned = {}
            if frozen_profile is not None:
                pinned["pinned_index_snapshot"] = restored_index
                pinned["pinned_expert_index_snapshot"] = (
                    frozen_profile.get("expert_index_snapshot")
                    if frozen_profile.get("expert_taxonomy_root") == str(self.runtime.retrieval.artifact_root)
                    else None
                )
            self._environment = self.runtime.retrieval.bind_search(
                session_scope=session_scope, capability_directories=skill_directories,
                visible_capability_ids=visible_skill_names, disabled_capability_ids=disabled_skills,
                source_by_id=source_by_name, config=self._settings, pin_index_revision=True,
                catalog_candidates=self._catalog_candidates, **pinned,
            )
        self._frozen_prompt_snapshot = restored_snapshot or self._environment.prompt_snapshot(refresh=False)
        restored_cards = frozen_profile.get("selection_cards") if frozen_profile is not None else None
        self._frozen_selection_cards = (
            deepcopy(dict(restored_cards)) if isinstance(restored_cards, Mapping)
            else self._environment.selection_cards(refresh=False)
        )
        self._effective_strategy = resolve_skill_retrieval_strategy(
            enabled=True, candidate_scale=self._candidate_scale,
            layout=self._environment.artifact.layout, index_state=self._frozen_prompt_snapshot.index_state,
        )
        self._auto_build_result: dict[str, Any] | None = None
        if auto_build_index and consume_index and self._candidate_scale == "large":
            self._start_background_index_build()

    @property
    def runtime(self) -> SymphonyRuntime:
        return self._runtime

    @property
    def environment(self) -> SkillFS:
        return self._environment

    @property
    def settings(self) -> RetrievalConfig:
        return self._settings

    @property
    def session_scope(self) -> str:
        return self._session_scope

    @property
    def index_enabled(self) -> bool:
        return self._index_enabled

    @property
    def candidate_scale(self) -> str:
        return self._candidate_scale

    @property
    def estimated_candidate_tokens(self) -> int:
        return self._estimated_candidate_tokens

    @property
    def candidate_budget_tokens(self) -> int:
        return self._candidate_budget_tokens

    @property
    def effective_strategy(self) -> str:
        return self._effective_strategy

    @property
    def frozen_prompt_snapshot(self) -> SkillPromptSnapshot:
        return self._frozen_prompt_snapshot

    @property
    def frozen_selection_cards(self) -> dict[str, dict[str, str]]:
        return deepcopy(self._frozen_selection_cards)

    @property
    def auto_build_result(self) -> dict[str, Any] | None:
        return self._auto_build_result

    def session_profile(self) -> dict[str, Any]:
        pinned = self._environment.pinned_index_snapshot
        profile = {
            "schema_version": SKILL_RETRIEVAL_SESSION_PROFILE_VERSION,
            "session_id": self._session_scope, "enabled": self._config.retrieval.enabled,
            "index_enabled": self._index_enabled, "candidate_scale": self._candidate_scale,
            "estimated_candidate_tokens": self._estimated_candidate_tokens,
            "candidate_budget_tokens": self._candidate_budget_tokens,
            "effective_strategy": self._effective_strategy,
            "layout": self._environment.artifact.layout,
            "index_state": self._frozen_prompt_snapshot.index_state,
            "pinned_index_revision": str(getattr(pinned, "fingerprint", "") or ""),
            "searchable_count": len(self._environment.selection_cards()),
            "settings": self._settings.model_dump(mode="json", include=_PROFILE_SETTINGS_FIELDS),
            "selection_cards": self.frozen_selection_cards,
            "prompt_snapshot": asdict(self._frozen_prompt_snapshot),
            "index_snapshot": pinned.to_dict() if pinned is not None else None,
        }
        if self._has_expert_sources:
            expert = self._environment.pinned_expert_index_snapshot
            profile["expert_taxonomy_root"] = str(self.runtime.retrieval.artifact_root)
            profile["expert_index_snapshot"] = expert.to_dict() if expert is not None else None
        return profile

    def current_records(self) -> tuple[SkillRecord, ...]:
        return self._environment.records()

    def bind_ability_manager(self, provider: Callable[[], Any]) -> None:
        self._ability_manager = provider

    def get_tools(self) -> list[Tool]:
        tool, = self.runtime.retrieval.get_tools()
        tool.card.description = (
            "Before starting work that needs independent review, distinct domain or platform expertise, "
            'or separate deliverables that can be developed in parallel, call this tool with type=["expert"] '
            "to find relevant installed experts. Search even if no expert was named. "
            "Use one focused query per role or workstream. "
            f"{tool.card.description} "
            "Use returned expert references when spawn_expert is available."
        )
        return [tool]

    async def _catalog_candidates(self, kind: str) -> tuple[CapabilityCandidate, ...]:
        from jiuwenswarm.runtime.symphony.capabilities import expert_candidates, mcp_candidates

        if kind == "expert":
            return await expert_candidates(self._config)
        manager = self._ability_manager() if self._ability_manager is not None else None
        return await mcp_candidates(self._config, self._environment, manager)

    def _start_background_index_build(self) -> None:
        from jiuwenswarm.runtime.symphony.service import get_swarm_symphony_service
        from jiuwenswarm.runtime.symphony.skill_retrieval import SkillTaxonomyRuntime

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Unable to schedule Skill taxonomy without a host event loop")
            return
        model = None
        try:
            model = get_swarm_symphony_service().runtime().model
        except (RuntimeError, ValueError) as exc:
            if not self._config.retrieval.index_build.task.allow_fallback_tree:
                self._auto_build_result = {"success": False, "result": str(exc),
                                           "data": {"state": "failed"}}
                logger.warning("Unable to resolve the host model for Skill taxonomy: %s", exc)
                return
        roots = configured_skill_roots(self._config)
        runtime = SkillTaxonomyRuntime(
            config=self._config, model=model,
            disabled_capability_ids=lambda: disabled_skills_from_directories(roots),
            source_by_id=lambda: skill_sources_from_directories(roots),
        )
        if runtime.status().get("fresh"):
            return
        try:
            self._auto_build_result = runtime.start_build()
        except RuntimeError:
            logger.warning("Unable to schedule Skill taxonomy on a host event loop", exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.runtime.retrieval.close_search()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._close_task = loop.create_task(self.runtime.aclose(), name="symphony-discovery-close")
        self._close_task.add_done_callback(self._consume_close)

    @staticmethod
    def _consume_close(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("Unable to close Skill discovery: %s", error)

    async def aclose(self) -> None:
        self.close()
        if self._close_task is not None:
            await asyncio.shield(self._close_task)
        else:
            await self.runtime.aclose()


def resolve_skill_retrieval_strategy(
    *,
    enabled: bool,
    candidate_scale: str,
    layout: str,
    index_state: str,
) -> str:
    if not enabled:
        return "legacy"
    if candidate_scale == "small":
        return "small_full"
    if layout != "tree":
        return "large_flat"
    return "indexed" if index_state == "fresh" else "indexed_stale"


__all__ = [
    "SkillRetrievalToolkit",
    "build_model_discovery_settings",
    "is_skill_retrieval_enabled",
    "resolve_skill_retrieval_strategy",
    "skill_retrieval_artifact_root",
    "skill_sources_from_manager",
]
