# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared fixtures for extension package (expert / plugin) unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import jiuwenswarm.common.utils as utils
from jiuwenswarm.server.runtime import extension_package_manager as catalog

AGENT_TEMPLATES = "agent_templates"
PLUGIN_PACKAGES = "plugin_packages"


@pytest.fixture
def extension_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate equipment workspace and hide the real resources shelf."""
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / ".jiuwenswarm")
    monkeypatch.setattr(catalog, "get_equipment_resources_agent_templates_dir", lambda: None)
    monkeypatch.setattr(catalog, "get_equipment_resources_plugin_packages_dir", lambda: None)
    return utils.get_agent_workspace_dir()


def point_resources_shelf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    experts: list[str] | None = None,
    plugins: list[str] | None = None,
) -> Path:
    """Point catalog getters at a temp resources/{agent_templates,plugin_packages} tree."""
    root = tmp_path / "fake_resources_plugins"
    for name in experts or []:
        pkg = root / AGENT_TEMPLATES / name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "manifest.json").write_text(
            json.dumps({"packageType": "agent_template"}), encoding="utf-8"
        )
    for name in plugins or []:
        pkg = root / PLUGIN_PACKAGES / name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "manifest.json").write_text(
            json.dumps({"packageType": "plugin"}), encoding="utf-8"
        )
    at = root / AGENT_TEMPLATES
    pp = root / PLUGIN_PACKAGES
    monkeypatch.setattr(
        catalog,
        "get_equipment_resources_agent_templates_dir",
        lambda: at if at.is_dir() else None,
    )
    monkeypatch.setattr(
        catalog,
        "get_equipment_resources_plugin_packages_dir",
        lambda: pp if pp.is_dir() else None,
    )
    return root


def seed_package(
    ws: Path,
    kind: str,
    package_id: str,
    *,
    under: str = "local",
    installed: bool | None = None,
    connectors: list[str] | tuple[str, ...] = (),
    extra_manifest: dict | None = None,
) -> Path:
    """Write one package body; optionally upsert marketplace flags."""
    package_type = "agent_template" if kind == AGENT_TEMPLATES else "plugin"
    pkg = ws / "plugins" / kind / under / package_id
    pkg.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"packageType": package_type}
    if extra_manifest:
        manifest.update(extra_manifest)
    if connectors:
        mcps = list(manifest.get("mcps") or [])
        mcps.extend({"connector": name} for name in connectors)
        manifest["mcps"] = mcps
    (pkg / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if installed is not None:
        source = "builtin" if under == "built_in" else "local"
        if kind == AGENT_TEMPLATES:
            catalog.upsert_agent_template_marketplace_entry(
                package_id, installed=installed, source=source
            )
        else:
            catalog.upsert_plugin_marketplace_entry(
                package_id, installed=installed, source=source
            )
    return pkg


def create_package(kind: str, package_id: str) -> None:
    """Call create RPC helper with the minimum valid params."""
    if kind == AGENT_TEMPLATES:
        catalog.create_agent_template(
            {
                "id": package_id,
                "name": "N",
                "description": "D",
                "persona": "P",
                "skills": [],
            }
        )
        return
    catalog.create_plugin_package(
        {
            "id": package_id,
            "name": "N",
            "description": "D",
            "skills": [],
        }
    )


def marketplace_entries(kind: str) -> list[dict]:
    """Read marketplace entries for one kind."""
    if kind == AGENT_TEMPLATES:
        return catalog.read_agent_template_marketplace_entries()
    return catalog.read_plugin_marketplace_entries()


def list_packages(kind: str, params: dict | None = None) -> list[dict]:
    """List cards for one kind."""
    if kind == AGENT_TEMPLATES:
        return catalog.list_agent_templates(params)
    return catalog.list_plugin_packages(params)
