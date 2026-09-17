# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
import runpy
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_core_agent_adapter_uses_a2ui_integration_boundary():
    source = (ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface.py").read_text(encoding="utf-8")

    assert "jiuwenswarm.server.runtime.a2ui.config" not in source
    assert "jiuwenswarm.server.runtime.a2ui.runtime.response_finalization" not in source
    assert "jiuwenswarm.server.runtime.a2ui.integration" in source


def test_backend_a2ui_lives_under_server_runtime():
    assert not (ROOT / "jiuwenswarm/a2ui").exists()
    assert (ROOT / "jiuwenswarm/server/runtime/a2ui").is_dir()


def test_websocket_hook_keeps_a2ui_feature_outside_generic_transport():
    source = (ROOT / "jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts").read_text(encoding="utf-8")

    assert "../features/a2ui/" not in source
    assert "sendA2UIClientEvent" not in source
    assert "sendStructuredChatContent" in source


def test_a2ui_build_dependencies_and_lockfiles_are_pinned_for_v08():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    frontend = json.loads(
        (ROOT / "jiuwenswarm/channels/web/frontend/package.json").read_text(encoding="utf-8")
    )
    frontend_lock = json.loads(
        (ROOT / "jiuwenswarm/channels/web/frontend/package-lock.json").read_text(encoding="utf-8")
    )

    locked_sdk_version = None
    for package in uv_lock["package"]:
        if package.get("name") == "a2ui-agent-sdk":
            locked_sdk_version = package.get("version")
            break

    assert "a2ui-agent-sdk==0.2.1" in pyproject["project"]["dependencies"]
    assert locked_sdk_version == "0.2.1"
    assert frontend["dependencies"]["@a2ui/react"] == "0.8.0"
    assert frontend["dependencies"]["@a2ui/web_core"] == "0.8.0"
    assert frontend_lock["packages"]["node_modules/@a2ui/react"]["version"] == "0.8.0"
    assert frontend_lock["packages"]["node_modules/@a2ui/web_core"]["version"] == "0.8.0"


def test_pyinstaller_bundle_includes_only_a2ui_v08_schema_assets():
    spec = (ROOT / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")

    assert 'includes=["assets/0.8/*.json"]' in spec
    assert 'collect_data_files("a2ui", include_py_files=False)' not in spec


def test_a2ui_bundle_verifier_accepts_v08_source_assets(monkeypatch):
    verifier = runpy.run_path(str(ROOT / "scripts/verify_a2ui_bundle.py"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    verifier["verify_a2ui_bundle"]()


def test_all_desktop_builds_run_frozen_a2ui_verifier():
    build_scripts = (
        ROOT / "scripts/build-exe.ps1",
        ROOT / "scripts/build-exe.bat",
        ROOT / "scripts/build-macos.sh",
    )

    for build_script in build_scripts:
        source = build_script.read_text(encoding="utf-8")
        assert "verify_a2ui_bundle.py" in source
