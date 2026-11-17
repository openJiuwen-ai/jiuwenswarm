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


def _collect_directory_files(
    root_dir: Path,
    *,
    roots: list[Path],
    max_depth: int,
    max_files: int,
) -> tuple[list[Path], bool, bool, int]:
    selected: list[Path] = []
    depth_truncated = False
    files_truncated = False
    skipped_external = 0

    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=True, followlinks=False):
        current = Path(dirpath)
        depth = len(current.relative_to(root_dir).parts)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in _PRUNED_DIRS:
                continue
            sub_path = current / dirname
            if sub_path.is_symlink():
                continue
            kept_dirs.append(dirname)

        if depth >= max_depth:
            if kept_dirs:
                depth_truncated = True
            dirnames[:] = []
        else:
            dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            file_path = current / filename
            if file_path.suffix.lower() not in _SUPPORTED_EXTS:
                continue
            try:
                resolved_file = file_path.resolve()
            except (OSError, RuntimeError):
                skipped_external += 1
                continue
            if not _is_within_roots(resolved_file, roots) or not resolved_file.is_file():
                skipped_external += 1
                continue
            if len(selected) >= max_files:
                files_truncated = True
                dirnames[:] = []
                return selected, depth_truncated, files_truncated, skipped_external
            selected.append(file_path)

    return selected, depth_truncated, files_truncated, skipped_external


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
            if ch == "/" and nxt == "/":
                out.append("  ")
                state = "line_comment"
                idx += 2
                continue
            if ch == "/" and nxt == "*":
                out.append("  ")
                state = "block_comment"
                idx += 2
                continue
            if ch == "'":
                out.append(" ")
                state = "single_quote"
                idx += 1
                continue
            if ch == '"':
                out.append(" ")
                state = "double_quote"
                idx += 1
                continue
            if ch == "`":
                out.append(" ")
                state = "template"
                idx += 1
                continue
            out.append(ch)
            idx += 1
            continue

        if state == "line_comment":
            if ch == "\n":
                out.append("\n")
                state = "code"
            else:
                out.append(" ")
            idx += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                out.append("  ")
                state = "code"
                idx += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            idx += 1
            continue

        if state in ("single_quote", "double_quote"):
            quote_char = "'" if state == "single_quote" else '"'
            if ch == "\\":
                if nxt == "\n":
                    out.append(" \n")
                    idx += 2
                elif nxt:
                    out.append("  ")
                    idx += 2
                else:
                    out.append(" ")
                    idx += 1
                continue
            if ch == quote_char:
                out.append(" ")
                state = "code"
                idx += 1
                continue
            if ch == "\n":
                out.append("\n")
                state = "code"
                idx += 1
                continue
            out.append(" ")
            idx += 1
            continue

        if state == "template":
            if ch == "\\":
                if nxt == "\n":
                    out.append(" \n")
                    idx += 2
                elif nxt:
                    out.append("  ")
                    idx += 2
                else:
                    out.append(" ")
                    idx += 1
                continue
            if ch == "`":
                out.append(" ")
                state = "code"
                idx += 1
                continue
            out.append("\n" if ch == "\n" else " ")
            idx += 1

    return "".join(out)


def _is_jsts_private(name: str, modifiers: str = "") -> bool:
    if name.startswith("_") or name.startswith("#"):
        return True
    return "private" in modifiers.split()


def _collapse_ws(text: str | None) -> str:
    return " ".join((text or "").split())


def _extract_jsts_outline(source: str, *, include_private: bool) -> list[str]:
    masked = _blank_comments_and_strings(source)
    lines: list[str] = []
    brace_depth = 0
    class_stack: list[tuple[int, bool]] = []
    pending_class: bool | None = None

    for lineno, line in enumerate(masked.splitlines(), start=1):
        depth_before = brace_depth
        open_count = line.count("{")
        close_count = line.count("}")
        depth_after = max(0, depth_before + open_count - close_count)

        if pending_class is not None and open_count > 0:
            class_stack.append((depth_before + 1, pending_class))
            pending_class = None

        in_class_body = bool(class_stack) and depth_before == class_stack[-1][0]
        skip_current_class = bool(class_stack) and class_stack[-1][1]

        if depth_before == 0:
            cls_match = _RE_CLASS.match(line)
            if cls_match:
                exp_prefix = _collapse_ws(cls_match.group(1))
                cls_name = cls_match.group(2)
                base_name = _collapse_ws(cls_match.group(3))
                hidden = (not include_private) and _is_jsts_private(cls_name)
                if not hidden:
                    prefix = f"{exp_prefix} " if exp_prefix else ""
                    ext = f" extends {base_name}" if base_name else ""
                    lines.append(f"{prefix}class {cls_name}{ext} [L{lineno}]")
                if open_count > close_count:
                    class_stack.append((depth_before + 1, hidden))
                else:
                    pending_class = hidden
                brace_depth = depth_after
                continue

            iface_match = _RE_INTERFACE.match(line)
            if iface_match:
                exp_prefix = _collapse_ws(iface_match.group(1))
                iface_name = iface_match.group(2)
                ext_text = _collapse_ws(iface_match.group(3))
                if include_private or not _is_jsts_private(iface_name):
                    prefix = f"{exp_prefix} " if exp_prefix else ""
                    ext = f" extends {ext_text}" if ext_text else ""
                    lines.append(f"{prefix}interface {iface_name}{ext} [L{lineno}]")
                brace_depth = depth_after
                continue

            type_match = _RE_TYPE.match(line)
            if type_match:
                exp_prefix = _collapse_ws(type_match.group(1))
                type_name = type_match.group(2)
                if include_private or not _is_jsts_private(type_name):
                    prefix = f"{exp_prefix} " if exp_prefix else ""
                    lines.append(f"{prefix}type {type_name} [L{lineno}]")
                brace_depth = depth_after
                continue

            enum_match = _RE_ENUM.match(line)
            if enum_match:
                exp_prefix = _collapse_ws(enum_match.group(1))
                enum_name = enum_match.group(2)
                if include_private or not _is_jsts_private(enum_name):
                    prefix = f"{exp_prefix} " if exp_prefix else ""
                    lines.append(f"{prefix}enum {enum_name} [L{lineno}]")
                brace_depth = depth_after
                continue

            fn_match = _RE_FUNCTION.match(line)
            if fn_match:
                exp_prefix = _collapse_ws(fn_match.group(1))
                async_kw = _collapse_ws(fn_match.group(2))
                star = fn_match.group(3) or ""
                fn_name = fn_match.group(4)
                params = _collapse_ws(fn_match.group(5))
                ret = _collapse_ws(fn_match.group(6))
                if include_private or not _is_jsts_private(fn_name):
                    parts = [p for p in (exp_prefix, async_kw, f"function{star}") if p]
                    ret_part = f": {ret}" if ret else ""
                    lines.append(
                        f"{' '.join(parts)} {fn_name}({params}){ret_part} [L{lineno}]"
                    )
                brace_depth = depth_after
                continue

            anon_match = _RE_DEFAULT_ANON_FUNCTION.match(line)
            if anon_match:
                async_kw = _collapse_ws(anon_match.group(1))
                star = anon_match.group(2) or ""
                params = _collapse_ws(anon_match.group(3))
                ret = _collapse_ws(anon_match.group(4))
                parts = ["export default", async_kw, f"function{star}"]
                head = " ".join(p for p in parts if p)
                ret_part = f": {ret}" if ret else ""
                lines.append(f"{head}({params}){ret_part} [L{lineno}]")
                brace_depth = depth_after
                continue

            arrow_match = _RE_ARROW.match(line)
            if arrow_match:
                exp_prefix = _collapse_ws(arrow_match.group(1))
                var_name = arrow_match.group(2)
                async_kw = _collapse_ws(arrow_match.group(3))
                params = _collapse_ws(arrow_match.group(4) or arrow_match.group(5))
                ret = _collapse_ws(arrow_match.group(6))
                if include_private or not _is_jsts_private(var_name):
                    prefix = f"{exp_prefix} " if exp_prefix else ""
                    async_part = f"{async_kw} " if async_kw else ""
                    ret_part = f": {ret}" if ret else ""
                    lines.append(
                        f"{prefix}const {var_name} = {async_part}({params})"
                        f"{ret_part} => [L{lineno}]"
                    )
                brace_depth = depth_after
                continue

            fexpr_match = _RE_FUNC_EXPR.match(line)
            if fexpr_match:
                exp_prefix = _collapse_ws(fexpr_match.group(1))
                var_name = fexpr_match.group(2)
                async_kw = _collapse_ws(fexpr_match.group(3))
                star = fexpr_match.group(4) or ""
                params = _collapse_ws(fexpr_match.group(5))
                ret = _collapse_ws(fexpr_match.group(6))
                if include_private or not _is_jsts_private(var_name):
                    prefix = f"{exp_prefix} " if exp_prefix else ""
                    async_part = f"{async_kw} " if async_kw else ""
                    ret_part = f": {ret}" if ret else ""
                    lines.append(
                        f"{prefix}const {var_name} = {async_part}function{star}"
                        f"({params}){ret_part} [L{lineno}]"
                    )
                brace_depth = depth_after
                continue

        elif in_class_body and not skip_current_class:
            method_match = _RE_METHOD.match(line)
            if method_match:
                modifiers = _collapse_ws(method_match.group(1))
                method_name = method_match.group(2)
                params = _collapse_ws(method_match.group(3))
                ret = _collapse_ws(method_match.group(4))
                if method_name not in _JSTS_CONTROL_KEYWORDS and (
                    include_private or not _is_jsts_private(method_name, modifiers)
                ):
                    mod_part = f"{modifiers} " if modifiers else ""
                    ret_part = f": {ret}" if ret else ""
                    lines.append(
                        f"  {mod_part}{method_name}({params}){ret_part} [L{lineno}]"
                    )

        brace_depth = depth_after
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


def _render_output(
    *,
    header: str,
    blocks: list[str],
    max_chars: int,
    files_truncated: bool,
    max_files: int,
    depth_truncated: bool,
    max_depth: int,
    skipped_external: int,
) -> str:
    static_notes: list[str] = []
    if files_truncated:
        static_notes.append(
            f"[File limit reached (max_files={max_files}): "
            "additional files were skipped.]"
        )
    if depth_truncated:
        static_notes.append(
            f"[Depth limit reached (max_depth={max_depth}): "
            "deeper subdirectories were skipped.]"
        )
    if skipped_external > 0:
        static_notes.append(
            f"[Skipped {skipped_external} symlink(s) pointing outside workspace.]"
        )

    if not blocks:
        parts = [header, "(no supported source files found)", *static_notes]
        return "\n\n".join(parts)[:max_chars]

    char_note_reserve = 96
    footer_len = sum(len(n) + 2 for n in static_notes) + char_note_reserve
    body_budget = max(200, max_chars - len(header) - footer_len)

    kept_blocks: list[str] = []
    used_chars = 0
    chars_truncated = False

    for block in blocks:
        sep = 2 if kept_blocks else 0
        if used_chars + sep + len(block) <= body_budget:
            kept_blocks.append(block)
            used_chars += sep + len(block)
            continue
        if not kept_blocks:
            slice_len = max(100, body_budget - len("\n[... truncated ...]"))
            kept_blocks.append(block[:slice_len] + "\n[... truncated ...]")
        chars_truncated = True
        break

    notes = list(static_notes)
    if chars_truncated:
        notes.insert(
            0,
            f"[Truncated at max_chars={max_chars}: "
            f"{len(kept_blocks)} of {len(blocks)} files shown.]",
        )

    parts = [header, *kept_blocks, *notes]
    output = "\n\n".join(parts)
    if len(output) > max_chars:
        output = output[:max_chars]
    return output


def _code_outline_sync(req: CodeOutlineRequest) -> str:
    runtime_paths = current_command_runtime_paths(require_runtime_cwd=False)
    resolved, roots = _resolve_target_path(req.path, runtime_paths)

    if resolved.is_file():
        if resolved.suffix.lower() not in _SUPPORTED_EXTS:
            ext = resolved.suffix or resolved.name
            raise ValueError(f"unsupported file type: {ext}")
        selected_files = [resolved]
        mode = "file"
        display_base = resolved.parent
        depth_truncated = False
        files_truncated = False
        skipped_external = 0
    elif resolved.is_dir():
        mode = "directory"
        display_base = resolved
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
    header = (
        f"Code Outline: {display_root} | "
        f"files: {len(selected_files)} | mode: {mode}"
    )
    blocks = [
        _extract_file_block(
            file_path,
            display_base=display_base,
            include_private=req.include_private,
        )
        for file_path in selected_files
    ]
    return _render_output(
        header=header,
        blocks=blocks,
        max_chars=req.max_chars,
        files_truncated=files_truncated,
        max_files=req.max_files,
        depth_truncated=depth_truncated,
        max_depth=req.max_depth,
        skipped_external=skipped_external,
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
        "include_private=False."
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
