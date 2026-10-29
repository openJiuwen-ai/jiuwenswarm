# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Execute release-script asset copies and load their installed lifecycle module."""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
DESKTOP = ROOT / "jiuwenswarm/channels/desktop/electron"


@pytest.mark.parametrize("platform", ["windows", "macos"])
def test_assembled_electron_contains_its_shutdown_dependency(platform, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable")
    destination = tmp_path / "assembled app"
    destination.mkdir()
    if platform == "windows":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            pytest.skip("PowerShell unavailable")
        text = (ROOT / "scripts/build-electron-exe.ps1").read_text(encoding="utf-8-sig")
        copies = re.findall(r'^Copy-Item -Path \(Join-Path \$DesktopDir "[^"\n]+\.cjs"\) -Destination \$AppDir -Force$', text, re.MULTILINE)
        script = tmp_path / "assemble.ps1"
        script.write_text('param($DesktopDir, $AppDir)\n$ErrorActionPreference = "Stop"\n' + "\n".join(copies), encoding="utf-8")
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(DESKTOP), str(destination)]
        env = None
    else:
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        shell = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
        if shell is None:
            pytest.skip("Bash unavailable")
        text = (ROOT / "scripts/build-electron-exe.sh").read_text(encoding="utf-8")
        copies = re.findall(r'^cp "\$DESKTOP_DIR/[^"\n]+\.cjs" "\$APP_DIR/"$', text, re.MULTILINE)
        script = tmp_path / "assemble.sh"
        script.write_text("set -eu\n" + "\n".join(copies) + "\n", encoding="utf-8")
        command = [shell, str(script)]
        env = {**os.environ, "DESKTOP_DIR": DESKTOP.as_posix(), "APP_DIR": destination.as_posix()}
    assert copies
    subprocess.run(command, check=True, env=env, capture_output=True, timeout=20)
    module = destination / "update_shutdown.cjs"
    assert module.read_bytes() == (DESKTOP / "update_shutdown.cjs").read_bytes()
    subprocess.run([node, "-e", "const m=require(process.argv[1]); if(typeof m.recordShutdownOutcome!=='function') process.exit(1)", str(module)],
                   check=True, capture_output=True, timeout=10)
    if platform == "macos":
        subprocess.run([node, "-e", "const m=require(process.argv[1]); if(typeof m.launchMacUpdate!=='function') process.exit(1)", str(destination / "macos_update.cjs")],
                       check=True, capture_output=True, timeout=10)
