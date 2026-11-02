"""Regression tests for lone UTF-16 surrogates in skill frontmatter.

PyYAML decodes JSON-style surrogate escapes (``\\ud83e\\udd9e``) as two
isolated surrogate code points instead of combining them into the astral
character they encode.  Such strings cannot be encoded as UTF-8 and used to
break the whole ``skills.list`` WebSocket response.
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

# ``🦞`` is the JSON escape for the lobster emoji (U+1F99E).
_POISON_METADATA = 'metadata: {"openclaw":{"emoji":"\\ud83e\\udd9e"}}'


def _write_skill(skills_dir, name, *, extra_frontmatter="", body="# skill\n"):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: demo skill\n"
        f"{extra_frontmatter}"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )
    return skill_dir


def _init_manager(monkeypatch, skills_dir, builtin_dir):
    skills_dir.mkdir(parents=True, exist_ok=True)
    builtin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    return SkillManager()


def test_parse_skill_md_combines_surrogate_pair_escapes(tmp_path):
    skill_dir = _write_skill(
        tmp_path, "emoji-skill", extra_frontmatter=f"{_POISON_METADATA}\n"
    )

    meta = SkillManager._parse_skill_md(skill_dir / "SKILL.md")

    assert meta is not None
    assert meta["metadata"]["openclaw"]["emoji"] == "\U0001F99E"
    # The exact failing boundary from the defect: strict UTF-8 encoding.
    json.dumps(meta, ensure_ascii=False).encode("utf-8")


def test_parse_skill_md_replaces_unpaired_surrogate(tmp_path):
    skill_dir = _write_skill(
        tmp_path,
        "broken-emoji-skill",
        extra_frontmatter='metadata: {"emoji": "\\ud83e"}\n',
    )

    meta = SkillManager._parse_skill_md(skill_dir / "SKILL.md")

    assert meta is not None
    assert meta["metadata"]["emoji"] == "�"
    json.dumps(meta, ensure_ascii=False).encode("utf-8")


def test_parse_skill_display_md_combines_surrogate_pair_escapes(tmp_path):
    skill_dir = _write_skill(tmp_path, "display-skill")
    (skill_dir / "skill_display.md").write_text(
        '---\ndisplay_name_zh: "\\ud83e\\udd9e skill"\n---\n',
        encoding="utf-8",
    )

    display = SkillManager._parse_skill_display_md(skill_dir)

    assert display["display_name_zh"] == "\U0001F99E skill"
    json.dumps(display, ensure_ascii=False).encode("utf-8")


@pytest.mark.asyncio
async def test_skills_list_payload_with_poisoned_metadata_is_utf8_encodable(
    monkeypatch, tmp_path
):
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _write_skill(
        skills_dir, "emoji-skill", extra_frontmatter=f"{_POISON_METADATA}\n"
    )
    manager = _init_manager(monkeypatch, skills_dir, builtin_dir)

    response = await manager.handle_skills_list({})

    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
    skill = next(s for s in response["skills"] if s["name"] == "emoji-skill")
    assert skill["metadata"]["openclaw"]["emoji"] == "\U0001F99E"
    assert "\U0001F99E" in encoded.decode("utf-8")
