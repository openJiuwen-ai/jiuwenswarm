#!/usr/bin/env python3
"""Inventory source symbols for project-maintainer coverage audits."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from symbol_fingerprints import normalize_hash, symbol_fingerprint


SOURCE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c-header",
    ".hpp": "cpp-header",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
}

EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".doc_project_maintainer",
    "node_modules",
    "vendor",
    "vendors",
    "dist",
    "build",
    "out",
    "target",
    "coverage",
    ".venv",
    "venv",
    "__pycache__",
}

GENERATED_NAME_PATTERNS = (
    ".min.js",
    ".bundle.js",
    ".generated.",
    ".g.",
    "_generated.",
    "generated_",
)

MULTI_AGENT_SOURCE_FILE_THRESHOLD = 20
MULTI_AGENT_SYMBOL_THRESHOLD = 80
MODULE_SYMBOL_THRESHOLD = 40
MAX_SYMBOLS_PER_SLICE = 60

AUDIT_STATUSES = ("unaudited", "script_assessed", "agent_audited", "human_audited", "audit_expired", "out_of_scope")
AUDITED_STATUSES = {"agent_audited", "human_audited"}
REQUIRED_ENTRY_DOC_KINDS = {"class", "function", "method"}
HEALTH_DIMENSIONS = (
    "overall",
    "name_behavior_match",
    "responsibility_focus",
    "length",
    "complexity",
    "implementation_soundness",
    "input_contract",
    "output_contract",
    "boundary_safety",
    "side_effects",
    "state_mutation",
    "error_handling",
    "dependency_coupling",
    "test_coverage",
    "observability",
    "performance_risk",
)

DEFAULT_HEALTH_AUDIT_SCOPE = "default_health_audit"
REPOSITORY_COVERAGE_ONLY_SCOPE = "repository_coverage_only"
DEFAULT_HEALTH_AUDIT_ROLES = {"runtime_source", "library_source"}

FIXTURE_PARTS = {
    "__fixtures__",
    "__mocks__",
    "__snapshots__",
    "fixture",
    "fixtures",
    "mock",
    "mocks",
    "snapshot",
    "snapshots",
    "test_data",
    "testdata",
}
TEST_PARTS = {"__tests__", "spec", "specs", "test", "tests"}
TEST_NAME_MARKERS = (
    ".spec.",
    ".test.",
    "_spec.",
    "_test.",
)
SCRIPT_PARTS = {"bin", "script", "scripts"}
TOOLING_PARTS = {
    ".github",
    "ci",
    "config",
    "configs",
    "devtools",
    "tool",
    "tooling",
    "tools",
}
DOC_PARTS = {"doc", "docs", "documentation", "example", "examples"}
RUNTIME_PARTS = {
    "api",
    "apis",
    "app",
    "apps",
    "backend",
    "client",
    "clients",
    "cmd",
    "commands",
    "daemon",
    "daemons",
    "frontend",
    "handler",
    "handlers",
    "route",
    "routes",
    "server",
    "servers",
    "service",
    "services",
    "web",
    "worker",
    "workers",
}
LIBRARY_PARTS = {"core", "internal", "lib", "libs", "libraries", "library", "package", "packages", "pkg", "src"}
PACKAGE_METADATA_NAMES = {
    "__about__.py",
    "__version__.py",
    "package.py",
    "setup.py",
}


def posix(path: Path) -> str:
    return path.as_posix()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="replace"), "decoded with replacement characters"
        except OSError as exc:
            return None, str(exc)
    except OSError as exc:
        return None, str(exc)


def git_ls_files(root: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if raw:
            files.append(root / raw.decode("utf-8", errors="replace"))
    return files


def run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def git_head(root: Path) -> str | None:
    output = run_git(root, "rev-parse", "HEAD")
    return output.strip() if output else None


def git_status_short(root: Path) -> list[str]:
    output = run_git(root, "status", "--short", "--untracked-files=all")
    if output is None:
        return []
    return [line for line in output.splitlines() if line.strip()]


def status_path(entry: str) -> str:
    path = entry[3:] if len(entry) > 3 else ""
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    return path.strip().strip('"')


def untracked_candidate_source_files(root: Path, status_entries: list[str]) -> list[str]:
    candidates: list[str] = []
    for entry in status_entries:
        if not entry.startswith("?? "):
            continue
        raw_path = status_path(entry)
        if not raw_path:
            continue
        relative = Path(raw_path)
        path = root / relative
        if not path.is_file() or not language_for(path):
            continue
        excluded, _reason = is_generated_or_excluded(relative)
        if excluded:
            continue
        candidates.append(posix(relative))
    return sorted(candidates)


def is_generated_or_excluded(relative: Path) -> tuple[bool, str | None]:
    parts = {part.lower() for part in relative.parts}
    excluded = parts.intersection(EXCLUDED_PARTS)
    if excluded:
        return True, f"excluded path component: {sorted(excluded)[0]}"

    name = relative.name.lower()
    if name.endswith(".d.ts"):
        return True, "type declaration file"
    for pattern in GENERATED_NAME_PATTERNS:
        if pattern in name:
            return True, f"generated filename pattern: {pattern}"
    return False, None


def language_for(path: Path) -> str | None:
    return SOURCE_EXTENSIONS.get(path.suffix.lower())


def directory_key(relative: Path) -> str:
    parent = relative.parent.as_posix()
    return "(root)" if parent == "." else parent


def increment_count(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def directory_summary_entry(entries: dict[str, dict[str, Any]], directory: str) -> dict[str, Any]:
    if directory not in entries:
        entries[directory] = {"directory": directory, "files": 0, "reasons": {}}
    return entries[directory]


def source_role_for(relative: Path) -> str:
    parts = [part.lower() for part in relative.parts]
    part_set = set(parts)
    name = relative.name.lower()

    if any(pattern in name for pattern in GENERATED_NAME_PATTERNS):
        return "generated"
    if name in PACKAGE_METADATA_NAMES:
        return "package_metadata"
    if part_set.intersection(FIXTURE_PARTS):
        return "fixture"
    if (
        part_set.intersection(TEST_PARTS)
        or name.startswith("test_")
        or any(marker in name for marker in TEST_NAME_MARKERS)
    ):
        return "test_source"
    if part_set.intersection(DOC_PARTS):
        return "docs"
    if part_set.intersection(SCRIPT_PARTS):
        return "script"
    if part_set.intersection(TOOLING_PARTS):
        return "tooling"
    if part_set.intersection(RUNTIME_PARTS):
        return "runtime_source"
    if part_set.intersection(LIBRARY_PARTS):
        return "library_source"
    return "library_source"


def audit_scope_for_role(source_role: str) -> str:
    if source_role in DEFAULT_HEALTH_AUDIT_ROLES:
        return DEFAULT_HEALTH_AUDIT_SCOPE
    return REPOSITORY_COVERAGE_ONLY_SCOPE


def stable_source_files(root: Path) -> tuple[list[Path], str, dict[str, Any]]:
    files = git_ls_files(root)
    source = "git ls-files"
    if files is None:
        source = "filesystem walk"
        files = [path for path in root.rglob("*") if path.is_file()]

    selected: list[Path] = []
    recorded: dict[str, dict[str, Any]] = {}
    excluded: dict[str, dict[str, Any]] = {}
    skipped_non_source: dict[str, dict[str, Any]] = {}
    for path in files:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        directory = directory_key(relative)
        is_excluded, reason = is_generated_or_excluded(relative)
        if is_excluded:
            entry = directory_summary_entry(excluded, directory)
            entry["files"] += 1
            increment_count(entry["reasons"], reason or "excluded")
            continue
        language = language_for(path)
        if not language:
            entry = directory_summary_entry(skipped_non_source, directory)
            entry["files"] += 1
            increment_count(entry["reasons"], "unsupported source extension")
            continue

        selected.append(path)
        source_role = source_role_for(relative)
        audit_scope = audit_scope_for_role(source_role)
        if directory not in recorded:
            recorded[directory] = {
                "directory": directory,
                "source_files": 0,
                "source_roles": {},
                "audit_scopes": {},
            }
        recorded_entry = recorded[directory]
        recorded_entry["source_files"] += 1
        increment_count(recorded_entry["source_roles"], source_role)
        increment_count(recorded_entry["audit_scopes"], audit_scope)

    directory_summary = {
        "listing_source": source,
        "totals": {
            "recorded_directories": len(recorded),
            "excluded_directories": len(excluded),
            "skipped_non_source_directories": len(skipped_non_source),
            "recorded_source_files": len(selected),
            "excluded_files": sum(item["files"] for item in excluded.values()),
            "skipped_non_source_files": sum(item["files"] for item in skipped_non_source.values()),
        },
        "recorded_directories": sorted(recorded.values(), key=lambda item: item["directory"]),
        "excluded_directories": sorted(excluded.values(), key=lambda item: item["directory"]),
        "skipped_non_source_directories": sorted(
            skipped_non_source.values(),
            key=lambda item: item["directory"],
        ),
        "notes": [
            "recorded_directories are stable source directories included in source-symbol-inventory.json.",
            "excluded_directories are omitted before language extraction because they match generated, vendored, build, dependency, or local-state exclusion rules.",
            "skipped_non_source_directories contain tracked files that did not match supported source extensions.",
        ],
    }
    return sorted(selected), source, directory_summary


def symbol(
    *,
    name: str,
    kind: str,
    line: int | None,
    end_line: int | None = None,
    class_name: str | None = None,
    confidence: str,
    extractor: str,
) -> dict[str, Any]:
    qualified = f"{class_name}.{name}" if class_name and kind == "method" else name
    item: dict[str, Any] = {
        "symbol": qualified,
        "name": name,
        "kind": kind,
        "line": line,
        "end_line": end_line,
        "confidence": confidence,
        "extractor": extractor,
    }
    if class_name:
        item["class"] = class_name
    return item


def extract_python(path: Path, text: str) -> tuple[list[dict[str, Any]], list[str], bool]:
    warnings: list[str] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [], [f"python ast parse failed: {exc}"], True

    symbols: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                symbol(
                    name=node.name,
                    kind="function",
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None),
                    confidence="confirmed",
                    extractor="python_ast",
                )
            )
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                symbol(
                    name=node.name,
                    kind="class",
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None),
                    confidence="confirmed",
                    extractor="python_ast",
                )
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        symbol(
                            name=item.name,
                            kind="method",
                            class_name=node.name,
                            line=item.lineno,
                            end_line=getattr(item, "end_lineno", None),
                            confidence="confirmed",
                            extractor="python_ast",
                        )
                    )
    return symbols, warnings, False


def extract_ctags(path: Path) -> tuple[list[dict[str, Any]], list[str], bool] | None:
    ctags = shutil.which("ctags")
    if not ctags:
        return None
    try:
        result = subprocess.run(
            [
                ctags,
                "--output-format=json",
                "--fields=+nK",
                "--extras=",
                "-f",
                "-",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"ctags failed: {exc}"], True
    if result.returncode != 0:
        return None

    symbols: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            warnings.append("ctags produced non-json output")
            continue
        kind = str(entry.get("kind", "")).lower()
        name = entry.get("name")
        if not name:
            continue
        line_number = entry.get("line")
        scope = entry.get("scope")
        scope_kind = str(entry.get("scopeKind", "")).lower()

        if kind in {"class", "struct", "interface", "trait"}:
            symbols.append(
                symbol(
                    name=str(name),
                    kind="class",
                    line=line_number,
                    confidence="tool",
                    extractor="ctags",
                )
            )
        elif kind in {"method", "member"} or scope_kind in {"class", "struct", "interface", "trait"}:
            if scope:
                symbols.append(
                    symbol(
                        name=str(name),
                        kind="method",
                        class_name=str(scope).split(".")[-1],
                        line=line_number,
                        confidence="tool",
                        extractor="ctags",
                    )
                )
        elif kind in {"function", "func", "procedure", "subroutine"}:
            symbols.append(
                symbol(
                    name=str(name),
                    kind="function",
                    line=line_number,
                    confidence="tool",
                    extractor="ctags",
                )
            )
    return symbols, warnings, False


CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")
FUNCTION_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
ASSIGNED_FUNCTION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
)
METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|async|override|final|open|suspend|\s)*([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:\{|=>)"
)
GO_FUNCTION_RE = re.compile(r"^\s*func\s+([A-Za-z_]\w*)\s*\(")
GO_METHOD_RE = re.compile(r"^\s*func\s+\([^)]*\*?\s*([A-Za-z_]\w*)\)\s+([A-Za-z_]\w*)\s*\(")
RUST_FUNCTION_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(")
RUST_IMPL_RE = re.compile(r"^\s*impl(?:<[^>]+>)?\s+([A-Za-z_]\w*)")
RUBY_CLASS_RE = re.compile(r"^\s*class\s+([A-Z]\w*)")
RUBY_DEF_RE = re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)")


def brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def extract_heuristic(path: Path, text: str, language: str) -> tuple[list[dict[str, Any]], list[str], bool]:
    symbols: list[dict[str, Any]] = []
    warnings = [f"heuristic extractor used for {language}; manual review required for current coverage"]
    class_stack: list[tuple[str, int]] = []
    impl_stack: list[tuple[str, int]] = []
    depth = 0

    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        while class_stack and depth < class_stack[-1][1]:
            class_stack.pop()
        while impl_stack and depth < impl_stack[-1][1]:
            impl_stack.pop()

        if language in {"javascript", "typescript", "java", "kotlin", "csharp", "php", "swift"}:
            class_match = CLASS_RE.match(line)
            if class_match:
                class_name = class_match.group(1)
                symbols.append(
                    symbol(name=class_name, kind="class", line=index, confidence="heuristic", extractor="heuristic")
                )
                class_stack.append((class_name, depth + 1))
            elif class_stack:
                method_match = METHOD_RE.match(line)
                if method_match and method_match.group(1) not in {"if", "for", "while", "switch", "catch"}:
                    symbols.append(
                        symbol(
                            name=method_match.group(1),
                            kind="method",
                            class_name=class_stack[-1][0],
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )
            elif depth == 0:
                function_match = FUNCTION_RE.match(line) or ASSIGNED_FUNCTION_RE.match(line)
                if function_match:
                    symbols.append(
                        symbol(
                            name=function_match.group(1),
                            kind="function",
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )
        elif language == "go":
            method_match = GO_METHOD_RE.match(line)
            if method_match:
                symbols.append(
                    symbol(
                        name=method_match.group(2),
                        kind="method",
                        class_name=method_match.group(1),
                        line=index,
                        confidence="heuristic",
                        extractor="heuristic",
                    )
                )
            else:
                function_match = GO_FUNCTION_RE.match(line)
                if function_match:
                    symbols.append(
                        symbol(
                            name=function_match.group(1),
                            kind="function",
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )
        elif language == "rust":
            impl_match = RUST_IMPL_RE.match(line)
            if impl_match:
                impl_stack.append((impl_match.group(1), depth + 1))
            function_match = RUST_FUNCTION_RE.match(line)
            if function_match:
                if impl_stack:
                    symbols.append(
                        symbol(
                            name=function_match.group(1),
                            kind="method",
                            class_name=impl_stack[-1][0],
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )
                elif depth == 0:
                    symbols.append(
                        symbol(
                            name=function_match.group(1),
                            kind="function",
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )
        elif language == "ruby":
            class_match = RUBY_CLASS_RE.match(line)
            if class_match:
                class_stack.append((class_match.group(1), len(line) - len(line.lstrip()) + 1))
                symbols.append(
                    symbol(name=class_match.group(1), kind="class", line=index, confidence="heuristic", extractor="heuristic")
                )
            def_match = RUBY_DEF_RE.match(line)
            if def_match:
                if class_stack:
                    symbols.append(
                        symbol(
                            name=def_match.group(1),
                            kind="method",
                            class_name=class_stack[-1][0],
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )
                else:
                    symbols.append(
                        symbol(
                            name=def_match.group(1),
                            kind="function",
                            line=index,
                            confidence="heuristic",
                            extractor="heuristic",
                        )
                    )

        if language != "ruby":
            depth += brace_delta(stripped)

    return symbols, warnings, True


def expected_doc_paths(relative: Path, item: dict[str, Any]) -> dict[str, str]:
    file_dir = Path("code") / relative
    file_doc = file_dir / f"{relative.name}.md"
    if item["kind"] == "function":
        entry_doc = file_dir / f"{item['name']}.md"
    elif item["kind"] == "method":
        class_name = item.get("class", "Unknown")
        entry_doc = file_dir / f"Class {class_name}" / f"{class_name}.{item['name']}.md"
    elif item["kind"] == "class":
        entry_doc = file_dir / f"Class {item['name']}" / f"Class {item['name']}.md"
    else:
        entry_doc = file_doc
    return {"file_doc": posix(file_doc), "entry_doc": posix(entry_doc)}


def heading_has_content(text: str, heading: str) -> bool:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return False
    rest = text[match.end() :]
    next_heading = re.search(r"^##\s+", rest, re.MULTILINE)
    content = rest[: next_heading.start()] if next_heading else rest
    return bool(content.strip())


def has_health(text: str) -> bool:
    return bool(re.search(r"(?m)^health:\s*$", text))


def scalar_value(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def split_key_value(text: str) -> tuple[str, Any] | None:
    if ":" not in text:
        return None
    key, value = text.split(":", 1)
    key = key.strip()
    if not key:
        return None
    return key, scalar_value(value)


def extract_frontmatter(text: str) -> list[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index]
    return []


def parse_indented_mapping(lines: list[str], start: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.startswith("  ") or line.startswith("    "):
            break
        parsed = split_key_value(line.strip())
        if parsed:
            key, value = parsed
            result[key] = value
        index += 1
    return result, index


def parse_indented_list(lines: list[str], start: int) -> tuple[list[dict[str, Any]], int]:
    result: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.startswith("  "):
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is not None:
                result.append(current)
            current = {}
            parsed = split_key_value(stripped[2:].strip())
            if parsed:
                key, value = parsed
                current[key] = value
        elif current is not None and line.startswith("    "):
            parsed = split_key_value(stripped)
            if parsed:
                key, value = parsed
                current[key] = value
        index += 1
    if current is not None:
        result.append(current)
    return result, index


def parse_frontmatter(text: str) -> dict[str, Any]:
    lines = extract_frontmatter(text)
    metadata: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.startswith(" "):
            index += 1
            continue
        parsed = split_key_value(line)
        if not parsed:
            index += 1
            continue
        key, value = parsed
        if value is None and index + 1 < len(lines) and lines[index + 1].startswith("  - "):
            items, index = parse_indented_list(lines, index + 1)
            metadata[key] = items
            continue
        if value is None and index + 1 < len(lines) and lines[index + 1].startswith("  "):
            mapping, index = parse_indented_mapping(lines, index + 1)
            metadata[key] = mapping
            continue
        metadata[key] = value
        index += 1
    return metadata


def normalize_health(metadata: dict[str, Any]) -> dict[str, str]:
    raw = metadata.get("health")
    health: dict[str, str] = {}
    raw_mapping = raw if isinstance(raw, dict) else {}
    for dimension in HEALTH_DIMENSIONS:
        value = raw_mapping.get(dimension)
        health[dimension] = str(value) if value not in {None, ""} else "unknown"
    return health


def normalize_issues(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("issues")
    if not isinstance(raw, list):
        return []
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        issue = {
            "id": str(item.get("id") or f"ISSUE-{index:03d}"),
            "dimension": str(item.get("dimension") or "unknown"),
            "severity": str(item.get("severity") or "unknown"),
            "status": str(item.get("status") or "open"),
            "summary": str(item.get("summary") or ""),
            "evidence": str(item.get("evidence") or ""),
            "suggested_action": str(item.get("suggested_action") or ""),
        }
        issues.append(issue)
    return issues


def normalize_doc_audit(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("audit")
    legacy_machine = metadata.get("machine_assessment")
    if not isinstance(raw, dict):
        if isinstance(legacy_machine, dict) and legacy_machine.get("status") == "script_assessed":
            return {
                "status": "script_assessed",
                "auditor": legacy_machine.get("tool"),
                "audited_at": legacy_machine.get("assessed_at"),
                "audited_commit": None,
                "audited_source_hash": legacy_machine.get("source_hash"),
                "audited_symbol_hash": None,
                "confidence": "unknown",
                "expired_reason": None,
                "downgrade_reason": "legacy_machine_assessment",
            }
        return None
    status = str(raw.get("status") or "unaudited")
    if status not in AUDIT_STATUSES:
        status = "unaudited"
    audit = {
        "status": status,
        "auditor": raw.get("auditor"),
        "audited_at": raw.get("audited_at"),
        "audited_commit": raw.get("audited_commit"),
        "audited_source_hash": raw.get("audited_source_hash"),
        "audited_symbol_hash": raw.get("audited_symbol_hash"),
        "confidence": raw.get("confidence"),
        "expired_reason": raw.get("expired_reason"),
    }
    if raw.get("downgrade_reason"):
        audit["downgrade_reason"] = raw.get("downgrade_reason")
    return audit


def verify_docs(root: Path, relative: Path, symbols: list[dict[str, Any]], docs_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "file_doc": posix(Path("code") / relative / f"{relative.name}.md"),
        "file_doc_exists": False,
        "missing_entry_docs": [],
        "missing_actual_role": [],
        "missing_health": [],
    }
    file_doc_path = docs_root / result["file_doc"]
    result["file_doc_exists"] = file_doc_path.exists()

    for item in symbols:
        docs = expected_doc_paths(relative, item)
        item["docs"] = docs
        entry_path = docs_root / docs["entry_doc"]
        item["doc_exists"] = entry_path.exists()
        item["has_actual_role"] = False
        item["has_health"] = False
        item["health"] = {dimension: "unknown" for dimension in HEALTH_DIMENSIONS}
        item["issues"] = []
        item["audit"] = None
        if not entry_path.exists():
            if item["kind"] in REQUIRED_ENTRY_DOC_KINDS:
                result["missing_entry_docs"].append(item["symbol"])
            continue
        text, error = read_text(entry_path)
        if error or text is None:
            if item["kind"] in REQUIRED_ENTRY_DOC_KINDS:
                result["missing_actual_role"].append(item["symbol"])
                result["missing_health"].append(item["symbol"])
            continue
        metadata = parse_frontmatter(text)
        item["health"] = normalize_health(metadata)
        item["issues"] = normalize_issues(metadata)
        item["audit"] = normalize_doc_audit(metadata)
        item["has_actual_role"] = heading_has_content(text, "Actual Role")
        item["has_health"] = has_health(text)
        if item["kind"] in REQUIRED_ENTRY_DOC_KINDS and not item["has_actual_role"]:
            result["missing_actual_role"].append(item["symbol"])
        if item["kind"] in REQUIRED_ENTRY_DOC_KINDS and not item["has_health"]:
            result["missing_health"].append(item["symbol"])
    return result


def inventory_file(path: Path, root: Path, docs_root: Path | None, verify: bool) -> dict[str, Any]:
    relative = path.relative_to(root)
    language = language_for(path)
    source_role = source_role_for(relative)
    audit_scope = audit_scope_for_role(source_role)
    text, read_warning = read_text(path)
    warnings: list[str] = []
    if read_warning:
        warnings.append(read_warning)
    if text is None or language is None:
        return {
            "path": posix(relative),
            "language": language,
            "source_role": source_role,
            "audit_scope": audit_scope,
            "sha256": None,
            "extractor": "none",
            "confidence": "unknown",
            "requires_review": True,
            "symbols": [],
            "warnings": warnings or ["file could not be read or language is unsupported"],
        }

    extractor = "heuristic"
    confidence = "heuristic"
    requires_review = True
    symbols: list[dict[str, Any]]

    if language == "python":
        symbols, extractor_warnings, requires_review = extract_python(path, text)
        warnings.extend(extractor_warnings)
        if requires_review:
            fallback_symbols, fallback_warnings, _ = extract_heuristic(path, text, language)
            if fallback_symbols:
                symbols = fallback_symbols
            warnings.extend(fallback_warnings)
            extractor = "heuristic"
            confidence = "heuristic"
        else:
            extractor = "python_ast"
            confidence = "confirmed"
    else:
        ctags_result = extract_ctags(path)
        if ctags_result is not None:
            symbols, extractor_warnings, requires_review = ctags_result
            warnings.extend(extractor_warnings)
            extractor = "ctags"
            confidence = "tool"
        else:
            symbols, extractor_warnings, requires_review = extract_heuristic(path, text, language)
            warnings.extend(extractor_warnings)
            extractor = "heuristic"
            confidence = "heuristic"

    for item in symbols:
        item["source_hash"], item["source_hash_mode"] = symbol_fingerprint(path, item, text)

    result: dict[str, Any] = {
        "path": posix(relative),
        "language": language,
        "source_role": source_role,
        "audit_scope": audit_scope,
        "sha256": file_hash(path),
        "extractor": extractor,
        "confidence": confidence,
        "requires_review": requires_review,
        "symbols": symbols,
        "warnings": warnings,
    }
    if verify and docs_root is not None:
        result["doc_verification"] = verify_docs(root, relative, symbols, docs_root)
    return result


def build_summary(files: list[dict[str, Any]], verify_docs_enabled: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "source_files": len(files),
        "symbols": sum(len(item["symbols"]) for item in files),
        "top_level_functions": 0,
        "classes": 0,
        "class_methods": 0,
        "requires_review_files": 0,
        "extractors": {},
        "source_roles": {},
        "audit_scopes": {},
        "default_health_audit_source_files": 0,
        "repository_coverage_only_source_files": 0,
        "default_health_audit_symbols": 0,
        "repository_coverage_only_symbols": 0,
        "missing_file_docs": 0,
        "missing_entry_docs": 0,
        "missing_actual_role": 0,
        "missing_health": 0,
    }
    for file_item in files:
        summary["extractors"][file_item["extractor"]] = summary["extractors"].get(file_item["extractor"], 0) + 1
        source_role = file_item.get("source_role") or "unknown"
        audit_scope = file_item.get("audit_scope") or REPOSITORY_COVERAGE_ONLY_SCOPE
        summary["source_roles"][source_role] = summary["source_roles"].get(source_role, 0) + 1
        summary["audit_scopes"][audit_scope] = summary["audit_scopes"].get(audit_scope, 0) + 1
        if audit_scope == DEFAULT_HEALTH_AUDIT_SCOPE:
            summary["default_health_audit_source_files"] += 1
            summary["default_health_audit_symbols"] += len(file_item["symbols"])
        else:
            summary["repository_coverage_only_source_files"] += 1
            summary["repository_coverage_only_symbols"] += len(file_item["symbols"])
        if file_item["requires_review"]:
            summary["requires_review_files"] += 1
        for item in file_item["symbols"]:
            if item["kind"] == "function":
                summary["top_level_functions"] += 1
            elif item["kind"] == "class":
                summary["classes"] += 1
            elif item["kind"] == "method":
                summary["class_methods"] += 1
        if verify_docs_enabled and "doc_verification" in file_item:
            verification = file_item["doc_verification"]
            if not verification["file_doc_exists"]:
                summary["missing_file_docs"] += 1
            summary["missing_entry_docs"] += len(verification["missing_entry_docs"])
            summary["missing_actual_role"] += len(verification["missing_actual_role"])
            summary["missing_health"] += len(verification["missing_health"])
    return summary


def relative_output_path(root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return posix(path.resolve().relative_to(root))
    except ValueError:
        return str(path.resolve())


def load_previous_coverage_map(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def previous_files_by_path(previous_map: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not previous_map:
        return {}
    files = previous_map.get("files")
    if not isinstance(files, list):
        return {}
    return {str(item.get("path")): item for item in files if isinstance(item, dict) and item.get("path")}


def doc_state(value: bool | None, verify_docs_enabled: bool) -> str:
    if not verify_docs_enabled:
        return "not_checked"
    return "present" if value else "missing"


def symbol_coverage_state(
    item: dict[str, Any],
    verify_docs_enabled: bool,
    source_role: str,
    audit_scope: str,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "symbol": item["symbol"],
        "name": item["name"],
        "kind": item["kind"],
        "line": item.get("line"),
        "source_role": source_role,
        "audit_scope": audit_scope,
        "confidence": item.get("confidence"),
        "extractor": item.get("extractor"),
    }
    if item.get("class"):
        state["class"] = item["class"]

    if item["kind"] not in REQUIRED_ENTRY_DOC_KINDS:
        state["doc_status"] = "not_required_for_closure"
        state["actual_role_status"] = "not_required_for_closure"
        state["health_status"] = "not_required_for_closure"
        return state

    docs = item.get("docs")
    if docs:
        state["entry_doc"] = docs.get("entry_doc")
    state["doc_status"] = doc_state(item.get("doc_exists"), verify_docs_enabled)
    state["actual_role_status"] = doc_state(item.get("has_actual_role"), verify_docs_enabled)
    state["health_status"] = doc_state(item.get("has_health"), verify_docs_enabled)
    return state


def required_symbol_is_documented(symbol_state: dict[str, Any]) -> bool:
    if symbol_state["kind"] not in REQUIRED_ENTRY_DOC_KINDS:
        return True
    return (
        symbol_state["doc_status"] == "present"
        and symbol_state["actual_role_status"] == "present"
        and symbol_state["health_status"] == "present"
    )


def file_coverage_status(
    file_item: dict[str, Any],
    previous_item: dict[str, Any] | None,
    verify_docs_enabled: bool,
) -> str:
    previous_hash = previous_item.get("source_hash") if previous_item else None
    if previous_hash and previous_hash != file_item.get("sha256"):
        return "stale"
    if file_item.get("requires_review"):
        return "pending_review"
    if not verify_docs_enabled:
        return "not_checked"

    verification = file_item.get("doc_verification", {})
    if not verification.get("file_doc_exists"):
        return "pending"
    if verification.get("missing_entry_docs") or verification.get("missing_actual_role") or verification.get("missing_health"):
        return "pending"
    return "documented"


def actionable_symbol_count(file_entry: dict[str, Any]) -> int:
    symbols = [item for item in file_entry["symbols"] if item["kind"] in REQUIRED_ENTRY_DOC_KINDS]
    if file_entry["status"] in {"stale", "pending_review", "not_checked"}:
        return len(symbols)
    return sum(1 for item in symbols if not required_symbol_is_documented(item))


def module_key(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "(root)"
    if len(parts) == 1:
        return "(root)"
    return parts[0]


def slice_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "root"


def count_status(files: list[dict[str, Any]], status: str) -> int:
    return sum(1 for item in files if item["status"] == status)


def build_suggested_slices(
    files: list[dict[str, Any]],
    *,
    scope: str,
    audit_scope_filter: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    actionable = [item for item in files if item["status"] != "documented"]
    if audit_scope_filter is not None:
        actionable = [item for item in actionable if item.get("audit_scope") == audit_scope_filter]
    groups: dict[str, list[dict[str, Any]]] = {}
    for file_entry in actionable:
        groups.setdefault(module_key(file_entry["path"]), []).append(file_entry)

    suggested: list[dict[str, Any]] = []
    assigned: dict[str, str] = {}
    for group_name in sorted(groups):
        chunk: list[dict[str, Any]] = []
        chunk_symbols = 0
        index = 1
        for file_entry in sorted(groups[group_name], key=lambda item: item["path"]):
            symbol_count = len(file_entry["symbols"])
            if chunk and chunk_symbols + symbol_count > MAX_SYMBOLS_PER_SLICE:
                slice_id = f"{slice_slug(group_name)}-{index:03d}"
                suggested.append(slice_entry(slice_id, group_name, chunk, scope))
                for item in chunk:
                    assigned[item["path"]] = slice_id
                index += 1
                chunk = []
                chunk_symbols = 0
            chunk.append(file_entry)
            chunk_symbols += symbol_count
        if chunk:
            slice_id = f"{slice_slug(group_name)}-{index:03d}"
            suggested.append(slice_entry(slice_id, group_name, chunk, scope))
            for item in chunk:
                assigned[item["path"]] = slice_id
    return suggested, assigned


def slice_entry(slice_id: str, group_name: str, files: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    statuses = sorted({item["status"] for item in files})
    source_roles = sorted({str(item.get("source_role") or "unknown") for item in files})
    audit_scopes = sorted({str(item.get("audit_scope") or REPOSITORY_COVERAGE_ONLY_SCOPE) for item in files})
    reason = (
        "Audit or review default runtime/library health targets before default product health audit can be current."
        if scope == DEFAULT_HEALTH_AUDIT_SCOPE
        else "Complete or review files before project-wide repository coverage can be current."
    )
    return {
        "id": slice_id,
        "kind": "module" if group_name != "(root)" else "root",
        "module": group_name,
        "scope": scope,
        "source_roles": source_roles,
        "audit_scopes": audit_scopes,
        "files": [item["path"] for item in files],
        "source_files": len(files),
        "symbols": sum(len(item["symbols"]) for item in files),
        "pending_symbols": sum(actionable_symbol_count(item) for item in files),
        "statuses": statuses,
        "reason": reason,
    }


def build_coverage_files(
    inventory_files: list[dict[str, Any]],
    previous_by_path: dict[str, dict[str, Any]],
    current_head: str | None,
    verify_docs_enabled: bool,
) -> list[dict[str, Any]]:
    coverage_files: list[dict[str, Any]] = []
    for file_item in inventory_files:
        previous_item = previous_by_path.get(file_item["path"])
        source_role = file_item.get("source_role") or "unknown"
        audit_scope = file_item.get("audit_scope") or audit_scope_for_role(source_role)
        symbols = [
            symbol_coverage_state(item, verify_docs_enabled, source_role, audit_scope)
            for item in file_item["symbols"]
        ]
        verification = file_item.get("doc_verification", {})
        file_entry: dict[str, Any] = {
            "path": file_item["path"],
            "language": file_item["language"],
            "source_role": source_role,
            "audit_scope": audit_scope,
            "source_hash": file_item.get("sha256"),
            "previous_hash": previous_item.get("source_hash") if previous_item else None,
            "last_scanned_commit": current_head,
            "extractor": file_item["extractor"],
            "confidence": file_item["confidence"],
            "requires_review": file_item["requires_review"],
            "status": file_coverage_status(file_item, previous_item, verify_docs_enabled),
            "file_doc": verification.get("file_doc"),
            "file_doc_status": doc_state(verification.get("file_doc_exists"), verify_docs_enabled),
            "symbols": symbols,
            "blockers": [],
        }
        if file_item.get("warnings"):
            file_entry["blockers"].extend(file_item["warnings"])
        if file_entry["status"] == "stale":
            file_entry["blockers"].append("source hash changed since previous coverage map")
        if file_entry["status"] == "pending_review":
            file_entry["blockers"].append("extractor confidence requires manual review before current coverage")
        coverage_files.append(file_entry)
    return coverage_files


def removed_coverage_files(
    previous_by_path: dict[str, dict[str, Any]],
    current_paths: set[str],
    current_head: str | None,
) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    for path, previous in sorted(previous_by_path.items()):
        if path in current_paths:
            continue
        removed.append(
            {
                "path": path,
                "status": "removed",
                "previous_hash": previous.get("source_hash"),
                "previous_status": previous.get("status"),
                "last_scanned_commit": current_head,
            }
        )
    return removed


def build_coverage_summary(
    files: list[dict[str, Any]],
    removed_files: list[dict[str, Any]],
    untracked_candidates: list[str],
) -> dict[str, Any]:
    required_symbols = [
        symbol
        for file_entry in files
        for symbol in file_entry["symbols"]
        if symbol["kind"] in REQUIRED_ENTRY_DOC_KINDS
    ]
    default_health_audit_symbols = [
        symbol
        for file_entry in files
        if file_entry.get("audit_scope") == DEFAULT_HEALTH_AUDIT_SCOPE
        for symbol in file_entry["symbols"]
        if symbol["kind"] in REQUIRED_ENTRY_DOC_KINDS
    ]
    repository_only_symbols = [
        symbol
        for file_entry in files
        if file_entry.get("audit_scope") != DEFAULT_HEALTH_AUDIT_SCOPE
        for symbol in file_entry["symbols"]
        if symbol["kind"] in REQUIRED_ENTRY_DOC_KINDS
    ]
    source_roles: dict[str, int] = {}
    audit_scopes: dict[str, int] = {}
    for file_entry in files:
        source_role = str(file_entry.get("source_role") or "unknown")
        audit_scope = str(file_entry.get("audit_scope") or REPOSITORY_COVERAGE_ONLY_SCOPE)
        source_roles[source_role] = source_roles.get(source_role, 0) + 1
        audit_scopes[audit_scope] = audit_scopes.get(audit_scope, 0) + 1
    documented_required_symbols = sum(1 for symbol in required_symbols if required_symbol_is_documented(symbol))
    documented_default_health_audit_symbols = sum(
        1 for symbol in default_health_audit_symbols if required_symbol_is_documented(symbol)
    )
    documented_repository_only_symbols = sum(1 for symbol in repository_only_symbols if required_symbol_is_documented(symbol))
    return {
        "source_files": len(files),
        "documented_files": count_status(files, "documented"),
        "pending_files": count_status(files, "pending"),
        "stale_files": count_status(files, "stale"),
        "pending_review_files": count_status(files, "pending_review"),
        "not_checked_files": count_status(files, "not_checked"),
        "removed_files": len(removed_files),
        "untracked_candidate_source_files": len(untracked_candidates),
        "source_roles": source_roles,
        "audit_scopes": audit_scopes,
        "required_symbols": len(required_symbols),
        "documented_required_symbols": documented_required_symbols,
        "pending_required_symbols": len(required_symbols) - documented_required_symbols,
        "default_health_audit_required_symbols": len(default_health_audit_symbols),
        "documented_default_health_audit_required_symbols": documented_default_health_audit_symbols,
        "pending_default_health_audit_required_symbols": len(default_health_audit_symbols)
        - documented_default_health_audit_symbols,
        "repository_coverage_only_required_symbols": len(repository_only_symbols),
        "documented_repository_coverage_only_required_symbols": documented_repository_only_symbols,
        "pending_repository_coverage_only_required_symbols": len(repository_only_symbols)
        - documented_repository_only_symbols,
    }


def build_coverage_map(
    *,
    root: Path,
    inventory: dict[str, Any],
    inventory_output_path: Path | None,
    coverage_output_path: Path | None,
    previous_map: dict[str, Any] | None,
    verify_docs_enabled: bool,
) -> dict[str, Any]:
    current_head = git_head(root)
    status_entries = git_status_short(root)
    untracked_candidates = untracked_candidate_source_files(root, status_entries)
    previous_by_path = previous_files_by_path(previous_map)
    coverage_files = build_coverage_files(
        inventory["files"],
        previous_by_path,
        current_head,
        verify_docs_enabled,
    )
    suggested_slices, assigned = build_suggested_slices(coverage_files, scope="repository_coverage")
    suggested_audit_slices, audit_assigned = build_suggested_slices(
        coverage_files,
        scope=DEFAULT_HEALTH_AUDIT_SCOPE,
        audit_scope_filter=DEFAULT_HEALTH_AUDIT_SCOPE,
    )
    for file_entry in coverage_files:
        file_entry["assigned_slice"] = assigned.get(file_entry["path"])
        file_entry["assigned_audit_slice"] = audit_assigned.get(file_entry["path"])

    current_paths = {item["path"] for item in coverage_files}
    removed_files = removed_coverage_files(previous_by_path, current_paths, current_head)
    module_symbol_counts: dict[str, int] = {}
    for file_entry in coverage_files:
        key = module_key(file_entry["path"])
        module_symbol_counts[key] = module_symbol_counts.get(key, 0) + len(file_entry["symbols"])

    max_module_symbols = max(module_symbol_counts.values(), default=0)
    recommended_mode = (
        "multi-agent"
        if len(coverage_files) > MULTI_AGENT_SOURCE_FILE_THRESHOLD
        or inventory["summary"]["symbols"] > MULTI_AGENT_SYMBOL_THRESHOLD
        or max_module_symbols > MODULE_SYMBOL_THRESHOLD
        else "single-agent"
    )
    return {
        "schema": "project-maintainer.coverage-map.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "git": {
            "head": current_head,
            "status_short": status_entries,
            "untracked_candidate_source_files": untracked_candidates,
        },
        "inventory": {
            "path": relative_output_path(root, inventory_output_path),
            "listing_source": inventory["listing_source"],
            "summary": inventory["summary"],
        },
        "directory_summary": inventory.get("directory_summary"),
        "thresholds": {
            "multi_agent_source_files": MULTI_AGENT_SOURCE_FILE_THRESHOLD,
            "multi_agent_symbols": MULTI_AGENT_SYMBOL_THRESHOLD,
            "module_symbols": MODULE_SYMBOL_THRESHOLD,
            "max_symbols_per_slice": MAX_SYMBOLS_PER_SLICE,
        },
        "recommended_mode": recommended_mode,
        "summary": build_coverage_summary(coverage_files, removed_files, untracked_candidates),
        "suggested_slices": suggested_slices,
        "suggested_audit_slices": suggested_audit_slices,
        "files": coverage_files,
        "removed_files": removed_files,
        "notes": [
            "Use suggested_slices for full repository coverage, including tests, scripts, tooling, fixtures, and package metadata.",
            "Use suggested_audit_slices for the default product/runtime health audit queue; tests remain verification evidence unless explicitly requested as audit targets.",
            "Use documented only when the file hash is current, extractor confidence is sufficient, and required class, function, and method docs include Actual Role plus health.",
        ],
        "coverage_output": relative_output_path(root, coverage_output_path),
    }


def symbol_record_id(source_path: str, item: dict[str, Any]) -> str:
    return f"{source_path}::{item['kind']}::{item['symbol']}"


def symbol_signature_hash(item: dict[str, Any]) -> str:
    signature = str(item.get("signature") or f"{item.get('kind')}|{item.get('class', '')}|{item.get('name')}")
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def previous_symbols_by_id(previous_map: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not previous_map:
        return {}
    symbols = previous_map.get("symbols")
    if not isinstance(symbols, list):
        return {}
    return {str(item.get("id")): item for item in symbols if isinstance(item, dict) and item.get("id")}


def default_audit_record(confidence: str | None = None) -> dict[str, Any]:
    return {
        "status": "unaudited",
        "auditor": None,
        "audited_at": None,
        "audited_commit": None,
        "audited_source_hash": None,
        "audited_symbol_hash": None,
        "confidence": confidence or "unknown",
        "expired_reason": None,
    }


def normalized_audit_record(
    audit: dict[str, Any] | None,
    *,
    current_source_hash: str | None,
    current_symbol_hash: str | None,
    current_commit: str | None,
    fallback_confidence: str | None,
) -> dict[str, Any]:
    if not audit:
        return default_audit_record(fallback_confidence)
    status = str(audit.get("status") or "unaudited")
    if status not in AUDIT_STATUSES:
        status = "unaudited"
    audited_source_hash = audit.get("audited_source_hash")
    audited_symbol_hash = audit.get("audited_symbol_hash")
    if status in AUDITED_STATUSES and not audited_source_hash and not audited_symbol_hash:
        audited_source_hash = current_source_hash
        audited_symbol_hash = current_symbol_hash
    audited_commit = audit.get("audited_commit")
    if status in AUDITED_STATUSES and not audited_commit:
        audited_commit = current_commit
    return {
        "status": status,
        "auditor": audit.get("auditor"),
        "audited_at": audit.get("audited_at"),
        "audited_commit": audited_commit,
        "audited_source_hash": audited_source_hash,
        "audited_symbol_hash": audited_symbol_hash,
        "confidence": audit.get("confidence") or fallback_confidence or "unknown",
        "expired_reason": audit.get("expired_reason"),
        **({"downgrade_reason": audit.get("downgrade_reason")} if audit.get("downgrade_reason") else {}),
    }


def resolved_symbol_audit(
    item: dict[str, Any],
    previous_symbol: dict[str, Any] | None,
    current_source_hash: str | None,
    current_symbol_hash: str | None,
    current_commit: str | None,
) -> dict[str, Any]:
    doc_audit = normalized_audit_record(
        item.get("audit"),
        current_source_hash=current_source_hash,
        current_symbol_hash=current_symbol_hash,
        current_commit=current_commit,
        fallback_confidence=item.get("confidence"),
    )
    previous_audit = None
    if previous_symbol and isinstance(previous_symbol.get("audit"), dict):
        previous_audit = normalized_audit_record(
            previous_symbol["audit"],
            current_source_hash=current_source_hash,
            current_symbol_hash=current_symbol_hash,
            current_commit=current_commit,
            fallback_confidence=item.get("confidence"),
        )

    audit = doc_audit
    if audit["status"] == "unaudited" and previous_audit:
        audit = previous_audit

    if audit["status"] in AUDITED_STATUSES:
        audited_symbol_hash = normalize_hash(audit.get("audited_symbol_hash"))
        normalized_symbol_hash = normalize_hash(current_symbol_hash)
        audited_source_hash = normalize_hash(audit.get("audited_source_hash"))
        normalized_source_hash = normalize_hash(current_source_hash)
        if audited_symbol_hash and normalized_symbol_hash and audited_symbol_hash != normalized_symbol_hash:
            audit = {
                **audit,
                "status": "audit_expired",
                "expired_reason": "symbol_hash_changed",
            }
        elif not audited_symbol_hash and audited_source_hash and normalized_source_hash:
            if audited_source_hash != normalized_source_hash:
                audit = {
                    **audit,
                    "status": "audit_expired",
                    "expired_reason": "source_hash_changed",
                }
            elif normalized_symbol_hash:
                audit["audited_symbol_hash"] = normalized_symbol_hash
    return audit


def symbol_doc_status(item: dict[str, Any], verify_docs_enabled: bool) -> dict[str, Any]:
    docs = item.get("docs") or {}
    return {
        "entry_doc": docs.get("entry_doc"),
        "doc_status": doc_state(item.get("doc_exists"), verify_docs_enabled),
        "actual_role_status": doc_state(item.get("has_actual_role"), verify_docs_enabled)
        if item["kind"] in REQUIRED_ENTRY_DOC_KINDS
        else "not_required_for_closure",
        "health_status": doc_state(item.get("has_health"), verify_docs_enabled)
        if item["kind"] in REQUIRED_ENTRY_DOC_KINDS
        else "not_required_for_closure",
    }


def symbol_audit_entry(
    file_item: dict[str, Any],
    item: dict[str, Any],
    previous_symbol: dict[str, Any] | None,
    current_commit: str | None,
    verify_docs_enabled: bool,
) -> dict[str, Any]:
    source_hash = file_item.get("sha256")
    current_symbol_hash = item.get("source_hash")
    record_id = symbol_record_id(file_item["path"], item)
    audit = resolved_symbol_audit(item, previous_symbol, source_hash, current_symbol_hash, current_commit)
    entry: dict[str, Any] = {
        "id": record_id,
        "symbol": item["symbol"],
        "name": item["name"],
        "kind": item["kind"],
        "source": file_item["path"],
        "source_role": file_item.get("source_role") or "unknown",
        "audit_scope": file_item.get("audit_scope") or REPOSITORY_COVERAGE_ONLY_SCOPE,
        "class": item.get("class"),
        "signature": item.get("signature"),
        "location": {
            "line": item.get("line"),
            "end_line": item.get("end_line"),
        },
        "source_fingerprint": {
            "commit": current_commit,
            "source_hash": source_hash,
            "symbol_hash": current_symbol_hash,
            "symbol_hash_mode": item.get("source_hash_mode"),
            "signature_hash": symbol_signature_hash(item),
        },
        "extractor": item.get("extractor"),
        "confidence": item.get("confidence"),
        "audit": audit,
        "health": item.get("health") or {dimension: "unknown" for dimension in HEALTH_DIMENSIONS},
        "issues": item.get("issues") or [],
        "docs": symbol_doc_status(item, verify_docs_enabled),
    }
    if entry["class"] is None:
        entry.pop("class")
    if entry["signature"] is None:
        entry.pop("signature")
    return entry


def build_symbol_audit_summary(symbols: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "symbols": len(symbols),
        "classes": 0,
        "top_level_functions": 0,
        "class_methods": 0,
        "unaudited": 0,
        "script_assessed": 0,
        "agent_audited": 0,
        "human_audited": 0,
        "audit_expired": 0,
        "out_of_scope": 0,
        "open_issues": 0,
    }
    for item in symbols:
        if item["kind"] == "class":
            summary["classes"] += 1
        elif item["kind"] == "function":
            summary["top_level_functions"] += 1
        elif item["kind"] == "method":
            summary["class_methods"] += 1
        status = item["audit"]["status"]
        if status in summary:
            summary[status] += 1
        summary["open_issues"] += sum(1 for issue in item.get("issues", []) if issue.get("status") != "closed")
    return summary

def count_symbols_by_field(symbols: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in symbols:
        value = str(item.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_symbol_audit_map(
    *,
    root: Path,
    inventory: dict[str, Any],
    inventory_output_path: Path | None,
    coverage_output_path: Path | None,
    audit_output_path: Path | None,
    previous_map: dict[str, Any] | None,
    verify_docs_enabled: bool,
) -> dict[str, Any]:
    current_commit = git_head(root)
    previous_symbols = previous_symbols_by_id(previous_map)
    symbols: list[dict[str, Any]] = []
    for file_item in inventory["files"]:
        for item in file_item["symbols"]:
            record_id = symbol_record_id(file_item["path"], item)
            symbols.append(
                symbol_audit_entry(
                    file_item,
                    item,
                    previous_symbols.get(record_id),
                    current_commit,
                    verify_docs_enabled,
                )
            )
    health_audit_symbols = [item for item in symbols if item.get("audit_scope") == DEFAULT_HEALTH_AUDIT_SCOPE]

    return {
        "schema": "project-maintainer.symbol-audit-map.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "git": {
            "head": current_commit,
            "status_short": git_status_short(root),
        },
        "inventory": {
            "path": relative_output_path(root, inventory_output_path),
            "coverage_map": relative_output_path(root, coverage_output_path),
            "summary": inventory["summary"],
        },
        "audit_output": relative_output_path(root, audit_output_path),
        "audit_statuses": list(AUDIT_STATUSES),
        "health_dimensions": list(HEALTH_DIMENSIONS),
        "source_role_summary": count_symbols_by_field(symbols, "source_role"),
        "audit_scope_summary": count_symbols_by_field(symbols, "audit_scope"),
        "summary": build_symbol_audit_summary(symbols),
        "health_audit_summary": build_symbol_audit_summary(health_audit_symbols),
        "symbols": symbols,
        "health_audit_symbols": health_audit_symbols,
        "notes": [
            "This map records repository-wide audit state, health snapshots, and issues for every discovered class, top-level function, and top-level class method.",
            "Use health_audit_summary and health_audit_symbols for the default product/runtime health audit; repository-only roles remain coverage and verification evidence unless explicitly requested.",
            "Agent and human audits expire when audited_symbol_hash differs from the current symbol hash; legacy records without a symbol hash use the file hash once for safe migration.",
            "script_assessed records controlled script processing only; it does not satisfy trusted agent, human, or out-of-scope audit closure.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path, help="Repository root to inventory")
    parser.add_argument("--output", type=Path, help="Write JSON inventory to this path")
    parser.add_argument("--coverage-map-output", type=Path, help="Write git-linked coverage map JSON to this path")
    parser.add_argument("--audit-map-output", type=Path, help="Write symbol audit map JSON to this path")
    parser.add_argument(
        "--docs-root",
        type=Path,
        help="Project-maintainer artifact root for doc verification; defaults to <repo>/.doc_project_maintainer",
    )
    parser.add_argument("--verify-docs", action="store_true", help="Check expected entry docs and required fields")
    parser.add_argument("--fail-on-review", action="store_true", help="Exit 1 when any file needs manual review")
    parser.add_argument("--fail-on-missing-docs", action="store_true", help="Exit 1 when required docs are missing")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    root = args.repo_root.resolve()
    docs_root = (args.docs_root or (root / ".doc_project_maintainer")).resolve()
    source_files, listing_source, directory_summary = stable_source_files(root)
    files = [inventory_file(path, root, docs_root, args.verify_docs) for path in source_files]
    summary = build_summary(files, args.verify_docs)
    inventory = {
        "schema": "project-maintainer.source-symbol-inventory.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(root),
        "listing_source": listing_source,
        "extractor_priority": ["python_ast", "ctags", "heuristic"],
        "available_extractors": {
            "python_ast": True,
            "ctags": shutil.which("ctags") is not None,
            "heuristic": True,
        },
        "summary": summary,
        "directory_summary": directory_summary,
        "files": files,
    }

    encoded = json.dumps(inventory, indent=2 if args.pretty else None, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)

    if args.coverage_map_output:
        previous_map = load_previous_coverage_map(args.coverage_map_output)
        coverage_map = build_coverage_map(
            root=root,
            inventory=inventory,
            inventory_output_path=args.output,
            coverage_output_path=args.coverage_map_output,
            previous_map=previous_map,
            verify_docs_enabled=args.verify_docs,
        )
        coverage_encoded = json.dumps(coverage_map, indent=2 if args.pretty else None, sort_keys=True)
        args.coverage_map_output.parent.mkdir(parents=True, exist_ok=True)
        args.coverage_map_output.write_text(coverage_encoded + "\n", encoding="utf-8")

    if args.audit_map_output:
        previous_audit_map = load_previous_coverage_map(args.audit_map_output)
        audit_map = build_symbol_audit_map(
            root=root,
            inventory=inventory,
            inventory_output_path=args.output,
            coverage_output_path=args.coverage_map_output,
            audit_output_path=args.audit_map_output,
            previous_map=previous_audit_map,
            verify_docs_enabled=args.verify_docs,
        )
        audit_encoded = json.dumps(audit_map, indent=2 if args.pretty else None, sort_keys=True)
        args.audit_map_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_map_output.write_text(audit_encoded + "\n", encoding="utf-8")

    should_fail = False
    if args.fail_on_review and summary["requires_review_files"]:
        should_fail = True
    if args.fail_on_missing_docs and (
        summary["missing_file_docs"]
        or summary["missing_entry_docs"]
        or summary["missing_actual_role"]
        or summary["missing_health"]
    ):
        should_fail = True
    return 1 if should_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
