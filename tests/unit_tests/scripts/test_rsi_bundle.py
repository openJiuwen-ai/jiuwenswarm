# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Desktop bundle coverage for RSI's lazy service packages and resources."""

from __future__ import annotations

import re
import runpy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_pyinstaller_collects_rsi_service_packages_and_resources():
    spec = (ROOT / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")

    assert re.search(
        r'collect_all\(\s*"jiuwenswarm\.agents\.harness\.common\.rsi"\s*\)',
        spec,
    )
    assert re.search(r'collect_all\(\s*"jiuwenswarm\.server\.rsi"\s*\)', spec)


def test_all_desktop_builds_run_the_frozen_rsi_smoke_check():
    powershell = (ROOT / "scripts/build-exe.ps1").read_text(encoding="utf-8")
    assert "$RsiVerifyProcess = Start-Process" in powershell
    assert "-FilePath $FrozenExe" in powershell
    assert "-ArgumentList @($RsiVerifier)" in powershell
    assert "if ($RsiVerifyProcess.ExitCode -ne 0)" in powershell

    batch = (ROOT / "scripts/build-exe.bat").read_text(encoding="utf-8")
    assert re.search(
        r'start "" /wait "[^\r\n]*BUILD_EXECUTABLE_NAME_WINDOWS%" '
        r'"[^\r\n]*verify_rsi_bundle\.py"\s*if errorlevel 1 exit /b 1',
        batch,
        re.IGNORECASE,
    )

    macos = (ROOT / "scripts/build-macos.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in macos
    assert (
        '"$APP_PATH/Contents/MacOS/$BUILD_EXECUTABLE_NAME" '
        '"$PROJECT_ROOT/scripts/verify_rsi_bundle.py"'
    ) in macos


def test_rsi_bundle_smoke_check_is_frozen_only(monkeypatch):
    verifier_path = ROOT / "scripts/verify_rsi_bundle.py"
    assert verifier_path.is_file(), "Desktop build RSI smoke verifier is missing"

    verifier = runpy.run_path(str(verifier_path))
    monkeypatch.delattr(sys, "frozen", raising=False)

    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier["verify_rsi_bundle"]()


def test_rsi_bundle_smoke_check_requires_the_packaged_baseline(monkeypatch, tmp_path):
    verifier = runpy.run_path(str(ROOT / "scripts/verify_rsi_bundle.py"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    with pytest.raises(RuntimeError, match="missing or unreadable"):
        verifier["verify_rsi_bundle"]()

    baseline = tmp_path / verifier["_RSI_BASELINE_RELATIVE_PATH"]
    baseline.parent.mkdir(parents=True)
    baseline.write_text("schema_version: expert_harness.v1\n", encoding="utf-8")
    verifier["verify_rsi_bundle"]()
