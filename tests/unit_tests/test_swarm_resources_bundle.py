"""Regression checks for the standalone frozen Swarm resource gate."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import verify_swarm_resources_bundle as verifier


def test_verifier_requires_a_frozen_process(monkeypatch):
    monkeypatch.setattr(verifier.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier.verify_swarm_resources_bundle()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_isolates_resource_check_and_propagates_failure(tmp_path, monkeypatch, exit_code):
    report = tmp_path / "runtime-root.txt"
    probe = tmp_path / "entry probe.py"
    entry = Path(__file__).resolve().parents[2] / "scripts/jiuwenswarm_exe_entry.py"
    probe.write_text(
        "import os, sys, runpy\nfrom pathlib import Path\n"
        f"sys.path[:] = {sys.path!r}\n"
        "if os.name == 'nt':\n    import pywintypes\n"
        "sys.frozen = True\n"
        "root = Path(os.environ['JIUWENSWARM_DATA_DIR'])\n"
        "assert Path.cwd() == root\n"
        "assert Path(os.environ['JIUWENSWARM_HOME']) == root\n"
        "assert (root / 'config').is_dir()\n"
        "assert os.environ['AGENTOS_GATEWAY_LOG_DIR'] == str(root / 'logs')\n"
        "assert 'JIUWENSWARM_RUNTIME_WORKSPACE_READY' not in os.environ\n"
        f"runpy.run_path({str(entry)!r}, run_name='swarm_entry_probe')\n"
        "from scripts.verify_swarm_resources_bundle import main\n"
        "assert main() == 0\n"
        f"Path({str(report)!r}).write_text(str(root))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_WORKSPACE_READY", "1")
    monkeypatch.setattr(verifier, "__file__", str(probe))
    original_env = verifier.os.environ.copy()
    original_cwd = Path.cwd()
    assert verifier.run_isolated(Path(sys.executable)) == exit_code
    assert not Path(report.read_text()).exists()
    assert verifier.os.environ == original_env
    assert Path.cwd() == original_cwd


@pytest.mark.parametrize("missing", [None, "tools", "catalog"])
def test_verifier_checks_physical_swarm_resources(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(verifier.sys, "frozen", True, raising=False)
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    for language in ("en", "zh"):
        (catalog / f"{language}.yaml").write_text("{}", encoding="utf-8")
    if missing == "catalog":
        (catalog / "zh.yaml").unlink()
    modules = {
        "jiuwenswarm.agents.harness.common.tools": SimpleNamespace(__all__=["diff_files"]),
        "jiuwenswarm.agents.harness.common.tools.builtin_tool_registry": SimpleNamespace(
            discover_builtin_tool_specs=lambda: [] if missing == "tools" else [SimpleNamespace(name="diff_files")],
        ),
        "jiuwenswarm.common.i18n.translate": SimpleNamespace(
            __file__=str(tmp_path / "translate.py"),
            t=lambda message_id, **params: "Sent 2 files",
        ),
    }
    monkeypatch.setattr(verifier, "import_module", modules.__getitem__)
    if missing:
        with pytest.raises(RuntimeError, match="diff_files" if missing == "tools" else "i18n catalog: zh"):
            verifier.verify_swarm_resources_bundle()
    else:
        verifier.verify_swarm_resources_bundle()
