# -*- coding: utf-8 -*-
"""_load_skill_meta 接受条件与回退语义（含仅 external_name 的最小 meta.json）。"""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.server.runtime.skill_turbo.environment import _load_skill_meta


def _make_skill_dir(tmp_path: Path, meta: dict | None) -> Path:
    skill_dir = tmp_path / "demo_skill"
    skill_dir.mkdir()
    if meta is not None:
        (skill_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    return skill_dir


def test_meta_with_only_external_name_is_accepted(tmp_path: Path) -> None:
    """仅含 external_name 的 meta.json 不应被整体丢弃（路由目录名必须生效）。"""
    skill_dir = _make_skill_dir(tmp_path, {"external_name": "demo-external"})
    meta = _load_skill_meta("demo_skill", skill_dir)
    assert meta.get("external_name") == "demo-external"
    # 其余字段走 setdefault 默认
    assert meta.get("description") == "demo_skill 任务流"
    assert meta.get("match_keywords") == ["demo_skill"]


def test_meta_with_full_fields(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(
        tmp_path,
        {
            "external_name": "demo-external",
            "description": "演示任务流",
            "match_keywords": ["demo"],
        },
    )
    meta = _load_skill_meta("demo_skill", skill_dir)
    assert meta == {
        "external_name": "demo-external",
        "description": "演示任务流",
        "match_keywords": ["demo"],
    }


def test_meta_without_known_keys_returns_empty(tmp_path: Path) -> None:
    """只含未知键的 meta.json 视为无效，返回空 dict 走默认。"""
    skill_dir = _make_skill_dir(tmp_path, {"version": 1})
    assert _load_skill_meta("demo_skill", skill_dir) == {}


def test_meta_missing_file_returns_empty(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(tmp_path, None)
    assert _load_skill_meta("demo_skill", skill_dir) == {}


def test_meta_corrupted_json_returns_empty(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo_skill"
    skill_dir.mkdir()
    (skill_dir / "meta.json").write_text("{not valid json", encoding="utf-8")
    assert _load_skill_meta("demo_skill", skill_dir) == {}
