"""ResearchPipelineRail — stage-aware guard + journal for research pipelines.

A FARS-style research pipeline (ideation -> planning -> experiment ->
writing) is driven by an external orchestrator that writes a `stage.json`
marker (``{"stage": N, "name": "..."}``) into the session's project
directory before each stage prompt. When that marker exists this rail:

1. **Guards file writes** (``write_file``/``edit_file``/``write``): during a
   pipeline run, file mutations outside the session project directory are
   blocked, keeping all research artifacts in one auditable location.
2. **Journals execution** into ``pipeline_trace.jsonl`` in the same
   directory: one record per tool call with the active stage, tool name,
   path and outcome. The trace is the raw material for reproducible
   resource reports (token/time accounting per stage).

When no ``stage.json`` is present the rail is inert, so registering it
globally has zero impact on non-pipeline sessions.

Blocking convention follows ``memory_forbidden_rail.py``: set
``ctx.extra["_skip_tool"] = True`` and provide ``ctx.inputs["tool_result"]``
/ ``ctx.inputs["tool_msg"]``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

_GUARDED_TOOLS = {"write_file", "edit_file", "write", "write_text_file", "apply_patch"}


def _tool_call_data(ctx: AgentCallbackContext) -> tuple[str, dict[str, Any]]:
    """tool_name/tool_args with dict-or-attribute inputs (template pattern)."""
    inputs = ctx.inputs
    if isinstance(inputs, dict):
        tool_name = inputs.get("tool_name", "")
        raw_args = inputs.get("tool_args", {})
    else:
        tool_name = getattr(inputs, "tool_name", "")
        raw_args = getattr(inputs, "tool_args", {})
    if not isinstance(raw_args, dict):
        raw_args = {}
    return str(tool_name or "").lower(), raw_args


def _session_id_of(ctx: AgentCallbackContext) -> str | None:
    session = getattr(ctx, "session", None)
    if session is None:
        return None
    if isinstance(session, str):
        return session
    for attr in ("session_id", "id", "_session_id"):
        value = getattr(session, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


class ResearchPipelineRail(DeepAgentRail):
    """Stage-aware guard rail for headless research pipelines."""

    priority = 70

    def __init__(self) -> None:
        super().__init__()
        self._workspace: Path | None = None

    # ------------------------------------------------------------------ #
    # workspace wiring (mirrors DeepAgentRail.set_workspace)
    # ------------------------------------------------------------------ #
    def set_workspace(self, workspace: object | None) -> None:
        super().set_workspace(workspace)
        if workspace is None:
            return
        try:
            root = getattr(workspace, "root_path", workspace)
            projects_rel = "projects"
            if hasattr(workspace, "get_directory"):
                projects_rel = workspace.get_directory("projects") or projects_rel
            self._workspace = Path(root) / projects_rel
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[ResearchPipelineRail] workspace resolution failed: %s", exc
            )
            self._workspace = None

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _session_project_dir(self, ctx: AgentCallbackContext) -> Path | None:
        if self._workspace is None:
            return None
        session_id = _session_id_of(ctx)
        if not session_id:
            return None
        return self._workspace / session_id

    def _stage_marker(self, project_dir: Path) -> dict | None:
        marker = project_dir / "stage.json"
        if not marker.is_file():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ #
    # hooks
    # ------------------------------------------------------------------ #
    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Guard file mutations outside the pipeline project directory."""
        project_dir = self._session_project_dir(ctx)
        if project_dir is None:
            return
        stage = self._stage_marker(project_dir)
        if stage is None:
            return
        tool_name, args = _tool_call_data(ctx)
        if tool_name not in _GUARDED_TOOLS:
            return
        target = args.get("path") or args.get("file_path") or args.get("file")
        if not target:
            return
        target_path = Path(str(target))
        # lexical path first, so `resolved` is always bound even if resolve() fails
        lexical = (
            target_path if target_path.is_absolute() else project_dir / target_path
        )
        try:
            resolved = lexical.resolve()
        except (OSError, ValueError):
            resolved = lexical
        try:
            inside = resolved.is_relative_to(project_dir.resolve())
        except (OSError, ValueError):
            inside = str(resolved).startswith(str(project_dir.resolve()))
        if not inside:
            ctx.extra["_skip_tool"] = True
            inputs = ctx.inputs
            tool_call = (
                inputs.get("tool_call")
                if isinstance(inputs, dict)
                else getattr(inputs, "tool_call", None)
            )
            tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
            denial = (
                f"BLOCKED by ResearchPipelineRail: stage {stage.get('stage')} "
                f"({stage.get('name', '?')}) forbids writing outside the pipeline "
                f"project directory {project_dir}."
            )
            if isinstance(inputs, dict):
                inputs["tool_result"] = denial
                inputs["tool_msg"] = ToolMessage(
                    content=denial, tool_call_id=tool_call_id
                )
            else:
                inputs.tool_result = denial
                inputs.tool_msg = ToolMessage(content=denial, tool_call_id=tool_call_id)
            logger.info(
                "[ResearchPipelineRail] blocked %s on %s (stage %s)",
                tool_name,
                str(resolved),
                stage.get("stage"),
            )

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Append one journal record per tool call during a pipeline run."""
        project_dir = self._session_project_dir(ctx)
        if project_dir is None:
            return
        stage = self._stage_marker(project_dir)
        if stage is None:
            return
        tool_name, args = _tool_call_data(ctx)
        blocked = bool(ctx.extra.get("_skip_tool"))
        record = {
            "ts": time.time(),
            "stage": stage.get("stage"),
            "stage_name": stage.get("name"),
            "tool": tool_name,
            "path": args.get("path") or args.get("file_path") or args.get("file"),
            "outcome": "blocked" if blocked else "ok",
        }
        journal = project_dir / "pipeline_trace.jsonl"
        try:
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # journal must never break the session
            logger.warning("[ResearchPipelineRail] journal write failed: %s", exc)
