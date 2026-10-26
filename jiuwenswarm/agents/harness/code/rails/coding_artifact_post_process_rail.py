# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Code-mode artifact post-processing without task lifecycle ownership."""

from __future__ import annotations

import codecs
import re
import time
from pathlib import Path

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    ARTIFACT_DETECTION_TOOL_NAMES,
    detect_artifact_paths,
    filter_unhooked,
    fire_artifact_hook,
    get_current_task_id,
    mark_hooked,
    pop_tool_start_time,
    resolve_workspace_base,
)
from jiuwenswarm.common.utils import logger

_COAUTHOR_HEADER = "Co-authored by OfficeAce Coding Agent"
_PYTHON_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_HASH_COMMENT_SUFFIXES = frozenset({
    ".py", ".pyw", ".pyx", ".pxd", ".pxi",
    ".sh", ".bash", ".zsh", ".fish",
    ".rb", ".rake", ".pl", ".pm", ".r",
    ".ps1", ".psm1", ".psd1",
})
_SLASH_COMMENT_SUFFIXES = frozenset({
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".cs", ".java", ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".go", ".rs", ".swift", ".kt", ".kts",
    ".dart", ".scala", ".groovy",
})
_DASH_COMMENT_SUFFIXES = frozenset({".sql", ".lua"})
_BLOCK_COMMENT_SUFFIXES = frozenset({".css", ".scss", ".sass", ".less"})
_MARKUP_COMMENT_SUFFIXES = frozenset({".html", ".htm", ".xml", ".vue", ".svelte"})
_HASH_COMMENT_FILENAMES = frozenset({
    "dockerfile", "makefile", "rakefile", "jenkinsfile", "cmakelists.txt",
})


def _comment_text(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _HASH_COMMENT_SUFFIXES or path.name.lower() in _HASH_COMMENT_FILENAMES:
        return f"# {_COAUTHOR_HEADER}"
    if suffix in _SLASH_COMMENT_SUFFIXES:
        return f"// {_COAUTHOR_HEADER}"
    if suffix in _DASH_COMMENT_SUFFIXES:
        return f"-- {_COAUTHOR_HEADER}"
    if suffix in _BLOCK_COMMENT_SUFFIXES:
        return f"/* {_COAUTHOR_HEADER} */"
    if suffix in _MARKUP_COMMENT_SUFFIXES:
        return f"<!-- {_COAUTHOR_HEADER} -->"
    if suffix == ".php":
        return f"// {_COAUTHOR_HEADER}"
    return None


def _header_insert_index(path: Path, lines: list[str]) -> int:
    if not lines:
        return 0

    suffix = path.suffix.lower()
    index = 1 if lines[0].startswith("#!") else 0

    if suffix in {".py", ".pyw", ".pyx", ".pxd", ".pxi"}:
        for candidate in range(min(2, len(lines))):
            if _PYTHON_CODING_COOKIE.match(lines[candidate]):
                index = max(index, candidate + 1)
    elif suffix == ".php" and lines[0].lstrip().lower().startswith("<?php"):
        index = 1
    elif suffix in {".xml", ".html", ".htm", ".vue", ".svelte"}:
        if lines[0].lstrip().lower().startswith("<?xml"):
            index = 1

    return index


def add_officeace_coauthor_header(path: str | Path) -> bool:
    """Add the configured OfficeAce attribution header to one code file."""
    file_path = Path(path)
    comment = _comment_text(file_path)
    if comment is None or not file_path.is_file():
        return False

    try:
        raw = file_path.read_bytes()
        has_utf8_bom = raw.startswith(codecs.BOM_UTF8)
        source = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug(
            "[CodingArtifactPostProcessRail] skip coauthor header path=%s error=%s",
            file_path,
            exc,
        )
        return False

    if _COAUTHOR_HEADER.casefold() in source.casefold():
        return False

    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    index = _header_insert_index(file_path, lines)
    prefix = "".join(lines[:index])
    suffix = "".join(lines[index:])
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    updated = f"{prefix}{comment}{newline}{suffix}"
    encoded = updated.encode("utf-8")
    if has_utf8_bom:
        encoded = codecs.BOM_UTF8 + encoded

    try:
        file_path.write_bytes(encoded)
    except OSError as exc:
        logger.warning(
            "[CodingArtifactPostProcessRail] coauthor header write failed path=%s error=%s",
            file_path,
            exc,
        )
        return False
    return True


class CodingArtifactPostProcessRail(DeepAgentRail):
    """Detect code-mode artifacts and run extension post-process hooks.

    This rail deliberately owns no todo or task-plan state. It only reuses the
    canonical artifact detection and hook helpers shared with TaskExecutionRail
    and SkillTurboArtifactRail.
    """

    priority = 85
    # The rail keeps per-invoke mutable detection state. A child agent must use
    # its own instance instead of resetting or mutating the parent's state.
    inherit_to_subagents = False

    def __init__(self, *, coauthor_header_enabled: bool = False) -> None:
        super().__init__()
        self._coauthor_header_enabled = bool(coauthor_header_enabled)
        self._tool_start_times: dict[str, float] = {}
        self._hooked_artifacts: set[tuple[str, int, int]] = set()

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        """Reset per-invoke tool timing while retaining cross-turn deduplication."""
        self._tool_start_times = {}

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        if ctx.inputs.tool_name not in ARTIFACT_DETECTION_TOOL_NAMES:
            return

        tool_call = getattr(ctx.inputs, "tool_call", None)
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if tool_call_id:
            self._tool_start_times[tool_call_id] = time.time()

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None or not isinstance(ctx.inputs, ToolCallInputs):
            return
        if ctx.inputs.tool_name not in ARTIFACT_DETECTION_TOOL_NAMES:
            return

        try:
            session_id = str(ctx.session.get_session_id() or "")
        except Exception:
            logger.debug(
                "[CodingArtifactPostProcessRail] failed to get session_id",
                exc_info=True,
            )
            return

        detection = detect_artifact_paths(
            ctx.inputs.tool_name,
            getattr(ctx.inputs, "tool_args", None),
            getattr(ctx.inputs, "tool_result", None),
            tool_start_time=pop_tool_start_time(self._tool_start_times, ctx),
            workspace_base=resolve_workspace_base(),
        )
        paths = filter_unhooked(detection.paths, self._hooked_artifacts)
        if not paths:
            return

        if self._coauthor_header_enabled:
            for path in paths:
                if add_officeace_coauthor_header(path):
                    logger.info(
                        "[CodingArtifactPostProcessRail] added coauthor header path=%s",
                        path,
                    )

        fired = await fire_artifact_hook(
            session_id=session_id,
            tool_name=detection.tool_name,
            task_id=get_current_task_id(),
            artifact_paths=paths,
            log_prefix="[CodingArtifactPostProcessRail]",
        )
        if fired:
            mark_hooked(paths, self._hooked_artifacts)


__all__ = ["CodingArtifactPostProcessRail", "add_officeace_coauthor_header"]
