#!/usr/bin/env python3
"""
Static compile-safety checker for MoeTracing instrumentation.

Catches common instrumentation errors that would cause compilation failures,
without requiring an actual compiler. Intended for environments where the
operator cannot be compiled locally.

Checks performed:
  1. Brace balance: every .h/.cpp file has matched { }
  2. Preprocessor balance: #if / #ifdef / #ifndef / #endif are paired
  3. Header reachability: files using MoeTracing transitively include the base header
  4. TRACE_POINT syntax: arguments are ("label", "B"|"E")
  5. Scope variable check: variables passed to MoeTracing (groupIdx etc.) exist in enclosing scope
  6. Profiling guard balance: #if ENABLE_MOE_PROFILING blocks are closed with #endif
  7. Kernel signature vs op_host output count consistency
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)


class CheckResult:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def error(self, file: str, line: int, msg: str) -> None:
        self.errors.append(f"ERROR {file}:{line}: {msg}")

    def warn(self, file: str, line: int, msg: str) -> None:
        self.warnings.append(f"WARN  {file}:{line}: {msg}")

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


COMMENT_LINE = re.compile(r"^\s*//")
STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


def strip_comments_and_strings(line: str) -> str:
    if COMMENT_LINE.match(line):
        return ""
    idx = line.find("//")
    if idx >= 0:
        line = line[:idx]
    return STRING_LITERAL.sub('""', line)


def check_brace_balance(path: Path, lines: List[str], result: CheckResult) -> None:
    depth = 0
    for i, raw_line in enumerate(lines, 1):
        line = strip_comments_and_strings(raw_line)
        depth += line.count("{") - line.count("}")
        if depth < 0:
            result.error(str(path), i, f"brace depth went negative ({depth}), likely extra '}}'")
            return
    if depth != 0:
        result.error(str(path), len(lines), f"brace imbalance: depth={depth} at end of file (expected 0)")


def check_preprocessor_balance(path: Path, lines: List[str], result: CheckResult) -> None:
    stack: List[Tuple[int, str]] = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r"#\s*(?:if|ifdef|ifndef)\b", stripped):
            stack.append((i, stripped))
        elif re.match(r"#\s*endif\b", stripped):
            if not stack:
                result.error(str(path), i, "unmatched #endif (no corresponding #if/#ifdef/#ifndef)")
                return
            stack.pop()
    for line_no, directive in stack:
        result.error(str(path), line_no, f"unclosed preprocessor directive: {directive}")


MOETRACING_CALL = re.compile(r"\bMoeTracing\b")
TRACE_POINT_RE = re.compile(r'TRACE_POINT\s*\(\s*"([^"]*?)"\s*,\s*"([^"]*?)"\s*\)')
MOETRACING_FULL = re.compile(
    r"MoeTracing(?:<[^>]*>)?\s*\(\s*TRACE_POINT\s*\([^)]*\)"
    r"(?:\s*,\s*(\w+))?(?:\s*,\s*(\w+))?\s*\)"
)
INCLUDE_RE = re.compile(r'#\s*include\s*"([^"]+)"')


def check_trace_point_syntax(path: Path, lines: List[str], result: CheckResult) -> None:
    for i, line in enumerate(lines, 1):
        if "TRACE_POINT" not in line:
            continue
        m = TRACE_POINT_RE.search(line)
        if not m:
            stripped = line.strip()
            if COMMENT_LINE.match(stripped):
                continue
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if stripped.startswith("#define") or stripped.startswith("#ifndef"):
                continue
            result.warn(str(path), i, f"TRACE_POINT found but could not parse arguments")
            continue
        label, event_type = m.group(1), m.group(2)
        if event_type not in ("B", "E"):
            result.error(str(path), i, f'TRACE_POINT event_type must be "B" or "E", got "{event_type}"')
        if not label:
            result.error(str(path), i, "TRACE_POINT label is empty")


def find_includes(path: Path, lines: List[str]) -> List[str]:
    includes = []
    for line in lines:
        m = INCLUDE_RE.match(line.strip())
        if m:
            includes.append(m.group(1))
    return includes


def resolve_include(from_file: Path, include_path: str) -> Path | None:
    candidate = from_file.parent / include_path
    if candidate.exists():
        return candidate.resolve()
    return None


def check_header_reachability(
    src_dir: Path, files: List[Path], result: CheckResult
) -> None:
    base_names = {"fused_deep_moe_base.h", "moe_base.h", "moe_dispatch_normal_base.h"}

    def has_base_transitively(path: Path, visited: set) -> bool:
        if path in visited:
            return False
        visited.add(path)
        if path.name in base_names:
            return True
        if not path.exists():
            return False
        lines = read_lines(path)
        for inc in find_includes(path, lines):
            resolved = resolve_include(path, inc)
            if resolved and has_base_transitively(resolved, visited):
                return True
        return False

    for f in files:
        lines = read_lines(f)
        uses_moetracing = any(MOETRACING_CALL.search(line) for line in lines)
        if not uses_moetracing:
            continue
        if not has_base_transitively(f, set()):
            result.error(
                str(f), 1,
                "file uses MoeTracing but does not transitively include a _base.h header "
                "that defines MoeTracing"
            )


COMMON_LOOP_VARS = {"groupIdx", "groupId", "loopIdx", "tokenIndex", "expertIdx", "stageId", "i", "j"}


def check_scope_variables(path: Path, lines: List[str], result: CheckResult) -> None:
    for i, line in enumerate(lines, 1):
        m = MOETRACING_FULL.search(line)
        if not m:
            continue
        extra_args = [a for a in (m.group(1), m.group(2)) if a]
        for arg in extra_args:
            if arg.isdigit():
                continue
            found = False
            search_start = max(0, i - 80)
            for j in range(search_start, i):
                if re.search(rf"\b{re.escape(arg)}\b", lines[j]):
                    ctx = strip_comments_and_strings(lines[j])
                    if re.search(rf"\b{re.escape(arg)}\b", ctx):
                        found = True
                        break
            if not found:
                result.warn(
                    str(path), i,
                    f'variable "{arg}" used in MoeTracing but not found in preceding 80 lines'
                )


def check_profiling_guard(path: Path, lines: List[str], result: CheckResult) -> None:
    in_guard = False
    guard_line = 0
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r"#\s*if\s+ENABLE_MOE_PROFILING\b", stripped):
            if in_guard:
                result.error(str(path), i, "nested #if ENABLE_MOE_PROFILING (likely missing #endif)")
            in_guard = True
            guard_line = i
        elif in_guard and re.match(r"#\s*endif\b", stripped):
            in_guard = False
    if in_guard:
        result.error(str(path), guard_line, "unclosed #if ENABLE_MOE_PROFILING block")


def check_kernel_output_consistency(op_dir: Path, result: CheckResult) -> None:
    kernel_cpp = op_dir / "op_kernel" / next(
        (f.name for f in (op_dir / "op_kernel").glob("*.cpp") if not f.name.startswith(".")),
        "",
    )
    host_cpp = op_dir / "op_host" / next(
        (f.name for f in (op_dir / "op_host").glob("*.cpp")
         if not f.name.startswith(".") and "tiling" not in f.name and "infer" not in f.name),
        "",
    )
    if not kernel_cpp.exists() or not host_cpp.exists():
        return

    kernel_lines = read_lines(kernel_cpp)
    host_lines = read_lines(host_cpp)

    kernel_gm_count = 0
    in_kernel_func = False
    for line in kernel_lines:
        if "extern" in line and "__global__" in line and "__aicore__" in line:
            in_kernel_func = True
        if in_kernel_func:
            kernel_gm_count += line.count("GM_ADDR")
            if ")" in line and "{" in line:
                break
            if line.strip() == ")":
                break

    host_output_count = sum(1 for host_line in host_lines if "this->Output(" in host_line)

    kernel_system_params = 2
    kernel_input_output = kernel_gm_count - kernel_system_params
    host_input_count = sum(1 for host_line in host_lines if "this->Input(" in host_line)
    expected_kernel_params = host_input_count + host_output_count + kernel_system_params

    if kernel_gm_count > 0 and expected_kernel_params != kernel_gm_count:
        result.warn(
            str(kernel_cpp), 1,
            f"kernel has {kernel_gm_count} GM_ADDR params, "
            f"but op_host registers {host_input_count} inputs + {host_output_count} outputs + 2 system = "
            f"{expected_kernel_params} (mismatch may indicate missing profiling_data parameter)"
        )


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Static compile-safety checker for MoeTracing instrumentation."
    )
    parser.add_argument("operator_dir", help="root directory of the operator (containing op_kernel/ and op_host/)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args()

    op_dir = Path(args.operator_dir)
    result = CheckResult()

    cpp_files = sorted(op_dir.rglob("*.cpp")) + sorted(op_dir.rglob("*.h")) + sorted(op_dir.rglob("*.hpp"))
    cpp_files = [f for f in cpp_files if "build_out" not in str(f) and "pregen" not in str(f)]

    for f in cpp_files:
        lines = read_lines(f)
        check_brace_balance(f, lines, result)
        check_preprocessor_balance(f, lines, result)
        check_trace_point_syntax(f, lines, result)
        check_scope_variables(f, lines, result)
        check_profiling_guard(f, lines, result)

    kernel_files = [f for f in cpp_files if "op_kernel" in str(f)]
    check_header_reachability(op_dir, kernel_files, result)

    check_kernel_output_consistency(op_dir, result)

    for w in result.warnings:
        logger.warning("%s", w)
    for e in result.errors:
        logger.error("%s", e)

    if result.ok and not (args.strict and result.warnings):
        logger.info(
            "\ncheck_compile_safety: PASSED (%d files, %d warnings, 0 errors)",
            len(cpp_files),
            len(result.warnings),
        )
    else:
        error_count = len(result.errors) + (len(result.warnings) if args.strict else 0)
        logger.info("\ncheck_compile_safety: FAILED (%d issues)", error_count)
        sys.exit(1)


if __name__ == "__main__":
    main()
