"""Architecture gate: keep Gateway's dependency on ``jiuwenswarm.server`` bounded.

Gateway does not spawn an AgentServer process, yet it reads and writes the
``~/.jiuwenswarm`` user directory directly and falls back to local adapters when
the AgentServer is unreachable.  The direct edges are few, but importing them
eagerly drags in the whole agent execution layer through parent-package
``__init__`` side effects.

These tests freeze two budgets so the coupling cannot silently grow:

1. the set of ``jiuwenswarm.server`` and ``jiuwenswarm.agents`` modules Gateway
   may import at all;
2. the size of the eager import closure reachable from *both* allow-lists.

Both budgets are lowered as each decoupling step lands, so a drop is expected
progress and a rise is a regression.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PKG_ROOT = PROJECT_ROOT / "jiuwenswarm"
GATEWAY_ROOT = PKG_ROOT / "gateway"

# Every jiuwenswarm.server module Gateway is currently allowed to import.
# Shrinks as modules move to a neutral package; must never grow without review.
ALLOWED_SERVER_MODULES = {
    "jiuwenswarm.server.hooks.executor",
    "jiuwenswarm.server.runtime.a2ui.integration",
    "jiuwenswarm.server.runtime.attachments",
    # imported as a submodule, so it counts as its own import target
    "jiuwenswarm.server.runtime.attachments.media_attachments",
    "jiuwenswarm.server.runtime.attachments.upload_storage",
    # Package root: the offline fallback asks the adapter registry which
    # adapter owns a method, so importing it loads every adapter.
    "jiuwenswarm.server.runtime.gateway_adapter",
    "jiuwenswarm.server.runtime.gateway_adapter.base",
    "jiuwenswarm.server.runtime.harmonyos.harmonyos_dev",
    "jiuwenswarm.server.runtime.session.git_diff_status",
    "jiuwenswarm.server.runtime.session.git_diff_watcher",
    "jiuwenswarm.server.runtime.session.project_git",
    "jiuwenswarm.server.runtime.session.project_store",
    "jiuwenswarm.server.runtime.session.session_info",
    "jiuwenswarm.server.runtime.session.session_metadata",
    "jiuwenswarm.server.runtime.session.session_rename",
    "jiuwenswarm.server.runtime.session.work_mode",
    "jiuwenswarm.server.runtime.team_binding_store",
    "jiuwenswarm.server.runtime.team_entity_store",
}

# Every jiuwenswarm.agents module Gateway is currently allowed to import.
# Gateway must never reach the agent execution layer beyond these.
ALLOWED_AGENT_MODULES = {
    "jiuwenswarm.agents.harness.common.auto_harness",
    "jiuwenswarm.agents.harness.common.rails.permissions.auto_config",
    "jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc",
    "jiuwenswarm.agents.harness.common.session_ops_service",
    "jiuwenswarm.agents.harness.common.tools.web_file_download",
    # Package root: its ``__init__`` re-exports TeamManager eagerly, so this
    # single edge drags the agent execution layer in behind it.
    "jiuwenswarm.agents.harness.team",
}

# Eager closure budget reachable from the allow-lists above.  Both lists seed
# the closure: Gateway reaches the agent execution layer through server modules
# (team_entity_store -> team.config_loader) *and* directly (auto_config,
# session_ops_service, web_file_download), so seeding from server alone would
# leave the direct agent edges unmeasured -- 5 modules invisible today, and an
# unbounded hole as soon as someone imports a heavier agent module.
#
# Today's measured values, frozen as the starting point: 151 modules, 81 of
# which belong to the agent execution layer that Gateway never executes.
# Each decoupling step lowers both numbers, so a drop is expected progress.
MAX_CLOSURE_MODULES = 151
MAX_CLOSURE_AGENT_MODULES = 81

_CLOSURE_SEEDS = ALLOWED_SERVER_MODULES | ALLOWED_AGENT_MODULES


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _parent_packages(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


class _EagerImportCollector(ast.NodeVisitor):
    """Collect module-level jiuwenswarm imports.

    Imports inside functions are lazy and cost nothing at startup, and
    ``if TYPE_CHECKING:`` blocks never execute, so both are skipped -- counting
    them would inflate the closure and invent a fake server -> gateway cycle.
    """

    def __init__(self, known_modules: set[str]) -> None:
        self._known = known_modules
        self._function_depth = 0
        self.imports: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_type_checking:
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def _record(self, name: str | None) -> None:
        if name and name.startswith("jiuwenswarm"):
            self.imports.add(name)

    def visit_Import(self, node: ast.Import) -> None:
        if self._function_depth:
            return
        for alias in node.names:
            self._record(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._function_depth or node.level:
            return
        module = node.module
        if not module or not module.startswith("jiuwenswarm"):
            return
        self._record(module)
        for alias in node.names:
            submodule = f"{module}.{alias.name}"
            if submodule in self._known:
                self._record(submodule)


def _all_modules() -> dict[str, Path]:
    return {_module_name(path): path for path in PKG_ROOT.rglob("*.py")}


def _build_eager_graph(modules: dict[str, Path]) -> dict[str, set[str]]:
    known = set(modules)
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        collector = _EagerImportCollector(known)
        try:
            collector.visit(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:  # pragma: no cover - defensive
            pass
        graph[name] = collector.imports
    return graph


def _eager_closure(
    seeds: set[str], modules: dict[str, Path], graph: dict[str, set[str]]
) -> set[str]:
    """Modules loaded when the seeds are imported.

    Python executes every parent ``__init__`` before a submodule, so parents are
    pulled in along with their own eager imports -- that side effect is exactly
    what makes this closure much larger than the direct edges suggest.
    """
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        module = stack.pop()
        if module in seen or module not in modules:
            continue
        seen.add(module)
        stack.extend(p for p in _parent_packages(module) if p in modules and p not in seen)
        stack.extend(graph.get(module, ()))
    return seen


def _gateway_edges(prefix: str) -> dict[str, set[str]]:
    """Map each Gateway file to the ``prefix`` modules it imports.

    Unlike the closure, this walks every import site (lazy ones included),
    because an in-function import is still a dependency on that package.
    """
    modules = _all_modules()
    known = set(modules)
    edges: dict[str, set[str]] = {}
    for path in GATEWAY_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(a.name for a in node.names if a.name.startswith(prefix))
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                if not module.startswith(prefix):
                    continue
                targets.add(module)
                for alias in node.names:
                    submodule = f"{module}.{alias.name}"
                    if submodule in known:
                        targets.add(submodule)
        if targets:
            edges[str(path.relative_to(PROJECT_ROOT))] = targets
    return edges


def _gateway_server_edges() -> dict[str, set[str]]:
    return _gateway_edges("jiuwenswarm.server")


def _gateway_agent_edges() -> dict[str, set[str]]:
    return _gateway_edges("jiuwenswarm.agents")


def _budget_report(title: str, problems: list[str]) -> str:
    bar = "=" * 74
    return "\n".join(
        [
            "",
            bar,
            f"BLOCKED: {title}",
            bar,
            *(f"  - {problem}" for problem in problems),
            "",
            "Gateway must not grow new dependencies on jiuwenswarm.server.",
            "If the new edge is unavoidable, justify it in review and widen the",
            "allow-list or budget in this file as part of the same change.",
            bar,
        ]
    )


def test_gateway_only_imports_allowed_server_modules():
    """Gateway imports nothing from jiuwenswarm.server outside the allow-list."""
    edges = _gateway_server_edges()
    used: set[str] = set()
    for targets in edges.values():
        used.update(targets)

    unexpected = sorted(used - ALLOWED_SERVER_MODULES)
    problems = [
        f"{module} <- imported by "
        + ", ".join(sorted(f for f, t in edges.items() if module in t))
        for module in unexpected
    ]
    assert not problems, _budget_report("Gateway imports a new server module", problems)


def test_gateway_only_imports_allowed_agent_modules():
    """Gateway imports nothing from jiuwenswarm.agents outside the allow-list."""
    edges = _gateway_agent_edges()
    used: set[str] = set()
    for targets in edges.values():
        used.update(targets)

    unexpected = sorted(used - ALLOWED_AGENT_MODULES)
    problems = [
        f"{module} <- imported by "
        + ", ".join(sorted(f for f, t in edges.items() if module in t))
        for module in unexpected
    ]
    assert not problems, _budget_report("Gateway imports a new agent module", problems)


def test_allow_list_has_no_stale_entries():
    """Every allow-list entry is still imported, so the list tracks reality."""
    used: set[str] = set()
    for targets in _gateway_server_edges().values():
        used.update(targets)
    for targets in _gateway_agent_edges().values():
        used.update(targets)

    stale = sorted((ALLOWED_SERVER_MODULES | ALLOWED_AGENT_MODULES) - used)
    assert not stale, _budget_report(
        "allow-list is stale (decoupling progressed)",
        [f"{module} is no longer imported -> drop it from the allow-list" for module in stale],
    )


def test_eager_closure_stays_within_budget():
    """Importing every allowed module stays within the closure budget."""
    modules = _all_modules()
    graph = _build_eager_graph(modules)
    closure = _eager_closure(_CLOSURE_SEEDS, modules, graph)
    agent_modules = {m for m in closure if m.startswith("jiuwenswarm.agents")}

    problems = []
    if len(closure) > MAX_CLOSURE_MODULES:
        problems.append(
            f"eager closure is {len(closure)} modules, budget is {MAX_CLOSURE_MODULES}"
        )
    if len(agent_modules) > MAX_CLOSURE_AGENT_MODULES:
        problems.append(
            f"eager closure pulls {len(agent_modules)} jiuwenswarm.agents modules, "
            f"budget is {MAX_CLOSURE_AGENT_MODULES}"
        )
    assert not problems, _budget_report("eager import closure grew", problems)


def test_gateway_is_not_imported_by_the_closure():
    """No import-time cycle: the closure never reaches back into Gateway."""
    modules = _all_modules()
    graph = _build_eager_graph(modules)
    closure = _eager_closure(_CLOSURE_SEEDS, modules, graph)

    gateway_modules = sorted(m for m in closure if m.startswith("jiuwenswarm.gateway"))
    assert not gateway_modules, _budget_report(
        "import-time cycle server -> gateway",
        [f"{module} is loaded at import time from the server closure" for module in gateway_modules],
    )
