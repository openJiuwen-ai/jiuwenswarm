"""Executable checks for the macOS bundle layout and update selection."""
import ast
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import updater
from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("layout", ["MacOS/app", "Resources/backend/app"])
def test_frozen_entry_uses_bundled_node_for_both_layouts(tmp_path, monkeypatch, layout):
    """Execute the real bootstrapping block with the two installed layouts."""
    contents = tmp_path / "WorkSwarm.app" / "Contents"
    node_bin = contents / "Resources" / "node-runtime" / "bin"
    node_bin.mkdir(parents=True)
    executable = contents / layout
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    tree = ast.parse((ROOT / "scripts/jiuwenswarm_exe_entry.py").read_text(encoding="utf-8"))
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "sys.platform == 'darwin'")
    module = ast.Module(body=[block], type_ignores=[])
    exec(compile(module, "<frozen-bootstrap>", "exec"), {  # noqa: S102 - execute the real bootstrap with isolated state
        "Path": Path, "os": os,
        "sys": SimpleNamespace(platform="darwin", executable=str(executable)),
        "_frozen_exe_dir": executable.parent,
    })
    assert os.environ["PATH"].split(os.pathsep)[0] == str(node_bin)


@pytest.mark.parametrize("electron", [False, True])
def test_existing_updater_accepts_the_single_electron_release(monkeypatch, tmp_path, electron):
    """Legacy and Electron clients share the existing release selection policy."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setenv("JIUWENSWARM_ELECTRON", "1" if electron else "0")
    monkeypatch.setattr(updater, "get_user_workspace_dir", lambda: tmp_path)
    expected = "WorkSwarm-1.0-macos.dmg"
    release = ReleaseInfo(version="1.0", assets=[
        ReleaseAsset(name=expected, download_url="https://example.test/update.dmg")
    ])
    service = updater.UpdaterService()
    service._resolve_desktop_asset({}, release)
    assert service.get_status()["matched_asset"] == expected


@pytest.mark.parametrize("architecture,tui_arch", [("x86_64", "x64"), ("arm64", "arm64")])
@pytest.mark.parametrize("produces_binary", [True, False])
@pytest.mark.skipif(sys.platform == "win32", reason="macOS executable-bit semantics required")
def test_tui_build_checks_artifact_instead_of_log_output(tmp_path, architecture, tui_arch, produces_binary):
    """A successful Bun log is not the binary path; a missing output must fail."""
    bash = shutil.which("bash")
    if sys.platform == "win32":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            bash = str(git_bash)
    if not bash:
        pytest.skip("bash is required for the shell-fragment check")
    source = (ROOT / "scripts/build-electron-exe.sh").read_text(encoding="utf-8")
    block = source.split('TUI_BINARY=""', 1)[1].split("# ── 4. PyInstaller", 1)[0]
    (tmp_path / "jiuwenswarm/channels/tui/frontend").mkdir(parents=True)
    binary = tmp_path / f"packages/jiuwenswarm-tui/jiuwenswarm_tui/resources/tui-bin/macos-{tui_arch}/jiuwenswarm-tui"
    if produces_binary:
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    script = """
set -euo pipefail
bun() { echo "bun progress"; }
uv() { echo "compile progress: no path on stdout"; }
""" + block + '\n[ "$TUI_BINARY" = "$EXPECTED_BINARY" ]\n'
    result = subprocess.run(
        [bash, "-c", script],
        env={**os.environ, "FRONTEND_ONLY": "false", "PROJECT_ROOT": str(tmp_path),
             "PACKAGING_ROOT": str(tmp_path / "release"),
             "BUILD_ARCH": architecture, "EXPECTED_BINARY": str(binary)},
        capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) == produces_binary, result.stderr
    if not produces_binary:
        assert "did not produce an executable" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Uses real macOS lipo")
@pytest.mark.parametrize("caller", ["build", "update"])
def test_architecture_checks_use_real_lipo(tmp_path, caller):
    """Verify command syntax against Apple's lipo, not a permissive stub."""
    binary = tmp_path / "Fixture.app/Contents/MacOS/fixture"
    binary.parent.mkdir(parents=True)
    shutil.copyfile(sys.executable, binary)
    if caller == "build":
        source = (ROOT / "scripts/build-electron-exe.sh").read_text(encoding="utf-8")
        check = source.split("check_arch() {", 1)[1].split('\n}', 1)[0]
        script = "check_arch() {" + check + '\n}\ncheck_arch "$BINARY"'
    else:
        source = (ROOT / "jiuwenswarm/channels/desktop/electron/install_macos_update.sh").read_text(
            encoding="utf-8"
        )
        script = next(line for line in source.splitlines() if line.startswith("lipo "))
    env = {**os.environ, "BUILD_ARCH": platform.machine(), "ARCH": platform.machine(),
           "BINARY": str(binary), "SOURCE_APP": str(tmp_path / "Fixture.app"), "NEW_EXE": "fixture"}
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Uses macOS PlistBuddy")
def test_bundle_renames_electron_executable_and_updates_plist(tmp_path):
    """A renamed .app alone is still treated as Electron's development runtime."""
    bundle = tmp_path / "WorkSwarm.app"
    executable = bundle / "Contents/MacOS/Electron"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    plist = bundle / "Contents/Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleExecutable": "Electron"}))
    source = (ROOT / "scripts/build-electron-exe.sh").read_text(encoding="utf-8")
    block = source.split('# Electron uses the executable name', 1)[1].split('# Sign nested', 1)[0]
    # Retain the opening comment's marker while executing the actual assembly.
    result = subprocess.run(
        ["/bin/bash", "-euc", "# Electron uses the executable name" + block],
        env={**os.environ, "APP_BUNDLE": str(bundle), "BUILD_EXECUTABLE_NAME": "workswarm",
             "BUILD_DISPLAY_NAME": "WorkSwarm", "BUILD_BUNDLE_IDENTIFIER": "com.workswarm.desktop",
             "BUILD_VERSION": "0.2.5.beta1"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not executable.exists()
    assert (executable.parent / "workswarm").is_file()
    assert plistlib.loads(plist.read_bytes())["CFBundleExecutable"] == "workswarm"
