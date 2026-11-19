"""Clean-cut checks for the single JiuwenSwarm PersonalContext host class."""

from __future__ import annotations

import importlib.util
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).parents[3]
HOST = ROOT / "jiuwenswarm" / "server" / "personal_context"
LEGACY_PACKAGE = ROOT / "jiuwenswarm" / "server" / "proactive_harness"


def _classes(path: Path) -> set[str]:
    result: set[str] = set()
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    for file in files:
        # Tokenization avoids the shared AST recursion state bug in older CPython 3.11.
        tokens = tokenize.generate_tokens(io.StringIO(file.read_text(encoding="utf-8")).readline)
        for token in tokens:
            if token.type == tokenize.NAME and token.string == "class":
                result.add(next(tokens).string)
    return result


def test_class_inventory_ignores_strings_and_comments(tmp_path: Path) -> None:
    source = tmp_path / "classes.py"
    source.write_text(
        '"""class DocstringDecoy: pass"""\n'
        '# class CommentDecoy: pass\n'
        'text = "class StringDecoy: pass"\n'
        'class Outer:\n'
        '    class Nested:\n'
        '        pass\n'
        'def factory():\n'
        '    class Local(Outer):\n'
        '        pass\n',
        encoding="utf-8",
    )
    expected = {"Outer", "Nested", "Local"}
    assert _classes(source) == expected
    assert _classes(tmp_path) == expected


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
