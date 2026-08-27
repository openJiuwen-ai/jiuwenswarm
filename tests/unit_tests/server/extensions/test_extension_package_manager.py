# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Minimal manager tests: disk model, list/show, gated install, uninstall."""

from __future__ import annotations

import importlib
import json
import zipfile
from pathlib import Path

import pytest

import jiuwenswarm.common.utils as utils
from jiuwenswarm.server.runtime import extension_package_manager as catalog
from jiuwenswarm.server.runtime.mcp import state_store as mcp_state

from tests.unit_tests.server.extensions.conftest import (
    AGENT_GROUPS,
    AGENT_TEMPLATES,
    PLUGIN_PACKAGES,
    create_package,
    marketplace_entries,
    point_resources_shelf,
    seed_package,
)

_KINDS = (AGENT_TEMPLATES, PLUGIN_PACKAGES)


def _seed_valid_agent_group(
    workspace: Path,
    package_id: str,
    *,
    under: str,
) -> Path:
    package = workspace / "plugins" / AGENT_GROUPS / under / package_id
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "name": package_id,
                "package_type": "agent_group",
                "instruction": "Leader 负责汇总，reviewer 负责独立复核。",
                "agents": ["leader", "reviewer"],
                "skills": ["shared-review"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    leader = package / "agents" / "leader"
    leader.mkdir(parents=True)
    (leader / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "agent_template",
                "name": "评审主席",
                "description": "负责组织评审并汇总结论",
                "persona": {"dir": "."},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (leader / "AGENT.md").write_text("# 评审主席\n", encoding="utf-8")

    reviewer = package / "agents" / "reviewer"
    persona = reviewer / "persona"
    persona.mkdir(parents=True)
    (reviewer / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "agent_template",
                "name": "风险复核专家",
                "description": "负责独立风险复核",
                "persona": {"dir": "./persona"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (persona / "reviewer.md").write_text("# 风险复核专家\n", encoding="utf-8")

    skill = package / "skills" / "shared-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: 共享评审规范\n"
        "description: 统一证据、风险和建议的输出结构\n"
        "---\n\n"
        "# 共享评审规范\n",
        encoding="utf-8",
    )
    (package / "README.md").write_text(
        f"# {package_id}\n\n用于验证专家团详情接口。\n",
        encoding="utf-8",
    )
    return package


class TestPrepareWorkspaceAndMarketplace:
    """Lazy-install disk: prepare does not seed; marketplace is installed source of truth."""

    def test_prepare_creates_dirs_without_seeding(self, tmp_path: Path) -> None:
        result = utils.prepare_workspace(overwrite=True, workspace_dir=tmp_path)
        assert result is not None
        plugins = tmp_path / "agent" / "workspace" / "plugins"
        for kind in _KINDS:
            assert (plugins / kind / "built_in").is_dir()
            assert (plugins / kind / "local").is_dir()
            assert not (plugins / kind / "marketplace.json").exists()
            assert not any((plugins / kind / "built_in").iterdir())
            assert not any((plugins / kind / "local").iterdir())
        assert (plugins / AGENT_GROUPS / "built_in").is_dir()
        assert (plugins / AGENT_GROUPS / "local").is_dir()

    def test_prepare_overwrite_true_resets_false_keeps_built_in(self, tmp_path: Path) -> None:
        utils.prepare_workspace(overwrite=True, workspace_dir=tmp_path)
        kind_root = tmp_path / "agent" / "workspace" / "plugins" / AGENT_TEMPLATES
        built_in = kind_root / "built_in" / "kept"
        built_in.mkdir(parents=True)
        (built_in / "manifest.json").write_text(
            json.dumps({"package_type": "agent_template"}), encoding="utf-8"
        )
        marker = built_in / "_keep.txt"
        marker.write_text("keep", encoding="utf-8")
        utils.prepare_workspace(overwrite=False, workspace_dir=tmp_path)
        assert marker.is_file()

        local = kind_root / "local" / "mine"
        local.mkdir(parents=True)
        (local / "manifest.json").write_text(
            json.dumps({"package_type": "agent_template"}), encoding="utf-8"
        )
        (kind_root / "marketplace.json").write_text(
            json.dumps({"plugins": [{"id": "kept", "installed": True}]}),
            encoding="utf-8",
        )
        utils.prepare_workspace(overwrite=True, workspace_dir=tmp_path)
        assert not local.exists()
        assert not built_in.exists()
        assert not (kind_root / "marketplace.json").exists()
        assert (kind_root / "built_in").is_dir()
        assert (kind_root / "local").is_dir()

    def test_disk_package_without_marketplace_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extension_workspace: Path
    ) -> None:
        point_resources_shelf(monkeypatch, tmp_path, experts=["preset"])
        seed_package(extension_workspace, AGENT_TEMPLATES, "preset", under="built_in")
        assert catalog.read_agent_template_marketplace_entries() == []
        card = next(c for c in catalog.list_agent_templates() if c["id"] == "preset")
        assert card["installed"] is False
        assert "enabled" not in card

    def test_agentserver_import_does_not_reconcile_equipment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path / ".jiuwenswarm"
        (workspace_root / "config").mkdir(parents=True)
        (workspace_root / "config" / "config.yaml").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(utils, "_workspace_base_dir", workspace_root)

        def _fail() -> None:
            raise AssertionError("AgentServer startup must not initialize equipment workspace")

        monkeypatch.setattr(catalog, "initialize_equipment_workspace", _fail, raising=False)
        from jiuwenswarm.server import app_agentserver

        importlib.reload(app_agentserver)


class TestAgentGroupResolution:
    def test_list_agent_groups_returns_only_loadable_selection_cards(
        self,
        extension_workspace: Path,
    ) -> None:
        _seed_valid_agent_group(
            extension_workspace,
            "local-review",
            under="local",
        )
        _seed_valid_agent_group(
            extension_workspace,
            "builtin-review",
            under="built_in",
        )
        invalid = (
            extension_workspace
            / "plugins"
            / AGENT_GROUPS
            / "local"
            / "invalid-group"
        )
        invalid.mkdir(parents=True)
        (invalid / "manifest.json").write_text("{}", encoding="utf-8")

        cards = catalog.list_agent_groups()

        assert [card["name"] for card in cards] == [
            "builtin-review",
            "local-review",
        ]
        built_in = cards[0]
        assert built_in["name"] == "builtin-review"
        assert built_in["source"] == "builtin"
        assert built_in["memberCount"] == 2
        assert [member["id"] for member in built_in["members"]] == [
            "leader",
            "reviewer",
        ]
        assert built_in["members"][0]["role"] == "leader"
        assert built_in["members"][1]["role"] == "member"
        assert built_in["skills"] == [
            {
                "id": "shared-review",
                "displayName": {"zh": "共享评审规范", "en": "共享评审规范"},
                "displayDescription": {
                    "zh": "统一证据、风险和建议的输出结构",
                    "en": "统一证据、风险和建议的输出结构",
                },
            }
        ]
        assert str(extension_workspace) not in json.dumps(cards, ensure_ascii=False)
        assert [card["name"] for card in catalog.list_agent_groups({"filter": "local"})] == [
            "local-review"
        ]
        assert [
            card["name"] for card in catalog.list_agent_groups({"filter": "builtin"})
        ] == ["builtin-review"]

    def test_show_agent_group_returns_readme_detail_without_mcp_contract(
        self,
        extension_workspace: Path,
    ) -> None:
        _seed_valid_agent_group(
            extension_workspace,
            "local-review",
            under="local",
        )

        card = catalog.show_agent_group("local-review")

        assert card is not None
        assert card["name"] == "local-review"
        assert card["source"] == "local"
        assert card["memberCount"] == 2
        assert card["details"].startswith("# local-review")
        assert "mcps" not in card
        assert "connection_state" not in card
        assert "path" not in json.dumps(card, ensure_ascii=False)

    def test_show_agent_group_returns_none_when_missing(
        self,
        extension_workspace: Path,
    ) -> None:
        assert catalog.show_agent_group("missing-group") is None

    def test_resolve_local_agent_group(
        self,
        extension_workspace: Path,
    ) -> None:
        package = (
            extension_workspace
            / "plugins"
            / AGENT_GROUPS
            / "local"
            / "finance-group"
        )
        package.mkdir(parents=True)
        (package / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "finance-group",
                    "package_type": "agent_group",
                    "agents": ["leader"],
                }
            ),
            encoding="utf-8",
        )

        assert catalog.resolve_agent_group_dir("finance-group") == package.resolve()

    def test_resolve_agent_group_rejects_conflict(
        self,
        extension_workspace: Path,
    ) -> None:
        for source in ("local", "built_in"):
            package = (
                extension_workspace
                / "plugins"
                / AGENT_GROUPS
                / source
                / "duplicate"
            )
            package.mkdir(parents=True)
            (package / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "duplicate",
                        "package_type": "agent_group",
                        "agents": ["leader"],
                    }
                ),
                encoding="utf-8",
            )

        with pytest.raises(ValueError, match="package conflict"):
            catalog.resolve_agent_group_dir("duplicate")
        with pytest.raises(ValueError, match="package conflict"):
            catalog.list_agent_groups()


class TestCreateInstallUninstall:
    """create → local/; install copy or flip flags; uninstall deletes user copy."""

    @pytest.mark.parametrize("kind", _KINDS)
    def test_create_writes_local_uninstalled(
        self, extension_workspace: Path, kind: str
    ) -> None:
        create_package(kind, "mine")
        pkg = extension_workspace / "plugins" / kind / "local" / "mine"
        assert pkg.is_dir()
        assert not (extension_workspace / "plugins" / kind / "built_in" / "mine").exists()
        manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
        if kind == AGENT_TEMPLATES:
            from openjiuwen.harness.resources import load_agent_template_package

            assert manifest["package_type"] == "agent_template"
            assert "persona" in manifest
            assert manifest["name"] == "mine"
            assert manifest["description"] == "D"
            assert "agentCard" not in manifest
            template = load_agent_template_package(pkg / "manifest.json")
            assert template.agent_card.name == "mine"
        else:
            from openjiuwen.harness.resources import load_plugin_package

            assert manifest["package_type"] == "plugin"
            assert "persona" not in manifest
            assert "agentCard" not in manifest
            plugin = load_plugin_package(pkg / "manifest.json")
            assert plugin.id == "mine"
        entry = next(e for e in marketplace_entries(kind) if e["id"] == "mine")
        assert entry["installed"] is False
        assert entry["source"] == "local"
        assert set(entry) == {"id", "source", "installed"}
        assert "mcps" not in manifest

    @pytest.mark.parametrize("kind", _KINDS)
    def test_create_writes_mcp_connectors(
        self, extension_workspace: Path, kind: str
    ) -> None:
        params = {
            "id": "mine",
            "name": "N",
            "description": "D",
            "skills": [],
            "mcps": ["amap", "feishu"],
        }
        if kind == AGENT_TEMPLATES:
            catalog.create_agent_template({**params, "persona": "P"})
        else:
            catalog.create_plugin_package(params)
        pkg = extension_workspace / "plugins" / kind / "local" / "mine"
        manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["mcps"] == [
            {"connector": "amap"},
            {"connector": "feishu"},
        ]

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("conflict", ["local", "built_in", "resources"])
    def test_create_rejects_same_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extension_workspace: Path,
        kind: str,
        conflict: str,
    ) -> None:
        if conflict == "resources":
            kwargs = (
                {"experts": ["dup"]} if kind == AGENT_TEMPLATES else {"plugins": ["dup"]}
            )
            point_resources_shelf(monkeypatch, tmp_path, **kwargs)
        else:
            seed_package(extension_workspace, kind, "dup", under=conflict)
        with pytest.raises(ValueError, match="already exists"):
            create_package(kind, "dup")
        if conflict != "local":
            assert not (extension_workspace / "plugins" / kind / "local" / "dup").exists()

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("origin", ["preset", "local"])
    def test_install_copies_preset_or_flips_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extension_workspace: Path,
        kind: str,
        origin: str,
    ) -> None:
        package_id = "preset-pkg" if origin == "preset" else "my-local"
        if origin == "preset":
            kwargs = (
                {"experts": [package_id]}
                if kind == AGENT_TEMPLATES
                else {"plugins": [package_id]}
            )
            point_resources_shelf(monkeypatch, tmp_path, **kwargs)
        else:
            seed_package(extension_workspace, kind, package_id, under="local")
        if kind == AGENT_TEMPLATES:
            catalog.install_agent_template({"id": package_id})
        else:
            catalog.install_plugin_package({"id": package_id})
        built_in = extension_workspace / "plugins" / kind / "built_in" / package_id
        local = extension_workspace / "plugins" / kind / "local" / package_id
        if origin == "preset":
            assert built_in.is_dir()
            assert not local.exists()
            source = "builtin"
        else:
            assert local.is_dir()
            assert not built_in.exists()
            source = "local"
        entry = next(e for e in marketplace_entries(kind) if e["id"] == package_id)
        assert entry["installed"] is True
        assert entry["source"] == source
        assert "enabled" not in entry

    def test_install_missing_does_not_write_marketplace(
        self, extension_workspace: Path
    ) -> None:
        with pytest.raises(ValueError, match="not found"):
            catalog.install_agent_template({"id": "ghost"})
        assert catalog.read_agent_template_marketplace_entries() == []

    @pytest.mark.parametrize("kind", _KINDS)
    @pytest.mark.parametrize("origin", ["preset", "local"])
    def test_uninstall_deletes_user_copy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extension_workspace: Path,
        kind: str,
        origin: str,
    ) -> None:
        package_id = "preset-pkg" if origin == "preset" else "my-local"
        if origin == "preset":
            kwargs = (
                {"experts": [package_id]}
                if kind == AGENT_TEMPLATES
                else {"plugins": [package_id]}
            )
            point_resources_shelf(monkeypatch, tmp_path, **kwargs)
        else:
            seed_package(extension_workspace, kind, package_id)
        if kind == AGENT_TEMPLATES:
            catalog.install_agent_template({"id": package_id})
            catalog.uninstall_agent_template({"id": package_id})
            cards = catalog.list_agent_templates()
        else:
            catalog.install_plugin_package({"id": package_id})
            catalog.uninstall_plugin_package({"id": package_id})
            cards = catalog.list_plugin_packages()
        assert not (
            extension_workspace / "plugins" / kind / "built_in" / package_id
        ).exists()
        assert not (
            extension_workspace / "plugins" / kind / "local" / package_id
        ).exists()
        ids = {c["id"] for c in cards}
        if origin == "preset":
            assert package_id in ids
            assert next(c for c in cards if c["id"] == package_id)["installed"] is False
        else:
            assert package_id not in ids

    def test_uninstall_leaves_connector_state_and_notice(
        self, monkeypatch: pytest.MonkeyPatch, extension_workspace: Path
    ) -> None:
        pkg = seed_package(
            extension_workspace,
            AGENT_TEMPLATES,
            "with-conn",
            installed=True,
            connectors=["feishu"],
        )
        monkeypatch.setattr(mcp_state, "get_workspace_dir", lambda: extension_workspace)
        mcp_state.upsert_mcp_record(
            "feishu", {"transport": "stdio", "command": "echo"}, state="connected"
        )
        payload = catalog.uninstall_equipment_with_notice(
            AGENT_TEMPLATES, {"id": "with-conn"}
        )
        assert "notice" in payload
        assert not pkg.exists()
        rec = mcp_state.get_mcp_record("feishu")
        assert rec is not None
        assert rec.get("state") == "connected"


def _import_package(kind: str, params: dict) -> dict:
    if kind == AGENT_TEMPLATES:
        return catalog.import_agent_template(params)
    return catalog.import_plugin_package(params)


def _src_manifest(kind: str, package_id: str) -> dict:
    if kind == AGENT_TEMPLATES:
        return {
            "package_type": "agent_template",
            "name": package_id,
            "description": "Imported expert package.",
        }
    return {"package_type": "plugin", "id": package_id}


def _write_src_dir(root: Path, kind: str, package_id: str) -> Path:
    src = root / package_id
    src.mkdir(parents=True)
    (src / "manifest.json").write_text(
        json.dumps(_src_manifest(kind, package_id)), encoding="utf-8"
    )
    return src


def _write_src_zip(root: Path, kind: str, package_id: str) -> Path:
    src = _write_src_dir(root, kind, package_id)
    zip_path = root / f"{package_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, Path(src.name) / path.relative_to(src))
    return zip_path


class TestImportLocal:
    """import_local: path → local/{id}/ + marketplace installed=false."""

    @pytest.mark.parametrize("kind", _KINDS)
    def test_import_zip_writes_local_uninstalled(
        self, extension_workspace: Path, tmp_path: Path, kind: str
    ) -> None:
        zip_path = _write_src_zip(tmp_path, kind, "office-kit")
        result = _import_package(kind, {"path": str(zip_path)})
        assert result == {"id": "office-kit"}
        dest = extension_workspace / "plugins" / kind / "local" / "office-kit"
        assert dest.is_dir()
        assert (dest / "manifest.json").is_file()
        entry = next(e for e in marketplace_entries(kind) if e["id"] == "office-kit")
        assert entry["installed"] is False
        assert entry["source"] == "local"

    @pytest.mark.parametrize("kind", _KINDS)
    def test_import_dir_writes_local_uninstalled(
        self, extension_workspace: Path, tmp_path: Path, kind: str
    ) -> None:
        src = _write_src_dir(tmp_path, kind, "from-dir")
        result = _import_package(kind, {"path": str(src)})
        assert result == {"id": "from-dir"}
        dest = extension_workspace / "plugins" / kind / "local" / "from-dir"
        assert dest.is_dir()
        entry = next(e for e in marketplace_entries(kind) if e["id"] == "from-dir")
        assert entry["installed"] is False
        assert entry["source"] == "local"

    @pytest.mark.parametrize("kind", _KINDS)
    def test_import_rejects_existing_id(
        self, extension_workspace: Path, tmp_path: Path, kind: str
    ) -> None:
        create_package(kind, "mine")
        pkg = extension_workspace / "plugins" / kind / "local" / "mine"
        marker = pkg / "_keep.txt"
        marker.write_text("keep", encoding="utf-8")
        src = _write_src_dir(tmp_path, kind, "mine")
        with pytest.raises(ValueError, match="already exists"):
            _import_package(kind, {"path": str(src)})
        assert marker.read_text(encoding="utf-8") == "keep"
        manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
        if kind == AGENT_TEMPLATES:
            assert manifest["name"] == "mine"
            assert "agent_card" not in manifest
            assert (pkg / "persona" / "mine.md").is_file()
        else:
            assert manifest["id"] == "mine"

    @pytest.mark.parametrize("kind", _KINDS)
    def test_import_rejects_relative_path(
        self, extension_workspace: Path, kind: str
    ) -> None:
        with pytest.raises(ValueError):
            _import_package(kind, {"path": "packs/office-kit"})
        local_root = extension_workspace / "plugins" / kind / "local"
        assert not local_root.exists() or not any(local_root.iterdir())

    @pytest.mark.parametrize("kind", _KINDS)
    def test_import_rejects_missing_manifest(
        self, extension_workspace: Path, tmp_path: Path, kind: str
    ) -> None:
        src = tmp_path / "no-manifest"
        src.mkdir()
        (src / "README.md").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            _import_package(kind, {"path": str(src)})
        dest = extension_workspace / "plugins" / kind / "local"
        assert not dest.exists() or not any(dest.iterdir())

    def test_import_rejects_wrong_package_type(
        self, extension_workspace: Path, tmp_path: Path
    ) -> None:
        zip_path = _write_src_zip(tmp_path, PLUGIN_PACKAGES, "office-kit")
        with pytest.raises(ValueError):
            catalog.import_agent_template({"path": str(zip_path)})
        assert not (
            extension_workspace / "plugins" / AGENT_TEMPLATES / "local" / "office-kit"
        ).exists()
        assert not (
            extension_workspace / "plugins" / PLUGIN_PACKAGES / "local" / "office-kit"
        ).exists()


class TestInstallPendingConnectorsGate:
    """Two-phase install: read-only gate, no connect_mcp, pending then retry."""

    @pytest.mark.parametrize("state", [None, "connecting", "disconnected"])
    def test_unready_returns_pending_without_writing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extension_workspace: Path,
        state: str | None,
    ) -> None:
        point_resources_shelf(monkeypatch, tmp_path, experts=["preset-conn"])
        manifest = tmp_path / "fake_resources_plugins" / AGENT_TEMPLATES / "preset-conn" / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "package_type": "agent_template",
                    "mcps": [{"connector": "feishu"}],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            catalog,
            "get_mcp_record",
            lambda _n: None if state is None else {"state": state},
        )
        ok, payload = catalog.install_equipment_gated(
            AGENT_TEMPLATES, {"id": "preset-conn"}
        )
        assert ok is False
        assert payload["pending_connectors"] == ["feishu"]
        assert "connector not connected" in payload["error"]
        assert catalog.read_agent_template_marketplace_entries() == []
        assert not (
            extension_workspace / "plugins" / AGENT_TEMPLATES / "built_in" / "preset-conn"
        ).exists()

    def test_pending_then_connected_retry_does_not_call_connect_mcp(
        self, monkeypatch: pytest.MonkeyPatch, extension_workspace: Path
    ) -> None:
        seed_package(
            extension_workspace,
            AGENT_TEMPLATES,
            "retry-me",
            connectors=["feishu"],
        )
        records: dict[str, dict | None] = {"feishu": {"state": "connected", "enabled": False}}
        monkeypatch.setattr(catalog, "get_mcp_record", lambda name: records.get(name))
        connect_calls: list[str] = []

        def _boom(*_a, **_k):
            connect_calls.append("connect_mcp")
            raise AssertionError("connect_mcp must not run during install gate")

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.mcp.registry.connect_mcp",
            _boom,
            raising=False,
        )
        def _list_connected_boom():
            raise AssertionError("list_connected_mcps must not be used")

        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.mcp.state_store.list_connected_mcps",
            _list_connected_boom,
        )

        records["feishu"] = None
        ok, payload = catalog.install_equipment_gated(
            AGENT_TEMPLATES, {"id": "retry-me"}
        )
        assert ok is False
        assert payload["pending_connectors"] == ["feishu"]
        assert catalog.read_agent_template_marketplace_entries() == []

        records["feishu"] = {"state": "connected", "enabled": False}
        ok, payload = catalog.install_equipment_gated(
            AGENT_TEMPLATES, {"id": "retry-me"}
        )
        assert ok is True
        assert payload == {}
        assert connect_calls == []
        entry = next(
            e
            for e in catalog.read_agent_template_marketplace_entries()
            if e["id"] == "retry-me"
        )
        assert entry["installed"] is True


class TestListShowAndFileRead:
    """list/show contract, filter, connection_state, file.read user-disk only."""

    def test_list_show_keep_camel_case_card_fields(
        self, extension_workspace: Path
    ) -> None:
        seed_package(
            extension_workspace,
            AGENT_TEMPLATES,
            "named",
            extra_manifest={
                "display_name": {"zh": "专家", "en": "Expert"},
                "display_description": {"zh": "简介", "en": "Desc"},
                "quick_inputs": [{"zh": "问我", "en": "Ask me"}],
                "tools": [
                    {
                        "class": "DemoTool",
                        "display_name": {"zh": "工具", "en": "Tool"},
                        "display_description": {"zh": "做某事", "en": "Does a thing"},
                    }
                ],
            },
        )
        listed = next(c for c in catalog.list_agent_templates() if c["id"] == "named")
        assert listed["displayName"] == {"zh": "专家", "en": "Expert"}
        assert listed["displayDescription"] == {"zh": "简介", "en": "Desc"}
        assert "display_name" not in listed
        shown = catalog.show_agent_template("named")
        assert shown is not None
        assert shown["displayName"] == {"zh": "专家", "en": "Expert"}
        assert shown["quickInputs"] == [{"zh": "问我", "en": "Ask me"}]
        assert "quick_inputs" not in shown
        assert shown["tools"] == [
            {
                "id": "DemoTool",
                "displayName": {"zh": "工具", "en": "Tool"},
                "displayDescription": {"zh": "做某事", "en": "Does a thing"},
            }
        ]

    def test_list_resources_filter_and_card_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extension_workspace: Path
    ) -> None:
        point_resources_shelf(monkeypatch, tmp_path, experts=["preset"], plugins=["preset-pl"])
        seed_package(extension_workspace, AGENT_TEMPLATES, "mine")
        seed_package(extension_workspace, PLUGIN_PACKAGES, "mine-pl")
        experts = catalog.list_agent_templates()
        assert {c["id"] for c in experts} == {"preset", "mine"}
        preset = next(c for c in experts if c["id"] == "preset")
        assert preset["installed"] is False
        assert preset["source"] == "builtin"
        assert "enabled" not in preset
        assert "pending_connectors" not in preset
        assert preset["connection_state"] == "disconnected"
        assert [c["id"] for c in catalog.list_agent_templates({"filter": "builtin"})] == [
            "preset"
        ]
        assert [c["id"] for c in catalog.list_agent_templates({"filter": "local"})] == [
            "mine"
        ]
        assert [c["id"] for c in catalog.list_plugin_packages({"filter": "builtin"})] == [
            "preset-pl"
        ]

    def test_show_pending_connectors_and_resources_shelf(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extension_workspace: Path
    ) -> None:
        point_resources_shelf(monkeypatch, tmp_path, experts=["preset"])
        shown_shelf = catalog.show_agent_template("preset")
        assert shown_shelf is not None
        assert shown_shelf["installed"] is False
        assert shown_shelf["source"] == "builtin"
        assert shown_shelf["pending_connectors"] == []

        seed_package(
            extension_workspace,
            AGENT_TEMPLATES,
            "needs-auth",
            installed=True,
            connectors=["feishu", "amap"],
        )
        records = {"feishu": {"state": "connected"}, "amap": {"state": "connecting"}}
        monkeypatch.setattr(catalog, "get_mcp_record", lambda name: records.get(name))
        shown = catalog.show_agent_template("needs-auth")
        listed = catalog.list_agent_templates()
        assert shown is not None
        assert shown["pending_connectors"] == ["amap"]
        assert shown["connection_state"] == "connecting"
        listed_card = next(c for c in listed if c["id"] == "needs-auth")
        assert listed_card["connection_state"] == "connecting"
        assert "pending_connectors" not in listed_card
        assert "enabled" not in shown

    def test_file_read_user_disk_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extension_workspace: Path
    ) -> None:
        point_resources_shelf(monkeypatch, tmp_path, experts=["preset"])
        with pytest.raises(ValueError, match="not found"):
            catalog.list_agent_template_files("preset")

        pkg = seed_package(extension_workspace, AGENT_TEMPLATES, "alpha")
        (pkg / "README.md").write_text("body", encoding="utf-8")
        (pkg / "model.json").write_text("{}", encoding="utf-8")
        tree = catalog.list_agent_template_files("alpha")
        paths = {n["path"] for n in tree}
        assert "README.md" in paths
        assert "model.json" not in paths
        read = catalog.read_agent_template_file("alpha", "README.md")
        assert read["content"] == "body"
        with pytest.raises((ValueError, RuntimeError)):
            catalog.read_agent_template_file("alpha", "../secret.txt")
        with pytest.raises((ValueError, RuntimeError)):
            catalog.read_agent_template_file("alpha", "model.json")
