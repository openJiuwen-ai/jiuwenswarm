"""Clean-cut checks for the single JiuwenSwarm PersonalContext host class."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[3]
HOST = ROOT / "jiuwenswarm" / "server" / "personal_context"
LEGACY_PACKAGE = ROOT / "jiuwenswarm" / "server" / "proactive_harness"


def _classes(path: Path) -> set[str]:
    result: set[str] = set()
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        result.update(
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        )
    return result


def test_jiuwen_swarm_declares_only_the_host_class() -> None:
    assert _classes(HOST) == {"PersonalContextHostAPI"}


def test_embedded_personal_context_has_exactly_eighteen_production_classes() -> None:
    core_spec = importlib.util.find_spec("openjiuwen.harness.personal_context")
    rail_spec = importlib.util.find_spec("openjiuwen.harness.rails.personal_context")
    assert core_spec is not None and core_spec.origin is not None
    assert rail_spec is not None and rail_spec.origin is not None
    core = Path(core_spec.origin).parent
    core_rail = Path(rail_spec.origin)
    assert len(_classes(core) | _classes(core_rail) | _classes(HOST)) == 18


def test_legacy_host_package_is_removed() -> None:
    assert not any(LEGACY_PACKAGE.rglob("*.py"))


def test_host_has_no_legacy_transport_imports() -> None:
    source = "\n".join(file.read_text(encoding="utf-8") for file in HOST.rglob("*.py"))
    for forbidden in (
        "ProactiveHarness",
        "ProactiveContextService",
        "proactive_harness",
        "open_description_reader",
        "FastAPI",
        "uvicorn",
    ):
        assert forbidden not in source
