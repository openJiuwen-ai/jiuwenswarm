# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from computer_use import paths
from computer_use_native_tools import native


@pytest.mark.parametrize("absolute", [False, True])
def test_macos_bundle_path_is_relative_to_the_sdk_root(tmp_path, monkeypatch, absolute):
    monkeypatch.chdir(tmp_path)
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/build_computer_use.py"))
    bundle = Path("os_adapter/macos/.build/Jiuwen CUA.app")

    def native_driver(*, platform, root):
        executable = bundle / "Contents/MacOS/computer-use-driver"
        return {"command": str(root.resolve() / executable if absolute else executable)}

    monkeypatch.setattr(paths, "native_driver", native_driver)
    assert module["macos_bundle_relative_path"]() == bundle


@pytest.mark.skipif(os.name != "posix", reason="macOS signing uses a POSIX shell")
@pytest.mark.parametrize("embedded", [False, True])
def test_macos_signing_requires_the_embedded_driver(tmp_path, embedded):
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/build-macos.sh").read_text(encoding="utf-8")
    start = script.index("  native_bundle_name=")
    end = script.index("  codesign --force --deep", start)
    app = tmp_path / "JiuwenSwarm.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    if embedded:
        (resources / "Jiuwen CUA.app").mkdir()
    environment = dict(os.environ, APP_PATH=str(app), PROJECT_ROOT=str(root), SIGN_IDENTITY="-", ENTITLEMENTS="unused")
    commands = (
        "set -eu\n"
        "uv() { printf '%s\\n' 'Jiuwen CUA.app'; }\n"
        "codesign() { printf '%s\\n' 'signed'; }\n"
    )
    result = subprocess.run(["/bin/bash", "-c", commands + script[start:end]],
                            env=environment, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is embedded
    assert ("signed" in result.stdout) is embedded


def test_macos_driver_is_staged_for_restoration_as_a_whole_app(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    module = runpy.run_path(str(root / "scripts/build_computer_use.py"))
    relative = Path("os_adapter/macos/.build/Jiuwen CUA.app")
    sdk_root = tmp_path / "sdk"
    executable = relative / "Contents/MacOS/computer-use-driver"
    driver = sdk_root / executable
    driver.parent.mkdir(parents=True)
    driver.write_bytes(b"driver from the SDK")
    driver.chmod(0o755)
    assets = sdk_root / "os_adapter/assets"
    assets.mkdir()
    (assets / "cursor.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(paths, "repository_root", lambda: sdk_root)
    monkeypatch.setattr(native, "build_native", lambda root: driver)

    project = tmp_path / "project"
    entries = module["bundle_data"](project)
    assert not any("Jiuwen CUA.app" in target for _, target in entries)
    assert {Path(source).name for source, _ in entries} == {"cursor.svg", "manifest.json"}
    manifest = project / "build/computer-use/resources/manifest.json"
    assert executable.as_posix() in json.loads(manifest.read_bytes())

    collection = tmp_path / "collected"
    bundle = tmp_path / "JiuwenSwarm.app"

    def sign(command, *, check):
        assert command[-1] == str(bundle) and check
        for stage_root in (collection, bundle / "Contents/Resources"):
            restored = stage_root / "computer_use/_resources" / executable
            assert restored.read_bytes() == b"driver from the SDK"
            if os.name == "posix":
                assert restored.stat().st_mode & 0o111

    monkeypatch.setattr(subprocess, "run", sign)
    module["finalize_macos_bundle"](project, collection, bundle)
