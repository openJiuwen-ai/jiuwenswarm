"""Input and output matching helpers for Beam plans."""

from __future__ import annotations

from typing import Any, Iterable


def missing_inputs(
    skill: dict[str, Any],
    available: Iterable[tuple[str, str]],
) -> list[dict[str, Any]]:
    available_set = set(available)
    missing = []
    for item in skill.get("inputs", []):
        if not item.get("required", True):
            continue
        expected = (str(item.get("name")), str(item.get("type") or "unknown"))
        if not artifact_matches(expected, available_set):
            missing.append({"name": expected[0], "type": expected[1]})
    return missing


def artifact_matches(
    expected: tuple[str, str],
    available: set[tuple[str, str]],
) -> bool:
    name, type_ = expected
    if expected in available:
        return True
    return any(
        candidate_name == name and (candidate_type == "unknown" or type_ == "unknown")
        for candidate_name, candidate_type in available
    )


def output_keys(skill: dict[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset(
        (str(item.get("name")), str(item.get("type") or "unknown"))
        for item in skill.get("outputs", [])
        if item.get("name")
    )
