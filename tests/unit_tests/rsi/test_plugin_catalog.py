"""RSI catalog adaptation with the real registry and native package loader."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from openjiuwen.harness.resources import load_plugin_package
from jiuwenswarm.agents.harness.common.rsi.harness_activation import hash_harness_package
from jiuwenswarm.agents.harness.common.rsi.plugin_catalog import register_harness_plugin
from jiuwenswarm.server.runtime import extension_package_manager as catalog
from tests.unit_tests.rsi.test_plugin_roundtrip import _agent, _PRESETS


def _package(tmp_path, *, legacy=False):
    source = tmp_path / "published"
    shutil.copytree(_PRESETS / "coding-guard", source)
    manifest = source / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    skill = source / "skills" / "verification"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: verification\ndescription: Verify changes before delivery.\n---\nRun relevant checks.\n",
        encoding="utf-8",
    )
    payload["skills"] = [{"dir": "skills/verification", "mode": "auto_list"}]
    payload["prompt_sections"] = [{"name": "verification", "content": {"en": "Verify before delivery."}}]
    if legacy:
        manifest.unlink()
        payload = {key: payload[key] for key in ("tools", "rails", "skills", "prompt_sections")}
        for field, kind in (("tools", "tool"), ("rails", "rail")):
            payload[field] = [
                {"type": f"harness.{kind}.file", "params": {"file_path": item["file"], "class_name": item["class"]}}
                for item in payload[field]
            ]
        payload.update(extension_name="coding-guard", schema_version="expert_harness.v1")
        (source / "harness_config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    else:
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    return source


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_catalog_copy_preserves_all_four_capabilities_and_survives_source_removal(tmp_path, legacy):
    source = _package(tmp_path, legacy=legacy)
    original_hash = hash_harness_package(source)
    package_id = f"rsi-harness-{original_hash[:16]}"
    register_harness_plugin(source, package_id)
    assert hash_harness_package(source) == original_hash
    cards = catalog.list_plugin_packages({"filter": "local"})
    assert [card["id"] for card in cards] == [package_id]
    assert cards[0]["installed"] is True
    assert "RSI " in cards[0]["displayName"]["en"]
    installed = catalog.resolve_plugin_dir(package_id)
    assert "plugins/plugin_packages/local" in installed.as_posix()
    spec = load_plugin_package(installed / "manifest.json")
    assert spec.id == package_id
    assert spec.prompt_sections[0].content["en"] == "Verify before delivery."
    assert spec.skills[0].mode == "auto_list"
    for path in [spec.skills[0].dir, spec.tools[0].params["file_path"], spec.rails[0].params["file_path"]]:
        assert Path(path).is_relative_to(installed)
    shutil.rmtree(source)
    agent = _agent()
    record = await agent.load_plugin(str(installed))
    try:
        assert {ref.kind.value for ref in record.refs} >= {"skill", "tool", "rail", "prompt_section"}
    finally:
        await agent.unload_extension(record)


def test_catalog_registration_does_not_overwrite_edited_version(tmp_path):
    source = _package(tmp_path)
    package_id = "rsi-harness-aabbcc"
    register_harness_plugin(source, package_id)
    installed = catalog.resolve_plugin_dir(package_id)
    edited = installed / "skills/verification/SKILL.md"
    edited.write_text("user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="modified"):
        register_harness_plugin(source, package_id)
    assert edited.read_text(encoding="utf-8") == "user edit"
    assert catalog.is_plugin_allowed(package_id)


def test_versioned_registration_preserves_original_plugin_and_other_versions(tmp_path):
    source = _package(tmp_path)
    catalog.import_plugin_package({"path": str(source)})
    catalog.install_plugin_package({"id": "coding-guard"})
    original = hash_harness_package(catalog.resolve_plugin_dir("coding-guard"))
    register_harness_plugin(source, "rsi-harness-first")
    undo = register_harness_plugin(source, "rsi-harness-second")
    undo()
    assert hash_harness_package(catalog.resolve_plugin_dir("coding-guard")) == original
    assert catalog.is_plugin_allowed("rsi-harness-first")
    assert catalog.show_plugin_package("rsi-harness-second") is None


def test_registration_undo_preserves_preexisting_import(tmp_path):
    source = _package(tmp_path)
    package_id = "rsi-harness-aabbcc"
    register_harness_plugin(source, package_id)
    catalog.upsert_plugin_marketplace_entry(package_id, installed=False, source="local")
    undo = register_harness_plugin(source, package_id)
    assert catalog.is_plugin_allowed(package_id)
    undo()
    assert not catalog.is_plugin_allowed(package_id)
    assert catalog.resolve_plugin_dir(package_id).is_dir()


def test_install_failure_removes_only_new_catalog_copy(tmp_path, monkeypatch):
    source = _package(tmp_path)
    register_harness_plugin(source, "rsi-harness-previous")
    def fail_install(params):
        raise OSError("registry unavailable")
    monkeypatch.setattr(catalog, "install_plugin_package", fail_install)
    with pytest.raises(OSError, match="registry unavailable"):
        register_harness_plugin(source, "rsi-harness-next")
    assert catalog.show_plugin_package("rsi-harness-next") is None
    assert catalog.is_plugin_allowed("rsi-harness-previous")


def test_import_failure_does_not_leave_an_unregistered_directory(tmp_path, monkeypatch, rsi_catalog_workspace):
    source = _package(tmp_path)
    def fail_registry(*args, **kwargs):
        raise OSError("registry write failed")
    monkeypatch.setattr(catalog, "upsert_plugin_marketplace_entry", fail_registry)
    with pytest.raises(OSError, match="registry write failed"):
        register_harness_plugin(source, "rsi-harness-failed")
    assert not (rsi_catalog_workspace / "plugins/plugin_packages/local/rsi-harness-failed").exists()
