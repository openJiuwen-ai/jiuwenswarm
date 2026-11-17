# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
from pathlib import Path
import runpy
import subprocess
import sys

from computer_use import paths
from computer_use_native_tools import native


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
            assert restored.stat().st_mode & 0o111

    monkeypatch.setattr(subprocess, "run", sign)
    module["finalize_macos_bundle"](project, collection, bundle)
