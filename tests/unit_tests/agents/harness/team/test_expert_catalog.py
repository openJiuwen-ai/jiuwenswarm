# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenExpertCatalog: which expert packages are installed, and where (SDD-0032)."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.team.experts import JiuwenExpertCatalog, install_expert_catalog
from jiuwenswarm.agents.harness.team.experts import catalog as catalog_module


def _package(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[Path, Path, Path]]:
    layout = {
        kind: (tmp_path / kind / "marketplace.json", tmp_path / kind / "local", tmp_path / kind / "built_in")
        for kind in ("agent_template", "agent_group")
    }
    monkeypatch.setattr(catalog_module, "expert_package_roots", lambda package_type: layout[package_type])
    return layout


def _marketplace(path: Path, *entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": list(entries)}), encoding="utf-8")


@pytest.mark.asyncio
async def test_lists_only_installed_packages_with_a_directory(roots: dict) -> None:
    marketplace, local, built_in = roots["agent_template"]
    _package(built_in, "finance")
    _package(local, "mine")
    _package(local, "uninstalled")
    _marketplace(
        marketplace,
        {"id": "finance", "installed": True, "source": "builtin"},
        {"id": "mine", "installed": True},
        {"id": "mine", "installed": True},
        {"id": "uninstalled", "installed": False},
        {"id": "ghost", "installed": True},
        {"id": "../escape", "installed": True},
    )
    locations = await JiuwenExpertCatalog().list_expert_packages()
    assert [(item.name, item.source) for item in locations] == [("finance", "builtin"), ("mine", "local")]
    assert locations[0].path == (built_in / "finance").resolve()


@pytest.mark.asyncio
async def test_locate_rejects_missing_conflicting_and_uninstalled_groups(roots: dict) -> None:
    marketplace, local, built_in = roots["agent_group"]
    _package(built_in, "designer")
    _package(local, "twice")
    _package(built_in, "twice")
    _marketplace(
        marketplace,
        {"id": "designer", "installed": True, "source": "hub"},
        {"id": "twice", "installed": True},
        {"id": "off", "installed": False},
    )
    catalog = JiuwenExpertCatalog()
    assert (await catalog.locate_group("designer")).source == "hub"
    with pytest.raises(ValueError, match="conflict"):
        await catalog.locate_group("twice")
    with pytest.raises(ValueError, match="not installed"):
        await catalog.locate_group("off")
    assert [item.name for item in await catalog.list_group_packages()] == ["designer"]


@pytest.mark.asyncio
async def test_missing_or_corrupt_marketplace_lists_nothing(roots: dict) -> None:
    marketplace, _, _ = roots["agent_template"]
    assert await JiuwenExpertCatalog().list_expert_packages() == []
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text("{broken", encoding="utf-8")
    assert await JiuwenExpertCatalog().list_expert_packages() == []


@pytest.mark.asyncio
async def test_catalog_does_no_blocking_file_io(roots: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    marketplace, local, _ = roots["agent_template"]
    _package(local, "mine")
    _marketplace(marketplace, {"id": "mine", "installed": True})

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("blocking file IO in the expert catalog")

    monkeypatch.setattr(builtins, "open", _refuse)
    for name in ("read_text", "is_dir", "is_file", "exists", "iterdir", "resolve"):
        monkeypatch.setattr(Path, name, _refuse)
    assert [item.name for item in await JiuwenExpertCatalog().list_expert_packages()] == ["mine"]


def test_install_injects_the_catalog_and_tolerates_an_older_openjiuwen(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager

    try:
        install_expert_catalog()
        assert isinstance(TeamRuntimeManager.expert_catalog(), JiuwenExpertCatalog)
    finally:
        TeamRuntimeManager.set_expert_catalog(None)

    monkeypatch.delattr(TeamRuntimeManager, "set_expert_catalog")
    install_expert_catalog()  # older openjiuwen: silently skipped
