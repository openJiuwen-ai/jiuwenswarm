# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight AST and regex code outline tool for workspace source files."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.tools.command_runtime import (
    CommandRuntimePaths,
    _is_relative_to,
    current_command_runtime_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 3
_MIN_MAX_DEPTH = 0
_MAX_MAX_DEPTH = 10

DEFAULT_MAX_FILES = 50
_MIN_MAX_FILES = 1
_MAX_MAX_FILES = 200

DEFAULT_MAX_CHARS = 30_000
_MIN_MAX_CHARS = 1_000
_MAX_MAX_CHARS = 100_000

_MAX_FILE_BYTES = 2 * 1024 * 1024
_DIR_TIME_BUDGET_SECONDS = 15.0
_BINARY_SNIFF_BYTES = 8192
_DOCSTRING_SUMMARY_LIMIT = 120
_ANNOTATION_LIMIT = 50
_DEFAULT_EXPR_LIMIT = 35

_PRUNED_DIRS = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
})
_PYTHON_EXTS = frozenset({".py", ".pyi"})
_JS_TS_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_SUPPORTED_EXTS = _PYTHON_EXTS | _JS_TS_EXTS

_JSTS_CONTROL_KEYWORDS = frozenset({
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "throw",
    "typeof",
    "delete",
    "void",
    "await",
    "yield",
    "new",
})

_RE_CLASS = re.compile(
    r"^\s*(export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+"
    r"([A-Za-z_$][\w$]*)(?:\s*<[^>]*>)?(?:\s+extends\s+([^\s{]+))?"
)
_RE_INTERFACE = re.compile(
    r"^\s*(export\s+)?interface\s+([A-Za-z_$][\w$]*)"
    r"(?:\s*<[^>]*>)?(?:\s+extends\s+([^{]+))?"
)
_RE_TYPE = re.compile(
    r"^\s*(export\s+)?type\s+([A-Za-z_$][\w$]*)(?:\s*<[^>]*>)?\s*="
)
_RE_ENUM = re.compile(
    r"^\s*(export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)"
)
_RE_FUNCTION = re.compile(
    r"^\s*(export\s+(?:default\s+)?)?(async\s+)?function\s*(\*?)\s*"
    r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\(([^)]*)\)"
    r"(?:\s*:\s*([^{;=]+?))?\s*(?:\{|;|$)"
)
_RE_DEFAULT_ANON_FUNCTION = re.compile(
    r"^\s*export\s+default\s+(async\s+)?function\s*(\*?)\s*"
    r"\(([^)]*)\)(?:\s*:\s*([^{;]+?))?\s*\{"
)
_RE_ARROW = re.compile(
    r"^\s*(export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s*:\s*[^=]+?)?\s*=\s*(async\s+)?"
    r"(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*(?::\s*([^=]+?))?\s*=>"
)
_RE_FUNC_EXPR = re.compile(
    r"^\s*(export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s*:\s*[^=]+?)?\s*=\s*(async\s+)?function\s*(\*?)\s*"
    r"(?:[A-Za-z_$][\w$]*)?\s*\(([^)]*)\)(?:\s*:\s*([^{;]+?))?\s*\{"
)
_RE_METHOD = re.compile(
    r"^\s*((?:(?:public|private|protected|static|readonly|abstract|"
    r"override|async|get|set)\s+)*)([A-Za-z_$#][\w$]*)"
    r"\s*(?:<[^>]*>)?\(([^)]*)\)(?:\s*:\s*([^{;]+?))?\s*(?:\{|;)"
)


@dataclass(frozen=True)
class CodeOutlineRequest:
    """Normalized inputs for a code_outline call."""

    path: str
    max_depth: int
    max_files: int
    max_chars: int
    include_private: bool


def _normalize_int_bound(
    value: Any,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def _normalize_request(
    *,
    path: Any,
    max_depth: Any,
    max_files: Any,
    max_chars: Any,
    include_private: Any,
) -> CodeOutlineRequest:
    if path is None:
        normalized_path = "."
    elif isinstance(path, str):
        if "\x00" in path:
            raise ValueError("path contains NUL byte")
        normalized_path = path.strip() or "."
    else:
        raise ValueError("path must be a string")

    if include_private is None:
        normalized_private = False
    elif isinstance(include_private, bool):
        normalized_private = include_private
    else:
        raise ValueError("include_private must be a boolean")

    return CodeOutlineRequest(
        path=normalized_path,
        max_depth=_normalize_int_bound(
            max_depth,
            name="max_depth",
            default=DEFAULT_MAX_DEPTH,
            minimum=_MIN_MAX_DEPTH,
            maximum=_MAX_MAX_DEPTH,
        ),
        max_files=_normalize_int_bound(
            max_files,
            name="max_files",
            default=DEFAULT_MAX_FILES,
            minimum=_MIN_MAX_FILES,
            maximum=_MAX_MAX_FILES,
        ),
        max_chars=_normalize_int_bound(
            max_chars,
            name="max_chars",
            default=DEFAULT_MAX_CHARS,
            minimum=_MIN_MAX_CHARS,
            maximum=_MAX_MAX_CHARS,
        ),
        include_private=normalized_private,
    )


def _allowed_roots(runtime_paths: CommandRuntimePaths) -> list[Path]:
    roots: list[Path] = [runtime_paths.project_root]
    if runtime_paths.workspace_root is not None:
        roots.append(runtime_paths.workspace_root)
    roots.append(runtime_paths.agent_workspace_root)
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_team_workspace

        team_workspace = get_team_workspace()
        if team_workspace:
            roots.append(Path(team_workspace).resolve())
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    deduped: list[Path] = []
    for root in roots:
        if root not in deduped:
            deduped.append(root)
    return deduped


def _is_within_roots(path: Path, roots: list[Path]) -> bool:
    return any(_is_relative_to(path, root) for root in roots)


def _resolve_target_path(
    raw_path: str,
    runtime_paths: CommandRuntimePaths,
) -> tuple[Path, list[Path]]:
    if "\x00" in raw_path:
        raise ValueError("path contains NUL byte")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = runtime_paths.current_cwd / candidate
    resolved = candidate.resolve()
    roots = _allowed_roots(runtime_paths)
    if not _is_within_roots(resolved, roots):
        raise ValueError(f"path is outside workspace: {raw_path}")
    if not resolved.exists():
        raise FileNotFoundError(f"path does not exist: {raw_path}")
    return resolved, roots


def _display_path(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
        text = rel.as_posix()
        return text if text else "."
    except ValueError:
        return path.as_posix()


def _filter_walk_dirs(current: Path, dirnames: list[str]) -> list[str]:
    kept_dirs: list[str] = []
    for dirname in sorted(dirnames):
        if dirname in _PRUNED_DIRS:
            continue
        if (current / dirname).is_symlink():
            continue
        kept_dirs.append(dirname)
    return kept_dirs


def _validate_walk_file(file_path: Path, roots: list[Path]) -> bool:
    try:
        resolved_file = file_path.resolve()
    except (OSError, RuntimeError):
        return False
    return _is_within_roots(resolved_file, roots) and resolved_file.is_file()


def _collect_directory_files(
    root_dir: Path,
    *,
    roots: list[Path],
    max_depth: int,
    max_files: int,
) -> tuple[list[Path], bool, bool, int]:
    selected: list[Path] = []
    depth_truncated = False
    skipped_external = 0

    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=True, followlinks=False):
        current = Path(dirpath)
        kept_dirs = _filter_walk_dirs(current, dirnames)
        if len(current.relative_to(root_dir).parts) >= max_depth:
            if kept_dirs:
                depth_truncated = True
            dirnames[:] = []
        else:
            dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            file_path = current / filename
            if file_path.suffix.lower() not in _SUPPORTED_EXTS:
                continue
            if not _validate_walk_file(file_path, roots):
                skipped_external += 1
                continue
            if len(selected) >= max_files:
                dirnames[:] = []
                return selected, depth_truncated, True, skipped_external
            selected.append(file_path)

    return selected, depth_truncated, False, skipped_external


def _read_source_text(file_path: Path) -> tuple[str | None, str | None]:
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return None, f"[skipped: unreadable ({exc})]"
    if size > _MAX_FILE_BYTES:
        return None, f"[skipped: file exceeds {_MAX_FILE_BYTES} bytes]"
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        return None, f"[skipped: unreadable ({exc})]"
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return None, "[skipped: binary file (NUL byte detected)]"
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "[skipped: non-UTF-8 file]"


def _compact_expr(node: ast.AST, *, limit: int = _ANNOTATION_LIMIT) -> str:
    text = " ".join(ast.unparse(node).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _summarize_docstring(doc: str | None) -> str | None:
    if not doc:
        return None
    for raw_line in doc.splitlines():
        collapsed = " ".join(raw_line.split())
        if collapsed:
            if len(collapsed) > _DOCSTRING_SUMMARY_LIMIT:
                return collapsed[: _DOCSTRING_SUMMARY_LIMIT - 3] + "..."
            return collapsed
    return None


def _line_range(node: ast.AST) -> str:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    return f"L{start}-L{end}" if end != start else f"L{start}"


def _format_py_arg(arg: ast.arg, default_node: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {_compact_expr(arg.annotation, limit=_ANNOTATION_LIMIT)}"
    if default_node is not None:
        sep = " = " if arg.annotation is not None else "="
        text += f"{sep}{_compact_expr(default_node, limit=_DEFAULT_EXPR_LIMIT)}"
    return text


def _format_py_arguments(args: ast.arguments) -> str:
    parts: list[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    num_no_default = len(positional) - len(args.defaults)
    for idx, arg in enumerate(positional):
        default_idx = idx - num_no_default
        default_node = args.defaults[default_idx] if default_idx >= 0 else None
        parts.append(_format_py_arg(arg, default_node))
        if args.posonlyargs and idx == len(args.posonlyargs) - 1:
            parts.append("/")

    if args.vararg is not None:
        parts.append(f"*{_format_py_arg(args.vararg, None)}")

    if args.kwonlyargs:
        if args.vararg is None:
            parts.append("*")
        for arg, default_node in zip(args.kwonlyargs, args.kw_defaults):
            parts.append(_format_py_arg(arg, default_node))

    if args.kwarg is not None:
        parts.append(f"**{_format_py_arg(args.kwarg, None)}")
    return ", ".join(parts)


def _format_py_func(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    params = _format_py_arguments(node.args)
    ret = (
        f" -> {_compact_expr(node.returns, limit=_ANNOTATION_LIMIT)}"
        if node.returns is not None
        else ""
    )
    return f"{prefix} {node.name}({params}){ret} [{_line_range(node)}]"


def _append_py_class(
    node: ast.ClassDef,
    lines: list[str],
    *,
    indent: str,
    include_private: bool,
) -> None:
    if not include_private and node.name.startswith("_"):
        return
    if node.decorator_list:
        decs = " ".join(f"@{_compact_expr(d)}" for d in node.decorator_list)
        lines.append(f"{indent}{decs}")
    bases = [_compact_expr(base, limit=_ANNOTATION_LIMIT) for base in node.bases]
    bases_str = f"({', '.join(bases)})" if bases else ""
    lines.append(f"{indent}class {node.name}{bases_str} [{_line_range(node)}]")
    cls_doc = _summarize_docstring(ast.get_docstring(node, clean=True))
    if cls_doc:
        lines.append(f"{indent}  # {cls_doc}")
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                not include_private
                and child.name.startswith("_")
                and child.name != "__init__"
            ):
                continue
            if child.decorator_list:
                m_decs = " ".join(f"@{_compact_expr(d)}" for d in child.decorator_list)
                lines.append(f"{indent}  {m_decs}")
            lines.append(f"{indent}  {_format_py_func(child)}")
            m_doc = _summarize_docstring(ast.get_docstring(child, clean=True))
            if m_doc:
                lines.append(f"{indent}    # {m_doc}")
        elif isinstance(child, ast.ClassDef):
            _append_py_class(
                child,
                lines,
                indent=indent + "  ",
                include_private=include_private,
            )


def _extract_python_outline(
    source: str,
    file_path: Path,
    *,
    include_private: bool,
) -> list[str]:
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        line_no = exc.lineno if exc.lineno is not None else "?"
        return [f"[syntax error at line {line_no}]: {exc.msg}"]

    lines: list[str] = []
    mod_doc = _summarize_docstring(ast.get_docstring(tree, clean=True))
    if mod_doc:
        lines.append(f"# module: {mod_doc}")

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            _append_py_class(node, lines, indent="", include_private=include_private)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not include_private and node.name.startswith("_"):
                continue
            if node.decorator_list:
                decs = " ".join(f"@{_compact_expr(d)}" for d in node.decorator_list)
                lines.append(decs)
            lines.append(_format_py_func(node))
            f_doc = _summarize_docstring(ast.get_docstring(node, clean=True))
            if f_doc:
                lines.append(f"  # {f_doc}")

    return lines


def _step_code_state(ch: str, nxt: str, out: list[str]) -> tuple[str, int]:
    if ch == "/" and nxt == "/":
        out.append("  ")
        return "line_comment", 2
    if ch == "/" and nxt == "*":
        out.append("  ")
        return "block_comment", 2
    if ch == "'":
        out.append(" ")
        return "single_quote", 1
    if ch == '"':
        out.append(" ")
        return "double_quote", 1
    if ch == "`":
        out.append(" ")
        return "template", 1
    out.append(ch)
    return "code", 1


def _step_quoted_escape(nxt: str, out: list[str]) -> int:
    if nxt == "\n":
        out.append(" \n")
        return 2
    if nxt:
        out.append("  ")
        return 2
    out.append(" ")
    return 1


def _step_quote_or_template(
    state: str, ch: str, nxt: str, out: list[str]
) -> tuple[str, int]:
    if ch == "\\":
        return state, _step_quoted_escape(nxt, out)
    if state in ("single_quote", "double_quote"):
        quote_char = "'" if state == "single_quote" else '"'
        if ch in (quote_char, "\n"):
            out.append("\n" if ch == "\n" else " ")
            return "code", 1
        out.append(" ")
        return state, 1
    if ch == "`":
        out.append(" ")
        return "code", 1
    out.append("\n" if ch == "\n" else " ")
    return state, 1


def _step_non_code_state(
    state: str, ch: str, nxt: str, out: list[str]
) -> tuple[str, int]:
    if state == "line_comment":
        out.append("\n" if ch == "\n" else " ")
        return ("code" if ch == "\n" else "line_comment"), 1
    if state == "block_comment":
        if ch == "*" and nxt == "/":
            out.append("  ")
            return "code", 2
        out.append("\n" if ch == "\n" else " ")
        return "block_comment", 1
    return _step_quote_or_template(state, ch, nxt, out)


def _blank_comments_and_strings(source: str) -> str:
    """Mask JS/TS comments and string/template literals while preserving newlines."""
    out: list[str] = []
    state = "code"
    idx = 0
    length = len(source)
    while idx < length:
        ch = source[idx]
        nxt = source[idx + 1] if idx + 1 < length else ""
        if state == "code":
            state, step = _step_code_state(ch, nxt, out)
        else:
            state, step = _step_non_code_state(state, ch, nxt, out)
        idx += step
    return "".join(out)


def _is_jsts_private(name: str, modifiers: str = "") -> bool:
    if name.startswith("_") or name.startswith("#"):
        return True
    return "private" in modifiers.split()


def _collapse_ws(text: str | None) -> str:
    return " ".join((text or "").split())


def _match_jsts_type_decl(
    line: str, lineno: int, *, include_private: bool
) -> str | None:
    iface_match = _RE_INTERFACE.match(line)
    if iface_match:
        exp_prefix = _collapse_ws(iface_match.group(1))
        iface_name = iface_match.group(2)
        ext_text = _collapse_ws(iface_match.group(3))
        if not include_private and _is_jsts_private(iface_name):
            return ""
        prefix = f"{exp_prefix} " if exp_prefix else ""
        ext = f" extends {ext_text}" if ext_text else ""
        return f"{prefix}interface {iface_name}{ext} [L{lineno}]"
    for regex, kw in ((_RE_TYPE, "type"), (_RE_ENUM, "enum")):
        m = regex.match(line)
        if m:
            exp_prefix = _collapse_ws(m.group(1))
            name = m.group(2)
            if not include_private and _is_jsts_private(name):
                return ""
            prefix = f"{exp_prefix} " if exp_prefix else ""
            return f"{prefix}{kw} {name} [L{lineno}]"
    return None


def _match_jsts_func_decl(
    line: str, lineno: int, *, include_private: bool
) -> str | None:
    fn_match = _RE_FUNCTION.match(line)
    if fn_match:
        fn_name = fn_match.group(4)
        if not include_private and _is_jsts_private(fn_name):
            return ""
        exp_prefix = _collapse_ws(fn_match.group(1))
        async_kw = _collapse_ws(fn_match.group(2))
        star = fn_match.group(3) or ""
        params = _collapse_ws(fn_match.group(5))
        ret = _collapse_ws(fn_match.group(6))
        parts = [p for p in (exp_prefix, async_kw, f"function{star}") if p]
        ret_part = f": {ret}" if ret else ""
        return f"{' '.join(parts)} {fn_name}({params}){ret_part} [L{lineno}]"
    anon_match = _RE_DEFAULT_ANON_FUNCTION.match(line)
    if anon_match:
        async_kw = _collapse_ws(anon_match.group(1))
        star = anon_match.group(2) or ""
        params = _collapse_ws(anon_match.group(3))
        ret = _collapse_ws(anon_match.group(4))
        head = " ".join(p for p in ["export default", async_kw, f"function{star}"] if p)
        ret_part = f": {ret}" if ret else ""
        return f"{head}({params}){ret_part} [L{lineno}]"
    return None


def _match_jsts_var_func(
    line: str, lineno: int, *, include_private: bool
) -> str | None:
    arrow_match = _RE_ARROW.match(line)
    if arrow_match:
        var_name = arrow_match.group(2)
        if not include_private and _is_jsts_private(var_name):
            return ""
        exp_prefix = _collapse_ws(arrow_match.group(1))
        async_kw = _collapse_ws(arrow_match.group(3))
        params = _collapse_ws(arrow_match.group(4) or arrow_match.group(5))
        ret = _collapse_ws(arrow_match.group(6))
        prefix = f"{exp_prefix} " if exp_prefix else ""
        async_part = f"{async_kw} " if async_kw else ""
        ret_part = f": {ret}" if ret else ""
        return f"{prefix}const {var_name} = {async_part}({params}){ret_part} => [L{lineno}]"
    fexpr_match = _RE_FUNC_EXPR.match(line)
    if fexpr_match:
        var_name = fexpr_match.group(2)
        if not include_private and _is_jsts_private(var_name):
            return ""
        exp_prefix = _collapse_ws(fexpr_match.group(1))
        async_kw = _collapse_ws(fexpr_match.group(3))
        star = fexpr_match.group(4) or ""
        params = _collapse_ws(fexpr_match.group(5))
        ret = _collapse_ws(fexpr_match.group(6))
        prefix = f"{exp_prefix} " if exp_prefix else ""
        async_part = f"{async_kw} " if async_kw else ""
        ret_part = f": {ret}" if ret else ""
        return (
            f"{prefix}const {var_name} = {async_part}function{star}"
            f"({params}){ret_part} [L{lineno}]"
        )
    return None


def _match_jsts_toplevel(
    line: str, lineno: int, *, include_private: bool
) -> str | None:
    for matcher in (_match_jsts_type_decl, _match_jsts_func_decl, _match_jsts_var_func):
        res = matcher(line, lineno, include_private=include_private)
        if res is not None:
            return res
    return None


def _match_jsts_method(
    line: str, lineno: int, *, include_private: bool
) -> str | None:
    method_match = _RE_METHOD.match(line)
    if not method_match:
        return None
    modifiers = _collapse_ws(method_match.group(1))
    method_name = method_match.group(2)
    params = _collapse_ws(method_match.group(3))
    ret = _collapse_ws(method_match.group(4))
    if method_name in _JSTS_CONTROL_KEYWORDS:
        return None
    if not include_private and _is_jsts_private(method_name, modifiers):
        return None
    mod_part = f"{modifiers} " if modifiers else ""
    ret_part = f": {ret}" if ret else ""
    return f"  {mod_part}{method_name}({params}){ret_part} [L{lineno}]"


def _handle_jsts_class_match(
    cls_match: re.Match[str], lineno: int, *, include_private: bool
) -> tuple[str | None, bool]:
    exp_prefix = _collapse_ws(cls_match.group(1))
    cls_name = cls_match.group(2)
    base_name = _collapse_ws(cls_match.group(3))
    hidden = (not include_private) and _is_jsts_private(cls_name)
    if hidden:
        return None, True
    prefix = f"{exp_prefix} " if exp_prefix else ""
    ext = f" extends {base_name}" if base_name else ""
    return f"{prefix}class {cls_name}{ext} [L{lineno}]", False


def _extract_jsts_outline(source: str, *, include_private: bool) -> list[str]:
    lines: list[str] = []
    brace_depth = 0
    class_stack: list[tuple[int, bool]] = []
    pending_class: bool | None = None

    for lineno, line in enumerate(_blank_comments_and_strings(source).splitlines(), start=1):
        delta = line.count("{") - line.count("}")
        if pending_class is not None and line.count("{") > 0:
            class_stack.append((brace_depth + 1, pending_class))
            pending_class = None

        if brace_depth == 0:
            cls_match = _RE_CLASS.match(line)
            if cls_match:
                rendered, hidden = _handle_jsts_class_match(
                    cls_match, lineno, include_private=include_private
                )
                if rendered:
                    lines.append(rendered)
                if delta > 0:
                    class_stack.append((brace_depth + 1, hidden))
                elif line.count("{") == 0:
                    pending_class = hidden
            else:
                top_item = _match_jsts_toplevel(
                    line, lineno, include_private=include_private
                )
                if top_item:
                    lines.append(top_item)
        elif class_stack and brace_depth == class_stack[-1][0] and not class_stack[-1][1]:
            method_item = _match_jsts_method(
                line, lineno, include_private=include_private
            )
            if method_item:
                lines.append(method_item)

        brace_depth = max(0, brace_depth + delta)
        while class_stack and brace_depth < class_stack[-1][0]:
            class_stack.pop()
    return lines


def _extract_file_block(
    file_path: Path,
    *,
    display_base: Path,
    include_private: bool,
) -> str:
    rel_posix = _display_path(file_path, display_base)
    text, skip_reason = _read_source_text(file_path)
    if skip_reason is not None:
        outline_lines = [skip_reason]
    else:
        suffix = file_path.suffix.lower()
        if suffix in _PYTHON_EXTS:
            outline_lines = _extract_python_outline(
                text or "",
                file_path,
                include_private=include_private,
            )
        else:
            outline_lines = _extract_jsts_outline(
                text or "",
                include_private=include_private,
            )
        if not outline_lines:
            outline_lines = ["(no top-level symbols)"]
    return f"## {rel_posix}\n" + "\n".join(outline_lines)


def _build_static_notes(
    *,
    files_truncated: bool,
    max_files: int,
    depth_truncated: bool,
    max_depth: int,
    skipped_external: int,
) -> list[str]:
    notes: list[str] = []
    if files_truncated:
        notes.append(
            f"[File limit reached (max_files={max_files}): "
            "additional files were skipped.]"
        )
    if depth_truncated:
        notes.append(
            f"[Depth limit reached (max_depth={max_depth}): "
            "deeper subdirectories were skipped.]"
        )
    if skipped_external > 0:
        notes.append(
            f"[Skipped {skipped_external} file(s): outside workspace, "
            "unresolvable, or not a regular file.]"
        )
    return notes


def _fit_blocks_to_budget(blocks: list[str], body_budget: int) -> tuple[list[str], bool]:
    kept_blocks: list[str] = []
    used_chars = 0
    for block in blocks:
        sep = 2 if kept_blocks else 0
        if used_chars + sep + len(block) <= body_budget:
            kept_blocks.append(block)
            used_chars += sep + len(block)
            continue
        if not kept_blocks:
            slice_len = max(100, body_budget - len("\n[... truncated ...]"))
            kept_blocks.append(block[:slice_len] + "\n[... truncated ...]")
        return kept_blocks, True
    return kept_blocks, False


def _build_blocks(
    selected_files: list[Path],
    *,
    display_base: Path,
    include_private: bool,
    max_chars: int,
) -> tuple[list[str], str | None]:
    """Outline files until the total time or character budget is exhausted."""
    blocks: list[str] = []
    used_chars = 0
    started = time.monotonic()
    for file_path in selected_files:
        if blocks and used_chars >= max_chars:
            return blocks, _budget_note(len(blocks), len(selected_files), "character")
        if blocks and time.monotonic() - started > _DIR_TIME_BUDGET_SECONDS:
            return blocks, _budget_note(len(blocks), len(selected_files), "time")
        block = _extract_file_block(
            file_path,
            display_base=display_base,
            include_private=include_private,
        )
        blocks.append(block)
        used_chars += len(block) + 2
    return blocks, None


def _budget_note(done: int, total: int, kind: str) -> str:
    return f"[Outline {kind} budget reached: {done} of {total} files outlined.]"


def _render_output(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    *,
    header: str,
    blocks: list[str],
    max_chars: int,
    files_truncated: bool,
    max_files: int,
    depth_truncated: bool,
    max_depth: int,
    skipped_external: int,
    budget_note: str | None = None,
) -> str:
    static_notes = _build_static_notes(
        files_truncated=files_truncated,
        max_files=max_files,
        depth_truncated=depth_truncated,
        max_depth=max_depth,
        skipped_external=skipped_external,
    )
    if budget_note:
        static_notes.append(budget_note)
    if not blocks:
        return "\n\n".join([header, "(no supported source files found)", *static_notes])[:max_chars]

    body_budget = max(200, max_chars - len(header) - sum(len(n) + 2 for n in static_notes) - 96)
    kept_blocks, chars_truncated = _fit_blocks_to_budget(blocks, body_budget)
    notes = list(static_notes)
    if chars_truncated:
        notes.insert(
            0,
            f"[Truncated at max_chars={max_chars}: "
            f"{len(kept_blocks)} of {len(blocks)} files shown.]",
        )
    output = "\n\n".join([header, *kept_blocks, *notes])
    return output[:max_chars] if len(output) > max_chars else output


def _code_outline_sync(req: CodeOutlineRequest) -> str:
    runtime_paths = current_command_runtime_paths(require_runtime_cwd=False)
    resolved, roots = _resolve_target_path(req.path, runtime_paths)

    if resolved.is_file():
        if resolved.suffix.lower() not in _SUPPORTED_EXTS:
            ext = resolved.suffix or resolved.name
            raise ValueError(f"unsupported file type: {ext}")
        selected_files = [resolved]
        mode, display_base = "file", resolved.parent
        depth_truncated, files_truncated, skipped_external = False, False, 0
    elif resolved.is_dir():
        mode, display_base = "directory", resolved
        selected_files, depth_truncated, files_truncated, skipped_external = (
            _collect_directory_files(
                resolved,
                roots=roots,
                max_depth=req.max_depth,
                max_files=req.max_files,
            )
        )
    else:
        raise ValueError(f"unsupported path type: {req.path}")

    display_root = _display_path(resolved, runtime_paths.current_cwd)
    header = f"Code Outline: {display_root} | files: {len(selected_files)} | mode: {mode}"
    blocks, budget_note = _build_blocks(
        selected_files,
        display_base=display_base,
        include_private=req.include_private,
        max_chars=req.max_chars,
    )
    return _render_output(
        header=header,
        blocks=blocks,
        max_chars=req.max_chars,
        files_truncated=files_truncated,
        max_files=req.max_files,
        depth_truncated=depth_truncated,
        max_depth=req.max_depth,
        skipped_external=skipped_external,
        budget_note=budget_note,
    )


@tool(
    name="code_outline",
    description=(
        "Extract a compact structural outline of classes, functions, signatures, "
        "and docstring summaries from Python (.py/.pyi) and JS/TS "
        "(.js/.jsx/.ts/.tsx/.mjs/.cjs) files or directories in the workspace. "
        f"Defaults: path='.', max_depth={DEFAULT_MAX_DEPTH} (0-{_MAX_MAX_DEPTH}), "
        f"max_files={DEFAULT_MAX_FILES} (1-{_MAX_MAX_FILES}), "
        f"max_chars={DEFAULT_MAX_CHARS} ({_MIN_MAX_CHARS}-{_MAX_MAX_CHARS}), "
        "include_private=False. JS/TS outlines use a lightweight line scanner "
        "(not a full parser): class-field arrow functions, decorators, and "
        "multi-line signatures may be missed or truncated. "
        "Directory scans stop at a time/character budget with a note."
    ),
)
async def code_outline(
    path: str = ".",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_private: bool = False,
) -> str:
    """Extract a compact code outline for a workspace file or directory.

    Args:
        path: File or directory path inside the workspace (default '.').
        max_depth: Maximum directory recursion depth (0-10, default 3).
        max_files: Maximum number of source files to parse (1-200, default 50).
        max_chars: Maximum output character limit (1000-100000, default 30000).
        include_private: Include underscore-prefixed private symbols (default False).

    Returns:
        Compact structural outline text, or '[ERROR]: code_outline failed: ...'.
    """
    try:
        req = _normalize_request(
            path=path,
            max_depth=max_depth,
            max_files=max_files,
            max_chars=max_chars,
            include_private=include_private,
        )
        logger.info(
            "[code_outline] path=%s depth=%s files=%s chars=%s private=%s",
            req.path,
            req.max_depth,
            req.max_files,
            req.max_chars,
            req.include_private,
        )
        return await asyncio.to_thread(_code_outline_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: code_outline failed: {exc}"
