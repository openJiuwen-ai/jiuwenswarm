# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill_acceleration_exec 工具描述：内置 + 外部 turbo 双源扫描。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_min_skill(root: Path, external: str, skill_name: str):
    codes = root / external / "turbo" / "turbo_codes" / skill_name
    codes.mkdir(parents=True)
    (codes / f"{skill_name}_gen_root.py").write_text(
        "from skill_turbo_runtime import PlanNode\n"
        "class _R(PlanNode):\n"
        "    def __init__(self):\n"
        "        super().__init__(plan_name='r', instruction='i')\n"
        "    async def _execute(self, inputs):\n"
        "        return inputs\n"
        "root = _R()\n",
        encoding="utf-8",
    )
    (root / external / "turbo" / "meta.json").write_text(
        '{"external_name": "%s", "description": "外部 %s", "match_keywords": ["extkw"]}'
        % (external, skill_name),
        encoding="utf-8",
    )


def _patch_discover_to(tmp_path, monkeypatch):
    """把 discover 的默认 roots 固定为 tmp_path（保留真实发现逻辑）。"""
    from jiuwenswarm.server.runtime.skill_turbo import (
        turbo_package_loader as tpl,
    )

    orig = tpl.discover_external_turbo_skills
    monkeypatch.setattr(
        tpl,
        "discover_external_turbo_skills",
        lambda skill_roots=None: orig(skill_roots=[tmp_path]),
    )


@pytest.fixture(autouse=True)
def _isolated_entries_cache(monkeypatch):
    """隔离模块级 TTL 清单缓存：缓存 key 取自真实 roots 而测试用 patch
    discover 驱动不同结果，不隔离会跨测试命中陈旧缓存。"""
    import jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools as stt

    monkeypatch.setattr(stt, "_entries_cache", {})


def test_description_includes_external_skills(tmp_path, monkeypatch):
    _write_min_skill(tmp_path, "pptx-craft", "ppt")
    _write_min_skill(tmp_path, "docx-craft", "docx")

    import jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools as stt

    _patch_discover_to(tmp_path, monkeypatch)
    desc = stt._build_tool_description()
    assert "pptx-craft" in desc
    assert "外部 ppt" in desc
    assert "docx-craft" in desc
    assert "extkw" in desc


def test_description_dedup_external_wins(tmp_path, monkeypatch):
    """external_name 去重：同名条目只出现一次。"""
    _write_min_skill(tmp_path, "pptx-craft", "ppt")
    import jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools as stt

    _patch_discover_to(tmp_path, monkeypatch)
    desc = stt._build_tool_description()
    assert desc.count("pptx-craft（") == 1  # external_name 作为条目主名仅一次
