# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression checks for modules only imported by installed Gateway packages."""

from pathlib import Path

import pytest

from scripts import verify_gateway_extensions_bundle as verifier


ROOT = Path(__file__).resolve().parents[2]


def test_spec_keeps_plugin_only_gateway_modules():
    source = (ROOT / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert '"jiuwenswarm.gateway.extensions_v2.webui.ops",' in compact


def test_verifier_requires_a_frozen_process(monkeypatch):
    monkeypatch.setattr(verifier.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="frozen executable"):
        verifier.verify_gateway_extensions_bundle()


def test_verifier_imports_plugin_only_modules(monkeypatch):
    monkeypatch.setattr(verifier.sys, "frozen", True, raising=False)
    imported = []
    monkeypatch.setattr(verifier, "import_module", imported.append)
    verifier.verify_gateway_extensions_bundle()
    assert imported == list(verifier.PLUGIN_ONLY_MODULES)


@pytest.mark.parametrize(
    "builder",
    ["build-exe.ps1", "build-exe.bat", "build-electron-exe.ps1", "build-macos.sh"],
)
def test_desktop_builds_run_the_frozen_gateway_extensions_check(builder):
    source = (ROOT / "scripts" / builder).read_text(encoding="utf-8")
    assert "verify_gateway_extensions_bundle.py" in source
