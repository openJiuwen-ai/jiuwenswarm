#!/usr/bin/env python3
"""
validate_swarmskill.py — Compliance checker for Swarm Skills (v0.1 spec).

Usage:
    python validate_swarmskill.py <path/to/swarmskill-name/>

Exit codes:
    0  = PASS (no errors; warnings may still print)
    1  = FAIL (one or more errors)
    2  = USAGE error (bad arguments, missing PyYAML, etc.)

Requires: PyYAML  (pip install pyyaml)

What this script catches (deterministic):
    - 5-file structure (SKILL.md, roles/, workflow.md, bind.md, dependencies.yaml)
    - Script-only SwarmFlow structure (SKILL.md + scripts/workflow.py only;
      prompts must be inline in the script)
    - Frontmatter required fields + types
    - `name` field equals directory name
    - roles/<id>.md exists for every roles[].id; no orphan role files
    - Each role file has the 5 mandatory sections (## Identity / Success Criteria /
      Boundary / Output Schema / Inline Persona for Teammate)
    - Each `## Identity` body's first non-blank line matches motto pattern `> *"..."*`
    - Each `## Boundary` contains both **Forbidden** and **Mandatory** markers
    - SKILL.md body has ## Workflow / ## Roles / ## Files
    - workflow.md has ## Overview / ## Detailed Steps / ## Acceptance Criteria
      and at least one ```mermaid``` block
    - bind.md has ## Resource Constraints / ## Behavioral Constraints / ## Failure Handling
    - dependencies.yaml has both `skills:` and `tools:` keys (even if empty)
    - Every roles[].skills / roles[].tools in SKILL.md appears in dependencies.yaml
    - scripts/workflow.py, when present, satisfies the executable SwarmFlow
      safety envelope: standalone shape, literal META, inline prompts, safe
      imports, phase/agent consistency, human/session call discipline,
      portable child-workflow paths and one-level composition, budget access
      discipline, permissive schemas, and blocked runtime patterns. The full
      authoring constraint list lives in
      templates/scripts/workflow.py.template.

What this script does NOT catch (judgment calls — see reference/compliance-checklist.md):
    - Whether mottos are mutually antagonistic (anti-convergence quality)
    - Whether Boundary lists really cover all sibling territories
    - Whether bind.md numbers actually match the workflow's needs
    - Whether the Swarm Skill is even justified vs a single-agent skill (Stage 0)
"""

from __future__ import annotations

import ast
import builtins
import sys
import re
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    logger.info("[USAGE ERROR] PyYAML is required. Install with: pip install pyyaml")
    raise


# ---------------------------------------------------------------------------
# Result accumulator
# ---------------------------------------------------------------------------

class Report:

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, file: str, msg: str) -> None:
        self.errors.append(f"  [ERROR] {file}: {msg}")

    def warn(self, file: str, msg: str) -> None:
        self.warnings.append(f"  [WARN]  {file}: {msg}")

    def passed(self) -> bool:
        return not self.errors

    def emit(self) -> None:
        if self.errors:
            logger.info("\nERRORS:")
            for e in self.errors:
                logger.info(e)
        if self.warnings:
            logger.info("\nWARNINGS:")
            for w in self.warnings:
                logger.info(w)
        if self.passed():
            logger.info("\n[PASS] %s warning(s), 0 error(s).", len(self.warnings))
        else:
            logger.info(
                "\n[FAIL] %s error(s), %s warning(s).",
                len(self.errors),
                len(self.warnings),
            )


# ---------------------------------------------------------------------------
# Frontmatter + section parsing helpers
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split a Markdown file into (frontmatter_dict, body_str)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter YAML parse error: {e}") from e
    return fm, m.group(2)


def find_h2_sections(body: str) -> dict[str, int]:
    """Return mapping of `## Section Title` -> line number (1-indexed)."""
    sections: dict[str, int] = {}
    for i, line in enumerate(body.splitlines(), 1):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            sections[m.group(1).strip()] = i
    return sections


def section_body(body: str, header: str) -> str | None:
    """Return the body text under `## <header>` up to the next `## ` heading or EOF."""
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if re.match(rf"^##\s+{re.escape(header)}\s*$", line):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^##\s+\S", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Per-file validators
# ---------------------------------------------------------------------------

def validate_skill_md(path: Path, report: Report, script_only: bool = False) -> tuple[dict | None, list[str]]:
    """Validate <root>/SKILL.md. Returns (frontmatter, role_ids_declared)."""
    if not path.exists():
        report.err("SKILL.md", "missing — required for every Swarm Skill")
        return None, []

    text = path.read_text(encoding="utf-8")

    try:
        fm, body = split_frontmatter(text)
    except ValueError as e:
        report.err("SKILL.md", str(e))
        return None, []

    if fm is None:
        report.err("SKILL.md", "missing YAML frontmatter (--- ... ---)")
        return None, []

    # Required frontmatter fields. Script-only SwarmFlow skills do not need
    # roles[] because the script is the execution definition.
    required_fields = ("name", "description", "version", "kind")
    if not script_only:
        required_fields += ("roles",)
    for field in required_fields:
        if field not in fm:
            report.err("SKILL.md", f"frontmatter missing required field `{field}`")

    if fm.get("kind") != "swarm-skill":
        report.err("SKILL.md", f"frontmatter `kind` must be `swarm-skill` (got: {fm.get('kind')!r})")

    # name == directory name
    dir_name = path.parent.name
    if fm.get("name") != dir_name:
        report.err("SKILL.md", f"frontmatter `name` ({fm.get('name')!r}) must equal directory name ({dir_name!r})")

    # description discipline (the trigger blob the model reads BEFORE loading the body)
    desc = fm.get("description")
    if isinstance(desc, str) and desc.strip():
        desc_chars = len(desc.strip())
        desc_lines = [ln for ln in desc.strip().splitlines() if ln.strip()]
        if desc_chars > 500:
            report.warn(
                "SKILL.md",
                f"description is {desc_chars} chars — HARD CAP is 500 (platform limit: 1024). "
                "Strip synonym enumeration / scope thresholds / Stage-0 rationale; see "
                "../swarmskill-creator/SKILL.md Stage 5 for the discipline rules.",
            )
        if len(desc_lines) > 4:
            report.warn(
                "SKILL.md",
                f"description has {len(desc_lines)} non-blank lines — HARD CAP is 4. "
                "Use 3-line structure: WHAT / WHEN / NOT.",
            )
        if re.search(r"^\s*Triggers\s*:", desc, re.MULTILINE | re.IGNORECASE):
            report.warn(
                "SKILL.md",
                "description contains a `Triggers:` line — DEPRECATED. Merge synonym phrases "
                "into one natural-language `Use when` sentence; semantic match handles variants.",
            )

    # roles[] structure
    role_ids: list[str] = []
    roles = fm.get("roles", [])
    if script_only:
        if "roles" in fm and roles not in (None, []):
            report.err("SKILL.md", "script-only SwarmFlow skills must omit `roles` or set `roles: []`")
        roles = []
    elif not isinstance(roles, list) or (not roles and not script_only):
        report.err("SKILL.md", "frontmatter `roles` must be a non-empty list")
    else:
        for i, role in enumerate(roles):
            if not isinstance(role, dict):
                report.err("SKILL.md", f"roles[{i}] must be an object")
                continue
            if "id" not in role:
                report.err("SKILL.md", f"roles[{i}] missing required field `id`")
            else:
                role_ids.append(role["id"])
            if "kind" not in role:
                report.err("SKILL.md", f"roles[{i}] (id={role.get('id')!r}) missing required field `kind`")
            elif role["kind"] not in ("ai_agent", "human_agent"):
                report.err(
                    "SKILL.md",
                    f"roles[{i}] (id={role.get('id')!r}) `kind` must be "
                    f"`ai_agent` or `human_agent` (got: {role['kind']!r})",
                )
            if "purpose" not in role:
                report.err("SKILL.md", f"roles[{i}] (id={role.get('id')!r}) missing required field `purpose`")
            else:
                # Per-role purpose is a 1-line summary; details go in roles/<id>.md
                purpose = role.get("purpose") or ""
                if isinstance(purpose, str) and len(purpose) > 150:
                    report.warn(
                        "SKILL.md",
                        f"roles[{i}] (id={role.get('id')!r}) `purpose` is {len(purpose)} "
                        f"chars — HARD CAP is 150. Move detail into roles/{role.get('id')}.md.",
                    )

    # Check if all AI roles have empty skills and tools (suggests auto-matching was skipped).
    # Human-agent roles are excluded — they legitimately have no skills/tools.
    if isinstance(roles, list) and roles:
        ai_roles = [r for r in roles if isinstance(r, dict) and r.get("kind", "ai_agent") != "human_agent"]
        all_empty = ai_roles and all(
            (not role.get("skills")) and (not role.get("tools"))
            for role in ai_roles
        )
        if all_empty and len(role_ids) > 0:
            report.warn(
                "SKILL.md",
                "all roles have empty `skills` and `tools` — was Stage 2 auto-matching run? "
                "Empty lists are valid only when the local scan confirms no matches. "
                "Run the auto-matching sub-step in Stage 2 before declaring no dependencies.",
            )

    # Body required sections
    sections = find_h2_sections(body)
    required_body_sections = ("Workflow", "Files") if script_only else ("Workflow", "Roles", "Files")
    for required in required_body_sections:
        if required not in sections:
            report.err("SKILL.md", f"body missing required section `## {required}`")

    # Anti-pattern: redundant sections that the description / workflow.md already cover
    for deprecated in ("When to Use", "Anti-Patterns", "Output Format"):
        if deprecated in sections:
            report.warn(
                "SKILL.md",
                f"body contains `## {deprecated}` which is already covered by `description` "
                "or `workflow.md` — remove to avoid duplication",
            )

    # Anti-pattern: mermaid in SKILL.md body (redundant with workflow.md).
    # Script-only skills have no workflow.md, so this warning does not apply.
    if not script_only and re.search(r"```mermaid", body):
        report.warn(
            "SKILL.md",
            "body contains a ```mermaid``` block — keep mermaid only in workflow.md "
            "to avoid duplication",
        )

    return fm, role_ids


def validate_role_file(path: Path, role_id: str, report: Report) -> dict[str, list[str]]:
    """Validate one roles/<id>.md.

    Returns extracted role-level skills/tools (currently unused — declared in
    SKILL.md frontmatter).
    """
    file_label = f"roles/{role_id}.md"
    if not path.exists():
        report.err(file_label, f"missing — required because SKILL.md frontmatter declares roles[].id = {role_id!r}")
        return {}

    text = path.read_text(encoding="utf-8")
    sections = find_h2_sections(text)

    required_sections = (
        "Identity",
        "Success Criteria",
        "Boundary",
        "Output Schema",
        "Inline Persona for Teammate",
    )
    for s in required_sections:
        if s not in sections:
            report.err(file_label, f"missing required section `## {s}`")

    # Identity must start with a 1-line motto: > *"..."*
    identity_body = section_body(text, "Identity")
    if identity_body is not None:
        first_nonblank = next((ln.strip() for ln in identity_body.splitlines() if ln.strip()), "")
        # Accept either > *"..."* or > *'...'*  (italic blockquote with quotes)
        motto_pattern = re.compile(r'^>\s*\*["\'].+["\']\*\s*$')
        if not motto_pattern.match(first_nonblank):
            report.err(
                file_label,
                "## Identity first non-blank line must be a 1-line motto in "
                f"`> *\"...\"*` format. Got: {first_nonblank[:80]!r}",
            )

    # Boundary must contain BOTH Forbidden and Mandatory markers
    # Accept common variations: **Forbidden**, **禁止**, **Forbidden actions**, etc.
    boundary_body = section_body(text, "Boundary")
    if boundary_body is not None:
        has_forbidden = re.search(r"\*\*(Forbidden|禁止)\*\*", boundary_body) is not None
        has_mandatory = re.search(r"\*\*(Mandatory|必须|必做)\*\*", boundary_body) is not None
        if not has_forbidden:
            report.err(file_label, "## Boundary missing `**Forbidden**` (or `**禁止**`) block")
        if not has_mandatory:
            report.err(file_label, "## Boundary missing `**Mandatory**` (or `**必须**` / `**必做**`) block")

    # Inline Persona must be non-trivially long (sanity check — empty section is the
    # most common authoring error)
    persona_body = section_body(text, "Inline Persona for Teammate")
    if persona_body is not None and len(persona_body.strip()) < 100:
        report.warn(
            file_label,
            f"## Inline Persona for Teammate is suspiciously short "
            f"({len(persona_body.strip())} chars). "
            "It must be a self-contained pasteable prompt — see reference/role-design.md.",
        )

    # Anti-pattern: section names that should be merged into the 5 mandatory sections
    for deprecated in ("Mindset", "Priorities", "Inspection Process", "MUST-Find Rule", "Inline Persona for Sub-Agent"):
        if deprecated in sections:
            if deprecated == "Inline Persona for Sub-Agent":
                report.err(
                    file_label,
                    "uses old `## Inline Persona for Sub-Agent` heading — rename to "
                    "`## Inline Persona for Teammate`",
                )
            else:
                report.warn(
                    file_label,
                    f"contains `## {deprecated}` — merge it into one of the 5 mandatory "
                    "sections (Identity / Success Criteria / Boundary / Output Schema / "
                    "Inline Persona for Teammate)",
                )

    return {}


def validate_workflow_md(path: Path, report: Report) -> None:
    if not path.exists():
        report.err("workflow.md", "missing — required by the 5-file structure")
        return

    text = path.read_text(encoding="utf-8")
    sections = find_h2_sections(text)

    for required in ("Overview", "Detailed Steps", "Acceptance Criteria"):
        if required not in sections:
            report.err("workflow.md", f"missing required section `## {required}`")

    # Mermaid is required in workflow.md — the primary expression difference from single-agent skills
    if not re.search(r"```mermaid", text):
        report.err(
            "workflow.md",
            "missing ```mermaid``` block — workflow.md must contain at least one "
            "mermaid diagram",
        )


def validate_bind_md(path: Path, report: Report) -> None:
    if not path.exists():
        report.err("bind.md", "missing — required by the 5-file structure")
        return

    text = path.read_text(encoding="utf-8")
    sections = find_h2_sections(text)

    for required in ("Resource Constraints", "Behavioral Constraints", "Failure Handling"):
        if required not in sections:
            report.err("bind.md", f"missing required section `## {required}`")

    # Resource Constraints should mention the 3 baseline items
    rc_body = section_body(text, "Resource Constraints")
    if rc_body is not None:
        for required_item in ("max_parallel_teammates", "total_wall_clock_budget", "total_token_budget"):
            if required_item not in rc_body:
                report.warn(
                    "bind.md",
                    f"## Resource Constraints does not mention `{required_item}` — "
                    "recommended as a baseline item",
                )


def validate_dependencies_yaml(path: Path, report: Report) -> dict[str, list[str]]:
    """Returns {'skills': [names], 'tools': [names]} for cross-file consistency check."""
    if not path.exists():
        report.err(
            "dependencies.yaml",
            "missing — required by the 5-file structure (write `skills: []` / "
            "`tools: []` if no deps)",
        )
        return {"skills": [], "tools": []}

    text = path.read_text(encoding="utf-8")

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        report.err("dependencies.yaml", f"YAML parse error: {e}")
        return {"skills": [], "tools": []}

    if not isinstance(data, dict):
        report.err("dependencies.yaml", "root must be a mapping with `skills` and `tools` keys")
        return {"skills": [], "tools": []}

    declared = {"skills": [], "tools": []}

    for segment in ("skills", "tools"):
        if segment not in data:
            report.err("dependencies.yaml", f"missing required `{segment}:` segment (write `{segment}: []` if empty)")
            continue

        entries = data[segment]
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            report.err("dependencies.yaml", f"`{segment}:` must be a list (or `[]`)")
            continue

        required_fields = {
            "skills": ("name", "source", "required", "purpose"),
            "tools": ("name", "required", "purpose"),
        }[segment]

        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                report.err("dependencies.yaml", f"{segment}[{i}] must be an object")
                continue
            for field in required_fields:
                if field not in entry:
                    report.err(
                        "dependencies.yaml",
                        f"{segment}[{i}] (name={entry.get('name')!r}) missing required "
                        f"field `{field}`",
                    )
            if segment in declared and "name" in entry:
                declared[segment].append(entry["name"])

    return declared


# ---------------------------------------------------------------------------
# SwarmFlow script validator
# ---------------------------------------------------------------------------

SUPPORTED_SWARMFLOW_OPERATORS = {
    "agent",
    "agent_session",
    "budget",
    "compact",
    "flatten_filter",
    "human",
    "human_session",
    "log",
    "map_parallel",
    "parallel",
    "phase",
    "pipeline",
    "pmap",
    "workflow",
}

SUPPORTED_SWARMFLOW_TYPES = {"AgentSession", "HumanSession"}
PROTECTED_SWARMFLOW_NAMES = SUPPORTED_SWARMFLOW_OPERATORS
DEFERRED_THUNK_OPERATORS = {"map_parallel", "parallel", "pipeline", "pmap"}
NON_DETERMINISTIC_IMPORTS = {"random", "time", "datetime"}
FILESYSTEM_CALLS = {"open"}
PATH_IO_METHODS = {"read_text", "write_text", "read_bytes", "write_bytes", "open"}
ASYNCIO_ORCHESTRATION_CALLS = {"gather", "create_task"}


def _is_literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError):
        return False


def _string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_or_none(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _set_parent_links(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            setattr(child, "_parent", parent)


def _parent(node: ast.AST) -> ast.AST | None:
    parent = getattr(node, "_parent", None)
    return parent if isinstance(parent, ast.AST) else None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        parts = [node.func.attr]
        value = node.func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
            return ".".join(reversed(parts))
    return None


def _is_inside_deferred_lambda(node: ast.AST) -> bool:
    current = _parent(node)
    while current is not None:
        if isinstance(current, ast.Lambda):
            lambda_node = current
            container = _parent(lambda_node)
            while container is not None:
                if isinstance(container, ast.Call):
                    return _call_name(container) in DEFERRED_THUNK_OPERATORS
                if isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return False
                container = _parent(container)
            return False
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        current = _parent(current)
    return False


def _is_directly_awaited(node: ast.AST) -> bool:
    current = _parent(node)
    while isinstance(current, ast.Attribute):
        current = _parent(current)
    return isinstance(current, ast.Await)


def _first_effective_statement(body: list[ast.stmt]) -> ast.stmt | None:
    for stmt in body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        return stmt
    return None


def _run_starts_with_parse_args(run_node: ast.AsyncFunctionDef) -> bool:
    stmt = _first_effective_statement(run_node.body)
    if not isinstance(stmt, ast.Assign):
        return False
    if len(stmt.targets) != 1:
        return False
    target = stmt.targets[0]
    value = stmt.value
    return (
        isinstance(target, ast.Name)
        and target.id == "args"
        and isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "parse_args"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == "args"
    )


class _AgentCallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[ast.Call] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "agent":
            self.calls.append(node)
        self.generic_visit(node)


def _phase_call_info(stmt: ast.stmt) -> tuple[bool, str | None, int | str]:
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False, None, "?"
    call = stmt.value
    if not isinstance(call.func, ast.Name) or call.func.id != "phase":
        return False, None, "?"
    if not call.args:
        return True, None, getattr(call, "lineno", "?")
    return True, _string_literal(call.args[0]), getattr(call, "lineno", "?")


def _phase_titles_from_meta(meta: dict[str, Any]) -> set[str]:
    phases = meta.get("phases")
    if not isinstance(phases, list):
        return set()
    return {
        phase["title"]
        for phase in phases
        if isinstance(phase, dict) and isinstance(phase.get("title"), str)
    }


def _agent_phase_mismatches(
    run_node: ast.AsyncFunctionDef,
    allowed_phases: set[str] | None = None,
) -> list[str]:
    mismatches: list[str] = []
    current_phase: str | None = None

    for stmt in run_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        is_phase_call, phase_value, phase_line = _phase_call_info(stmt)
        if is_phase_call:
            if phase_value is None:
                mismatches.append(f"line {phase_line}: phase() must use a string literal")
            else:
                current_phase = phase_value
                if allowed_phases is not None and phase_value not in allowed_phases:
                    mismatches.append(
                        f"line {phase_line}: phase {phase_value!r} is not declared in META.phases"
                    )

        collector = _AgentCallCollector()
        collector.visit(stmt)
        for call in collector.calls:
            line = getattr(call, "lineno", "?")
            label_kw = next((kw for kw in call.keywords if kw.arg == "label"), None)
            if label_kw is None:
                mismatches.append(f"line {line}: agent must pass a stable label='...'")
            else:
                label_value = _string_literal(label_kw.value)
                if label_value is None or not label_value.strip():
                    mismatches.append(f"line {line}: agent label must be a non-empty string literal")

            phase_kw = next((kw for kw in call.keywords if kw.arg == "phase"), None)
            if current_phase is None:
                mismatches.append(f"line {line}: agent must appear after a phase(...) call")
                continue
            if phase_kw is None:
                mismatches.append(f"line {line}: agent must pass phase={current_phase!r}")
                continue

            agent_phase = _string_literal(phase_kw.value)
            if agent_phase is None:
                mismatches.append(
                    f"line {line}: agent phase must be the literal current phase {current_phase!r}"
                )
            elif agent_phase != current_phase:
                mismatches.append(
                    f"line {line}: agent phase {agent_phase!r} does not match active phase {current_phase!r}"
                )

    return mismatches


def _phase_coverage_issues(
    run_node: ast.AsyncFunctionDef,
    required_phases: set[str],
) -> list[str]:
    if not required_phases:
        return []

    called_phases: set[str] = set()
    for stmt in run_node.body:
        is_phase_call, phase_value, _phase_line = _phase_call_info(stmt)
        if is_phase_call and phase_value is not None:
            called_phases.add(phase_value)

    return [
        f"META.phases declares {phase_title!r}, but run() never calls phase({phase_title!r})"
        for phase_title in sorted(required_phases - called_phases)
    ]


def _validate_meta_shape(meta: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in ("name", "description"):
        if not isinstance(meta.get(field), str) or not meta.get(field, "").strip():
            issues.append(f"META.{field} must be a non-empty string literal")

    phases = meta.get("phases")
    if not isinstance(phases, list):
        issues.append("META.phases must be a list of literal phase objects")
        return issues

    for index, item in enumerate(phases):
        if not isinstance(item, dict):
            issues.append(f"META.phases[{index}] must be an object")
            continue
        for field in ("title", "detail"):
            if not isinstance(item.get(field), str) or not item.get(field, "").strip():
                issues.append(f"META.phases[{index}].{field} must be a non-empty string")
    return issues


def _log_call_issues(tree: ast.Module) -> list[str]:
    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "log":
            continue
        if len(node.args) != 1 or node.keywords:
            line = getattr(node, "lineno", "?")
            issues.append(f"line {line}: log() must be called with exactly one positional argument")
    return issues


def _schema_strictness_issues(tree: ast.Module) -> list[str]:
    issues: list[str] = []
    candidate_dicts: list[ast.Dict] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
            if not isinstance(value, ast.Dict):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and "SCHEMA" in target.id.upper():
                    candidate_dicts.append(value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "schema" and isinstance(kw.value, ast.Dict):
                    candidate_dicts.append(kw.value)

    def visit_schema_dict(node: ast.Dict) -> None:
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            key = _literal_or_none(key_node) if key_node is not None else None
            line = getattr(key_node or node, "lineno", getattr(node, "lineno", "?"))
            if key == "required":
                issues.append(f"line {line}: JSON Schema must not use required")
            if key == "additionalProperties" and _literal_or_none(value_node) is False:
                issues.append(f"line {line}: JSON Schema must not use additionalProperties=False")

        for key_node, value_node in zip(node.keys, node.values, strict=False):
            key = _literal_or_none(key_node) if key_node is not None else None
            if key == "properties" and isinstance(value_node, ast.Dict):
                for property_schema in value_node.values:
                    if isinstance(property_schema, ast.Dict):
                        visit_schema_dict(property_schema)
                continue
            if isinstance(value_node, ast.Dict):
                visit_schema_dict(value_node)
            elif isinstance(value_node, (ast.List, ast.Tuple)):
                for item in value_node.elts:
                    if isinstance(item, ast.Dict):
                        visit_schema_dict(item)

    for schema_node in candidate_dicts:
        visit_schema_dict(schema_node)
    return sorted(set(issues))


def _asyncio_orchestration_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                if alias.name in ASYNCIO_ORCHESTRATION_CALLS:
                    function_aliases.add(alias.asname or alias.name)

    return module_aliases, function_aliases


def _is_module_level_mutable_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    value = node.value
    targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
    target_names = [target.id for target in targets if isinstance(target, ast.Name)]
    is_constant = all(name.isupper() for name in target_names if name)
    is_mutable_literal = isinstance(value, (ast.List, ast.Dict, ast.Set))
    is_set_call = (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"set", "dict", "list"}
    )
    if not target_names:
        return False
    if is_constant:
        return False
    return is_mutable_literal or is_set_call


def _session_variable_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        if _call_name(value) not in {"agent_session", "human_session"}:
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _session_method_name(node: ast.Call, session_names: set[str]) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    if func.value.id not in session_names:
        return None
    if func.attr not in {"send", "aclose"}:
        return None
    return func.attr


def _keyword(node: ast.Call, name: str) -> ast.keyword | None:
    return next((keyword for keyword in node.keywords if keyword.arg == name), None)


def _is_literal_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_literal_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _has_notify_schema_conflict(node: ast.Call) -> bool:
    notify_kw = _keyword(node, "notify")
    if notify_kw is None or not _is_literal_true(notify_kw.value):
        return False

    schema_kw = _keyword(node, "schema")
    if schema_kw is None:
        return False

    return not _is_literal_none(schema_kw.value)


def _workflow_path_argument(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    keyword = _keyword(node, "name_or_path")
    return keyword.value if keyword is not None else None


def _workflow_path_is_missing_or_empty(node: ast.AST | None) -> bool:
    if node is None:
        return True
    if not isinstance(node, ast.Constant):
        return False
    return not isinstance(node.value, str) or not node.value.strip()


def _single_assignment_bindings(tree: ast.Module) -> dict[str, ast.AST]:
    candidates: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                candidates.setdefault(target.id, []).append(value)
    return {
        name: values[0]
        for name, values in candidates.items()
        if len(values) == 1
    }


def _unwrap_path_expression(
    node: ast.AST,
    bindings: dict[str, ast.AST],
    seen: set[str] | None = None,
) -> ast.AST:
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id != "str":
            return node
        if len(node.args) != 1 or node.keywords:
            return node
        return _unwrap_path_expression(node.args[0], bindings, seen)
    if isinstance(node, ast.Name) and node.id in bindings:
        visited = set() if seen is None else set(seen)
        if node.id in visited:
            return node
        visited.add(node.id)
        return _unwrap_path_expression(bindings[node.id], bindings, visited)
    return node


def _split_path_join(
    node: ast.AST,
    bindings: dict[str, ast.AST],
) -> tuple[ast.AST, list[str]] | None:
    node = _unwrap_path_expression(node, bindings)
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return node, []
    left = _split_path_join(node.left, bindings)
    part = _string_literal(node.right)
    if left is None or part is None:
        return None
    base, parts = left
    return base, [*parts, part]


def _is_installed_skills_root(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    if _literal_or_none(node.slice) != 2:
        return False
    parents = node.value
    if not isinstance(parents, ast.Attribute) or parents.attr != "parents":
        return False
    resolved_file = parents.value
    if not isinstance(resolved_file, ast.Call):
        return False
    if resolved_file.args or resolved_file.keywords:
        return False
    if not isinstance(resolved_file.func, ast.Attribute):
        return False
    if resolved_file.func.attr != "resolve":
        return False
    path_file = resolved_file.func.value
    if not isinstance(path_file, ast.Call):
        return False
    if path_file.keywords or len(path_file.args) != 1:
        return False
    if not isinstance(path_file.func, ast.Name) or path_file.func.id != "Path":
        return False
    path_argument = path_file.args[0]
    return isinstance(path_argument, ast.Name) and path_argument.id == "__file__"


def _is_portable_child_workflow_path(
    node: ast.AST,
    bindings: dict[str, ast.AST],
) -> bool:
    node = _unwrap_path_expression(node, bindings)
    if not isinstance(node, ast.Call):
        return False
    if node.args or node.keywords:
        return False
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "resolve":
        return False
    split = _split_path_join(node.func.value, bindings)
    if split is None:
        return False
    base, parts = split
    if not _is_installed_skills_root(base) or len(parts) != 3:
        return False
    if parts[1:] != ["scripts", "workflow.py"]:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", parts[0]))


def _is_absolute_path_literal(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _is_budget_attribute(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Name)
        and node.value.id == "budget"
    )


def _is_none_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _proves_budget_total_available(node: ast.AST) -> bool:
    if _is_budget_attribute(node, "total"):
        return True
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_proves_budget_total_available(value) for value in node.values)
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False

    op = node.ops[0]
    comparator = node.comparators[0]
    if not isinstance(op, (ast.IsNot, ast.NotEq)):
        return False
    return (
        _is_budget_attribute(node.left, "total") and _is_none_literal(comparator)
    ) or (
        _is_none_literal(node.left) and _is_budget_attribute(comparator, "total")
    )


def _is_guarded_by_budget_total(node: ast.AST) -> bool:
    current = node
    parent = _parent(current)
    while parent is not None:
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And):
            current_index = parent.values.index(current)
            if any(
                _proves_budget_total_available(value)
                for value in parent.values[:current_index]
            ):
                return True
        elif isinstance(parent, (ast.If, ast.While)):
            if current in parent.body and _proves_budget_total_available(parent.test):
                return True
        elif isinstance(parent, ast.IfExp):
            if current is parent.body and _proves_budget_total_available(parent.test):
                return True
        current = parent
        parent = _parent(current)
    return False


def _budget_remaining_is_numeric(node: ast.Call) -> bool:
    current: ast.AST = node
    parent = _parent(current)
    while isinstance(parent, (ast.UnaryOp, ast.BinOp)):
        current = parent
        parent = _parent(current)
    if isinstance(parent, ast.Compare):
        return any(
            isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
            for op in parent.ops
        )
    return isinstance(current, (ast.UnaryOp, ast.BinOp))


def _has_guarded_budget_remaining(test: ast.AST) -> bool:
    return any(
        isinstance(candidate, ast.Call)
        and _call_name(candidate) == "budget.remaining"
        and _budget_remaining_is_numeric(candidate)
        and _is_guarded_by_budget_total(candidate)
        for candidate in ast.walk(test)
    )


def _caught_exception_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return {"<bare>"}
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        names: set[str] = set()
        for element in node.elts:
            names.update(_caught_exception_names(element))
        return names
    return set()


def _is_asyncio_orchestration_alias_call(node: ast.Call, asyncio_modules: set[str]) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ASYNCIO_ORCHESTRATION_CALLS:
        return False
    if not isinstance(node.func.value, ast.Name):
        return False
    return node.func.value.id in asyncio_modules


def _runtime_call_issues(tree: ast.Module) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    asyncio_modules, asyncio_functions = _asyncio_orchestration_aliases(tree)
    bindings = _single_assignment_bindings(tree)
    session_names = _session_variable_names(tree)
    imported_swarmflow_types = _swarmflow_imported_names(tree) & SUPPORTED_SWARMFLOW_TYPES

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in NON_DETERMINISTIC_IMPORTS:
                    warnings.append(
                        f"line {getattr(node, 'lineno', '?')}: imports non-deterministic module "
                        f"{alias.name!r}; pass values via args"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in NON_DETERMINISTIC_IMPORTS:
                warnings.append(
                    f"line {getattr(node, 'lineno', '?')}: imports non-deterministic module "
                    f"{module!r}; pass values via args"
                )
        elif isinstance(node, ast.While) and not _has_guarded_budget_remaining(node.test):
            warnings.append(
                f"line {getattr(node, 'lineno', '?')}: loop must document a stop condition "
                "(max_rounds, dry_count, budget guard, or input cap)"
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(_parent(node), ast.Module):
            if _is_module_level_mutable_assignment(node):
                warnings.append(
                    f"line {getattr(node, 'lineno', '?')}: avoid module-level mutable state; "
                    "keep run state inside run(args)"
                )
        elif isinstance(node, ast.ExceptHandler):
            caught = _caught_exception_names(node.type)
            if caught & {"<bare>", "BaseException", "BudgetExhausted"}:
                warnings.append(
                    f"line {getattr(node, 'lineno', '?')}: exception handler may catch "
                    "budget exhaustion; use finally for cleanup and let BudgetExhausted propagate"
                )
        elif isinstance(node, ast.Call):
            line = getattr(node, "lineno", "?")
            call_name = _call_name(node)
            if call_name in {"asyncio.gather", "asyncio.create_task"}:
                errors.append(f"line {line}: use SwarmFlow parallel/pipeline instead of asyncio")
            elif _is_asyncio_orchestration_alias_call(node, asyncio_modules):
                errors.append(f"line {line}: use SwarmFlow parallel/pipeline instead of asyncio")
            elif call_name in asyncio_functions:
                errors.append(f"line {line}: use SwarmFlow parallel/pipeline instead of asyncio")
            elif call_name == "print":
                errors.append(f"line {line}: use swarmflow.log(), not print()")
            elif call_name == "budget":
                errors.append(f"line {line}: budget is an imported singleton; use budget.remaining(), not budget()")
            elif call_name == "budget.total":
                errors.append(f"line {line}: budget.total is a property; do not call it")
            elif call_name == "budget.remaining":
                if _budget_remaining_is_numeric(node) and not _is_guarded_by_budget_total(node):
                    errors.append(
                        f"line {line}: guard budget.remaining() numeric use with "
                        "budget.total or `budget.total is not None`"
                    )
            elif call_name == "workflow":
                path_argument = _workflow_path_argument(node)
                resolved_path_argument = (
                    _unwrap_path_expression(path_argument, bindings)
                    if path_argument is not None
                    else None
                )
                path_literal = (
                    _string_literal(resolved_path_argument)
                    if resolved_path_argument is not None
                    else None
                )
                if _workflow_path_is_missing_or_empty(resolved_path_argument):
                    errors.append(
                        f"line {line}: workflow() requires a non-empty child workflow name/path"
                    )
                elif path_literal is not None and _is_absolute_path_literal(path_literal):
                    errors.append(
                        f"line {line}: workflow() must not hardcode an absolute machine path; "
                        "derive a portable absolute child path from Path(__file__).resolve()"
                    )
                elif path_literal is not None:
                    errors.append(
                        f"line {line}: workflow() does not accept a bare Skill name or relative "
                        "path; derive an absolute child path from Path(__file__).resolve()"
                    )
                elif resolved_path_argument is not None and not _is_portable_child_workflow_path(
                    resolved_path_argument,
                    bindings,
                ):
                    warnings.append(
                        f"line {line}: dynamic child workflow path cannot be validated; "
                        "require an absolute .py path and validate its allowed location"
                    )
                if not _is_directly_awaited(node) and not _is_inside_deferred_lambda(node):
                    errors.append(
                        f"line {line}: workflow() must be awaited or returned from a "
                        "deferred SwarmFlow thunk"
                    )
            elif call_name == "agent":
                if node.args and _string_literal(node.args[0]) == "":
                    errors.append(f"line {line}: agent prompt must not be an empty string")
                if not _is_directly_awaited(node) and not _is_inside_deferred_lambda(node):
                    errors.append(
                        f"line {line}: agent() must be awaited or returned from a "
                        "deferred SwarmFlow thunk"
                    )
            elif call_name == "human":
                if node.args and _string_literal(node.args[0]) == "":
                    errors.append(f"line {line}: human prompt must not be an empty string")
                if _keyword(node, "notify") is not None:
                    errors.append(f"line {line}: human() does not accept notify; use human_session().send()")
                if not _is_directly_awaited(node) and not _is_inside_deferred_lambda(node):
                    errors.append(
                        f"line {line}: human() must be awaited or returned from a "
                        "deferred SwarmFlow thunk"
                    )
            elif call_name in {"agent_session", "human_session"}:
                if _is_directly_awaited(node):
                    errors.append(f"line {line}: {call_name}() is synchronous and must not be awaited")
            elif call_name in imported_swarmflow_types:
                errors.append(
                    f"line {line}: do not construct {call_name} directly; "
                    "use agent_session() or human_session()"
                )
            elif call_name in {"map_parallel", "pmap"}:
                warnings.append(
                    f"line {line}: dynamic fan-out should declare an item cap and log skipped work "
                    "(no silent caps)"
                )
            elif call_name == "pipeline":
                warnings.append(
                    f"line {line}: dynamic pipeline should declare an item cap and handle empty input"
                )
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Lambda) and len(arg.args.args) != 3:
                        warnings.append(
                            f"line {getattr(arg, 'lineno', line)}: pipeline stage should accept "
                            "(prev, item, index)"
                        )
            elif call_name in FILESYSTEM_CALLS:
                warnings.append(f"line {line}: avoid direct filesystem access in workflow.py")
            elif call_name in {"time.time", "datetime.now", "datetime.utcnow"}:
                warnings.append(f"line {line}: current time access is non-deterministic; pass values via args")

            method_name = _session_method_name(node, session_names)
            if method_name is not None:
                if not _is_directly_awaited(node):
                    errors.append(
                        f"line {line}: session.{method_name}() must be awaited"
                    )
                if method_name == "send" and _has_notify_schema_conflict(node):
                    errors.append(
                        f"line {line}: session.send(notify=True) must not also pass schema"
                    )

            if isinstance(node.func, ast.Attribute) and node.func.attr in PATH_IO_METHODS:
                warnings.append(
                    f"line {line}: avoid direct filesystem access via Path.{node.func.attr} in workflow.py"
                )
        elif isinstance(node, ast.Attribute):
            line = getattr(node, "lineno", "?")
            if _is_budget_attribute(node, "total") and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            ):
                errors.append(f"line {line}: budget.total is read-only")
            elif (
                node.attr in {"remaining", "spent"}
                and _is_budget_attribute(node, node.attr)
                and isinstance(node.ctx, ast.Load)
            ):
                parent = _parent(node)
                if not (isinstance(parent, ast.Call) and parent.func is node):
                    errors.append(
                        f"line {line}: budget.{node.attr} is a method; "
                        f"use budget.{node.attr}()"
                    )

    return sorted(set(errors)), sorted(set(warnings))


def _swarmflow_imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != "swarmflow" and not module.startswith("swarmflow."):
            continue
        for alias in node.names:
            names.add(alias.asname or alias.name)
    return names


def _swarmflow_import_issues(tree: ast.Module) -> list[str]:
    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != "swarmflow" and not module.startswith("swarmflow."):
            continue
        line = getattr(node, "lineno", "?")
        if not isinstance(_parent(node), ast.Module):
            issues.append(f"line {line}: swarmflow imports must be top-level")
        if module != "swarmflow":
            issues.append(f"line {line}: import primitives directly from `swarmflow`, not {module!r}")
        for alias in node.names:
            if alias.name == "*":
                issues.append(f"line {line}: import * from swarmflow is not allowed")
                continue
            if (
                alias.name not in SUPPORTED_SWARMFLOW_OPERATORS
                and alias.name not in SUPPORTED_SWARMFLOW_TYPES
            ):
                issues.append(f"line {line}: unsupported swarmflow operator {alias.name!r}")
            if alias.asname:
                issues.append(f"line {line}: import swarmflow.{alias.name} without aliasing it")
    return sorted(set(issues))


def _dynamic_import_issues(tree: ast.Module) -> list[str]:
    issues: list[str] = []

    def is_sys_modules_lookup(node: ast.AST) -> bool:
        if not isinstance(node, ast.Subscript):
            return False
        if not isinstance(node.value, ast.Attribute):
            return False
        if not isinstance(node.value.value, ast.Name):
            return False
        if node.value.value.id != "sys":
            return False
        return node.value.attr == "modules"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        line = getattr(node, "lineno", "?")
        call_name = _call_name(node)
        if call_name == "__import__":
            issues.append(f"line {line}: dynamic import via __import__ is not allowed")
        elif call_name == "importlib.import_module":
            issues.append(f"line {line}: dynamic import via importlib.import_module is not allowed")
        elif call_name == "getattr" and node.args:
            first_arg = node.args[0]
            if is_sys_modules_lookup(first_arg):
                issues.append(f"line {line}: dynamic operator lookup through sys.modules is not allowed")
    return sorted(set(issues))


def _log_operator_issues(tree: ast.Module) -> list[str]:
    issues: list[str] = []
    swarmflow_names = _swarmflow_imported_names(tree)
    if "log" not in swarmflow_names:
        issues.append("missing `log` in explicit `from swarmflow import ...` primitive import")

    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in PROTECTED_SWARMFLOW_NAMES:
                issues.append(
                    f"line {line}: local `{node.__class__.__name__} {node.name}` "
                    f"shadows swarmflow.{node.name}"
                )
            for arg in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                if arg.arg in PROTECTED_SWARMFLOW_NAMES:
                    issues.append(
                        f"line {getattr(arg, 'lineno', line)}: parameter `{arg.arg}` "
                        f"shadows swarmflow.{arg.arg}"
                    )
            if node.args.vararg and node.args.vararg.arg in PROTECTED_SWARMFLOW_NAMES:
                issues.append(
                    f"line {getattr(node.args.vararg, 'lineno', line)}: parameter "
                    f"`{node.args.vararg.arg}` shadows swarmflow.{node.args.vararg.arg}"
                )
            if node.args.kwarg and node.args.kwarg.arg in PROTECTED_SWARMFLOW_NAMES:
                issues.append(
                    f"line {getattr(node.args.kwarg, 'lineno', line)}: parameter "
                    f"`{node.args.kwarg.arg}` shadows swarmflow.{node.args.kwarg.arg}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if bound in PROTECTED_SWARMFLOW_NAMES:
                    issues.append(f"line {line}: import binds `{bound}` outside swarmflow.{bound}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in PROTECTED_SWARMFLOW_NAMES and module != "swarmflow":
                    issues.append(f"line {line}: import binds `{bound}` outside swarmflow.{bound}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in PROTECTED_SWARMFLOW_NAMES:
                issues.append(f"line {line}: assignment target `{node.id}` shadows swarmflow.{node.id}")

    return sorted(set(issues))


def _collect_bound_names(tree: ast.Module) -> set[str]:
    """Over-approximate every name that could be bound anywhere in the module.

    Intentionally scope-agnostic: collecting names module-wide errs toward
    fewer false positives for the f-string check below.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            # Covers assignments, for/with targets, comprehension vars, walrus.
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def _fstring_undefined_names(tree: ast.Module) -> list[tuple[int, str]]:
    """Find names referenced inside f-strings that are never bound anywhere.

    Catches the literal-brace trap: a prompt with `{N}` inside an f-string is
    parsed as the expression N and raises NameError at runtime (valid syntax,
    so ast.parse alone never flags it).
    """
    bound = _collect_bound_names(tree)
    builtin_names = set(dir(builtins))
    findings: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            for sub in ast.walk(value.value):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id in bound or sub.id in builtin_names:
                        continue
                    line = getattr(sub, "lineno", getattr(node, "lineno", 0))
                    key = (line, sub.id)
                    if key not in seen:
                        seen.add(key)
                        findings.append(key)
    return findings


def validate_swarmflow_script(path: Path, skill_name: str, report: Report) -> None:
    file_label = "scripts/workflow.py"
    if not path.exists():
        report.err(file_label, "missing — required for SwarmFlow script or script-only mode")
        return

    text = path.read_text(encoding="utf-8")

    if "TEMPLATE NOTES" in text:
        report.err(file_label, "contains template notes — delete before publishing")
    if re.search(r"<[^>\n]+>", text):
        report.err(file_label, "contains unresolved template placeholder(s) like `<...>`")
    if re.search(r"([A-Za-z]:\\|/Users/|/home/)", text):
        report.err(file_label, "contains an obvious local absolute path")

    try:
        feature_version = min(sys.version_info[:2], (3, 13))
        tree = ast.parse(text, filename=str(path), feature_version=feature_version)
    except SyntaxError as e:
        report.err(file_label, f"Python syntax error: {e.msg} at line {e.lineno}")
        return
    _set_parent_links(tree)

    for line, name in _fstring_undefined_names(tree):
        report.err(
            file_label,
            f"line {line}: f-string references undefined name {name!r} — a literal "
            f"brace like {{{name}}} in prompt text is parsed as a Python expression "
            f"and crashes at runtime. Use string.Template ($-placeholders) or "
            f"concatenate the dynamic part instead.",
        )

    meta_node: ast.AST | None = None
    allowed_phases: set[str] | None = None
    run_node: ast.AsyncFunctionDef | None = None
    has_swarmflow_import = False
    dangerous_imports: list[str] = []
    blocked_imports: list[str] = []
    dangerous_calls: list[str] = []
    non_inline_prompt_calls: list[int] = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "META":
                    meta_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "META"
        ):
            meta_node = node.value
        elif isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            run_node = node

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in {"os", "subprocess"}:
                    dangerous_imports.append(alias.name)
                if alias.name.split(".", 1)[0] == "skill_context":
                    blocked_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "swarmflow" or module.startswith("swarmflow."):
                has_swarmflow_import = True
            if module.split(".", 1)[0] in {"os", "subprocess"}:
                dangerous_imports.append(module)
            if module.split(".", 1)[0] == "skill_context":
                blocked_imports.append(module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                dangerous_calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                base = node.func.value
                if isinstance(base, ast.Name) and base.id in {"os", "subprocess"}:
                    dangerous_calls.append(f"{base.id}.{node.func.attr}")
                if node.func.attr == "build_prompt":
                    non_inline_prompt_calls.append(getattr(node, "lineno", 0))

    if meta_node is None:
        report.err(file_label, "missing top-level `META` assignment")
    elif not isinstance(meta_node, ast.Dict) or not _is_literal(meta_node):
        report.err(file_label, "`META` must be a pure literal dict")
    else:
        meta = ast.literal_eval(meta_node)
        allowed_phases = _phase_titles_from_meta(meta)
        for issue in _validate_meta_shape(meta):
            report.err(file_label, issue)
        if meta.get("name") != skill_name:
            report.warn(
                file_label,
                f"META.name ({meta.get('name')!r}) does not match "
                f"directory name ({skill_name!r})",
            )

    if run_node is None:
        report.err(file_label, "missing top-level `async def run(args)`")
    else:
        args = run_node.args.args
        arg_names = [arg.arg for arg in args]
        if arg_names != ["args"] or run_node.args.defaults:
            report.err(file_label, f"`run` parameters must be exactly `(args)` (got: {arg_names})")
        if not _run_starts_with_parse_args(run_node):
            report.err(file_label, "`run` must start with `args = parse_args(args)`")
        phase_mismatches = _agent_phase_mismatches(run_node, allowed_phases)
        for mismatch in phase_mismatches:
            report.err(file_label, mismatch)
        for issue in _phase_coverage_issues(run_node, allowed_phases or set()):
            report.err(file_label, issue)

    for issue in _log_call_issues(tree):
        report.err(file_label, issue)
    for issue in _log_operator_issues(tree):
        report.err(file_label, issue)
    for issue in _swarmflow_import_issues(tree):
        report.err(file_label, issue)
    for issue in _dynamic_import_issues(tree):
        report.err(file_label, issue)
    for issue in _schema_strictness_issues(tree):
        report.err(file_label, issue)
    runtime_errors, runtime_warnings = _runtime_call_issues(tree)
    for issue in runtime_errors:
        report.err(file_label, issue)
    for issue in runtime_warnings:
        report.warn(file_label, issue)

    if not has_swarmflow_import:
        report.err(file_label, "missing explicit `from swarmflow import ...` primitive import")
    if dangerous_imports:
        report.err(
            file_label,
            f"imports blocked module(s): {', '.join(sorted(set(dangerous_imports)))}",
        )
    if blocked_imports:
        report.err(
            file_label,
            "uses a non-standard SwarmFlow context import; "
            "use the standalone workflow format",
        )
    if dangerous_calls:
        report.err(file_label, f"uses blocked call(s): {', '.join(sorted(set(dangerous_calls)))}")
    if non_inline_prompt_calls:
        lines = ", ".join(str(line) for line in sorted(set(non_inline_prompt_calls)) if line)
        report.err(
            file_label,
            "SwarmFlow scripts must keep agent prompts inline in scripts/workflow.py "
            f"(non-inline prompt call at line(s): {lines})",
        )


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------

def cross_check_skills_tools(skill_fm: dict, declared: dict[str, list[str]], report: Report) -> None:
    """Every roles[].skills / roles[].tools in SKILL.md must appear in dependencies.yaml."""
    for role in skill_fm.get("roles", []):
        if not isinstance(role, dict):
            continue
        role_id = role.get("id", "?")
        for kind, segment in (("skills", "skills"), ("tools", "tools")):
            for item in role.get(kind, []) or []:
                if item not in declared[segment]:
                    report.err(
                        "dependencies.yaml",
                        f"`{item}` is declared in SKILL.md `roles[id={role_id!r}].{kind}` "
                        f"but missing from `{segment}:` segment",
                    )


def check_orphan_role_files(roles_dir: Path, declared_role_ids: list[str], report: Report) -> None:
    if not roles_dir.exists():
        return
    for f in roles_dir.glob("*.md"):
        role_id = f.stem
        if role_id not in declared_role_ids:
            report.warn(
                "roles/",
                f"orphan file `roles/{f.name}` — not declared in SKILL.md "
                "frontmatter `roles[]`",
            )


def detect_structure(root: Path, report: Report) -> tuple[bool, bool]:
    """Return (has_swarmflow_script, is_script_only)."""
    script_path = root / "scripts" / "workflow.py"
    has_script = script_path.exists()
    prompts_dir = root / "prompts"
    markdown_paths = [
        root / "roles",
        root / "workflow.md",
        root / "bind.md",
        root / "dependencies.yaml",
    ]
    present = [p for p in markdown_paths if p.exists()]
    has_partial_spec = 0 < len(present) < len(markdown_paths)

    if has_script and not present:
        if prompts_dir.exists():
            report.err(
                "prompts/",
                "script-only SwarmFlow skills must keep prompts inline in "
                "scripts/workflow.py; do not create prompts/",
            )
        return True, True

    if has_script and has_partial_spec:
        missing = [p.name for p in markdown_paths if not p.exists()]
        report.err(
            "structure",
            "partial Markdown spec with SwarmFlow script — either provide the full "
            "5-file spec or use script-only mode with only SKILL.md + "
            f"scripts/workflow.py. Missing: {', '.join(missing)}",
        )

    return has_script, False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate(root: Path) -> int:
    if not root.exists() or not root.is_dir():
        logger.info("[USAGE ERROR] %s is not a directory", root)
        return 2

    logger.info("Validating Swarm Skill: %s\n", root.resolve())

    report = Report()

    # 0. Structure shape
    has_swarmflow_script, is_script_only = detect_structure(root, report)

    # 1. SKILL.md
    skill_fm, role_ids = validate_skill_md(root / "SKILL.md", report, script_only=is_script_only)

    if has_swarmflow_script:
        validate_swarmflow_script(root / "scripts" / "workflow.py", root.name, report)

    if is_script_only:
        report.emit()
        return 0 if report.passed() else 1

    # 2. roles/<id>.md
    roles_dir = root / "roles"
    if not roles_dir.exists():
        report.err("roles/", "directory missing — required by the 5-file structure")
    else:
        for role_id in role_ids:
            validate_role_file(roles_dir / f"{role_id}.md", role_id, report)
        check_orphan_role_files(roles_dir, role_ids, report)

    # 3. workflow.md
    validate_workflow_md(root / "workflow.md", report)

    # 4. bind.md
    validate_bind_md(root / "bind.md", report)

    # 5. dependencies.yaml + cross-check
    declared = validate_dependencies_yaml(root / "dependencies.yaml", report)
    if skill_fm is not None:
        cross_check_skills_tools(skill_fm, declared, report)

    report.emit()
    return 0 if report.passed() else 1


def main() -> int:
    if len(sys.argv) != 2:
        logger.info(__doc__)
        return 2
    return validate(Path(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
