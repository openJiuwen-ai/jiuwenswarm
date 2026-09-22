# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the single-agent SkillUseRail ``external_only`` wiring.

``_build_skill_rail`` receives the ``react`` sub-configuration as ``config``;
``skills.external_only`` lives at the root of the config (sibling to ``react``),
so it must be read from ``config_base`` (or ``self._config_base_cache``), never
from ``config`` — otherwise the benchmark / CI mode is silently disabled.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from openjiuwen.harness.rails import SkillUseRail

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _make_adapter(
    *,
    config_base: dict | None,
    external_dirs: list[str],
    personal_dir: str,
) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = config_base
    adapter._config_cache = {"react": {}}
    manager = MagicMock()
    manager.get_external_skill_dirs.return_value = [Path(p) for p in external_dirs]
    manager.list_execution_disabled_skills.return_value = []
    adapter._skill_manager = manager
    adapter._resolve_skill_mode = MagicMock(return_value=SkillUseRail.SKILL_MODE_ALL)
    adapter._skill_retrieval_tools_enabled_for_runtime = MagicMock(return_value=False)
    adapter._skill_scan_dirs = MagicMock(return_value=[personal_dir])
    return adapter


def _build_rail(adapter: JiuWenSwarmDeepAdapter, **kwargs) -> SkillUseRail:
    rail = adapter._build_skill_rail({"some": "react sub-config"}, include_tools=False, **kwargs)
    assert rail is not None
    return rail


def test_external_only_read_from_config_base_not_react_config(tmp_path: Path) -> None:
    """``skills.external_only`` is honored even though ``config`` is the react
    sub-configuration that never carries a ``skills`` key."""
    personal = str(tmp_path / "skills")
    external = [str(tmp_path / "external-a"), str(tmp_path / "external-b")]
    adapter = _make_adapter(
        config_base={"skills": {"external_only": True}},
        external_dirs=external,
        personal_dir=personal,
    )

    rail = _build_rail(adapter, config_base={"skills": {"external_only": True}})

    assert rail.skills_dir == external
    assert personal not in rail.skills_dir


def test_external_only_falls_back_to_config_base_cache(tmp_path: Path) -> None:
    """Omitting ``config_base`` falls back to ``self._config_base_cache``."""
    personal = str(tmp_path / "skills")
    external = [str(tmp_path / "external-a")]
    adapter = _make_adapter(
        config_base={"skills": {"external_only": True}},
        external_dirs=external,
        personal_dir=personal,
    )

    rail = _build_rail(adapter)

    assert rail.skills_dir == external


def test_without_external_only_keeps_personal_and_external(tmp_path: Path) -> None:
    """Default mode appends external dirs to the personal library."""
    personal = str(tmp_path / "skills")
    external = [str(tmp_path / "external-a")]
    adapter = _make_adapter(
        config_base={"skills": {"external_dirs": external, "external_only": False}},
        external_dirs=external,
        personal_dir=personal,
    )

    rail = _build_rail(adapter, config_base={"skills": {"external_only": False}})

    assert rail.skills_dir == [personal, *external]
