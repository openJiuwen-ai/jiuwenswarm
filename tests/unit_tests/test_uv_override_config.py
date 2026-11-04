"""Guard rails for [tool.uv] override-dependencies extras replay.

A bare ``openjiuwen`` override replaces entire requirements — including their
extras — so every optional-dependency extra that requests ``openjiuwen[...]``
must be replayed in override-dependencies with a matching ``extra ==`` marker.
Without the replay, ``uv sync --extra cua`` silently drops pyautogui & friends
(the Windows packaging script installs with ``--extra cua``), and claude/codex/
postgres/zmq lose their SDK dependencies the same way.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_REQUIREMENT_RE = re.compile(r"^\s*openjiuwen\s*(?P<extras>\[[^\]]*\])?\s*(?:[;@]|$)")
_MARKER_RE = re.compile(r";\s*extra\s*==\s*'(?P<extra_name>[^']+)'\s*$")


def _parse_requirement_extras(requirement: str) -> frozenset[str] | None:
    """Return openjiuwen extras requested by a requirement, None if not openjiuwen."""
    match = _REQUIREMENT_RE.match(requirement)
    if match is None:
        return None
    extras = match.group("extras")
    return frozenset(e.strip() for e in (extras[1:-1].split(",") if extras else []))


def _parse_overrides(entries: list[str]) -> dict[str, frozenset[str]]:
    """Map parent-extra name -> openjiuwen extras replayed by marker overrides.

    Entries without an ``extra ==`` marker are ignored: they replace every
    openjiuwen requirement everywhere and cannot preserve extras selection.
    """
    replayed: dict[str, frozenset[str]] = {}
    for entry in entries:
        extras = _parse_requirement_extras(entry)
        if extras is None:
            continue
        marker = _MARKER_RE.search(entry)
        if marker is None:
            continue
        replayed[marker.group("extra_name")] = extras
    return replayed


def _extras_requesting_openjiuwen_extras(optional: dict[str, list[str]]) -> dict[str, frozenset[str]]:
    """Map parent-extra name -> non-empty openjiuwen extras it requests.

    Extras referencing bare ``openjiuwen`` are skipped: the bare override
    covers them without changing their resolution.
    """
    referenced: dict[str, frozenset[str]] = {}
    for extra_name, requirements in optional.items():
        for requirement in requirements:
            extras = _parse_requirement_extras(requirement)
            if extras:
                referenced[extra_name] = extras
    return referenced


def test_marker_overrides_replay_every_openjiuwen_extra() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides: list[str] = pyproject["tool"]["uv"]["override-dependencies"]

    replayed = _parse_overrides(overrides)
    referenced = _extras_requesting_openjiuwen_extras(
        pyproject["project"]["optional-dependencies"]
    )

    assert referenced, "expected at least one extra requesting openjiuwen[...]"
    assert replayed == referenced, (
        "override-dependencies must replay each openjiuwen[...] extra with an "
        "`extra ==` marker on the parent extra name; missing or drifted: "
        f"{ {k: (referenced.get(k), replayed.get(k)) for k in referenced.keys() | replayed.keys() if referenced.get(k) != replayed.get(k)} }"
    )


def test_bare_openjiuwen_override_remains_as_fallback() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides: list[str] = pyproject["tool"]["uv"]["override-dependencies"]

    bare = [e for e in overrides if _MARKER_RE.search(e) is None and e.strip().startswith("openjiuwen @")]
    assert len(bare) == 1, (
        "exactly one marker-free openjiuwen override must stay as the fallback "
        "for bare requirements and the agent-gateway transitive pin"
    )


def test_bare_only_overrides_cannot_replay_extras() -> None:
    # Regression path: a bare-only override set silently loses extras selection.
    replayed = _parse_overrides(["openjiuwen @ git+https://example.invalid/repo@rev"])
    assert replayed == {}


def test_marker_override_parsing_extracts_extras() -> None:
    replayed = _parse_overrides(
        [
            "openjiuwen @ git+https://example.invalid/repo@rev",
            "openjiuwen[postgres,zmq] @ git+https://example.invalid/repo@rev ; extra == 'distribute'",
            "openjiuwen[cua] @ git+https://example.invalid/repo@rev ; extra == 'cua'",
            "openjiuwen-protocol @ git+https://example.invalid/other@rev",
        ]
    )
    assert replayed == {
        "distribute": frozenset({"postgres", "zmq"}),
        "cua": frozenset({"cua"}),
    }
