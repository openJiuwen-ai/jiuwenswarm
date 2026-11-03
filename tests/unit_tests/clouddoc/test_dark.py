"""Until the wiring PR, nothing outside the package imports ``jiuwenswarm.clouddoc``.

The modules land one PR at a time and must be inert: a deployment that carries them
behaves exactly as one that does not. The wiring PR removes this test.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_no_caller_outside_the_package_yet():
    offenders = []
    for root in (REPO / "jiuwenswarm", REPO / "tests"):
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith(("jiuwenswarm/clouddoc/", "tests/unit_tests/clouddoc/")):
                continue
            if "jiuwenswarm.clouddoc" in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(rel)
    assert offenders == []
