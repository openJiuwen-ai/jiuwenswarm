"""Validate bundle delivery through the skill's actual packaging helper."""

import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.bundle import read_bundle_spec
from openjiuwen.core.extension.package_manager import PackageManager


SKILL_ROOT = (
    Path(__file__).parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities"
    / "jiuwen-system-plugin-dev"
)


def _builder():
    spec = importlib.util.spec_from_file_location("bundle_skill_builder", SKILL_ROOT / "scripts/build_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_package


def _write_manifest(root: Path, document: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")


def _source(root: Path) -> Path:
    _write_manifest(root, {
        "schemaVersion": 1, "package_type": "extension", "is_bundle": True,
        "package": {"id": "test.bundle", "version": "0.0.1"},
        "display": {"name": "Bundle"}, "members": [{"path": "a"}, {"path": "b"}],
    })
    for name, domain in (("a", "agentserver"), ("b", "gateway")):
        _write_manifest(root / name, {
            "schemaVersion": 1, "package_type": "extension",
            "package": {"id": f"test.{name}", "version": "0.0.1", "domain": domain},
            "extensions": [{"id": "main", "runtime": "backend", "module": "entry.py"}],
        })
        (root / name / "entry.py").write_text("raise RuntimeError('must not execute while packaging')\n")
    return root


def test_bundle_archive_is_readable_by_core_without_executing_members(tmp_path: Path) -> None:
    archive = _builder()(_source(tmp_path / "source"), tmp_path / "bundle.zip")
    with ZipFile(archive) as zip_file:
        assert {"manifest.json", "a/manifest.json", "b/manifest.json", "a/entry.py", "b/entry.py"} <= set(
            zip_file.namelist(),
        )
    with PackageManager(tmp_path / "opened").open_source(archive) as opened:
        spec = read_bundle_spec(opened.root)
        assert {member.domain for member in spec.members} == {"agentserver", "gateway"}


@pytest.mark.parametrize("invalid", ["duplicate", "missing", "cycle", "nested", "excluded-entry"])
def test_invalid_bundle_cannot_be_delivered(tmp_path: Path, invalid: str) -> None:
    root = _source(tmp_path / "source")
    manifest = root / "a/manifest.json"
    document = json.loads(manifest.read_text())
    if invalid == "duplicate":
        document["package"]["id"] = "test.b"
    elif invalid == "missing":
        (root / "b/manifest.json").unlink()
    elif invalid == "nested":
        document["is_bundle"] = True
        document["members"] = [{"path": "child"}]
    elif invalid == "cycle":
        document["requires"] = {"packages": [{"id": "test.b", "version": ">=0.0.1"}]}
        other = json.loads((root / "b/manifest.json").read_text())
        other["requires"] = {"packages": [{"id": "test.a", "version": ">=0.0.1"}]}
        _write_manifest(root / "b", other)
    elif invalid == "excluded-entry":
        document["extensions"][0]["module"] = "node_modules/entry.py"
        (root / "a/node_modules").mkdir()
        (root / "a/node_modules/entry.py").write_text("pass\n")
    _write_manifest(root / "a", document)
    output = tmp_path / "invalid.zip"
    with pytest.raises((ExtensionFailure, ValueError)):
        _builder()(root, output)
    assert not output.exists()
