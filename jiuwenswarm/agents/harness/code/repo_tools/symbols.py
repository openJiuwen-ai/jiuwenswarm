# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``find_symbol`` and ``symbol_outline`` — explicit symbol navigation for Code mode.

What this module does
    Two model-facing tools that answer "where is X defined / used" and "what is
    in this file" without the model hand-writing grep pipelines:

    * ``find_symbol(name, kind=definition|references|all, file_path?, line?)``
    * ``symbol_outline(file_path)``

    Both prefer the Language Server Protocol when the LSP subsystem that
    ``LspRail`` initialises has a running server for the file type, and fall
    back to per-language regex heuristics run through ``CodeSearchEngine``
    (ripgrep or pure Python) otherwise. The result text says which engine
    answered, so the model can judge precision.

Why it exists
    ``LspRail`` mounts a generic ``lsp`` tool, but agent-core's progressive
    tool policy defers every tool that is not on its built-in direct list, so
    the model never saw it (0 LSP calls in the SWE-bench Pro pilot); it also
    needs positions the model rarely knows. These wrappers are DIRECT, take a
    symbol *name*, and degrade gracefully when no language server is installed
    (the common case in benchmark containers and fresh developer machines).

Who calls it
    ``JiuwenSwarmCodeAdapter`` (``_build_find_symbol_tool``,
    ``_build_symbol_outline_tool``) and the swarm ``swarm.repo_tools`` element.

What it does not do
    It never starts a language server on its own (that stays with ``LspRail``
    and the ``lsp`` tool), never edits files and never leaves the workspace.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.common.logging import tool_logger as logger
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
from openjiuwen.core.foundation.tool.schema import ToolOutput

from jiuwenswarm.agents.harness.code.repo_tools._common import (
    IGNORED_DIRS,
    MAX_RESULTS_CEILING,
    FileHits,
    RootResolver,
    as_int,
    clip_line,
    contain_path,
    extensions_for_language,
    iter_source_files,
    language_for_path,
    normalize_language,
    relativize,
    render_hits,
    resolve_root,
    truncate_hits,
)
from jiuwenswarm.agents.harness.code.repo_tools.code_search import CodeSearchEngine
from jiuwenswarm.agents.harness.code.repo_tools.read_policy import (
    ReadPolicyFactory,
    filter_lsp_text,
    read_policy_notice,
    resolve_read_policy,
)

FIND_SYMBOL_TOOL = "find_symbol"
SYMBOL_OUTLINE_TOOL = "symbol_outline"
DEFAULT_SYMBOL_RESULTS = 20
DEFAULT_OUTLINE_SYMBOLS = 200
DEFAULT_LSP_TIMEOUT = 10.0
_KINDS = ("definition", "references", "all")

# ─── LSP bridge ─────────────────────────────────────────────


class LspBridge:
    """Thin, injectable access to the SDK LSP subsystem.

    ``available(file)`` is true only when ``LspRail`` already initialised the
    manager *and* a server that handles the file's extension is **already
    running**. The probe reads ``LSPServerManager.get_status()`` only; it
    never calls ``get_or_start_server`` and therefore never spawns a language
    server or blocks on its start-up. Starting servers stays with ``LspRail``
    and the ``lsp`` tool. ``call`` targets a running server, so it does not
    start one either.
    """

    def __init__(self, timeout: float = DEFAULT_LSP_TIMEOUT) -> None:
        self._timeout = timeout

    async def available(self, file_path: Path) -> bool:
        try:
            from openjiuwen.harness.lsp.core.manager import LSPServerManager
        except Exception:  # noqa: BLE001 - SDK without LSP support
            return False
        manager = LSPServerManager.get_instance()
        if manager is None:
            return False
        try:
            statuses = list(manager.get_status() or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("[repo_tools] LSP status probe failed for %s: %s", file_path, exc)
            return False
        running = [st for st in statuses if getattr(st, "running", False)]
        if not running:
            return False
        ext = file_path.suffix.lower()
        ext_map = getattr(manager, "_extension_map", None)
        if isinstance(ext_map, dict):
            server_ids = set(ext_map.get(ext, []) or [])
            if not server_ids:
                return False
            return any(getattr(st, "server_id", None) in server_ids for st in running)
        # No extension index exposed: accept a running server whose workspace
        # root contains the file, which is the only other evidence available.
        try:
            resolved = file_path.resolve()
        except OSError:
            return False
        for st in running:
            root = getattr(st, "root", None)
            if not root:
                continue
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except (ValueError, OSError):
                continue
        return False

    async def call(self, inputs: dict[str, Any], workspace: Path) -> tuple[bool, str]:
        """Run one LSP operation; returns ``(success, text)``."""
        try:
            from openjiuwen.harness.tools.lsp_tool._tool import call_lsp_tool

            result = await asyncio.wait_for(
                call_lsp_tool(inputs, workspace=workspace), timeout=self._timeout
            )
        except TimeoutError:
            return False, "LSP request timed out"
        except Exception as exc:  # noqa: BLE001
            return False, f"LSP request failed: {exc}"
        if not result.get("success"):
            return False, str(result.get("error") or "LSP request failed")
        data = result.get("data") or {}
        return True, str(data.get("result") or "")


# ─── language heuristics ────────────────────────────────────

# Definition patterns per language. ``NAME`` is replaced by the escaped symbol.
# They must be valid for both Rust regex (ripgrep) and Python ``re``: no
# look-around, no back-references.
_DEFINITION_PATTERNS: dict[str, tuple[str, ...]] = {
    "python": (
        r"^\s*(?:async\s+def|def|class)\s+NAME\b",
        r"^NAME\s*(?::[^=\n]*)?=",
    ),
    "javascript": (
        r"\b(?:function\*?|class|interface|type|enum|namespace)\s+NAME\b",
        r"\b(?:const|let|var)\s+NAME\b",
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:static\s+)?(?:public\s+|private\s+|protected\s+)?(?:get\s+|set\s+)?NAME\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;]*)?\{",
        r"\bNAME\s*[:=]\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>",
    ),
    "go": (
        r"^func\s+(?:\([^)]*\)\s*)?NAME\b",
        r"^type\s+NAME\b",
        r"^\s*(?:var|const)\s+NAME\b",
    ),
    "rust": (
        r"\b(?:fn|struct|enum|trait|type|const|static|mod|union)\s+NAME\b",
        r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?NAME\b",
        r"macro_rules!\s+NAME\b",
    ),
    "java": (
        r"\b(?:class|interface|enum|record)\s+NAME\b",
        r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|default)\s+)*[\w<>\[\],.? ]+\s+NAME\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{",
    ),
    "kotlin": (
        r"\b(?:class|interface|object|enum class|data class|sealed class)\s+NAME\b",
        r"\bfun\s+(?:<[^>]*>\s+)?(?:[\w.]+\.)?NAME\b",
        r"\b(?:val|var)\s+NAME\b",
    ),
    "csharp": (
        r"\b(?:class|interface|enum|record|struct|delegate)\s+NAME\b",
        r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|async|sealed)\s+)*[\w<>\[\],.? ]+\s+NAME\s*\([^;]*\)\s*\{",
    ),
    "scala": (
        r"\b(?:class|trait|object|case class)\s+NAME\b",
        r"\bdef\s+NAME\b",
        r"\b(?:val|var)\s+NAME\b",
    ),
    "c": (
        r"\b(?:struct|enum|union)\s+NAME\b",
        r"^\s*#\s*define\s+NAME\b",
        r"^[\w\s\*]+\bNAME\s*\([^;{]*\)\s*\{",
        r"\btypedef\b[^;]*\bNAME\s*;",
    ),
    "cpp": (
        r"\b(?:struct|class|enum|union|namespace)\s+NAME\b",
        r"^\s*#\s*define\s+NAME\b",
        r"^[\w\s\*&:<>,]+\bNAME\s*\([^;{]*\)\s*(?:const)?\s*(?:noexcept)?\s*\{",
        r"\b(?:typedef|using)\b[^;]*\bNAME\b",
    ),
    "ruby": (r"^\s*(?:def|class|module)\s+(?:self\.)?NAME\b",),
    "php": (r"\b(?:function|class|interface|trait|enum)\s+NAME\b",),
    "swift": (r"\b(?:func|class|struct|enum|protocol|extension|var|let|typealias)\s+NAME\b",),
    "shell": (r"^\s*(?:function\s+)?NAME\s*\(\)\s*\{", r"^\s*NAME=\S"),
}

_GENERIC_DEFINITION = (
    r"\b(?:def|class|function|func|fn|struct|enum|interface|type|trait|module|namespace|const|let|var|val)\s+NAME\b",
)

_SEARCHABLE_LANGUAGES = tuple(_DEFINITION_PATTERNS)


def definition_regex(name: str, language: str | None) -> str:
    """Return one alternation regex matching definitions of ``name``."""
    escaped = re.escape(name)
    if language and language in _DEFINITION_PATTERNS:
        patterns = _DEFINITION_PATTERNS[language]
    else:
        seen: list[str] = []
        for group in _DEFINITION_PATTERNS.values():
            for pat in group:
                if pat not in seen:
                    seen.append(pat)
        patterns = tuple(seen) + _GENERIC_DEFINITION
    return "|".join(f"(?:{pat.replace('NAME', escaped)})" for pat in patterns)


def reference_regex(name: str) -> str:
    """Word-bounded occurrences of ``name``."""
    return rf"\b{re.escape(name)}\b"


def looks_like_definition(line: str, name: str, language: str | None) -> bool:
    """True when ``line`` matches a definition pattern for ``name``."""
    try:
        return re.search(definition_regex(name, language), line) is not None
    except re.error:
        return False


# ─── outline heuristics ─────────────────────────────────────


@dataclass
class OutlineEntry:
    line: int
    kind: str
    name: str
    depth: int


_PY_OUTLINE = re.compile(r"^(?P<indent>\s*)(?P<kw>async\s+def|def|class)\s+(?P<name>\w+)")
_JS_OUTLINE = (
    re.compile(
        r"^(?P<indent>\s*)(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?P<kw>class|interface|type|enum|namespace|function\*?)\s+(?P<name>\w+)"
    ),
    re.compile(
        r"^(?P<indent>\s*)(?:export\s+)?(?P<kw>const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"
    ),
    re.compile(
        r"^(?P<indent>\s+)(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(?P<name>\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;]*)?\{"
    ),
)
_GO_OUTLINE = (
    re.compile(r"^(?P<indent>)func\s+\((?P<recv>[^)]*)\)\s*(?P<name>\w+)"),
    re.compile(r"^(?P<indent>)(?P<kw>func|type)\s+(?P<name>\w+)(?:\s+(?P<shape>struct|interface))?"),
    re.compile(r"^(?P<indent>)(?P<kw>var|const)\s+(?P<name>\w+)"),
)
_RUST_OUTLINE = re.compile(
    r"^(?P<indent>\s*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|unsafe\s+|const\s+)*(?P<kw>fn|struct|enum|trait|type|mod|const|static|union|impl)\s+(?:<[^>]*>\s*)?(?P<name>[\w:]+)"
)
_JAVALIKE_OUTLINE = (
    re.compile(
        r"^(?P<indent>\s*)(?:(?:public|private|protected|internal|static|final|abstract|sealed|data|open|case)\s+)*(?P<kw>class|interface|enum|record|object|trait|struct)\s+(?P<name>\w+)"
    ),
    re.compile(
        r"^(?P<indent>\s+)(?:(?:public|private|protected|internal|static|final|abstract|synchronized|override|virtual|async|suspend|open)\s+)*(?:fun\s+|def\s+)?[\w<>\[\],.? ]*?\b(?P<name>\w+)\s*\([^;]*\)\s*(?::\s*[\w<>\[\]?]+\s*)?(?:throws\s+[\w.,\s]+)?\s*(?:\{|=)"
    ),
)
_C_OUTLINE = (
    re.compile(r"^(?P<indent>\s*)(?P<kw>struct|class|enum|union|namespace)\s+(?P<name>\w+)"),
    re.compile(r"^\s*#\s*define\s+(?P<name>\w+)"),
    re.compile(r"^(?P<indent>)[\w\s\*&:<>,]+?\b(?P<name>\w+)\s*\([^;{]*\)\s*(?:const)?\s*(?:noexcept)?\s*\{"),
)
_RUBY_OUTLINE = re.compile(r"^(?P<indent>\s*)(?P<kw>def|class|module)\s+(?:self\.)?(?P<name>[\w?!]+)")
_MD_OUTLINE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<name>.+?)\s*#*\s*$")
_SHELL_OUTLINE = re.compile(r"^\s*(?:function\s+)?(?P<name>[\w-]+)\s*\(\)\s*\{")

_KEYWORD_KIND = {
    "def": "function",
    "async def": "function",
    "async  def": "function",
    "fn": "function",
    "func": "function",
    "function": "function",
    "function*": "function",
    "fun": "function",
    "class": "class",
    "interface": "interface",
    "type": "type",
    "enum": "enum",
    "struct": "struct",
    "union": "union",
    "trait": "trait",
    "record": "record",
    "object": "object",
    "namespace": "namespace",
    "mod": "module",
    "module": "module",
    "impl": "impl",
    "const": "const",
    "static": "static",
    "let": "const",
    "var": "variable",
}


def _kind(keyword: str, indent: int, default: str = "symbol") -> str:
    keyword = re.sub(r"\s+", " ", keyword.strip())
    kind = _KEYWORD_KIND.get(keyword, default)
    if kind == "function" and indent > 0:
        return "method"
    return kind


def _depth(indent: str, unit: int = 4) -> int:
    """Nesting level from leading whitespace (4 columns in Python, 2 elsewhere)."""
    width = len(indent.expandtabs(4))
    return width // unit if width else 0


def outline_lines(lines: list[str], language: str | None) -> list[OutlineEntry]:
    """Regex outline of a file; nesting is inferred from indentation."""
    entries: list[OutlineEntry] = []
    for index, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        entry: OutlineEntry | None = None
        if language == "python":
            m = _PY_OUTLINE.match(line)
            if m:
                indent = _depth(m.group("indent"))
                entry = OutlineEntry(index, _kind(m.group("kw"), indent), m.group("name"), indent)
        elif language in ("javascript", "typescript"):
            for pattern in _JS_OUTLINE:
                m = pattern.match(line)
                if not m:
                    continue
                indent = _depth(m.group("indent"), 2)
                kw = m.groupdict().get("kw")
                kind = _kind(kw, indent) if kw else "method"
                if kw in ("const", "let", "var"):
                    kind = "function" if indent == 0 else "method"
                if m.group("name") in {"if", "for", "while", "switch", "return", "catch", "function"}:
                    break
                entry = OutlineEntry(index, kind, m.group("name"), indent)
                break
        elif language == "go":
            for pattern in _GO_OUTLINE:
                m = pattern.match(line)
                if not m:
                    continue
                if "recv" in m.groupdict():
                    entry = OutlineEntry(index, "method", m.group("name"), 1)
                else:
                    shape = m.groupdict().get("shape")
                    kind = shape if shape else _kind(m.group("kw"), 0)
                    entry = OutlineEntry(index, kind, m.group("name"), 0)
                break
        elif language == "rust":
            m = _RUST_OUTLINE.match(line)
            if m:
                indent = _depth(m.group("indent"), 2)
                entry = OutlineEntry(index, _kind(m.group("kw"), indent), m.group("name"), indent)
        elif language in ("java", "kotlin", "csharp", "scala"):
            for pattern in _JAVALIKE_OUTLINE:
                m = pattern.match(line)
                if not m:
                    continue
                indent = _depth(m.group("indent"), 2)
                kw = m.groupdict().get("kw")
                name = m.group("name")
                if kw is None and name in {"if", "for", "while", "switch", "return", "catch", "new", "else"}:
                    break
                entry = OutlineEntry(index, _kind(kw, indent) if kw else "method", name, indent)
                break
        elif language in ("c", "cpp"):
            for pattern in _C_OUTLINE:
                m = pattern.match(line)
                if not m:
                    continue
                indent = _depth(m.groupdict().get("indent") or "", 2)
                kw = m.groupdict().get("kw")
                if kw is None and "define" in line.split(m.group("name"))[0]:
                    entry = OutlineEntry(index, "macro", m.group("name"), 0)
                elif kw is None:
                    if m.group("name") in {"if", "for", "while", "switch", "return", "sizeof"}:
                        break
                    entry = OutlineEntry(index, "function", m.group("name"), indent)
                else:
                    entry = OutlineEntry(index, _kind(kw, indent), m.group("name"), indent)
                break
        elif language == "ruby":
            m = _RUBY_OUTLINE.match(line)
            if m:
                indent = _depth(m.group("indent"), 2)
                entry = OutlineEntry(index, _kind(m.group("kw"), indent), m.group("name"), indent)
        elif language == "markdown":
            m = _MD_OUTLINE.match(line)
            if m:
                level = len(m.group("hashes"))
                entry = OutlineEntry(index, f"h{level}", m.group("name"), level - 1)
        elif language == "shell":
            m = _SHELL_OUTLINE.match(line)
            if m:
                entry = OutlineEntry(index, "function", m.group("name"), 0)
        else:
            m = re.match(
                r"^(?P<indent>\s*)(?P<kw>def|class|function|func|fn|struct|enum|interface|type|trait|module|namespace)\s+(?P<name>\w+)",
                line,
            )
            if m:
                indent = _depth(m.group("indent"), 2)
                entry = OutlineEntry(index, _kind(m.group("kw"), indent), m.group("name"), indent)
        if entry is not None:
            entries.append(entry)
    return entries


def render_outline(entries: list[OutlineEntry], rel_path: str, *, engine: str, truncated: bool) -> str:
    header = f"{rel_path}: {len(entries)} symbol{'s' if len(entries) != 1 else ''} ({engine})"
    if truncated:
        header += ", truncated"
    if not entries:
        return header + "\nNo symbols recognised; read the file directly."
    body = [f"  L{e.line:<5} {'  ' * e.depth}{e.kind} {e.name}" for e in entries]
    return header + "\n" + "\n".join(body)


# ─── cards ──────────────────────────────────────────────────

_FIND_SYMBOL_EN = (
    "Find where a symbol (function, class, method, type, constant, variable) is defined "
    "and/or used, by name. Uses the language server when one is running for the file "
    "type, otherwise language-aware definition patterns over the working tree; the "
    "result says which engine answered. kind=definition (default) lists definitions, "
    "kind=references lists usages (definition lines are tagged), kind=all does both. "
    "Give file_path to pin the language (and, with line, to use exact LSP references). "
    "Use this before read_file so you open the right file at the right line; "
    "never use git history or the network to locate code."
)
_FIND_SYMBOL_CN = (
    "按名称查找符号（函数、类、方法、类型、常量、变量）的定义和/或引用。"
    "该文件类型有运行中的语言服务器时走 LSP，否则用按语言的定义模式在工作树中匹配；"
    "结果会标明使用的引擎。kind=definition（默认）列出定义，kind=references 列出引用"
    "（定义行会被标记），kind=all 两者都给。传 file_path 可固定语言（再加 line 可用精确的 "
    "LSP 引用查询）。先用它定位，再用 read_file 读取正确的文件与行；"
    "不要通过 git 历史或网络寻找代码。"
)
_OUTLINE_EN = (
    "List the symbols of one file (classes, functions, methods, types, constants; "
    "headings for Markdown) with line numbers and nesting, so you can jump to the right "
    "range with read_file instead of reading the whole file. Uses the language server "
    "when available, otherwise language-aware patterns."
)
_OUTLINE_CN = (
    "列出单个文件的符号（类、函数、方法、类型、常量；Markdown 为标题）及其行号与层级，"
    "以便用 read_file 只读取需要的范围而不是整个文件。有语言服务器时走 LSP，否则用按语言的模式匹配。"
)


def find_symbol_input_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact symbol name to look up."},
            "kind": {
                "type": "string",
                "enum": list(_KINDS),
                "description": "definition (default), references, or all.",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Optional file that pins the language and, with line, the position for "
                    "exact LSP reference lookup."
                ),
            },
            "line": {
                "type": "integer",
                "description": "Optional 1-based line of the symbol in file_path (LSP references).",
            },
            "language": {
                "type": "string",
                "description": "Optional language when file_path is not given (python, typescript, go, ...).",
            },
            "path": {
                "type": "string",
                "description": "Optional sub-directory to restrict the search to.",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum hits per section, default {DEFAULT_SYMBOL_RESULTS}.",
            },
        },
        "required": ["name"],
    }


def symbol_outline_input_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File to outline, relative to the workspace root."},
            "max_symbols": {
                "type": "integer",
                "description": f"Maximum symbols to list, default {DEFAULT_OUTLINE_SYMBOLS}.",
            },
        },
        "required": ["file_path"],
    }


def _card(name: str, description: str, params: dict[str, Any], agent_id: str | None) -> ToolCard:
    return ToolCard(
        id=f"{name}_{agent_id}" if agent_id else name,
        name=name,
        description=description,
        input_params=params,
        exposure=ToolExposure.DIRECT,
        parallel_safe=True,
        idempotent=True,
    )


def build_find_symbol_card(*, agent_id: str | None = None, language: str = "en") -> ToolCard:
    return _card(
        FIND_SYMBOL_TOOL,
        _FIND_SYMBOL_CN if language == "cn" else _FIND_SYMBOL_EN,
        find_symbol_input_params(),
        agent_id,
    )


def build_symbol_outline_card(*, agent_id: str | None = None, language: str = "en") -> ToolCard:
    return _card(
        SYMBOL_OUTLINE_TOOL,
        _OUTLINE_CN if language == "cn" else _OUTLINE_EN,
        symbol_outline_input_params(),
        agent_id,
    )


# ─── tools ──────────────────────────────────────────────────


def _first_source_file(
    root: Path,
    search_path: Path,
    language: str,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
) -> Path | None:
    """Pick any file of ``language`` under ``search_path`` to anchor an LSP query.

    Uses the bounded ``iter_source_files`` walk (ignore rules, no symlinks,
    pruned directories) and stops at the first hit instead of materialising
    the whole tree.
    """
    exts = extensions_for_language(language)
    if not exts:
        return None
    if search_path.is_file():
        return search_path if search_path.suffix.lower() in set(exts) else None
    for candidate in iter_source_files(root, search_path, extensions=exts, ignored_dirs=ignored_dirs):
        return candidate
    return None


class FindSymbolTool(Tool):
    """Model-facing ``find_symbol`` (see module docstring)."""

    def __init__(
        self,
        root_resolver: RootResolver | None = None,
        *,
        engine: CodeSearchEngine | None = None,
        lsp: LspBridge | None = None,
        agent_id: str | None = None,
        language: str = "en",
        default_max_results: int = DEFAULT_SYMBOL_RESULTS,
        read_policy_factory: ReadPolicyFactory | None = None,
    ) -> None:
        """``read_policy_factory`` is used only when ``engine`` is not given
        (a supplied engine carries its own read policy)."""
        super().__init__(build_find_symbol_card(agent_id=agent_id, language=language))
        self._root_resolver = root_resolver
        self._engine = engine or CodeSearchEngine(root_resolver, read_policy_factory=read_policy_factory)
        self._lsp = lsp or LspBridge()
        self._default_max_results = as_int(
            default_max_results, DEFAULT_SYMBOL_RESULTS, low=1, high=MAX_RESULTS_CEILING
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        name = str(inputs.get("name") or "").strip()
        if not name or not re.fullmatch(r"[\w$.:!?-]+", name):
            return ToolOutput(success=False, error="name must be a single identifier")
        kind = str(inputs.get("kind") or "definition").strip().lower()
        if kind not in _KINDS:
            return ToolOutput(success=False, error=f"kind must be one of {', '.join(_KINDS)}")
        max_results = as_int(
            inputs.get("max_results"), self._default_max_results, low=1, high=MAX_RESULTS_CEILING
        )
        root = resolve_root(self._root_resolver)
        try:
            search_path = contain_path(root, inputs.get("path"))
            anchor = contain_path(root, inputs.get("file_path")) if inputs.get("file_path") else None
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        try:
            language = normalize_language(inputs.get("language"))
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        policy = self._engine.read_policy(root)
        if policy.refusal is not None:
            return ToolOutput(success=False, error=policy.refusal)
        for label, target in (("path", search_path if search_path != root else None), ("file_path", anchor)):
            reason = None
            if target is not None:
                reason = policy.dir_hidden_reason(target) if target.is_dir() else policy.hidden_reason(target)
            if reason:
                return ToolOutput(
                    success=False,
                    error=f"{label} is not readable under the read policy: {inputs.get(label)} ({reason})",
                )
        if language is None and anchor is not None:
            language = language_for_path(anchor)
        line = as_int(inputs.get("line"), 0, low=0, high=10_000_000) or None

        sections: list[str] = []
        engines: set[str] = set()
        structured: dict[str, Any] = {"name": name, "kind": kind, "language": language}

        if kind in ("definition", "all"):
            text, engine, defs = await self._definitions(
                root, search_path, name, language, anchor, max_results
            )
            sections.append(text)
            engines.add(engine)
            structured["definitions"] = defs
        if kind in ("references", "all"):
            text, engine, refs = await self._references(
                root, search_path, name, language, anchor, line, max_results
            )
            sections.append(text)
            engines.add(engine)
            structured["references"] = refs

        structured["engine"] = "+".join(sorted(engines))
        structured["content"] = "\n\n".join(sections)
        return ToolOutput(success=True, data=structured)

    async def _lsp_anchor(self, root: Path, search_path: Path, language: str | None, anchor: Path | None) -> Path | None:
        candidate = anchor
        if candidate is None and language:
            # Off the event loop: with no file of that language the walk
            # covers the whole tree.
            candidate = await asyncio.to_thread(
                _first_source_file, root, search_path, language, self._engine.ignored_dirs
            )
        if candidate is None:
            return None
        if await self._lsp.available(candidate):
            return candidate
        return None

    async def _definitions(
        self,
        root: Path,
        search_path: Path,
        name: str,
        language: str | None,
        anchor: Path | None,
        max_results: int,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        lsp_anchor = await self._lsp_anchor(root, search_path, language, anchor)
        if lsp_anchor is not None:
            ok, text = await self._lsp.call(
                {"operation": "workspaceSymbol", "query": name, "file_path": str(lsp_anchor)},
                root,
            )
            if ok and text and not text.startswith("No symbols"):
                policy = self._engine.read_policy(root)
                text = filter_lsp_text(text, root, policy)
                # Everything the server found was hidden: answer exactly as if
                # it had found nothing (fall through to the filtered regex
                # search), so the result cannot tell the two cases apart.
                if text.strip():
                    exact = [
                        ln
                        for ln in text.splitlines()
                        if re.search(rf"\b{re.escape(name)}\b", ln) or ln.endswith(":")
                    ]
                    body = "\n".join(exact) if exact else text
                    notice = read_policy_notice(policy)
                    note = f" ({notice})" if notice else ""
                    return f"Definitions of {name} (lsp){note}:\n{body}", "lsp", [{"raw": body}]
        try:
            ranked, engine, timed_out, warning = await self._engine.search(
                pattern=definition_regex(name, language),
                path=relativize(root, search_path) if search_path != root else None,
                language=language,
                context=0,
            )
        except ValueError as exc:
            return f"Definitions of {name}: search failed ({exc})", "none", []
        if timed_out:
            warning = "; ".join(n for n in ("search timed out, results are partial", warning) if n)
        kept, total, truncated = truncate_hits(ranked, max_results)
        defs = [
            {"path": f.path, "line": h.line, "text": h.text} for f in kept for h in f.hits
        ]
        note = f"; {warning}" if warning else ""
        if not defs:
            hint = "" if language else " (give file_path or language to narrow the patterns)"
            message = (
                f"Definitions of {name} ({engine}): none found{hint}{note}. "
                "Try find_symbol with kind=references or code_search."
            )
            return message, engine, []
        header = f"Definitions of {name} ({engine}): {total}"
        if truncated:
            header += f", showing {len(defs)}"
        header += note
        return header + "\n" + render_hits(kept, show_context=False), engine, defs

    async def _references(
        self,
        root: Path,
        search_path: Path,
        name: str,
        language: str | None,
        anchor: Path | None,
        line: int | None,
        max_results: int,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        if anchor is not None and line and await self._lsp.available(anchor):
            ok, text = await self._lsp.call(
                {
                    "operation": "findReferences",
                    "file_path": str(anchor),
                    "line": line,
                    "character": 1,
                    "include_declaration": True,
                },
                root,
            )
            if ok and text:
                policy = self._engine.read_policy(root)
                text = filter_lsp_text(text, root, policy)
                if not text.strip():
                    # Same answer as a server that found nothing.
                    text = "No references found."
                notice = read_policy_notice(policy)
                note = f" ({notice})" if notice else ""
                return f"References to {name} (lsp){note}:\n{text}", "lsp", [{"raw": text}]
        try:
            ranked, engine, timed_out, warning = await self._engine.search(
                pattern=reference_regex(name),
                path=relativize(root, search_path) if search_path != root else None,
                language=language,
                context=0,
            )
        except ValueError as exc:
            return f"References to {name}: search failed ({exc})", "none", []
        if timed_out:
            warning = "; ".join(n for n in ("search timed out, results are partial", warning) if n)
        kept, total, truncated = truncate_hits(ranked, max_results)
        refs: list[dict[str, Any]] = []
        for item in kept:
            for hit in item.hits:
                is_def = looks_like_definition(hit.text, name, language)
                refs.append({"path": item.path, "line": hit.line, "text": hit.text, "definition": is_def})
                if is_def:
                    hit.text = clip_line(hit.text) + "    [definition]"
        header = f"References to {name} ({engine}): {total} in {len(ranked)} file{'s' if len(ranked) != 1 else ''}"
        if truncated:
            header += f", showing {len(refs)}"
        if warning:
            header += f"; {warning}"
        if not refs:
            return header, engine, []
        return header + "\n" + render_hits(kept, show_context=False), engine, refs

    def render_for_llm(self, output: Any) -> str:
        if isinstance(output, ToolOutput):
            if output.success and isinstance(output.data, dict):
                return str(output.data.get("content") or "")
            return output.error or "find_symbol failed"
        return str(output)

    async def stream(self, inputs: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        yield await self.invoke(inputs, **kwargs)


class SymbolOutlineTool(Tool):
    """Model-facing ``symbol_outline`` (see module docstring)."""

    def __init__(
        self,
        root_resolver: RootResolver | None = None,
        *,
        lsp: LspBridge | None = None,
        agent_id: str | None = None,
        language: str = "en",
        default_max_symbols: int = DEFAULT_OUTLINE_SYMBOLS,
        read_policy_factory: ReadPolicyFactory | None = None,
    ) -> None:
        super().__init__(build_symbol_outline_card(agent_id=agent_id, language=language))
        self._root_resolver = root_resolver
        self._lsp = lsp or LspBridge()
        self._read_policy_factory = read_policy_factory
        self._default_max_symbols = as_int(default_max_symbols, DEFAULT_OUTLINE_SYMBOLS, low=1, high=2000)

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        raw_path = str(inputs.get("file_path") or "").strip()
        if not raw_path:
            return ToolOutput(success=False, error="file_path is required")
        root = resolve_root(self._root_resolver)
        try:
            target = contain_path(root, raw_path)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        if not target.is_file():
            return ToolOutput(success=False, error=f"not a file: {raw_path}")
        reason = resolve_read_policy(self._read_policy_factory, root).hidden_reason(target)
        if reason:
            return ToolOutput(
                success=False,
                error=f"file_path is not readable under the read policy: {raw_path} ({reason})",
            )
        max_symbols = as_int(inputs.get("max_symbols"), self._default_max_symbols, low=1, high=2000)
        rel = relativize(root, target)
        language = language_for_path(target)

        if await self._lsp.available(target):
            ok, text = await self._lsp.call(
                {"operation": "documentSymbol", "file_path": str(target)}, root
            )
            if ok and text and not text.startswith("No symbols"):
                lines = text.splitlines()
                truncated = len(lines) > max_symbols
                body = "\n".join(lines[:max_symbols])
                content = f"{rel}: symbols (lsp){', truncated' if truncated else ''}\n{body}"
                return ToolOutput(
                    success=True,
                    data={"content": content, "engine": "lsp", "path": rel, "language": language, "truncated": truncated},
                )
        try:
            data = target.read_bytes()
        except OSError as exc:
            return ToolOutput(success=False, error=f"cannot read {raw_path}: {exc}")
        if b"\x00" in data[:1024]:
            return ToolOutput(success=False, error=f"binary file: {raw_path}")
        entries = outline_lines(data.decode("utf-8", "replace").splitlines(), language)
        truncated = len(entries) > max_symbols
        entries = entries[:max_symbols]
        content = render_outline(entries, rel, engine="regex", truncated=truncated)
        return ToolOutput(
            success=True,
            data={
                "content": content,
                "engine": "regex",
                "path": rel,
                "language": language,
                "truncated": truncated,
                "symbols": [
                    {"line": e.line, "kind": e.kind, "name": e.name, "depth": e.depth} for e in entries
                ],
            },
        )

    def render_for_llm(self, output: Any) -> str:
        if isinstance(output, ToolOutput):
            if output.success and isinstance(output.data, dict):
                return str(output.data.get("content") or "")
            return output.error or "symbol_outline failed"
        return str(output)

    async def stream(self, inputs: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        yield await self.invoke(inputs, **kwargs)


__all__ = [
    "DEFAULT_OUTLINE_SYMBOLS",
    "DEFAULT_SYMBOL_RESULTS",
    "FIND_SYMBOL_TOOL",
    "SYMBOL_OUTLINE_TOOL",
    "FileHits",
    "FindSymbolTool",
    "LspBridge",
    "OutlineEntry",
    "SymbolOutlineTool",
    "build_find_symbol_card",
    "build_symbol_outline_card",
    "definition_regex",
    "find_symbol_input_params",
    "looks_like_definition",
    "outline_lines",
    "reference_regex",
    "render_outline",
    "symbol_outline_input_params",
]
