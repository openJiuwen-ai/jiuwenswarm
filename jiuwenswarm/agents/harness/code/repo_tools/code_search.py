# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``code_search`` — ranked, grouped code search for Code mode.

What this module does
    Exposes one model-facing tool, ``code_search``, that runs ripgrep (or a
    pure-Python fallback) inside the workspace root and returns matches grouped
    by file, ranked by relevance, each with a few lines of surrounding context.
    The result is capped so one call costs a bounded number of tokens.

Why it exists
    In the SWE-bench Pro pilot the model spent 43 % of its tool calls in
    ``bash`` and only 7 % in ``grep``; most bash calls were hand-written
    ``grep -rn … | head`` pipelines whose raw output was kept verbatim in the
    context. ``code_search`` gives the model one call that does the
    search-group-rank-trim work and returns a compact, line-numbered view.

Who calls it
    ``JiuwenSwarmCodeAdapter`` builds it from ``modes.code.tools``
    (``_build_code_search_tool``); the swarm code profile builds it through
    ``swarm.repo_tools``. ``find_symbol`` reuses the search engine directly.

What it does not do
    It never modifies files, never leaves the workspace root and never reads
    git history or the network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from openjiuwen.core.common.logging import tool_logger as logger
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
from openjiuwen.core.foundation.tool.schema import ToolOutput

from jiuwenswarm.agents.harness.code.repo_tools._common import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CONTEXT_LINES,
    MAX_RESULTS_CEILING,
    FileHits,
    RootResolver,
    as_bool,
    as_int,
    build_rg_args,
    contain_path,
    extensions_for_language,
    find_rg_binary,
    mark_rg_unusable,
    normalize_globs,
    normalize_ignored_dirs,
    normalize_language,
    python_search_bounded,
    rank_files,
    render_hits,
    resolve_root,
    rg_type_for_language,
    rg_usable,
    run_rg_json,
    truncate_hits,
)
from jiuwenswarm.agents.harness.code.repo_tools.read_policy import (
    ReadPolicy,
    ReadPolicyFactory,
    read_policy_notice,
    resolve_read_policy,
)

TOOL_NAME = "code_search"
DEFAULT_MAX_RESULTS = 40
DEFAULT_CONTEXT_LINES = 3
_MAX_COUNT_PER_FILE = 50
_CASE_MODES = ("smart", "sensitive", "insensitive")

_DESCRIPTION_EN = (
    "Search the code of the current workspace and get a ranked, compact view: "
    "matches grouped by file, files with more matches first (source before tests "
    "and vendored code), each match with a few lines of context and line numbers. "
    "Use this before read_file to locate the exact place to look at, then read only "
    "that range. Prefer it over bash grep/rg pipelines: it already respects .gitignore, "
    "skips build/vendor directories, trims long lines and caps the output. "
    "Pattern is a regex by default (set literal=true for plain text). "
    "Narrow with path (a sub-directory or file), glob (e.g. '*.py' or '!**/tests/**'), "
    "or language (python, typescript, go, rust, java, ...; short forms like py/ts/js work). "
    "Never use git history or the network to find an answer; search the working tree."
)

_DESCRIPTION_CN = (
    "在当前工作区代码中搜索，并返回排好序的紧凑结果：按文件分组，匹配多的文件在前"
    "（源码优先于测试与第三方目录），每个匹配带若干行上下文与行号。"
    "在 read_file 之前先用它定位，再只读取需要的行范围。"
    "优先用它替代 bash 中的 grep/rg 管道：它已遵守 .gitignore、跳过构建/第三方目录、"
    "截断长行并限制输出量。pattern 默认为正则（literal=true 时按纯文本匹配）。"
    "可用 path（子目录或文件）、glob（如 '*.py' 或 '!**/tests/**'）、language"
    "（python、typescript、go、rust、java 等）缩小范围。"
    "不要通过 git 历史或网络寻找答案；只搜索工作树。"
)


def code_search_input_params() -> dict[str, Any]:
    """JSON schema of the ``code_search`` arguments."""
    return {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex (default) or literal text to search for.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional sub-directory or file, relative to the workspace root. "
                    "Defaults to the whole workspace."
                ),
            },
            "glob": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "Optional file glob(s), ripgrep style; prefix with '!' to exclude "
                    "(e.g. '*.py', 'src/**/*.ts', '!**/tests/**')."
                ),
            },
            "language": {
                "type": "string",
                "description": (
                    "Optional language filter: python, javascript, typescript, go, "
                    "rust, java, kotlin, csharp, c, cpp, ruby, php, swift, scala, "
                    "shell, markdown, yaml, toml, json."
                ),
            },
            "literal": {
                "type": "boolean",
                "description": "Treat pattern as plain text instead of a regex. Default false.",
            },
            "case": {
                "type": "string",
                "enum": list(_CASE_MODES),
                "description": (
                    "smart (default: case-insensitive unless the pattern has uppercase), "
                    "sensitive, or insensitive."
                ),
            },
            "context": {
                "type": "integer",
                "description": (
                    f"Lines of context around each match, 0–{MAX_CONTEXT_LINES}. "
                    f"Default {DEFAULT_CONTEXT_LINES}."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": (
                    f"Maximum matches to return across all files, 1–{MAX_RESULTS_CEILING}. "
                    f"Default {DEFAULT_MAX_RESULTS}."
                ),
            },
        },
        "required": ["pattern"],
    }


def build_code_search_card(
    *,
    agent_id: str | None = None,
    language: str = "en",
) -> ToolCard:
    """Build the ``code_search`` card with DIRECT exposure declared explicitly.

    agent-core's progressive tool policy defers every tool that is not on its
    built-in direct list; declaring ``exposure`` on the card is the documented
    way for a host tool to stay directly callable.
    """
    description = _DESCRIPTION_CN if language == "cn" else _DESCRIPTION_EN
    tool_id = f"{TOOL_NAME}_{agent_id}" if agent_id else TOOL_NAME
    return ToolCard(
        id=tool_id,
        name=TOOL_NAME,
        description=description,
        input_params=code_search_input_params(),
        exposure=ToolExposure.DIRECT,
        parallel_safe=True,
        idempotent=True,
    )


class CodeSearchEngine:
    """Search backend shared by ``code_search`` and ``find_symbol``.

    Picks ripgrep when available (``rg_path`` override → SDK bundled binary →
    ``PATH``) and falls back to a pure-Python walk otherwise. Both paths return
    the same ``FileHits`` shape, so callers never branch on the engine.

    A ripgrep that cannot start, or dies instead of exiting 0/1/2 (for example
    a quarantined binary macOS kills on launch), is set aside for
    ``RG_RETRY_AFTER_SECONDS`` and the call is answered by the built-in search
    with a one-line note. The built-in search runs in a worker thread under
    the same time budget, so it never stalls the event loop, and it reports
    when a file or time limit cut it short.
    """

    def __init__(
        self,
        root_resolver: RootResolver | None = None,
        *,
        rg_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        ignored_dirs: object = None,
        read_policy_factory: ReadPolicyFactory | None = None,
    ) -> None:
        self._root_resolver = root_resolver
        self._rg_path_override = rg_path
        self._timeout = timeout
        self._ignored_dirs = normalize_ignored_dirs(ignored_dirs)
        self._read_policy_factory = read_policy_factory

    def read_policy(self, root: Path | None = None) -> ReadPolicy:
        """The read policy for ``root`` (default: the current root); never raises."""
        return resolve_read_policy(self._read_policy_factory, root or self.root)

    @property
    def root(self) -> Path:
        return resolve_root(self._root_resolver)

    @property
    def ignored_dirs(self) -> frozenset[str]:
        """Directory names skipped at every depth (``modes.code.repo_tools.ignored_dirs``)."""
        return self._ignored_dirs

    def rg_binary(self) -> str | None:
        """The ripgrep to use, or ``None`` (none found, or set aside after a failure)."""
        path = find_rg_binary(self._rg_path_override)
        if path and not rg_usable(path):
            return None
        return path

    async def search(
        self,
        *,
        pattern: str,
        path: str | None = None,
        globs: Sequence[str] = (),
        language: str | None = None,
        literal: bool = False,
        case: str = "smart",
        context: int = DEFAULT_CONTEXT_LINES,
        max_count_per_file: int = _MAX_COUNT_PER_FILE,
    ) -> tuple[list[FileHits], str, bool, str]:
        """Return ``(ranked_hits, engine_name, timed_out, warning)`` for ``pattern``.

        ``warning`` is an empty string or a one-line note (e.g. ripgrep could
        not read some paths but still returned matches, or a read policy is
        active: then a fixed notice is always added, whether or not anything
        was filtered, so the result cannot reveal what a denied file contains).
        Raises ``ValueError`` for a bad path, a path the
        read policy hides, an invalid regex (both engines, same wording) or a
        ripgrep error that produced no matches.
        """
        root = self.root
        search_path = contain_path(root, path)
        policy = self.read_policy(root)
        if policy.refusal is not None:
            raise ValueError(policy.refusal)
        if search_path != root:
            reason = (
                policy.dir_hidden_reason(search_path)
                if search_path.is_dir()
                else policy.hidden_reason(search_path)
            )
            if reason:
                raise ValueError(f"path is not readable under the read policy: {path} ({reason})")
        hits, engine, timed_out, warning = await self._search_unfiltered(
            root,
            search_path,
            pattern=pattern,
            globs=globs,
            language=language,
            literal=literal,
            case=case,
            context=context,
            max_count_per_file=max_count_per_file,
        )
        note = read_policy_notice(policy)
        if note:
            hits = [f for f in hits if policy.allows(root / f.path)]
            warning = f"{warning}; {note}" if warning else note
        return hits, engine, timed_out, warning

    async def _search_unfiltered(
        self,
        root: Path,
        search_path: Path,
        *,
        pattern: str,
        globs: Sequence[str],
        language: str | None,
        literal: bool,
        case: str,
        context: int,
        max_count_per_file: int,
    ) -> tuple[list[FileHits], str, bool, str]:
        rg_bin = self.rg_binary()
        fallback_note = ""
        if rg_bin:
            args = build_rg_args(
                pattern=pattern,
                literal=literal,
                case=case,
                context=context,
                globs=globs,
                file_type=rg_type_for_language(language),
                max_count_per_file=max_count_per_file,
                extra_ignored_dirs=self._ignored_dirs,
            )
            try:
                result = await run_rg_json(rg_bin, args, search_path, timeout=self._timeout)
            except OSError as exc:  # missing, not executable, wrong format, ...
                mark_rg_unusable(rg_bin, f"could not start: {exc.__class__.__name__}: {exc}")
                fallback_note = "ripgrep could not start; used the built-in search"
            else:
                if result.failed and result.is_pattern_error:
                    raise ValueError(f"invalid pattern: {result.error}")
                if result.crashed:
                    mark_rg_unusable(rg_bin, result.describe_failure())
                    fallback_note = "ripgrep crashed; used the built-in search"
                elif result.failed and not result.hits:
                    fallback_note = (
                        f"ripgrep failed ({result.error or 'exit code 2'}); used the built-in search"
                    )
                else:
                    warning = ""
                    if result.failed and result.hits:
                        warning = f"ripgrep reported an error for some paths: {result.error}"
                    return rank_files(root, result.hits), "ripgrep", result.timed_out, warning
        try:
            scan = await asyncio.to_thread(
                python_search_bounded,
                root,
                search_path,
                pattern=pattern,
                literal=literal,
                case=case,
                context=context,
                globs=globs,
                extensions=(extensions_for_language(language) or None) if language else None,
                max_count_per_file=max_count_per_file,
                ignored_dirs=self._ignored_dirs,
                deadline=time.monotonic() + self._timeout,
            )
        except Exception as exc:  # re.error and friends
            raise ValueError(f"invalid pattern: {exc}") from exc
        warning = "; ".join(note for note in (fallback_note, scan.warning) if note)
        return rank_files(root, scan.hits), "python", scan.stopped == "time_limit", warning


class CodeSearchTool(Tool):
    """Model-facing ``code_search`` tool (see module docstring)."""

    def __init__(
        self,
        root_resolver: RootResolver | None = None,
        *,
        agent_id: str | None = None,
        language: str = "en",
        default_max_results: int = DEFAULT_MAX_RESULTS,
        default_context: int = DEFAULT_CONTEXT_LINES,
        rg_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        ignored_dirs: object = None,
        read_policy_factory: ReadPolicyFactory | None = None,
    ) -> None:
        super().__init__(build_code_search_card(agent_id=agent_id, language=language))
        self._engine = CodeSearchEngine(
            root_resolver,
            rg_path=rg_path,
            timeout=timeout,
            ignored_dirs=ignored_dirs,
            read_policy_factory=read_policy_factory,
        )
        self._default_max_results = as_int(
            default_max_results, DEFAULT_MAX_RESULTS, low=1, high=MAX_RESULTS_CEILING
        )
        self._default_context = as_int(
            default_context, DEFAULT_CONTEXT_LINES, low=0, high=MAX_CONTEXT_LINES
        )

    @property
    def engine(self) -> CodeSearchEngine:
        return self._engine

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        pattern = str(inputs.get("pattern") or "").strip()
        if not pattern:
            return ToolOutput(success=False, error="pattern is required")
        case = str(inputs.get("case") or "smart").strip().lower()
        if case not in _CASE_MODES:
            return ToolOutput(
                success=False,
                error=f"case must be one of {', '.join(_CASE_MODES)}",
            )
        try:
            language = normalize_language(inputs.get("language"))
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        globs = normalize_globs(inputs.get("glob"))
        literal = as_bool(inputs.get("literal"), False)
        context = as_int(inputs.get("context"), self._default_context, low=0, high=MAX_CONTEXT_LINES)
        max_results = as_int(
            inputs.get("max_results"), self._default_max_results, low=1, high=MAX_RESULTS_CEILING
        )
        try:
            ranked, engine, timed_out, warning = await self._engine.search(
                pattern=pattern,
                path=inputs.get("path"),
                globs=globs,
                language=language,
                literal=literal,
                case=case,
                context=context,
            )
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        except FileNotFoundError as exc:
            return ToolOutput(success=False, error=f"search binary missing: {exc}")
        except Exception as exc:  # noqa: BLE001 - never fail the turn on a search hiccup
            logger.warning("[code_search] unexpected failure: %s", exc)
            return ToolOutput(success=False, error=f"code_search failed: {exc}")

        kept, total, truncated = truncate_hits(ranked, max_results)
        total_files = len(ranked)
        header = (
            f"{total} match{'es' if total != 1 else ''} in {total_files} "
            f"file{'s' if total_files != 1 else ''}"
        )
        if truncated:
            header += (
                f"; showing the top {sum(f.count for f in kept)} "
                f"(raise max_results, or narrow with path/glob/language)"
            )
        if timed_out:
            header += "; search timed out, results are partial"
        if warning:
            header += f"; {warning}"
        if kept:
            body = render_hits(kept)
        else:
            body = "No matches. Try a broader pattern, literal=true, or a different path."
        content = f"{header}\n{body}"
        data = {
            "content": content,
            "pattern": pattern,
            "engine": engine,
            "total_matches": total,
            "total_files": total_files,
            "shown_matches": sum(f.count for f in kept),
            "truncated": truncated,
            "timed_out": timed_out,
            "warning": warning,
            "files": [
                {
                    "path": item.path,
                    "matches": [{"line": h.line, "text": h.text} for h in item.hits],
                }
                for item in kept
            ],
        }
        return ToolOutput(success=True, data=data)

    def render_for_llm(self, output: Any) -> str:
        if isinstance(output, ToolOutput):
            if output.success and isinstance(output.data, dict):
                return str(output.data.get("content") or "")
            return output.error or "code_search failed"
        return str(output)

    async def stream(self, inputs: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        """Streaming is not meaningful for a search; ``invoke`` is the API."""
        yield await self.invoke(inputs, **kwargs)


__all__ = [
    "DEFAULT_CONTEXT_LINES",
    "DEFAULT_MAX_RESULTS",
    "TOOL_NAME",
    "CodeSearchEngine",
    "CodeSearchTool",
    "build_code_search_card",
    "code_search_input_params",
]
