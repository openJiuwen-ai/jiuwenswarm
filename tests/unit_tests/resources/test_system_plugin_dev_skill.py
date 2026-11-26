"""Contract checks for the built-in system plugin development skill."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_ROOT = (
    Path(__file__).parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities"
    / "jiuwen-system-plugin-dev"
)


def test_system_plugin_source_defaults_use_release_branches():
    content = "\n".join(
        [
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/source-reading.md").read_text(encoding="utf-8"),
            (SKILL_ROOT / "references/package-workflow.md").read_text(encoding="utf-8"),
        ]
    )

    assert "releases/0.2.9.beta3" not in content
    assert "release/v0.1.20.post3" not in content
    assert "https://gitcode.com/openJiuwen/jiuwenswarm/tree/releases/0.2.10" in content
    assert "https://gitcode.com/openJiuwen/agent-core/tree/release/v0.1.22" in content
    assert "https://gitcode.com/openJiuwen/agent-gateway/tree/release/0.1.1" in content
    for branch in ("releases/0.2.10", "release/v0.1.22", "release/0.1.1"):
        assert content.count(f"--branch {branch}") == 1
        assert content.count(f"checkout {branch}") == 1
    assert "--branch develop" not in content


def test_finalizer_runs_relocated_example_against_delivered_zip(tmp_path):
    """Copied business tests must use the finalizer's archive and source paths."""
    tests = tmp_path / "relocated-tests"
    tests.mkdir()
    relocated = tests / "test_plugin.py"
    shutil.copyfile(SKILL_ROOT / "assets/package_example/test_package.py", relocated)
    with relocated.open("a", encoding="utf-8") as stream:
        stream.write("""

def test_final_archive_is_used_without_rebuilding(tmp_path, monkeypatch):
    def unexpected_build(name):
        raise AssertionError("Final validation must not rebuild the ZIP")
    monkeypatch.setattr(sys.modules[__name__], "load_script", unexpected_build)
    assert package_zip(tmp_path) == Path(os.environ["JIUWEN_SYSTEM_PLUGIN_ZIP"]).resolve()
    assert SOURCE == Path(os.environ["JIUWEN_SYSTEM_PLUGIN_SOURCE"])
    skill = Path(os.environ["JIUWEN_SYSTEM_PLUGIN_SKILL_ROOT"])
    assert (skill / "scripts/finalize_package.py").is_file()
""")
    output = tmp_path / "greeting.zip"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[3]) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for key in ("JIUWEN_SYSTEM_PLUGIN_ZIP", "JIUWEN_SYSTEM_PLUGIN_SOURCE", "JIUWEN_SYSTEM_PLUGIN_SKILL_ROOT"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-B", str(SKILL_ROOT / "scripts/finalize_package.py"),
         str(SKILL_ROOT / "assets/package_example/greeting_package"), str(output), str(relocated)],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=55,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.is_file()
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(report["zip"]) == output
    assert "plugin_tests" in report["validated"]
