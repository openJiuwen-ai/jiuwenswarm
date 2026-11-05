"""Release metadata must agree even outside uv's project resolver."""

import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import tomlkit

from scripts import prepare_core_wheel as helper


CORE = "openjiuwen @ git+https://example.com/core.git@" + "a" * 40
OLD = "openjiuwen @ git+https://example.com/core.git@" + "b" * 40


def document(name="workswarm"):
    return tomlkit.parse(f'''
[project]
name = "{name}"
version = "1.2.3"
dependencies = ["{OLD}"]
[project.optional-dependencies]
cua = ["{OLD.replace('openjiuwen', 'openjiuwen[cua]')}"]
[tool.uv.sources]
openjiuwen = {{git = "https://example.com/core.git", rev = "{'b' * 40}"}}
[tool.uv]
override-dependencies = ["{OLD} ; extra == 'cua'", "other==2"]
''')


@pytest.mark.parametrize("core", [CORE, "openjiuwen==0.1.20"])
def test_rewrite_preserves_extras_markers_and_unrelated_overrides(core):
    doc = document()
    helper._rewrite_core(doc, helper._core_requirement(core))
    assert doc["project"]["dependencies"] == [core]
    assert doc["project"]["optional-dependencies"]["cua"] == [core.replace("openjiuwen", "openjiuwen[cua]")]
    assert "openjiuwen" not in doc["tool"]["uv"]["sources"]
    assert doc["tool"]["uv"]["override-dependencies"] == [core + ' ; extra == "cua"', "other==2"]


@pytest.mark.parametrize("core", ["openjiuwen>=1", "other==1", "openjiuwen==1.*", "openjiuwen[cua]==1", CORE + "; python_version>'3'", CORE[:-40] + "develop"])
def test_rejects_non_fixed_or_ambiguous_core(core):
    with pytest.raises(ValueError):
        helper._core_requirement(core)


@pytest.mark.parametrize("failure", [None, "fetch", "metadata"])
def test_preparation_builds_paired_metadata_before_changing_root(tmp_path, monkeypatch, capsys, failure):
    manifest = tmp_path / "pyproject.toml"
    doc = document()
    doc["project"]["dependencies"].append("agent-gateway @ git+https://example.com/gateway.git@" + "c" * 40)
    manifest.write_text(tomlkit.dumps(doc), encoding="utf-8")
    before = manifest.read_bytes()
    output = tmp_path / "delivery"

    def run(args, check, stdout=None):
        assert check
        print("build subprocess progress", file=stdout)
        if "fetch" in args and failure == "fetch":
            raise subprocess.CalledProcessError(1, args)
        if "checkout" in args:
            checkout = Path(args[2])
            checkout.mkdir(parents=True, exist_ok=True)
            (checkout / "pyproject.toml").write_text(tomlkit.dumps(document("agent-gateway")), encoding="utf-8")
        if args[:2] == ["uv", "build"]:
            prepared = tomlkit.parse((Path(args[-1]) / "pyproject.toml").read_text(encoding="utf-8"))
            assert prepared["project"]["dependencies"] == [CORE]
            version = prepared["project"]["version"]
            wheels = Path(args[4])
            wheels.mkdir()
            core = OLD if failure == "metadata" else CORE
            with ZipFile(wheels / "agent_gateway.whl", "w") as archive:
                archive.writestr("agent_gateway.dist-info/METADATA", f"Name: agent-gateway\nVersion: {version}\nRequires-Dist: {core}\n")

    monkeypatch.setattr(helper.subprocess, "run", run)
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            helper.prepare_core_wheel(tmp_path, CORE, output)
        assert manifest.read_bytes() == before
        assert not output.exists()
    else:
        helper.prepare_core_wheel(tmp_path, CORE, output)
        result = tomlkit.parse(manifest.read_text(encoding="utf-8"))
        gateway = result["project"]["dependencies"][1]
        assert gateway.startswith("agent-gateway==1.2.3+core.")
        path = result["tool"]["uv"]["sources"]["agent-gateway"]["path"]
        assert Path(path).is_file()
        assert (output / "gateway-build-info.json").is_file()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "build subprocess progress" in captured.err


def test_legacy_checkout_without_gateway_only_updates_core(tmp_path, monkeypatch):
    path = tmp_path / "pyproject.toml"
    path.write_text(tomlkit.dumps(document()), encoding="utf-8")
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **kw: pytest.fail("Unexpected build"))
    helper.prepare_core_wheel(tmp_path, CORE, tmp_path / "wheels")
    assert tomlkit.parse(path.read_text(encoding="utf-8"))["project"]["dependencies"] == [CORE]


@pytest.mark.parametrize("args", [["--prepare-core", CORE], ["--wheel-dir", "unused"], ["--prepare-core", CORE, "--wheel-dir", "unused", "--sync"]])
def test_cli_rejects_incomplete_or_conflicting_release_actions(args):
    script = Path(__file__).resolve().parents[2] / "scripts/build_config.py"
    result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)
    assert result.returncode == 2
    assert "error:" in result.stderr
