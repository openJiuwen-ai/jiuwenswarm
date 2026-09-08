"""JiuwenSwarm adaptation for Symphony's DCI-compatible Skill discovery."""

from __future__ import annotations

import os
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.symphony.discovery import (
    DiscoverySettings,
    InstalledSkillsDirectoryToolkit,
    SkillFS,
    SkillIndexSnapshot,
    SkillPromptBranch,
    SkillPromptEntry,
    SkillPromptSnapshot,
    SkillRecord,
    load_discovery_settings,
    scan_skill_directories,
)

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

SkillDirectoriesProvider = Sequence[str] | Callable[[], Sequence[str]]
DisabledSkillsProvider = Iterable[str] | Callable[[], Iterable[str]] | None
SkillSourcesProvider = Mapping[str, str] | Callable[[], Mapping[str, str]] | None
VisibleSkillsProvider = (
    set[str] | frozenset[str] | Callable[[], set[str] | frozenset[str] | None] | None
)

_CANDIDATE_BUDGET_RATIO = 0.01
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_SESSION_PROFILE_VERSION = 1


def _settings_from_profile(
    profile: Mapping[str, Any] | None,
) -> DiscoverySettings | None:
    raw = profile.get("settings") if isinstance(profile, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    values = dict(raw)
    preferred = values.get("prompt_preferred_skills")
    if isinstance(preferred, list):
        values["prompt_preferred_skills"] = tuple(str(item) for item in preferred)
    try:
        restored = load_discovery_settings(dict(raw))
        expected = asdict(restored)
        if any(
            key not in values
            or type(values[key]) is not type(value)
            or values[key] != value
            for key, value in expected.items()
        ):
            raise ValueError("settings are not canonical")
        return restored
    except (TypeError, ValueError, OverflowError):
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
    """Validate the persisted v1 profile before it can affect a session."""

    if not isinstance(profile, Mapping) or profile.get("schema_version") != 1:
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
    """Project JiuwenSwarm's local/plugin provenance into core records."""

    sources: dict[str, str] = {}
    for method_name in ("get_local_skills", "get_installed_plugins"):
        for item in _manager_items(manager, method_name):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            source = str(item.get("source") or item.get("marketplace") or "").strip()
            if name and source:
                sources.setdefault(name, source)
    return sources


def _config(config_base: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(config_base, dict):
        return config_base
    value = get_config() or {}
    return value if isinstance(value, dict) else {}


def _retrieval_config(
    config_base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = _config(config_base)
    symphony = config.get("symphony") if isinstance(config, dict) else {}
    retrieval = symphony.get("skill_retrieval") if isinstance(symphony, dict) else {}
    return retrieval if isinstance(retrieval, dict) else {}


def _env_switch(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip().lower() in _TRUE_VALUES


def is_skill_retrieval_enabled(
    config_base: dict[str, Any] | None = None,
) -> bool:
    """Return whether Symphony's flat-or-indexed discovery path is enabled."""

    override = _env_switch("SYMPHONY_SKILL_RETRIEVAL_ENABLED")
    return (
        override
        if override is not None
        else bool(_retrieval_config(config_base).get("enabled", False))
    )


def is_skill_retrieval_index_enabled(
    config_base: dict[str, Any] | None = None,
) -> bool:
    """Compatibility alias: taxonomy now follows the retrieval switch."""

    return is_skill_retrieval_enabled(config_base)


def build_discovery_settings(
    config_base: dict[str, Any] | None = None,
) -> DiscoverySettings:
    """Resolve the public Symphony discovery settings for this application."""

    # Candidate scale is a product-level invariant: complete metadata is used
    # only when it is strictly below one percent of the context window.
    # Ignore stale/user-authored ratio values so every entry point makes the
    # same session decision.
    # The retrieval switch controls both building and consuming taxonomy.
    # Legacy index/mode preferences no longer override this decision.
    config = _config(config_base)
    return replace(
        load_discovery_settings(config),
        candidate_budget_ratio=_CANDIDATE_BUDGET_RATIO,
        use_existing_index=is_skill_retrieval_enabled(config),
    )


def build_model_discovery_settings(
    config_base: dict[str, Any] | None = None,
    *,
    model: Any = None,
) -> DiscoverySettings:
    """Resolve discovery settings from the model actually built for a session."""

    settings = build_discovery_settings(config_base)
    if model is None:
        return settings

    model_name = str(
        getattr(getattr(model, "model_config", None), "model_name", "") or ""
    ).strip()
    config = _config(config_base)
    react = config.get("react") if isinstance(config, dict) else {}
    react = react if isinstance(react, dict) else {}
    context_engine = react.get("context_engine_config")
    context_engine = context_engine if isinstance(context_engine, dict) else {}

    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    # AgentOS attaches the selected entry's input window to the built Model;
    # otherwise use the normal context-engine fallback and model-specific map.
    fallback = _positive_int(getattr(model, "_agentos_ctx_window", None))
    if fallback is None:
        fallback = _positive_int(context_engine.get("context_window_tokens"))
    if not model_name and fallback is None:
        return settings

    model_windows = context_engine.get("model_context_window_tokens")
    model_windows = model_windows if isinstance(model_windows, dict) else None
    try:
        from openjiuwen.core.context_engine.context.context_utils import ContextUtils

        context_window_tokens = ContextUtils.resolve_context_max(
            model_name=model_name or None,
            fallback_context_window_tokens=fallback,
            model_context_window_tokens=model_windows,
        )
    except Exception:
        logger.warning(
            "Unable to resolve Skill retrieval model context: %s",
            model_name or "<unknown>",
            exc_info=True,
        )
        context_window_tokens = fallback or settings.context_window_tokens
    return replace(
        settings,
        context_window_tokens=max(1, int(context_window_tokens)),
    )


def skill_retrieval_artifact_root(
    config_base: dict[str, Any] | None = None,
) -> Path:
    raw = str(
        _retrieval_config(config_base).get("artifact_root")
        or os.getenv("SYMPHONY_SKILL_RETRIEVAL_ROOT")
        or ""
    ).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return get_agent_workspace_dir() / "symphony" / "skill_retrieval"


class SkillRetrievalToolkit:
    """Expose one session-local structured Skill index Tool."""

    TOOL_NAME = "skill_index"

    def __init__(
        self,
        *,
        skill_directories: SkillDirectoriesProvider,
        disabled_skills: DisabledSkillsProvider = None,
        source_by_name: SkillSourcesProvider = None,
        visible_skill_names: VisibleSkillsProvider = None,
        index_skill_directories: SkillDirectoriesProvider | None = None,
        session_scope: str = "default",
        config_base: dict[str, Any] | None = None,
        settings: DiscoverySettings | None = None,
        artifact_root: Path | str | None = None,
        auto_build_index: bool = False,
        frozen_profile: Mapping[str, Any] | None = None,
    ) -> None:
        self._skill_directories = skill_directories
        self._disabled_skills = disabled_skills
        self._source_by_name = source_by_name
        self._visible_skill_names = visible_skill_names
        self._index_skill_directories = index_skill_directories
        self._session_scope = session_scope
        # Configuration and the scale/index decision are session properties.
        # A settings save affects newly constructed sessions, never this one.
        self._config_base = (
            deepcopy(config_base) if isinstance(config_base, dict) else None
        )
        configured_settings = replace(
            settings or build_discovery_settings(self._config_base),
            candidate_budget_ratio=_CANDIDATE_BUDGET_RATIO,
        )
        if frozen_profile is not None and not is_valid_skill_retrieval_session_profile(
            frozen_profile
        ):
            logger.warning("Ignoring invalid persisted Skill retrieval profile")
            frozen_profile = None
        restored_settings = _settings_from_profile(frozen_profile)
        restored_snapshot = _snapshot_from_profile(frozen_profile)
        restored_index_snapshot: SkillIndexSnapshot | None = None
        if frozen_profile is not None:
            raw_index_snapshot = frozen_profile.get("index_snapshot")
            if raw_index_snapshot is not None:
                restored_index_snapshot = SkillIndexSnapshot.from_dict(
                    raw_index_snapshot
                )
        configured_settings = replace(
            restored_settings or configured_settings,
            candidate_budget_ratio=_CANDIDATE_BUDGET_RATIO,
        )
        self._index_enabled = bool(
            frozen_profile.get("enabled", is_skill_retrieval_enabled(self._config_base))
            if frozen_profile is not None
            else is_skill_retrieval_enabled(self._config_base)
        )
        explicit_artifact_root = str(artifact_root or "").strip()
        resolved_artifact_root = (
            Path(explicit_artifact_root).expanduser().resolve()
            if explicit_artifact_root
            else skill_retrieval_artifact_root(config_base)
        )
        # One filesystem scan feeds threshold estimation, the prompt appendix,
        # and the initial artifact. Subsequent Tool refreshes use the live
        # provider so installs and enable/disable changes remain reachable.
        initial_records = self.current_records()
        flat_settings = replace(configured_settings, use_existing_index=False)
        probe_environment = SkillFS(
            lambda: initial_records,
            settings=flat_settings,
            visible_skill_names=self._visible_skill_names,
            artifact_root=resolved_artifact_root,
            index_root=resolved_artifact_root,
        )
        flat_snapshot = probe_environment.prompt_snapshot()
        restored_cards = (
            frozen_profile.get("selection_cards")
            if isinstance(frozen_profile, Mapping)
            else None
        )
        self._frozen_selection_cards = (
            {
                str(key): {
                    "name": str(value.get("name") or key),
                    "description": str(value.get("description") or ""),
                }
                for key, value in restored_cards.items()
                if isinstance(value, Mapping) and str(key)
            }
            if isinstance(restored_cards, Mapping)
            else probe_environment.selection_cards()
        )
        self._candidate_scale = (
            "small"
            if (restored_snapshot or flat_snapshot).all_candidates_included
            else "large"
        )
        self._estimated_candidate_tokens = (
            restored_snapshot or flat_snapshot
        ).estimated_candidate_tokens
        self._candidate_budget_tokens = (
            restored_snapshot or flat_snapshot
        ).candidate_budget_tokens

        # Small inventories always use the complete frozen metadata snapshot.
        # Large inventories may consume only the taxonomy revision visible at
        # session creation; a build published later is for new sessions.
        consume_index = self._candidate_scale == "large" and self._index_enabled
        self._settings = replace(
            configured_settings,
            use_existing_index=consume_index,
        )
        initial_available = True

        def _session_records() -> tuple[SkillRecord, ...]:
            nonlocal initial_available
            if initial_available:
                initial_available = False
                return initial_records
            return self.current_records()

        pinned_snapshot_kwargs = (
            {"pinned_index_snapshot": restored_index_snapshot}
            if consume_index and frozen_profile is not None
            else {}
        )
        self._environment = SkillFS(
            _session_records,
            settings=self._settings,
            visible_skill_names=self._visible_skill_names,
            artifact_root=resolved_artifact_root,
            index_root=resolved_artifact_root,
            pin_index_revision=consume_index,
            **pinned_snapshot_kwargs,
        )
        environment_snapshot = self._environment.prompt_snapshot()
        if restored_snapshot is not None and restored_snapshot.mode in {
            "indexed",
            "indexed-stale",
        }:
            if not restored_snapshot.branches and environment_snapshot.branches:
                # Profiles written before branch orientation was persisted still
                # carry a pinned taxonomy snapshot. Recover only its root routing
                # hints; keep the frozen candidate entries and budget unchanged.
                restored_snapshot = replace(
                    restored_snapshot,
                    branches=environment_snapshot.branches,
                    omitted_branch_count=environment_snapshot.omitted_branch_count,
                )
        self._frozen_prompt_snapshot = restored_snapshot or environment_snapshot
        restored_strategy = (
            str(frozen_profile.get("effective_strategy") or "")
            if isinstance(frozen_profile, Mapping)
            else ""
        )
        self._effective_strategy = (
            restored_strategy
            if restored_strategy
            in {"small_full", "large_flat", "indexed", "indexed_stale"}
            else resolve_skill_retrieval_strategy(
                enabled=True,
                candidate_scale=self._candidate_scale,
                layout=self._environment.artifact.layout,
                index_state=self._environment.artifact.index_state,
            )
        )
        self._index_toolkit = InstalledSkillsDirectoryToolkit(
            self._environment,
            session_scope=session_scope,
            incremental_notice_max_chars=(self._settings.incremental_notice_max_chars),
        )
        self._auto_build_result: dict[str, Any] | None = None
        should_auto_build = auto_build_index and is_skill_retrieval_enabled(
            self._config_base
        )
        should_auto_build = should_auto_build and self._index_enabled
        if should_auto_build and self._candidate_scale == "large":
            # Session-selected MCP-bundled Skills participate in the frozen
            # 1% decision and the live SkillFS, but they are not stable global
            # inventory.  Judge shared-index freshness against the dedicated
            # local inventory so an MCP overlay does not trigger a rebuild for
            # every differently configured session.
            index_records = (
                initial_records
                if self._index_skill_directories is None
                else self.current_index_records()
            )
            index_probe = SkillFS(
                lambda: index_records,
                settings=replace(configured_settings, use_existing_index=True),
                # Shared taxonomy freshness is defined by the stable global
                # inventory. Member/session visibility is only a projection of
                # that generation and must never make it appear stale.
                visible_skill_names=None,
                artifact_root=resolved_artifact_root,
                index_root=resolved_artifact_root,
                pin_index_revision=True,
            )
            if index_probe.artifact.index_state in {"missing", "stale"}:
                self._start_background_index_build(resolved_artifact_root)

    @property
    def environment(self) -> SkillFS:
        """Return the stateful environment shared with the prompt rail."""

        return self._environment

    @property
    def settings(self) -> DiscoverySettings:
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
        """Return the metadata snapshot captured when the session was built."""

        return self._frozen_prompt_snapshot

    @property
    def frozen_selection_cards(self) -> dict[str, dict[str, str]]:
        return dict(self._frozen_selection_cards)

    @property
    def auto_build_result(self) -> dict[str, Any] | None:
        return self._auto_build_result

    def session_profile(self) -> dict[str, Any]:
        """Return the bounded frozen profile used by status and persistence."""

        artifact = self._environment.artifact
        pinned = getattr(self._environment, "pinned_index_snapshot", None)
        revision = str(getattr(pinned, "fingerprint", "") or "")
        return {
            "schema_version": _SESSION_PROFILE_VERSION,
            "session_id": self._session_scope,
            "index_enabled": self._index_enabled,
            "candidate_scale": self._candidate_scale,
            "estimated_candidate_tokens": self._estimated_candidate_tokens,
            "candidate_budget_tokens": self._candidate_budget_tokens,
            "effective_strategy": self._effective_strategy,
            "layout": artifact.layout,
            "index_state": self._frozen_prompt_snapshot.index_state,
            "pinned_index_revision": revision,
            "searchable_count": len(self._environment.selection_cards()),
            "settings": asdict(self._settings),
            "selection_cards": self.frozen_selection_cards,
            "prompt_snapshot": asdict(self._frozen_prompt_snapshot),
            "index_snapshot": pinned.to_dict() if pinned is not None else None,
        }

    def current_records(self) -> tuple[SkillRecord, ...]:
        return self._scan_records(self._skill_directories)

    def current_index_records(self) -> tuple[SkillRecord, ...]:
        """Return the stable inventory permitted in the shared taxonomy."""

        directories = self._index_skill_directories
        if directories is None:
            return self.current_records()
        return self._scan_records(directories)

    def _scan_records(
        self,
        directories_provider: SkillDirectoriesProvider,
    ) -> tuple[SkillRecord, ...]:
        directories = (
            directories_provider()
            if callable(directories_provider)
            else directories_provider
        )
        disabled = (
            self._disabled_skills()
            if callable(self._disabled_skills)
            else self._disabled_skills
        )
        sources = (
            self._source_by_name()
            if callable(self._source_by_name)
            else self._source_by_name
        )
        return scan_skill_directories(
            [str(directory) for directory in directories],
            disabled_skills=disabled or (),
            source_by_name=sources or {},
        ).items

    async def skill_index(
        self,
        **kwargs: Any,
    ):
        """Execute through the same structured directory implementation as the Tool."""

        return await self._index_toolkit.skill_index(**kwargs)

    def get_tools(self) -> list[Tool]:
        return list(self._index_toolkit.get_tools())

    def _start_background_index_build(self, artifact_root: Path) -> None:
        try:
            from jiuwenswarm.symphony.skill_retrieval import SkillTaxonomyRuntime

            self._auto_build_result = SkillTaxonomyRuntime(
                records_provider=self.current_index_records,
                index_root=artifact_root,
                config_base=self._config_base,
            ).start_build()
        except Exception:
            logger.warning(
                "Unable to start the enabled Skill taxonomy build; "
                "the session remains on its frozen fallback",
                exc_info=True,
            )


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
    "build_discovery_settings",
    "build_model_discovery_settings",
    "is_skill_retrieval_enabled",
    "is_skill_retrieval_index_enabled",
    "resolve_skill_retrieval_strategy",
    "skill_retrieval_artifact_root",
    "skill_sources_from_manager",
]
