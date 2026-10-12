# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: SkillManager scans MCP skills under mcp/skills/.

Design: MCP-bundled skills live under
``<workspace>/mcp/skills/<name>/<skill>/`` (physically
isolated from user skills). The scan list is DERIVED from state.json's
connected MCPs (state_store.connected_mcp_skill_dirs) — an MCP's skills
surface only while it is connected (state==connected). disconnect removes
the state.json record, which removes the MCP from the derived scan list,
so its skills disappear on the next scan with no separate unregister step.

The previous design persisted a separate ``connector_skills`` list in
skills_state.json; these tests verify the derived-view replacement: the
scan list tracks state.json, not a second persisted list.
"""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _mk_skill(dir_path: Path, name: str, body: str = "body") -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n---\n{body}", encoding="utf-8"
    )


def _new_manager(workspace: Path, monkeypatch) -> SkillManager:
    skills_dir = workspace / "skills"
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_state_file",
        lambda: skills_dir / "skills_state.json",
    )
    return SkillManager(workspace_dir=str(workspace))


def _patch_connected_dirs(monkeypatch, workspace: Path, names: list[str]) -> None:
    """Stub the derived mcp-scan-dirs lookup to return given MCPs."""
    skills_root = workspace / "mcp" / "skills"
    payload = [
        {"name": n, "dir": str(skills_root / f"{n}")}
        for n in names
    ]
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.mcp.state_store.connected_mcp_skill_dirs",
        lambda: list(payload),
    )


def test_scan_finds_user_skills_only_by_default(tmp_path: Path, monkeypatch) -> None:
    """No connected connectors -> only workspace/skills/* is scanned."""
    _mk_skill(tmp_path / "skills" / "user-skill", "user-skill")
    _patch_connected_dirs(monkeypatch, tmp_path, [])
    mgr = _new_manager(tmp_path, monkeypatch)
    found = {s["name"] for s in mgr._scan_local_skills()}
    assert "user-skill" in found


def test_scan_includes_connected_connector_skills(tmp_path: Path, monkeypatch) -> None:
    """A connected MCP's skill dir (in state.json) is scanned."""
    _mk_skill(tmp_path / "skills" / "user-skill", "user-skill")
    cs = tmp_path / "mcp" / "skills" / "feishu" / "lark-doc"
    _mk_skill(cs, "lark-doc")
    _patch_connected_dirs(monkeypatch, tmp_path, ["feishu"])
    mgr = _new_manager(tmp_path, monkeypatch)
    found = {s["name"] for s in mgr._scan_local_skills()}
    assert "user-skill" in found
    assert "lark-doc" in found


def test_scan_excludes_disconnected_connector_skills(tmp_path: Path, monkeypatch) -> None:
    """An MCP NOT in state.json (disconnected) is not scanned, even if its
    skill dir still exists on disk (orphan from a failed disconnect)."""
    _mk_skill(tmp_path / "skills" / "user-skill", "user-skill")
    cs = tmp_path / "mcp" / "skills" / "feishu" / "lark-doc"
    _mk_skill(cs, "lark-doc")
    _patch_connected_dirs(monkeypatch, tmp_path, [])  # not connected
    mgr = _new_manager(tmp_path, monkeypatch)
    found = {s["name"] for s in mgr._scan_local_skills()}
    assert "user-skill" in found
    assert "lark-doc" not in found


def test_disconnect_removes_connector_skills_from_scan(tmp_path: Path, monkeypatch) -> None:
    """disconnect (removing the state.json record) drops the MCP's skills
    from the scan on the next read — no separate unregister needed."""
    _mk_skill(tmp_path / "skills" / "user-skill", "user-skill")
    cdir = tmp_path / "mcp" / "skills" / "feishu"
    _mk_skill(cdir / "lark-doc", "lark-doc")
    _patch_connected_dirs(monkeypatch, tmp_path, ["feishu"])
    mgr = _new_manager(tmp_path, monkeypatch)
    assert "lark-doc" in {s["name"] for s in mgr._scan_local_skills()}
    # Simulate disconnect: the derived lookup no longer returns feishu.
    _patch_connected_dirs(monkeypatch, tmp_path, [])
    assert "lark-doc" not in {s["name"] for s in mgr._scan_local_skills()}


def test_scan_list_derived_from_state_not_persisted_list(
    tmp_path: Path, monkeypatch
) -> None:
    """A stale entry persisted in skills_state.json's connector_skills is
    IGNORED — only state.json's connected records drive the scan. This is the
    core regression for the bug where disconnected connectors' skills stayed
    visible because the persisted connector_skills list drifted."""
    _mk_skill(tmp_path / "skills" / "user-skill", "user-skill")
    cdir = tmp_path / "mcp" / "skills" / "feishu"
    _mk_skill(cdir / "lark-doc", "lark-doc")
    # state.json says feishu is NOT connected (empty derived list).
    _patch_connected_dirs(monkeypatch, tmp_path, [])
    # But skills_state.json carries a stale persisted entry.
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    import json
    (skills_dir / "skills_state.json").write_text(
        json.dumps({"connector_skills": [{"name": "feishu", "dir": str(cdir)}]}),
        encoding="utf-8",
    )
    mgr = _new_manager(tmp_path, monkeypatch)
    found = {s["name"] for s in mgr._scan_local_skills()}
    assert "lark-doc" not in found  # stale persisted entry ignored
