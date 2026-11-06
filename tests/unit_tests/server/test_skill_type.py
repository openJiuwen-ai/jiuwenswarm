# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for skill_type dual-read helpers (kind ↔ skill_type)."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.runtime.skill.skill_type import (
    SKILL_TYPE_SKILL,
    SKILL_TYPE_SWARM,
    apply_skill_type_fields,
    detect_skill_type,
    is_swarm_kind,
    skill_type_from_meta,
)


def _write_skill(dir_path: Path, *, kind: str | None = None, name: str = "demo") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: demo"]
    if kind is not None:
        lines.append(f"kind: {kind}")
    lines.extend(["---", "", "# body", ""])
    (dir_path / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return dir_path


def test_detect_skill_type_team_skill_alias(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path / "team_pkg", kind="team-skill")
    assert detect_skill_type(skill_dir) == SKILL_TYPE_SWARM


def test_detect_skill_type_swarm_skill(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path / "swarm_pkg", kind="swarm-skill")
    assert detect_skill_type(skill_dir) == SKILL_TYPE_SWARM


def test_detect_skill_type_ordinary(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path / "plain", kind=None)
    assert detect_skill_type(skill_dir) == SKILL_TYPE_SKILL


def test_detect_skill_type_reuses_kind_without_reread(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path / "plain", kind=None)
    assert detect_skill_type(skill_dir, kind="") == SKILL_TYPE_SKILL
    assert detect_skill_type(skill_dir, kind="swarm-skill") == SKILL_TYPE_SWARM


def test_skill_type_from_meta_prefers_kind_without_rescan() -> None:
    assert skill_type_from_meta({"kind": "team-skill"}) == SKILL_TYPE_SWARM
    assert skill_type_from_meta({"skill_type": "swarm_skill"}) == SKILL_TYPE_SWARM
    assert skill_type_from_meta({"kind": "other", "skill_type": "skill"}) == SKILL_TYPE_SKILL


def test_apply_skill_type_fields_keeps_kind(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path / "keep", kind="team-skill", name="keep")
    meta = {"name": "keep", "kind": "team-skill"}
    apply_skill_type_fields(meta, skill_dir)
    assert meta["kind"] == "team-skill"
    assert meta["skill_type"] == SKILL_TYPE_SWARM


def test_is_swarm_kind() -> None:
    assert is_swarm_kind("swarm-skill")
    assert is_swarm_kind("team-skill")
    assert not is_swarm_kind("skill")
    assert not is_swarm_kind("")


def test_read_frontmatter_kind(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.skill.skill_type import read_frontmatter_kind

    skill_dir = _write_skill(tmp_path / "shared", kind="team-skill")
    assert read_frontmatter_kind(skill_dir) == "team-skill"
