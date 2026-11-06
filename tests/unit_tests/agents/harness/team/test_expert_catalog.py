# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenExpertCatalog: which expert packages are installed, and where (SDD-0032)."""

from __future__ import annotations

import builtins
import asyncio
import json
from pathlib import Path
from contextvars import ContextVar

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
@pytest.mark.parametrize("kind", ["agent_template", "agent_group"])
async def test_catalog_does_no_blocking_file_io(roots: dict, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    marketplace, local, _ = roots[kind]
    _package(local, "mine")
    _marketplace(marketplace, {"id": "mine", "installed": True})

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("blocking file IO in the expert catalog")

    monkeypatch.setattr(builtins, "open", _refuse)
    for name in ("read_text", "is_dir", "is_file", "exists", "iterdir", "resolve"):
        monkeypatch.setattr(Path, name, _refuse)
    catalog = JiuwenExpertCatalog()
    reader = catalog.list_group_packages if kind == "agent_group" else catalog.list_expert_packages
    assert [item.name for item in await reader()] == ["mine"]


@pytest.mark.asyncio
async def test_group_allowlist_keeps_concurrent_request_contexts_separate(roots, monkeypatch):
    from jiuwenswarm.enterprise.capabilities.experts import request_packages as package_allowlist

    marketplace, local, _ = roots["agent_group"]
    for name in ("alpha", "beta"):
        _package(local, name)
    _marketplace(marketplace, {"id": "alpha", "installed": True}, {"id": "beta", "installed": True})
    permitted = ContextVar("permitted_group", default="")
    monkeypatch.setattr(package_allowlist, "allows", lambda kind, name: name == permitted.get())

    async def read_as(name):
        token = permitted.set(name)
        try:
            return [item.name for item in await JiuwenExpertCatalog().list_group_packages()]
        finally:
            permitted.reset(token)

    assert await asyncio.gather(read_as("alpha"), read_as("beta")) == [["alpha"], ["beta"]]


@pytest.mark.asyncio
async def test_locate_group_denies_before_resolving_a_package(roots, monkeypatch):
    from jiuwenswarm.enterprise.capabilities.experts import request_packages as package_allowlist

    def denied(kind, name):
        raise ValueError("package not allowed")

    monkeypatch.setattr(package_allowlist, "require_allowed", denied)
    with pytest.raises(ValueError, match="not allowed"):
        await JiuwenExpertCatalog().locate_group("hidden")


def test_install_injects_the_catalog_and_tolerates_an_older_openjiuwen(monkeypatch, caplog) -> None:
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager

    try:
        install_expert_catalog()
        assert isinstance(TeamRuntimeManager.expert_catalog(), JiuwenExpertCatalog)
    finally:
        TeamRuntimeManager.set_expert_catalog(None)

    monkeypatch.delattr(TeamRuntimeManager, "set_expert_catalog")
    install_expert_catalog()
    assert "AGENT_GROUP_CATALOG_NOT_CONFIGURED" in caplog.text
    assert "does not support expert catalog registration" in caplog.text


def test_install_reports_registration_that_does_not_take_effect(monkeypatch, caplog):
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager

    monkeypatch.setattr("openjiuwen.agent_teams.experts.catalog._process_catalog", None)
    monkeypatch.setattr(TeamRuntimeManager, "set_expert_catalog", lambda _catalog: None)

    install_expert_catalog()

    assert "AGENT_GROUP_CATALOG_NOT_CONFIGURED" in caplog.text
    assert "registration did not take effect" in caplog.text
