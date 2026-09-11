# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashGlobTool — Enhanced GlobTool with mtime sorting, gitignore, and exclude_patterns.

Subclass of stock ``GlobTool``:
- Results sorted by modification time (newest first)
- .gitignore patterns from search root applied as exclusions
- ``exclude_patterns`` parameter exposed in schema for user-specified exclusions
- Improved description with use-case guidance

The extension patches ``openjiuwen.harness.tools.filesystem.GlobTool`` so that
SlimSysOperationRail / SysOperationRail pick up this subclass in ``init()``.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import time
from typing import Any, Dict, List, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.filesystem import GlobTool

logger = logging.getLogger(__name__)

# ── tool description (injected as the tool-level system prompt) ──

FLASH_GLOB_DESCRIPTION: Dict[str, str] = {
    "cn": (
        "在工作目录内，使用 glob 通配符模式批量查找匹配文件路径，"
        "用于文件发现、批量定位源码/配置文件。"
        "支持 .gitignore 规则过滤，结果按文件修改时间降序排序（最新优先）。"
        "工具被限制在工作空间沙箱内，无法访问工作区外文件，属于只读文件操作工具。"
    ),
    "en": (
        "Search for file paths matching glob patterns within the working directory. "
        "Used for file discovery and batch-locating source/config files. "
        "Supports .gitignore rule filtering; results sorted by modification time (newest first). "
        "Restricted to the workspace sandbox; cannot access files outside the workspace. "
        "Read-only file operation."
    ),
}


def get_flash_glob_input_params(language: str = "cn") -> Dict[str, Any]:
    if language == "en":
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. *.py, **/*.js)",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Directory to search. Defaults to the current working "
                        "directory when omitted"
                    ),
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Additional patterns to exclude from results "
                        "(e.g. ['*.pyc', 'node_modules/**']), "
                        "stacked on top of .gitignore rules"
                    ),
                },
            },
            "required": ["pattern"],
        }
    return {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式（如 *.py, **/*.js）",
            },
            "path": {
                "type": "string",
                "description": "搜索目录，省略时默认当前工作目录",
            },
            "exclude_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "额外排除模式列表（如 ['*.pyc', 'node_modules/**']），"
                    "与 .gitignore 规则叠加过滤"
                ),
            },
        },
        "required": ["pattern"],
    }


# ── gitignore helpers ──

def _load_gitignore_patterns(root: str) -> List[str]:
    """Read .gitignore patterns from *root* directory.

    Only reads the top-level ``.gitignore`` in *root*.  Nested ``.gitignore``
    files in subdirectories are not followed — this covers the common case
    where the project root holds the primary ignore rules.
    """
    gitignore_path = pathlib.Path(root) / ".gitignore"
    if not gitignore_path.is_file():
        return []
    try:
        text = gitignore_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.debug("[FlashGlob] failed to read .gitignore at %s: %s", root, exc)
        return []

    patterns: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def _gitignore_to_regex(pattern: str) -> str:
    """Convert a single gitignore-style pattern to a regex string.

    Handles the most common cases:
    - ``*``  → matches anything except ``/``
    - ``**`` → matches anything including ``/``
    - ``?``  → matches a single char except ``/``
    - trailing ``/`` → directory-only (stripped, matches prefix)
    - leading ``/`` → anchored to root (stripped, treated as anchored)
    - no ``/`` in pattern → matches at any depth (basename match)
    """
    p = pattern

    # Trailing / means directory-only; strip and treat as prefix match
    is_dir_only = p.endswith("/")
    if is_dir_only:
        p = p[:-1]

    # Leading / means anchored to root
    anchored = p.startswith("/")
    if anchored:
        p = p[1:]

    # Build regex
    i = 0
    parts: List[str] = []
    while i < len(p):
        c = p[i]
        if c == "*":
            if i + 1 < len(p) and p[i + 1] == "*":
                # ** — match everything including /
                parts.append(".*")
                i += 2
                # Skip optional trailing /
                if i < len(p) and p[i] == "/":
                    i += 1
            else:
                # * — match anything except /
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c in r".+()[]{}^$|\\":
            parts.append("\\" + c)
            i += 1
        else:
            parts.append(c)
            i += 1

    regex_body = "".join(parts)

    if anchored:
        # Anchored to root
        if is_dir_only:
            return f"^{regex_body}(/.*)?$"
        return f"^{regex_body}$"
    else:
        # Match at any depth — try both full-path and basename
        if is_dir_only:
            return f"(^|/){regex_body}(/.*)?$"
        return f"(^|/){regex_body}$"


def _matches_gitignore(rel_path: str, patterns: List[str]) -> bool:
    """Check if *rel_path* matches any gitignore pattern.

    *rel_path* should be a forward-slash-separated relative path from the
    search root.
    """
    rel_path = rel_path.replace(os.sep, "/")
    basename = rel_path.rsplit("/", 1)[-1]

    for raw_pattern in patterns:
        if raw_pattern.startswith("!"):
            # Negation — skip for simplicity (rare in basic .gitignore)
            continue

        regex = _gitignore_to_regex(raw_pattern)
        try:
            if re.fullmatch(regex, rel_path):
                return True
        except re.error:
            pass

        # For non-anchored, non-slash patterns, also match basename
        if "/" not in raw_pattern and not raw_pattern.startswith("/"):
            try:
                if re.fullmatch(regex, basename):
                    return True
            except re.error:
                pass

    return False


def _relative_path(file_path: str, base: str) -> str:
    """Return a relative path string, tolerating symlink resolution diffs."""
    try:
        return os.path.relpath(file_path, base)
    except ValueError:
        return file_path


# ── FlashGlobTool ──

class FlashGlobTool(GlobTool):
    """Enhanced GlobTool: mtime sorting, gitignore filtering, exclude_patterns."""

    def __init__(
        self,
        operation: Any,
        language: str = "cn",
        agent_id: Optional[str] = None,
    ) -> None:
        super().__init__(operation, language, agent_id)
        lang = language or "cn"
        self.card.description = FLASH_GLOB_DESCRIPTION.get(
            lang, FLASH_GLOB_DESCRIPTION["cn"]
        )
        self.card.input_params = get_flash_glob_input_params(lang)

    async def invoke(
        self, inputs: Dict[str, Any], **kwargs
    ) -> ToolOutput:
        pattern = inputs.get("pattern")
        if not pattern:
            return ToolOutput(success=False, error="pattern is required")

        try:
            path = self._resolve_search_path(inputs.get("path"))
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        exclude_patterns = inputs.get("exclude_patterns") or []
        if not isinstance(exclude_patterns, list):
            exclude_patterns = [exclude_patterns]
        exclude_patterns = [str(p) for p in exclude_patterns if p]

        started_at = time.perf_counter()

        # 1. Search (reuse parent's brace expansion + search_files)
        expanded_patterns = self._expand_brace_pattern(pattern)
        all_items: List[Any] = []
        seen: set = set()

        for pat in expanded_patterns:
            res = await self.operation.fs().search_files(path, pat)
            if res.code != StatusCode.SUCCESS.code:
                return ToolOutput(success=False, error=res.message)
            if res.data:
                for item in res.data.matching_files:
                    if item.path not in seen:
                        seen.add(item.path)
                        all_items.append(item)

        # 2. Apply .gitignore filtering
        gitignore_patterns = _load_gitignore_patterns(path)
        gitignore_applied = False
        if gitignore_patterns:
            before_count = len(all_items)
            filtered: List[Any] = []
            for item in all_items:
                rel = _relative_path(item.path, path)
                if not _matches_gitignore(rel, gitignore_patterns):
                    filtered.append(item)
            all_items = filtered
            gitignore_applied = len(all_items) < before_count
            if gitignore_applied:
                logger.debug(
                    "[FlashGlob] gitignore filtered %d -> %d files",
                    before_count,
                    len(all_items),
                )

        # 3. Apply user-specified exclude_patterns
        if exclude_patterns:
            before_count = len(all_items)
            filtered2: List[Any] = []
            for item in all_items:
                rel = _relative_path(item.path, path)
                if not _matches_gitignore(rel, exclude_patterns):
                    filtered2.append(item)
            all_items = filtered2
            logger.debug(
                "[FlashGlob] exclude_patterns filtered %d -> %d files",
                before_count,
                len(all_items),
            )

        # 4. Sort by modified_time (newest first)
        all_items.sort(
            key=lambda item: str(getattr(item, "modified_time", "") or ""),
            reverse=True,
        )

        # 5. Truncate
        truncated = len(all_items) > self.DEFAULT_MAX_RESULTS
        limited_items = all_items[: self.DEFAULT_MAX_RESULTS]

        # 6. Build result (compatible with stock GlobTool output format)
        filenames = self._relativize_paths(
            [item.path for item in limited_items], path
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)

        return ToolOutput(
            success=True,
            data={
                "durationMs": duration_ms,
                "numFiles": len(filenames),
                "filenames": filenames,
                "truncated": truncated,
                "matching_files": [item.path for item in limited_items],
                "count": len(filenames),
                "sorted_by": "modified_time_desc",
                "gitignore_filtered": gitignore_applied,
            },
        )

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> Any:
        pass


__all__ = ["FlashGlobTool"]
