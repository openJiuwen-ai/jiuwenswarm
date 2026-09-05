# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""内置技能中写死的技能路径必须指向该技能自己的安装目录。

技能随附的脚本和参考文档以相对路径出现在 ``SKILL.md`` 中，一旦文中写死的
绝对路径指向别的目录名或过时的 skills 根目录，模型按该路径执行就会失败，
并且只能退化为在工作目录里盲搜——而 skills 目录通常是会话目录的兄弟目录，
搜不到。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BUILTIN_SKILLS_ROOT = (
    Path(__file__).resolve().parents[2]
    / "jiuwenswarm"
    / "resources"
    / "agent"
    / "workspace"
    / "skills"
)

# 当前 skills 根目录的结尾，直接取自随包发布的目录布局，不在断言里再写死一遍：
# 布局一旦搬家，随包目录先动，这里跟着动。``agent/skills`` 是迁移前的旧布局。
_EXPECTED_ROOT_TAIL = "/".join(_BUILTIN_SKILLS_ROOT.parts[-2:])

# 匹配 SKILL.md 中写死的 skills 根目录，Unix 与 Windows 两种写法。
_SKILLS_ROOT_RE = re.compile(
    r"[~%][^\s`\"']*?[/\\]\.jiuwenswarm[/\\][^\s`\"']*?skills[/\\]([\w.-]+)"
)


def _builtin_skill_dirs() -> list[Path]:
    return sorted(
        path
        for path in _BUILTIN_SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def test_builtin_skills_root_is_populated():
    assert _builtin_skill_dirs(), f"no builtin skills under {_BUILTIN_SKILLS_ROOT}"


@pytest.mark.parametrize(
    "skill_dir", _builtin_skill_dirs(), ids=lambda path: path.name
)
def test_hardcoded_skill_paths_name_their_own_skill(skill_dir: Path):
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    referenced = {match.group(1) for match in _SKILLS_ROOT_RE.finditer(text)}

    assert referenced <= {skill_dir.name}, (
        f"{skill_dir.name}/SKILL.md hardcodes paths under other skill "
        f"directories: {sorted(referenced - {skill_dir.name})}"
    )


@pytest.mark.parametrize(
    "skill_dir", _builtin_skill_dirs(), ids=lambda path: path.name
)
def test_hardcoded_skill_paths_use_the_current_skills_root(skill_dir: Path):
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    for match in _SKILLS_ROOT_RE.finditer(text):
        normalized = match.group(0).replace("\\", "/")
        assert _EXPECTED_ROOT_TAIL in normalized, (
            f"{skill_dir.name}/SKILL.md uses a stale skills root: {match.group(0)}"
        )
