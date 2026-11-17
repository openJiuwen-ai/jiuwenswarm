"""Exercise migrated capability scripts against isolated skill libraries."""

import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import utils


CAPABILITIES = utils.get_builtin_skills_dir() / "workswarm-builtin" / "references" / "capabilities"


def test_omni_scripts_write_and_finalize_in_the_host_library(tmp_path, monkeypatch):
    library = tmp_path / "skills"
    scripts = library / "workswarm-builtin" / "references" / "capabilities" / "skill-omni-creation" / "scripts"
    shutil.copytree(CAPABILITIES / "skill-omni-creation" / "scripts", scripts)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: library)
    spec = importlib.util.spec_from_file_location("common", scripts / "common.py")
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    monkeypatch.setitem(sys.modules, "common", common)
    monkeypatch.setitem(sys.modules, "environment_gate", SimpleNamespace(ensure_environment=lambda *a: None))
    stage = common.work_path("generated", "stage01.json")
    common.write_json(stage, {"blocks": [], "title": "Generated"})
    monkeypatch.setattr(sys, "argv", ["save_images.py", "generated", "--keep"])
    runpy.run_path(str(scripts / "save_images.py"), run_name="__main__")
    generated = library / "generated"
    assert (generated / "references").is_dir()
    assert not (scripts.parent.parent / "generated").exists()
    assert common.load_json(common.work_path("generated", "stage03.json"))["title"] == "Generated"

    output_scripts = generated / "scripts"
    output_scripts.mkdir()
    (output_scripts / "verified.py").write_text("pass", encoding="utf-8")
    (output_scripts / "failed.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["finalize_scripts.py", "generated", "--keep", "verified.py"])
    runpy.run_path(str(scripts / "finalize_scripts.py"), run_name="__main__")
    assert (output_scripts / "verified.py").is_file()
    assert not (output_scripts / "failed.py").exists()
    result = common.load_json(common.work_path("generated", "code_validation.json"))
    assert result["kept_scripts"] == ["verified.py"]
    assert result["mode"] == "with_scripts"


def test_ppt_sync_updates_the_active_catalog_and_manifests(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the runtime sync script")
    relative = Path("skills/workswarm-builtin/references/capabilities/ppt-creation")
    source_workspace = tmp_path / "repo" / "workspace"
    source = source_workspace / relative
    (source / "scripts").mkdir(parents=True)
    script = source / "scripts" / "sync-runtime-skill.js"
    shutil.copyfile(CAPABILITIES / "ppt-creation" / "scripts" / script.name, script)
    (source / "SKILL.md").write_text("new version", encoding="utf-8")
    home = tmp_path / "home"
    runtime_workspace = home / ".jiuwenswarm" / "agent" / "workspace"
    target = runtime_workspace / relative
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old version", encoding="utf-8")
    old_manifest = {"workspace/skills/ppt-creation": [
        {"name": "SKILL.md", "path": "agent/workspace/skills/ppt-creation/SKILL.md"},
    ]}
    key = "workspace/skills/workswarm-builtin/references/capabilities/ppt-creation"
    old_manifest[key] = [{"name": "SKILL.md", "path": "stale-root-path"}]
    for workspace in (source_workspace, runtime_workspace):
        (workspace / "agent-data.json").write_text(json.dumps(old_manifest), encoding="utf-8")
    environment = dict(os.environ, HOME=str(home))
    result = subprocess.run([node, str(script)], env=environment, capture_output=True, text=True, check=True)
    assert "ALL IN SYNC" in result.stdout
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "new version"
    assert not (runtime_workspace / "skills" / "ppt-creation").exists()
    for workspace in (source_workspace, runtime_workspace):
        manifest = json.loads((workspace / "agent-data.json").read_text(encoding="utf-8"))
        assert "workspace/skills/ppt-creation" not in manifest
        assert len(manifest[key]) == 1
        assert manifest[key][0]["path"] == f"agent/{key}/SKILL.md"
        assert manifest[f"{key}/scripts"][0]["path"] == f"agent/{key}/scripts/{script.name}"
