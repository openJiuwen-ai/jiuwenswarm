# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RailStateMachineBase — shared base for state-machine-driven rails.

Three independent rails (DesignRail / future ImplementRail / future
ProjectAnalysisRail) each own their state machine + advance tool, but share
the IMPLEMENTATION PATTERN via this base:

  * In-memory state (``self._stage``) — no persistence file.
  * Advance-tool registration/unregistration via ``agent.ability_manager``
    (rail-owns-tools pattern; tool lives only while the rail is mounted).
  * ``before_model_call`` skill-methodology injection (self-contained frame:
    strip front-matter + tell the LLM to follow inline, no skill toolkit).
  * Artifacts presence check (gate transitions on declared artifact files).
  * Feature-name resolution (latest-modified ``.aet/features/`` subdir —
    supports multi-flow).

Subclass contract (class attrs):
  ADVANCE_TOOL : str   — the tool name the LLM calls to advance (e.g.
                         ``"sdd_advance"``, ``"implement_advance"``).
  stages       : dict  — stage definitions; each value is a dict with
                         optional ``skill`` / ``artifacts`` / ``next``.
  SKILLS_DIR   : str   — subdirectory under ``rail_pkg_dir`` holding the
                         embedded skill methodologies (``<skill>/SKILL.md``).
  SECTION_NAME : str   — system-prompt section name for injected methodology.

Subclasses MAY override:
  _STAGE_LABELS       — human-readable stage labels for the methodology frame.
  _advance_tool_description / _advance_tool_input_params — tool schema.
  after_tool_call     — add rail-specific tool handling (e.g. ask_user
                         review approve/reject for DesignRail).

Subclasses do NOT touch: state management, tool registration mechanics,
skill-methodology framing, artifacts check, feature resolution.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

__all__ = ["RailStateMachineBase"]


class RailStateMachineBase(DeepAgentRail):
    """Shared base for state-machine-driven rails (design/implement/pa)."""

    # ── subclass contract (class attributes) ──
    ADVANCE_TOOL: str = ""
    stages: dict = {}
    SKILLS_DIR: str = ""
    SECTION_NAME: str = "rail_skill"
    _STAGE_LABELS: dict = {}

    def __init__(
        self,
        *,
        rail_pkg_dir: Path,
        project_dir: Path,
        priority: int = 60,
    ) -> None:
        super().__init__()
        self._rail_pkg_dir: Path = Path(rail_pkg_dir)
        self._project_dir: Path = Path(project_dir)
        self._priority: int = priority
        # In-memory state (None = not started; defaults to "init" on read).
        self._stage: Optional[str] = None
        self._system_prompt_builder = None
        self._owned_tool_names: set[str] = set()
        # Per-stage skill-methodology cache. SKILL.md files are static
        # (ship with the rail package); caching avoids re-stat + re-read
        # on every before_model_call (the rail's hottest hook).
        self._skill_cache: dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def init(self, agent) -> None:  # type: ignore[override]
        """Cache system_prompt_builder + register the advance tool."""
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self._system_prompt_builder is None:
            logger.warning(
                "[%s] no system_prompt_builder; injection disabled",
                type(self).__name__,
            )
        self._register_advance_tool(agent)
        logger.info("[%s] init, stage=%s", type(self).__name__, self._stage)

    def uninit(self, agent) -> None:  # type: ignore[override]
        """Unregister tools owned by this rail."""
        self._unregister_tools(agent)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------
    async def before_model_call(self, ctx: AgentCallbackContext) -> None:  # type: ignore[override]
        """Inject the current stage's skill methodology (self-contained frame).

        ``done`` and stages with no skill file get no injection — the LLM
        runs in plain mode (hand back to the base agent).
        """
        if self._system_prompt_builder is None:
            return
        # Always drop the previous section so state changes win.
        self._system_prompt_builder.remove_section(self.SECTION_NAME)

        stage = self._stage or "init"
        if stage not in self.stages:
            return

        content = self._load_skill_methodology(stage)
        if not content:
            # No skill for this stage (e.g. init has no skill). Inject a
            # bootstrap so the LLM knows about the advance tool + how to
            # start / advance the flow. ``done`` gets no injection (hand
            # back to plain agent mode).
            content = self._bootstrap_payload(stage)
            if not content:
                return
            logger.info(
                "[%s] bootstrap injected for stage=%s (no skill)",
                type(self).__name__,
                stage,
            )

        self._system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={"en": content, "cn": content},
                priority=self._priority,
            )
        )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:  # type: ignore[override]
        """Base: no-op. Subclasses override to handle rail-specific tools
        (e.g. DesignRail handles ``ask_user`` review approve/reject).

        The advance tool is handled in its own func (``_handle_advance``),
        not here — the tool func does the transition and returns a result
        to the LLM; the next ``before_model_call`` injects the new skill.
        """
        return

    # ------------------------------------------------------------------
    # Advance tool registration (rail-owns-tools pattern)
    # ------------------------------------------------------------------
    def _register_advance_tool(self, agent) -> None:
        """Register the advance tool with the agent's ability_manager.

        The tool lives only while this rail is mounted (uninit unregisters
        it), so the blast radius equals the rail's mount scope.
        """
        if not self.ADVANCE_TOOL:
            return
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            logger.warning(
                "[%s] no ability_manager; %s tool not registered",
                type(self).__name__,
                self.ADVANCE_TOOL,
            )
            return
        try:
            card = ToolCard(
                id=self.ADVANCE_TOOL,
                name=self.ADVANCE_TOOL,
                description=self._advance_tool_description(),
                input_params=self._advance_tool_input_params(),
            )

            async def _advance_func(**kwargs: Any) -> dict:
                return self._handle_advance(kwargs)

            local_func = LocalFunction(card=card, func=_advance_func)
            result = ability_manager.add_ability(card, local_func)
            if getattr(result, "added", False):
                self._owned_tool_names.add(self.ADVANCE_TOOL)
                logger.info(
                    "[%s] registered tool: %s",
                    type(self).__name__,
                    self.ADVANCE_TOOL,
                )
        except Exception as exc:  # noqa: BLE001 — never crash agent creation
            logger.warning(
                "[%s] register %s failed: %s",
                type(self).__name__,
                self.ADVANCE_TOOL,
                exc,
            )

    def _unregister_tools(self, agent) -> None:
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return
        for name in list(self._owned_tool_names):
            try:
                ability_manager.remove_ability(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] unregister %s failed: %s",
                    type(self).__name__,
                    name,
                    exc,
                )
        self._owned_tool_names.clear()

    # ------------------------------------------------------------------
    # Advance tool schema + handler (subclass may override schema)
    # ------------------------------------------------------------------
    def _advance_tool_description(self) -> str:
        rail_name = type(self).__name__
        tool = self.ADVANCE_TOOL
        domain = self._domain_description()
        init_next = (self.stages.get("init") or {}).get("next") or []
        first_stage = repr(init_next[0]) if init_next else "the first stage"
        return (
            f"Advance the {rail_name} state machine to the next stage. "
            f"MANDATORY: when {domain} mode is enabled, you MUST call this "
            f"tool before doing any work on a requirement — do NOT write code "
            f"or produce artifacts directly. Call {tool}(stage={first_stage}) "
            f"to start the flow; the system will then inject the stage's "
            f"methodology. Only after reaching 'done' may you implement."
        )

    def _advance_tool_input_params(self) -> dict:
        init_next = (self.stages.get("init") or {}).get("next") or []
        reset_hint = (
            repr(init_next[0]) if init_next
            else "a stage listed in init.next"
        )
        return {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Target stage name (must be a valid forward "
                    "next from the current stage, or " + reset_hint + " to "
                    "reset for a new flow when current is 'done').",
                },
                "feature_name": {
                    "type": "string",
                    "description": "Optional feature name for a new flow "
                    "(only used when resetting from 'done'); the system "
                    "creates the .aet/features/<name>/design/ dir for it.",
                },
            },
            "required": ["stage"],
        }

    def _handle_advance(self, kwargs: dict) -> dict:
        """Handle an advance tool call: validate + gate + transition.

        Returns a dict result to the LLM (never raises). The next
        ``before_model_call`` injects the new stage's skill.
        """
        try:
            target = kwargs.get("stage")
            if not isinstance(target, str) or target not in self.stages:
                return {"ok": False, "error": f"invalid stage: {target!r}"}

            current = self._stage or "init"

            # done -> reset: support multiple flows in one session.
            # If current is "done" and target is a valid init-next (e.g.
            # "analysis"), allow the reset (new flow). When feature_name is
            # provided, pre-create the feature dir so the new flow resolves
            # to it (otherwise latest-modified .aet/features/ subdir is used).
            if current == "done":
                init_next = (self.stages.get("init") or {}).get("next") or []
                if target in init_next:
                    feature_name = kwargs.get("feature_name")
                    if isinstance(feature_name, str) and feature_name.strip():
                        if not self._is_safe_feature_name(feature_name):
                            logger.warning(
                                "[%s] unsafe feature_name ignored "
                                "(must be a simple name, no path separators)",
                                type(self).__name__,
                            )
                        else:
                            self._ensure_feature_dir(feature_name)
                    self._stage = target
                    logger.info(
                        "[%s] done -> reset to %s (new flow)",
                        type(self).__name__,
                        target,
                    )
                    return {"ok": True, "from": current, "to": target,
                            "note": "new flow started"}
                return {"ok": False, "error": f"from done, only reset to "
                        f"init-next {init_next} is allowed (new flow)"}

            # Normal forward: target must be a valid next from current.
            valid_next = (self.stages.get(current) or {}).get("next") or []
            if target not in valid_next:
                return {"ok": False, "error": f"{target} is not a valid next "
                        f"from {current} (valid: {valid_next})"}

            # Artifacts gate: current stage's declared artifacts must exist.
            if not self._check_artifacts(current):
                feature = self._resolve_feature_name()
                declared = (self.stages.get(current) or {}).get("artifacts") or []
                if not feature:
                    return {
                        "ok": False,
                        "error": (
                            f"Cannot advance from '{current}' to '{target}': "
                            f"no feature directory found under .aet/features/. "
                            f"Ask the user for a feature name, create the "
                            f"directory via write_file, then produce the "
                            f"artifacts {declared} before calling "
                            f"{self.ADVANCE_TOOL} again."
                        ),
                    }
                design_dir = self._project_dir / ".aet" / "features" / feature / "design"
                missing = [
                    str(f) for f in declared
                    if not (design_dir / Path(str(f)).name).exists()
                ]
                return {
                    "ok": False,
                    "error": (
                        f"Cannot advance from '{current}' to '{target}': "
                        f"missing artifacts {missing}. "
                        f"Produce them at .aet/features/{feature}/design/ "
                        f"by following the '{current}' stage methodology, "
                        f"then call {self.ADVANCE_TOOL}(stage={target}) again."
                    ),
                }

            self._stage = target
            logger.info(
                "[%s] advance %s -> %s",
                type(self).__name__,
                current,
                target,
            )
            return {"ok": True, "from": current, "to": target}
        except Exception as exc:  # noqa: BLE001 — never crash the tool call
            logger.warning("[%s] _handle_advance failed: %s", type(self).__name__, exc)
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Shared helpers (subclasses use, don't override)
    # ------------------------------------------------------------------
    def _transition_to(self, stage: str) -> None:
        """Direct state transition (used by subclass after_tool_call, e.g.
        ask_user approve/reject handling).

        Validates ``stage in stages`` so typos in rework maps / config next
        don't silently set an invalid state (which would stall the machine:
        before_model_call returns early, _handle_advance rejects every target).
        """
        if stage not in self.stages:
            logger.warning(
                "[%s] _transition_to invalid stage=%r (not in stages); skip",
                type(self).__name__,
                stage,
            )
            return
        self._stage = stage

    def _current_stage(self) -> str:
        return self._stage or "init"

    def _check_artifacts(self, stage: str) -> bool:
        """Return True iff all stage-declared artifacts exist.

        Vacuous (no declared artifacts) -> True regardless of feature_name.
        Declared artifacts but feature_name unknown -> False.
        """
        declared = (self.stages.get(stage) or {}).get("artifacts") or []
        if not declared:
            return True
        feature = self._resolve_feature_name()
        if not feature:
            return False
        design_dir = self._project_dir / ".aet" / "features" / feature / "design"
        for filename in declared:
            if not (design_dir / Path(str(filename)).name).exists():
                return False
        return True

    def _resolve_feature_name(self) -> Optional[str]:
        """Return the most-recently-modified ``.aet/features/`` subdir name."""
        feature_root = self._project_dir / ".aet" / "features"
        try:
            if not feature_root.exists():
                return None
            subdirs = [
                p for p in feature_root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ]
        except OSError as exc:
            logger.warning(
                "[%s] resolve_feature_name scan failed for %s: %s",
                type(self).__name__,
                feature_root,
                exc,
            )
            return None
        if not subdirs:
            return None
        return max(subdirs, key=lambda p: p.stat().st_mtime).name

    def _ensure_feature_dir(self, feature_name: str) -> None:
        """Best-effort create ``.aet/features/<name>/design/`` for a new flow.

        Lets the LLM start a new flow for a specific feature in one
        ``sdd_advance(stage=..., feature_name=...)`` call (otherwise the
        LLM must create the dir via write_file before advancing).
        """
        design_dir = self._project_dir / ".aet" / "features" / feature_name / "design"
        try:
            design_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[%s] ensure_feature_dir failed for %s: %s",
                type(self).__name__,
                design_dir,
                exc,
            )

    @staticmethod
    def _is_safe_feature_name(name: str) -> bool:
        """Reject path-traversal components in user-supplied feature names."""
        if not name or not name.strip():
            return False
        n = name.strip()
        if "\x00" in n or "\n" in n or "\r" in n:
            return False
        if n in (".", ".."):
            return False
        if "/" in n or "\\" in n:
            return False
        if n.startswith("."):
            return False
        return True

    def _load_skill_methodology(self, stage: str) -> Optional[str]:
        """Return a self-contained methodology payload for ``stage``'s skill.

        Cached per-stage (SKILL.md is static — ship with the package, never
        changes at runtime), so the hot path ``before_model_call`` doesn't
        re-stat + re-read on every model call.

        Strips YAML front-matter (so the skill name isn't exposed to the LLM
        as a toolkit-loadable skill) and wraps the body in a frame that tells
        the LLM to follow the methodology inline (no skill toolkit call).
        """
        if stage in self._skill_cache:
            return self._skill_cache[stage]
        payload = self._build_skill_methodology(stage)
        self._skill_cache[stage] = payload
        return payload

    def _build_skill_methodology(self, stage: str) -> Optional[str]:
        """Load + frame the skill methodology for ``stage`` (uncached)."""
        stage_cfg = self.stages.get(stage) or {}
        skill_name = stage_cfg.get("skill")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return None
        skill_file = self._rail_pkg_dir / self.SKILLS_DIR / skill_name / "SKILL.md"
        try:
            if not skill_file.exists():
                return None
            raw = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[%s] load_skill_methodology read failed for %s: %s",
                type(self).__name__,
                skill_file,
                exc,
            )
            return None
        return self._build_methodology_payload(stage, stage_cfg, raw)

    def _build_methodology_payload(
        self, stage: str, stage_cfg: dict, raw_skill_md: str
    ) -> str:
        """Strip front-matter and wrap the body in a self-contained frame.

        Adapts the frame for **production** stages (have artifacts → produce a
        file) vs **review** stages (no artifacts → review the previous stage's
        file). Includes the actual next-stage name so the agent can call
        ``sdd_advance`` without guessing.
        """
        body = self._strip_front_matter(raw_skill_md)
        artifacts = stage_cfg.get("artifacts") or []
        has_artifacts = isinstance(artifacts, list) and len(artifacts) > 0
        next_stages = stage_cfg.get("next") or []
        next_stage = (
            next_stages[0] if isinstance(next_stages, list) and next_stages else None
        )
        stage_label = self._STAGE_LABELS.get(stage, stage)
        rail_name = type(self).__name__
        skill_name = stage_cfg.get("skill") or ""
        file_index = self._build_skill_file_index(skill_name)

        if has_artifacts:
            artifact_file = artifacts[0]
            stage_intro = (
                f"Output file path: .aet/features/<feature-name>/design/{artifact_file}\n"
                f"(<feature-name> is resolved from the latest-modified subdirectory "
                f"under .aet/features/; if none exists, ask the user for a feature "
                f"name and create the directory)\n\n"
            )
            advance_hint = "After producing the file, "
        else:
            prev_artifact = self._find_previous_stage_artifact(stage)
            review_target = prev_artifact or "<previous-stage-artifact>"
            stage_intro = (
                f"Review target: .aet/features/<feature-name>/design/{review_target}"
                f" (the file produced by the previous stage)\n"
                f"(<feature-name> is resolved from the latest-modified subdirectory "
                f"under .aet/features/)\n\n"
            )
            advance_hint = "After completing the review, "

        if next_stage:
            advance_instruction = (
                f"{advance_hint}call the `{self.ADVANCE_TOOL}` tool "
                f"(with stage={next_stage}) to advance to the next stage."
            )
        else:
            advance_instruction = (
                f"{advance_hint}call the `{self.ADVANCE_TOOL}` tool to advance the flow."
            )

        return (
            f"[{rail_name} Methodology — {stage_label} Stage]\n"
            f"You are currently in the \"{stage_label}\" stage of the {rail_name} flow. "
            f"Follow the methodology below to complete this stage.\n\n"
            f"{stage_intro}"
            f"IMPORTANT: Follow the methodology step by step. "
            f"Do NOT call skill toolkit (skill_retrieval / skill_index_build / "
            f"skill_toolkit, etc.) to load this methodology; it is not a registered "
            f"skill, and calling toolkit will return \"Skill not found\". "
            f"When the methodology references workflow/reference files, use "
            f"`read_file` with the absolute paths in the \"Skill File Index\" "
            f"section below. "
            f"{advance_instruction}\n\n"
            f"--- Methodology Body Start ---\n"
            f"{body}\n"
            f"--- Methodology Body End ---\n\n"
            f"{file_index}"
        )

    def _find_previous_stage_artifact(self, current: str) -> Optional[str]:
        """Return the artifact file from the stage that transitions to *current*.

        Review stages have no ``artifacts`` of their own — they review the
        file produced by the preceding production stage (found by scanning
        ``stages`` for a ``next`` entry containing *current*).
        """
        for _stage_name, stage_cfg in self.stages.items():
            next_list = stage_cfg.get("next") or []
            if current in next_list:
                prev_artifacts = stage_cfg.get("artifacts") or []
                if isinstance(prev_artifacts, list) and prev_artifacts:
                    return prev_artifacts[0]
        return None

    # ------------------------------------------------------------------
    # Skill file index (absolute paths only — no content inlining)
    # ------------------------------------------------------------------
    _DO_NOT_READ_DIRS = ("_templates",)
    _INDEXABLE_SUBDIRS = ("workflows", "references", "scripts")
    _INDEXABLE_EXTS = (".md", ".mjs")

    def _build_skill_file_index(self, skill_name: str) -> str:
        """Build a file-path index listing skill-package files with their
        absolute paths.

        The agent uses ``read_file`` with these paths to load workflow/reference
        content on demand — this avoids inlining large content into the system
        prompt (which the agent tends to ignore) and keeps the payload small.

        Absolute paths are valid in all installation modes (source, wheel,
        PyInstaller onedir/onefile) because the data files are collected by
        the build spec and placed on disk at runtime.
        """
        if not skill_name:
            return ""
        skill_dir = self._rail_pkg_dir / self.SKILLS_DIR / skill_name
        if not skill_dir.is_dir():
            return ""
        entries: list[str] = []
        for subdir in self._INDEXABLE_SUBDIRS:
            base = skill_dir / subdir
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if any(part in self._DO_NOT_READ_DIRS for part in path.parts):
                    continue
                if path.suffix not in self._INDEXABLE_EXTS:
                    continue
                rel = path.relative_to(skill_dir).as_posix()
                entries.append(f"  {rel}  →  {path}")
        if not entries:
            return ""
        return (
            "--- Skill File Index ---\n"
            "When the methodology references workflow/reference files, "
            "use `read_file` with the corresponding absolute path below to "
            "load the content:\n\n"
            + "\n".join(entries)
            + "\n\n"
            "Notes:\n"
            "- scripts/*.mjs are for subagent execution via `node` — do NOT "
            "read them yourself; pass the absolute path to a subagent.\n"
            "- scripts/_templates/ is an internal directory, excluded from "
            "this index — do NOT read it.\n"
            "--- End of File Index ---\n\n"
        )

    @staticmethod
    def _strip_front_matter(md: str) -> str:
        """Remove a leading YAML front-matter block (``---\\n...\\n---``)."""
        if not md.startswith("---"):
            return md
        end = md.find("\n---", 3)
        if end == -1:
            return md
        rest = md[end + 4:]
        if rest.startswith("\n"):
            rest = rest[1:]
        return rest.lstrip("\n")

    # ------------------------------------------------------------------
    # Bootstrap (for stages without a skill, e.g. init)
    # ------------------------------------------------------------------
    def _bootstrap_payload(self, stage: str) -> Optional[str]:
        """Generate a bootstrap prompt for stages without a skill methodology.

        ``init`` has no skill (it's the flow entry point) — without a
        bootstrap, the LLM wouldn't know about the ``ADVANCE_TOOL`` and
        couldn't start the flow. ``done`` returns ``None`` (hand back to
        plain agent mode — no injection).

        The bootstrap is DIRECTIVE: when this rail is mounted, the LLM MUST
        call the advance tool before doing any work on a requirement — it
        must not skip the flow and code directly.
        """
        if stage == "done":
            return None  # done — plain agent mode, no injection
        rail_name = type(self).__name__
        stage_cfg = self.stages.get(stage) or {}
        next_stages = stage_cfg.get("next") or []
        next_hint = ""
        if isinstance(next_stages, list) and next_stages:
            next_list = ", ".join(repr(s) for s in next_stages)
            next_hint = f" Valid next stages: {next_list}."
        stage_label = self._STAGE_LABELS.get(stage, stage)
        return (
            f"[{rail_name} — {stage_label} Stage (SDD mode enabled)]\n"
            f"You are currently in the \"{stage_label}\" stage of the "
            f"{rail_name} ({self._domain_description()}) flow.{next_hint}\n\n"
            f"**IMPORTANT (MANDATORY)**: SDD mode is enabled. For any requirement "
            f"implementation / analysis / design task the user raises, you MUST "
            f"first call the `{self.ADVANCE_TOOL}` tool to enter the SDD flow and "
            f"produce specification documents (Requirements Analysis Spec, "
            f"Requirements Design Spec, etc.) stage by stage. You must NOT skip "
            f"the SDD flow and write code directly. Only after the SDD flow "
            f"reaches the done stage may you begin implementation.\n\n"
            f"Now, call `{self.ADVANCE_TOOL}(stage="
            f"{repr(next_stages[0]) if next_stages else 'next_stage'})` "
            f"to start the SDD flow. After the call, the system will inject the "
            f"methodology for that stage — then follow it to produce the deliverable.\n"
        )

    def _domain_description(self) -> str:
        """Human-readable domain hint for the bootstrap (subclass overrides)."""
        return "Spec-Driven Development"
