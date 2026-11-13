from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import rea_setup


def _runtime(root: Path) -> rea_setup.NodeRuntime:
    node = root / "node.exe"
    npx = root / "npx.cmd"
    npx_cli = root / "node_modules" / "npm" / "bin" / "npx-cli.js"
    node.write_text("", encoding="utf-8")
    npx.write_text("", encoding="utf-8")
    npx_cli.parent.mkdir(parents=True)
    npx_cli.write_text("", encoding="utf-8")
    return rea_setup.NodeRuntime(node=node, npx=npx, npx_cli=npx_cli, version="26.13.0")


def test_supports_rea_node_uses_published_range() -> None:
    assert rea_setup.supports_rea_node("22.23.4")
    assert rea_setup.supports_rea_node("24.22.1")
    assert rea_setup.supports_rea_node("26.12.1")
    assert not rea_setup.supports_rea_node("22.23.3")
    assert not rea_setup.supports_rea_node("24.22.0")
    assert not rea_setup.supports_rea_node("26.12.0")
    assert not rea_setup.supports_rea_node("23.9.0")
    assert not rea_setup.supports_rea_node("25.0.0")


def test_run_preserves_windows_process_options(monkeypatch, mocker) -> None:
    monkeypatch.setattr(rea_setup, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    run = mocker.patch.object(subprocess, "run")
    argv = ["node.exe", "--version"]
    env = {"PATH": "C:/node"}

    rea_setup._run(argv, env=env, timeout=10)

    run.assert_called_once_with(
        argv,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        env=env,
        creationflags=0x08000000,
    )


def test_ghidra_skips_native_build_on_windows(tmp_path: Path, monkeypatch, mocker) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    run = mocker.patch.object(subprocess, "run")

    rea_setup._build_ghidra_macos_natives(tmp_path, {})

    run.assert_not_called()


def test_platform_id_uses_manifest_macos_names(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(rea_setup.platform, "machine", lambda: "arm64")
    assert rea_setup._platform_id() == "macos-arm64"

    monkeypatch.setattr(rea_setup.platform, "machine", lambda: "x86_64")
    assert rea_setup._platform_id() == "macos-x64"


def test_resolve_node_runtime_includes_bound_node_outside_search_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(rea_setup, "_candidate_node_paths", lambda: [])
    monkeypatch.setattr(rea_setup, "_node_version", lambda _node: "26.13.0")

    resolved = rea_setup.resolve_node_runtime(runtime.node)

    assert resolved is not None
    assert resolved.node == runtime.node.resolve()
    assert resolved.npx_cli == runtime.npx_cli.resolve()


def test_resolve_node_runtime_accepts_bound_npx_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    npx_sibling = runtime.npx.with_name("node")
    npx_sibling.write_text("", encoding="utf-8")
    monkeypatch.setattr(rea_setup, "_candidate_node_paths", lambda: [])
    monkeypatch.setattr(rea_setup, "_node_version", lambda _node: "26.13.0")

    resolved = rea_setup.resolve_node_runtime(runtime.npx)

    assert resolved is not None
    assert resolved.node == npx_sibling.resolve()

    resolved_from_cli = rea_setup.resolve_node_runtime(runtime.npx_cli)

    assert resolved_from_cli is not None
    assert resolved_from_cli.node == npx_sibling.resolve()


def test_scan_requires_base_rea_mcp(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(rea_setup, "resolve_node_runtime", lambda *_args: runtime)
    monkeypatch.setattr(rea_setup, "_installed_rea_version", lambda _runtime: "6.8.0")

    scan = rea_setup.scan_rea_setup(tmp_path / "workspace")

    assert scan["ready"] is False
    assert [item["id"] for item in scan["base"]] == ["node_npx", "rea_dependencies", "rea_mcp"]
    assert scan["base"][0]["ok"] is True
    assert scan["base"][1]["ok"] is True
    assert scan["base"][2]["ok"] is False
    assert scan["enhanced_blocked_by_base"] is True


def test_component_inventory_distinguishes_detection_states(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    workspace = tmp_path / "workspace"
    component = rea_setup._manifest_components()["adb"]
    artifact = rea_setup._component_artifact(component)
    cache = workspace / str(artifact["destination"])
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"downloaded")

    monkeypatch.setattr(rea_setup, "_configured_component", lambda *_args: (False, {}, None))
    monkeypatch.setattr(rea_setup, "_reuse_existing_component", lambda *_args: None)
    monkeypatch.setattr(rea_setup, "_partial_component_path", lambda *_args: None)

    cached = rea_setup._component_inventory("adb", {}, workspace)
    assert cached["state"] == "cached"
    assert cached["path"] == str(cache)

    monkeypatch.setattr(
        rea_setup,
        "_partial_component_path",
        lambda *_args: (str(cache), "found only part of the component"),
    )
    cache.unlink()
    partial = rea_setup._component_inventory("adb", {}, workspace)
    assert partial["state"] == "partial"

    discovered_path = tmp_path / "other" / "adb.exe"
    discovered_path.parent.mkdir()
    discovered_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        rea_setup,
        "_reuse_existing_component",
        lambda *_args: ({"REA_ADB_PATH": str(discovered_path)}, "installed"),
    )
    discovered = rea_setup._component_inventory("adb", {}, workspace)
    assert discovered["state"] == "discovered"
    assert discovered["configured"] is False

    monkeypatch.setattr(
        rea_setup,
        "_configured_component",
        lambda *_args: (True, {"REA_ADB_PATH": str(discovered_path)}, str(discovered_path)),
    )
    configured = rea_setup._component_inventory(
        "adb",
        {"REA_ADB_PATH": str(discovered_path)},
        workspace,
    )
    assert configured["state"] == "configured"
    assert configured["configured"] is True


def test_component_inventory_rejects_unsupported_and_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    unsupported = rea_setup._component_inventory("firmware", {}, tmp_path / "workspace")
    assert unsupported["state"] == "unsupported"

    monkeypatch.setattr(rea_setup, "_configured_component", lambda *_args: (False, {}, None))
    monkeypatch.setattr(rea_setup, "_reuse_existing_component", lambda *_args: None)
    monkeypatch.setattr(rea_setup, "_partial_component_path", lambda *_args: None)
    missing = rea_setup._component_inventory("ida", {}, tmp_path / "workspace")
    assert missing["state"] == "missing"

    try:
        rea_setup.configure_component(
            "hopper",
            {"launcher_path": str(missing["path"] or "")},
            tmp_path / "workspace",
        )
    except ValueError as exc:
        assert "当前平台不支持" in str(exc)
    else:
        raise AssertionError("unsupported components must not accept configuration")


def test_component_inventory_contract_covers_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(rea_setup, "_configured_component", lambda *_args: (False, {}, None))
    monkeypatch.setattr(rea_setup, "_reuse_existing_component", lambda *_args: None)
    monkeypatch.setattr(rea_setup, "_partial_component_path", lambda *_args: None)

    allowed_states = {"missing", "cached", "unsupported"}
    for component_id in rea_setup._manifest_components():
        inventory = rea_setup._component_inventory(component_id, {}, tmp_path / component_id)
        assert inventory["state"] in allowed_states
        assert isinstance(inventory["detected"], bool)
        assert isinstance(inventory["configured"], bool)


def test_component_inventory_detects_managed_tools_without_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    workspace = tmp_path / "workspace"
    ghidra = workspace / "rea" / "tools" / "ghidra" / "12.1.4" / "ghidra_12.1.4_PUBLIC"
    (ghidra / "Ghidra").mkdir(parents=True)
    (ghidra / "Ghidra" / "application.properties").write_text(
        "application.version=12.1.4\n",
        encoding="utf-8",
    )
    (ghidra / "support").mkdir()
    (ghidra / "support" / "analyzeHeadless").write_text("", encoding="utf-8")

    adb = workspace / "rea" / "tools" / "adb" / "37.0.1" / "platform-tools" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.write_text("", encoding="utf-8")

    jadx = workspace / "rea" / "tools" / "jadx" / "0.7.1" / "jadx-headless-mcp-0.7.1-all.jar"
    jadx.parent.mkdir(parents=True)
    jadx.write_text("", encoding="utf-8")

    apktool = workspace / "rea" / "tools" / "apktool" / "3.0.3" / "apktool.cmd"
    apktool.parent.mkdir(parents=True)
    apktool.write_text("", encoding="utf-8")

    pcap = workspace / "rea" / "tools" / "pcap" / "4.6.9" / "Wireshark"
    pcap.mkdir(parents=True)
    (pcap / "tshark.exe").write_text("", encoding="utf-8")
    (pcap / "capinfos.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(rea_setup, "_configured_component", lambda *_args: (False, {}, None))
    monkeypatch.setattr(rea_setup, "_find_pcap_tools", lambda *_args: None)
    for component_id in ("ghidra", "adb", "jadx", "apktool", "pcap"):
        inventory = rea_setup._component_inventory(component_id, {}, workspace)
        assert inventory["state"] == "discovered", component_id
        assert inventory["configured"] is False
        assert inventory["path"]


def test_new_managed_tools_are_detected_from_workspace_without_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    paths = {
        "wabt": workspace
        / "rea"
        / "tools"
        / "wabt"
        / "1.0.42"
        / "wabt-1.0.42"
        / "bin"
        / "wasm-validate.exe",
        "kuna": workspace
        / "rea"
        / "tools"
        / "kuna"
        / "1.833"
        / "kuna.exe",
        "rizin": workspace
        / "rea"
        / "tools"
        / "rizin"
        / "0.9.1"
        / "bin"
        / "rizin.exe",
        "cutter": workspace
        / "rea"
        / "tools"
        / "cutter"
        / "2.5.0"
        / "cutter.exe",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(rea_setup, "_configured_component", lambda *_args: (False, {}, None))
    monkeypatch.setattr(rea_setup, "_cutter_bridge_readiness", lambda _path: (True, "ready"))
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "linux-x64")

    for component_id in paths:
        inventory = rea_setup._component_inventory(component_id, {}, workspace)
        assert inventory["state"] == "discovered", component_id
        assert inventory["configured"] is False
        assert inventory["path"]


def test_manifest_marks_jadx_installable_with_full_jdk(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    monkeypatch.setattr(rea_setup, "_java_full_jdk", lambda *_args, **_kwargs: True)

    item = rea_setup._apply_manifest_overlay({"id": "jadx"})
    adb = rea_setup._apply_manifest_overlay({"id": "adb"})
    apktool = rea_setup._apply_manifest_overlay({"id": "apktool"})
    java = rea_setup._apply_manifest_overlay({"id": "java"})
    ghidra = rea_setup._apply_manifest_overlay({"id": "ghidra"})
    pcap = rea_setup._apply_manifest_overlay({"id": "pcap"})

    assert item["install_mode"] == "managed"
    assert item["install_supported"] is True
    assert item["default_selected"] is True
    assert adb["install_supported"] is True
    assert apktool["install_supported"] is True
    assert java["install_supported"] is True
    assert ghidra["install_supported"] is True
    assert pcap["install_supported"] is True
    assert pcap["install_mode"] == "managed"


def test_macos_java_home_uses_bundle_contents_home(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")

    env = rea_setup._component_env(rea_setup._manifest_components()["java"])

    assert env["JAVA_HOME"].endswith("jdk-21.0.13+9/Contents/Home")

    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")

    windows_env = rea_setup._component_env(rea_setup._manifest_components()["java"])

    assert windows_env["JAVA_HOME"].endswith("jdk-21.0.13+9")


def test_reuses_compatible_local_jdk_and_ghidra(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ghidra = tmp_path / "ghidra_12.1.4_PUBLIC"
    (ghidra / "support").mkdir(parents=True)
    (ghidra / "support" / "analyzeHeadless").write_text("", encoding="utf-8")
    (ghidra / "Ghidra").mkdir()
    (ghidra / "Ghidra" / "application.properties").write_text(
        "application.version=12.1.4\n",
        encoding="utf-8",
    )
    java_home = tmp_path / "jdk-21"
    monkeypatch.setattr(rea_setup, "_java_full_jdk_home", lambda *_args, **_kwargs: java_home)

    java = rea_setup._reuse_existing_component(
        "java",
        {"GHIDRA_INSTALL_DIR": str(ghidra)},
    )
    found_ghidra = rea_setup._reuse_existing_component(
        "ghidra",
        {"GHIDRA_INSTALL_DIR": str(ghidra), "JAVA_HOME": str(java_home)},
    )

    assert java == ({"JAVA_HOME": str(java_home)}, "jdk-21")
    assert found_ghidra == ({"GHIDRA_INSTALL_DIR": str(ghidra.resolve())}, "12.1.4")


def test_ghidra_builds_macos_native_decompiler_through_gradle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ghidra = tmp_path / "ghidra_12.1.4_PUBLIC"
    gradle_dir = ghidra / "support" / "gradle"
    gradle_dir.mkdir(parents=True)
    gradlew = gradle_dir / "gradlew"
    gradlew.write_text("#!/bin/sh\n", encoding="utf-8")
    gradle_dir.joinpath("gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://services.gradle.org/distributions/"
        "gradle-9.7.1-bin.zip\nnetworkTimeout=10000\n",
        encoding="utf-8",
    )

    def fake_run(argv, **kwargs):
        assert argv == [str(gradlew), "buildNatives"]
        assert kwargs["cwd"] == gradle_dir
        decompiler = (
            ghidra
            / "Ghidra"
            / "Features"
            / "Decompiler"
            / "build"
            / "os"
            / "mac_arm_64"
            / "decompile"
        )
        decompiler.parent.mkdir(parents=True)
        decompiler.write_text("", encoding="utf-8")
        decompiler.chmod(0o755)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rea_setup._build_ghidra_macos_natives(
        ghidra,
        {"JAVA_HOME": str(tmp_path / "jdk")},
    )

    wrapper = (gradle_dir / "gradle-wrapper.properties").read_text(encoding="utf-8")
    assert "mirrors.cloud.tencent.com/gradle/gradle-9.7.1-bin.zip" in wrapper
    assert "networkTimeout=60000" in wrapper


def test_hopper_demo_is_automatically_installable_on_macos(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")

    hopper = rea_setup._apply_manifest_overlay({"id": "hopper"})

    assert hopper["install_mode"] == "rea_setup"
    assert hopper["default_selected"] is True
    assert hopper["install_supported"] is True


def test_macos_hopper_installer_downloads_verifies_and_installs_bundle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    package = tmp_path / "Hopper-6.6.2-demo.dmg"
    package.write_bytes(b"dmg")
    release = {
        "OS X": {
            "version": "6.6.2",
            "filename": (
                "https://www.hopperapp.com:443/downloader/public/"
                "Hopper-6.6.2-demo.dmg"
            ),
            "file_length": 3,
            "file_hash": "a" * 40,
        }
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return json.dumps(release).encode("utf-8")

    def fake_run(argv, **_kwargs):
        if argv[0] == "hdiutil" and argv[1] == "attach":
            mount = Path(argv[argv.index("-mountpoint") + 1])
            app = mount / "Hopper Disassembler.app"
            launcher = app / "Contents" / "MacOS" / "hopper"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("", encoding="utf-8")
            launcher.chmod(0o755)
        elif argv[0] == "ditto":
            staged = Path(argv[2])
            launcher = staged / "Contents" / "MacOS" / "hopper"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("", encoding="utf-8")
            launcher.chmod(0o755)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rea_setup.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(rea_setup, "_download_component_artifact", lambda *_args: package)
    monkeypatch.setattr(rea_setup, "_run", fake_run)
    monkeypatch.setattr(rea_setup.Path, "home", lambda: home)

    env = rea_setup._install_macos_hopper(tmp_path / "workspace")

    launcher = home / "Applications" / "Hopper Disassembler.app" / "Contents" / "MacOS" / "hopper"
    assert env == {"HOPPER_LAUNCHER_PATH": str(launcher)}
    assert launcher.is_file()


def test_pcap_is_automatically_installed_from_verified_macos_dmg(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")

    pcap = rea_setup._apply_manifest_overlay({"id": "pcap", "detected": False})
    artifact = rea_setup._component_artifact(rea_setup._manifest_components()["pcap"])

    assert pcap["install_supported"] is True
    assert pcap.get("code") != "unsupported_host"
    assert artifact["url"].startswith("https://mirrors.tuna.tsinghua.edu.cn/")
    assert artifact["archive_type"] == "dmg"
    assert artifact["sha256"] == (
        "10bd54ccdc3f40457f6a1f0c517bc93703fcb327c4cd4b93002e7a623dacbf38"
    )
    assert rea_setup._component_installer(
        rea_setup._manifest_components()["pcap"]
    ) is None


def test_pcap_windows_keeps_existing_msi_installer(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    component = rea_setup._manifest_components()["pcap"]

    artifact = rea_setup._component_artifact(component)
    installer = rea_setup._component_installer(component)

    assert artifact["url"].endswith("Wireshark-4.6.9-x64.msi")
    assert installer == {
        "type": "msi_admin",
        "target": "rea/tools/pcap/4.6.9",
        "timeout_seconds": 600,
    }


def test_msi_admin_install_discovers_pcap_tools(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "workspace" / "rea" / "tools" / "pcap" / "4.6.9"
    tshark = target / "Wireshark" / "tshark.exe"
    capinfos = target / "Wireshark" / "capinfos.exe"
    tshark.parent.mkdir(parents=True)
    tshark.write_text("", encoding="utf-8")
    capinfos.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        rea_setup,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    component = rea_setup._manifest_components()["pcap"]

    env = rea_setup._install_msi_admin_component(
        component,
        tmp_path / "Wireshark.msi",
        tmp_path / "workspace",
        None,
        component_id="pcap",
        component_start=30,
        component_end=40,
        label="PCAP 分析",
    )

    assert env == {
        "REA_TSHARK_COMMAND": str(tshark),
        "REA_CAPINFOS_COMMAND": str(capinfos),
    }


def test_managed_macos_wireshark_is_discovered_without_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    binaries = (
        workspace
        / "rea"
        / "tools"
        / "pcap"
        / "4.6.9"
        / "Wireshark.app"
        / "Contents"
        / "MacOS"
    )
    binaries.mkdir(parents=True)
    tshark = binaries / "tshark"
    capinfos = binaries / "capinfos"
    tshark.write_text("", encoding="utf-8")
    capinfos.write_text("", encoding="utf-8")
    monkeypatch.setattr(rea_setup, "_find_pcap_tools", lambda *_args: None)

    reused = rea_setup._reuse_existing_component("pcap", {}, workspace)

    assert reused == (
        {
            "REA_TSHARK_COMMAND": str(tshark),
            "REA_CAPINFOS_COMMAND": str(capinfos),
        },
        "installed",
    )


def test_manifest_exposes_component_specific_guidance() -> None:
    ghidra = rea_setup._apply_manifest_overlay({"id": "ghidra"})
    ida = rea_setup._apply_manifest_overlay({"id": "ida"})
    hopper = rea_setup._apply_manifest_overlay({"id": "hopper"})
    adb = rea_setup._apply_manifest_overlay({"id": "adb"})
    apktool = rea_setup._apply_manifest_overlay({"id": "apktool"})

    assert ghidra["assisted_setup"]["inputs"][0]["type"] == "directory"
    assert "support/analyzeHeadless" in ghidra["assisted_setup"]["inputs"][0]["probe_files"]
    assert ida["assisted_setup"]["required_any"] == ["ida_config_path", "registration_json"]
    assert any("ida-pro-mcp" in link["url"] for link in ida["assisted_setup"]["links"])
    assert hopper["assisted_setup"]["inputs"][0]["expect"] == "executable"
    assert any("hopperapp.com" in link["url"] for link in hopper["assisted_setup"]["links"])
    assert adb["assisted_setup"]["inputs"][0]["env_key"] == "REA_ADB_PATH"
    assert apktool["assisted_setup"]["inputs"][0]["env_key"] == "REA_APKTOOL_COMMAND"


def test_install_writes_pinned_mcp_record(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    monkeypatch.setattr(rea_setup, "resolve_node_runtime", lambda *_args: runtime)
    monkeypatch.setattr(
        rea_setup,
        "_npx_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="6.8.0\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(rea_setup, "_installed_rea_version", lambda _runtime: "6.8.0")
    monkeypatch.setenv("JAVA_HOME", "D:/existing/jdk")
    monkeypatch.setenv("GHIDRA_INSTALL_DIR", "D:/existing/ghidra")
    monkeypatch.setattr(rea_setup, "_scan_enhancements", lambda *_args: [])
    enhanced_calls: list[bool] = []
    monkeypatch.setattr(
        rea_setup,
        "install_managed_enhancements",
        lambda *_args, **_kwargs: enhanced_calls.append(True) or {"env": {}, "results": []},
    )

    scan = rea_setup.install_rea_base(workspace)

    assert enhanced_calls == []
    assert scan["base"][2]["ok"] is False
    state_path = workspace / "mcp" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = state["mcp"]["rea"]
    assert record["command"] == str(runtime.node)
    assert str(runtime.npx_cli) in record["args"]
    assert "rea-agents@6.8.0" in record["args"]
    assert record["args"][-1] == "mcp"
    assert record["state"] == "registered"
    assert set(record["env"]) == {"PATH"}
    rea_setup.mark_rea_mcp_state("connected", workspace, tool_count=229)
    refreshed = rea_setup.scan_rea_setup(workspace)
    assert refreshed["base"][2]["ok"] is True
    assert refreshed["base"][2]["tool_count"] == 229


def test_cancelled_base_install_stops_before_writing(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(rea_setup, "resolve_node_runtime", lambda *_args: runtime)
    monkeypatch.setattr(
        rea_setup,
        "_npx_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "6.8.0\n", ""),
    )

    try:
        rea_setup.install_rea_base(workspace, cancel_check=lambda: True)
    except rea_setup.InstallCancelled:
        pass
    else:
        raise AssertionError("cancelled base install must stop")

    assert not (workspace / "mcp" / "state.json").exists()


def test_enhanced_install_parallelizes_independent_wave_then_java_dependents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcp": {
                    "rea": {
                        "transport": "stdio",
                        "command": "node.exe",
                        "args": ["rea-agents@6.8.0", "mcp"],
                        "env": {},
                        "state": "connected",
                    }
                },
                "mounts": {},
            }
        ),
        encoding="utf-8",
    )
    components = {
        "java": {
            "display_name": "Java",
            "install_mode": "managed",
            "default_selected": True,
        },
        "adb": {
            "display_name": "ADB",
            "install_mode": "managed",
            "default_selected": True,
        },
        "apktool": {
            "display_name": "Apktool",
            "install_mode": "managed",
            "default_selected": True,
        },
        "ghidra": {
            "display_name": "Ghidra",
            "install_mode": "managed",
            "default_selected": True,
            "requires": ["java-full-jdk-21"],
        },
        "ida": {
            "display_name": "IDA",
            "install_mode": "manual_license",
            "default_selected": True,
        },
    }
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    started: list[str] = []
    ghidra_seed: list[dict[str, str]] = []

    monkeypatch.setattr(rea_setup, "_manifest_components", lambda: components)
    monkeypatch.setattr(rea_setup, "resolve_node_runtime", lambda *_args: object())
    monkeypatch.setattr(
        rea_setup,
        "_requirement_satisfied",
        lambda _name, env=None: bool((env or {}).get("JAVA_HOME")),
    )

    def fake_install(
        _workspace,
        _progress=None,
        selected_ids=None,
        seed_env=None,
        progress_start=None,
        progress_end=None,
        cancel_check=None,
    ):
        del progress_start, progress_end, cancel_check
        component_id = selected_ids[0]
        with lock:
            started.append(component_id)
        if component_id in {"java", "adb", "apktool"}:
            barrier.wait(timeout=5)
        if component_id == "ghidra":
            ghidra_seed.append(dict(seed_env or {}))
            component_env = {"GHIDRA_INSTALL_DIR": "D:/tools/ghidra"}
        else:
            component_env = {f"{component_id.upper()}_PATH": f"D:/tools/{component_id}"}
            if component_id == "java":
                component_env = {"JAVA_HOME": "D:/tools/java"}
        return {
            "env": component_env,
            "results": [
                {
                    "id": component_id,
                    "ok": True,
                    "state": "installed",
                    "path": next(iter(component_env.values())),
                }
            ],
        }

    monkeypatch.setattr(rea_setup, "install_managed_enhancements", fake_install)
    monkeypatch.setattr(
        rea_setup,
        "_verify_component",
        lambda *_args: {"ok": True, "detail": "verified"},
    )
    monkeypatch.setattr(rea_setup, "scan_rea_setup", lambda _workspace=None: {"ready": True})

    result = rea_setup.install_enhanced_components(workspace)

    assert {"java", "adb", "apktool"}.issubset(set(started))
    assert started.index("ghidra") > started.index("java")
    assert ghidra_seed[0]["JAVA_HOME"] == "D:/tools/java"
    by_id = {item["id"]: item for item in result["results"]}
    assert by_id["java"]["state"] == "installed"
    assert by_id["ghidra"]["state"] == "installed"
    assert by_id["ida"]["state"] == "needs_action"
    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["mcp"]["rea"]["env"]["JAVA_HOME"] == "D:/tools/java"
    assert written["mcp"]["rea"]["env"]["GHIDRA_INSTALL_DIR"] == "D:/tools/ghidra"


def test_enhanced_install_persists_env_before_later_cancellation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcp": {
                    "rea": {
                        "transport": "stdio",
                        "command": "node.exe",
                        "args": ["rea-agents@6.8.0", "mcp"],
                        "env": {"PATH": "C:/Windows"},
                        "state": "connected",
                    }
                },
                "mounts": {},
            }
        ),
        encoding="utf-8",
    )
    components = {
        "java": {
            "id": "java",
            "display_name": "Java",
            "install_mode": "managed",
            "default_selected": True,
        },
    }

    monkeypatch.setattr(rea_setup, "_manifest_components", lambda: components)
    monkeypatch.setattr(rea_setup, "resolve_node_runtime", lambda *_args: object())

    def fake_install(*_args, **_kwargs):
        return {
            "env": {"JAVA_HOME": "D:/tools/java"},
            "results": [
                {
                    "id": "java",
                    "ok": True,
                    "state": "installed",
                    "path": "D:/tools/java",
                }
            ],
        }

    monkeypatch.setattr(rea_setup, "install_managed_enhancements", fake_install)
    monkeypatch.setattr(
        rea_setup,
        "_verify_component",
        lambda *_args: (_ for _ in ()).throw(rea_setup.InstallCancelled("cancelled")),
    )

    with pytest.raises(rea_setup.InstallCancelled):
        rea_setup.install_enhanced_components(
            workspace,
            selected_ids=["java"],
        )

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["mcp"]["rea"]["env"]["JAVA_HOME"] == "D:/tools/java"
    assert written["mcp"]["rea"]["env"]["PATH"] == "C:/Windows"


def test_enhanced_plan_is_os_aware_and_lists_manual_and_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    windows = {item["id"]: item for item in rea_setup.enhanced_component_plan()}
    assert windows["java"]["state"] == "planned"
    assert windows["ghidra"]["state"] == "planned"
    assert windows["kuna"]["state"] == "unsupported"
    assert windows["ida"]["state"] == "needs_action"
    assert windows["jeb"]["state"] == "needs_action"
    assert windows["hopper"]["state"] == "unsupported"
    assert windows["firmware"]["state"] == "unsupported"

    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")
    macos = {item["id"]: item for item in rea_setup.enhanced_component_plan()}
    assert macos["hopper"]["state"] == "planned"
    assert macos["pcap"]["state"] == "planned"
    assert macos["ida"]["state"] == "needs_action"
    assert macos["kuna"]["state"] == "planned"
    assert macos["firmware"]["state"] == "unsupported"

    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "linux-arm64")
    linux_arm = {item["id"]: item for item in rea_setup.enhanced_component_plan()}
    assert linux_arm["java"]["state"] == "planned"
    assert linux_arm["ghidra"]["state"] == "planned"
    assert linux_arm["hopper"]["state"] == "planned"
    assert linux_arm["jadx"]["state"] == "unsupported"
    assert linux_arm["adb"]["state"] == "unsupported"
    assert linux_arm["apktool"]["state"] == "unsupported"


def test_versions_are_pinned_and_enhanced_manifest_covers_product_cards() -> None:
    manifest = rea_setup.load_component_manifest()
    node_manifest = rea_setup.load_node_runtime_manifest()
    card_manifest = json.loads(
        rea_setup._CAPABILITY_CARD_MANIFEST.read_text(encoding="utf-8")
    )
    components = {item["id"]: item for item in manifest["components"]}

    assert rea_setup.REA_PACKAGE_VERSION == "6.8.0"
    assert manifest["rea_versions"] == ">=6.8 <7"
    assert node_manifest["version"] == "26.13.0"
    assert node_manifest["npm_version"] == "11.20.0"
    assert card_manifest["rea_inventory"]["version"] == "6.8.0"
    assert card_manifest["rea_inventory"]["tool_count"] == 229
    assert len(card_manifest["cards"]) == 10
    assert {
        "ghidra",
        "ida",
        "kuna",
        "hopper",
        "android",
        "jeb",
        "apple",
        "dynamic",
        "network_firmware",
        "script_formats",
    } == {item["id"] for item in card_manifest["cards"]}
    assert components["kuna"]["artifact_by_platform"]["linux-x64"]["version"] == "1.833"
    assert components["rizin"]["artifact_by_platform"]["linux-x64"]["version"] == "0.9.1"
    assert components["cutter"]["artifact_by_platform"]["windows-x64"]["version"] == "2.5.0"
    assert components["wabt"]["artifact_by_platform"]["windows-x64"]["version"] == "1.0.42"
    assert components["pwntools"]["python_packages"] == {
        "pwntools": "4.15.0",
        "pyelftools": "0.33",
        "unicorn": "2.1.2",
    }


def test_rea_skill_is_installed_into_workspace_and_reused(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "rea-package" / "skills" / rea_setup.REA_SKILL_NAME
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\n"
        f"name: {rea_setup.REA_SKILL_NAME}\n"
        "metadata:\n"
        '  version: "49"\n'
        "---\n",
        encoding="utf-8",
    )
    (source / "references").mkdir()
    (source / "references" / "guide.md").write_text("guide\n", encoding="utf-8")
    monkeypatch.setattr(
        rea_setup,
        "_find_rea_skill_source",
        lambda: (source, rea_setup.REA_PACKAGE_VERSION),
    )
    workspace = tmp_path / "workspace"

    first = rea_setup.ensure_rea_skill(workspace)
    second = rea_setup.ensure_rea_skill(workspace)

    installed = workspace / "skills" / rea_setup.REA_SKILL_NAME
    assert first["installed"] is True
    assert first["updated"] is True
    assert second["installed"] is True
    assert second["updated"] is False
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references" / "guide.md").read_text(encoding="utf-8") == "guide\n"


def test_install_rea_macos_frida_prebuild_uses_verified_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "npx" / "node_modules" / "rea-agents"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"version": "6.8.0"}),
        encoding="utf-8",
    )
    frida = package.parent / "frida"
    (frida / "build").mkdir(parents=True)
    (frida / "package.json").write_text(
        json.dumps({"version": rea_setup.REA_FRIDA_VERSION}),
        encoding="utf-8",
    )
    binding_source = tmp_path / "frida_binding.node"
    binding_source.write_bytes(b"native binding")
    archive = tmp_path / "frida-prebuilt.tar.gz"
    with tarfile.open(archive, "w:gz") as tarball:
        tarball.add(binding_source, arcname="build/frida_binding.node")

    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")
    monkeypatch.setattr(rea_setup, "_rea_skill_source_candidates", lambda: [package])
    monkeypatch.setattr(
        rea_setup,
        "_download_component_artifact",
        lambda *_args: archive,
    )

    rea_setup._install_rea_macos_frida_prebuild(tmp_path / "workspace")

    assert (frida / "build" / "frida_binding.node").read_bytes() == b"native binding"


def test_manual_and_system_components_never_enter_auto_install_plan(monkeypatch) -> None:
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "linux-x64")
    plan = {item["id"]: item for item in rea_setup.enhanced_component_plan()}

    for component_id in ("ida", "jeb", "gdb", "objdump", "sigrok", "firmware"):
        assert plan[component_id]["state"] == "needs_action"
        assert plan[component_id]["auto_install"] is False


def test_upgrade_backup_preserves_existing_provider_env(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    old_env = {
        "PATH": "old-path",
        "JAVA_HOME": "D:/existing/jdk",
        "REA_PWNTOOLS_PYTHON": "/existing/python",
    }
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcp": {
                    "rea": {
                        "transport": "stdio",
                        "command": str(runtime.node),
                        "args": ["rea-agents@6.7.0", "mcp"],
                        "env": old_env,
                        "state": "connected",
                    }
                },
                "mounts": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rea_setup, "resolve_node_runtime", lambda *_args: runtime)
    monkeypatch.setattr(rea_setup, "ensure_node_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        rea_setup,
        "_npx_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "6.8.0\n", ""),
    )
    monkeypatch.setattr(rea_setup, "_installed_rea_version", lambda _runtime: "6.8.0")
    monkeypatch.setattr(rea_setup, "_scan_enhancements", lambda *_args: [])

    rea_setup.install_rea_base(workspace)

    record = json.loads(state_path.read_text(encoding="utf-8"))["mcp"]["rea"]
    assert "rea-agents@6.8.0" in record["args"]
    assert record["env"]["JAVA_HOME"] == "D:/existing/jdk"
    assert record["env"]["REA_PWNTOOLS_PYTHON"] == "/existing/python"
    assert list((workspace / "mcp").glob("state.json.rea-setup-*.bak"))


def test_reset_removes_only_workswarm_rea_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcp": {
                    "rea": {"transport": "stdio", "command": "node", "state": "connected"},
                    "harmonyos-mcp": {"transport": "stdio", "command": "npx"},
                },
                "mounts": {},
            }
        ),
        encoding="utf-8",
    )
    managed_tool = workspace / "rea" / "tools" / "jadx" / "bridge.jar"
    managed_tool.parent.mkdir(parents=True)
    managed_tool.write_bytes(b"jar")
    (workspace / "mcp" / "state.json.rea-setup-1.bak").write_text("{}", encoding="utf-8")

    result = rea_setup.reset_rea_setup(workspace)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["removed_record"] is True
    assert result["removed_managed_tools"] is True
    assert "rea" not in state["mcp"]
    assert "harmonyos-mcp" in state["mcp"]
    assert not (workspace / "rea").exists()
    assert not list((workspace / "mcp").glob("state.json.rea-setup-*.bak"))


def test_configure_component_writes_validated_json_and_env(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcp": {
                    "rea": {
                        "transport": "stdio",
                        "command": "node",
                        "args": ["rea-agents@6.8.0", "mcp"],
                        "env": {},
                        "state": "connected",
                    }
                },
                "mounts": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rea_setup, "scan_rea_setup", lambda _workspace=None: {"ready": True})

    result = rea_setup.configure_component(
        "ida",
        {"registration_json": '{"command":"python","args":["server.py"]}'},
        workspace,
    )

    config_path = workspace / "rea" / "config" / "ida-mcp.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["ready"] is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["command"] == "python"
    assert state["mcp"]["rea"]["env"]["REA_IDA_MCP_CONFIG"] == str(config_path.resolve())


def test_configure_component_rejects_invalid_json(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"version": 1, "mcp": {"rea": {"env": {}, "state": "connected"}}, "mounts": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(rea_setup, "scan_rea_setup", lambda _workspace=None: {"ready": True})

    try:
        rea_setup.configure_component("ida", {"registration_json": "not-json"}, workspace)
    except ValueError as exc:
        assert "不是有效 JSON" in str(exc)
    else:
        raise AssertionError("invalid JSON must be rejected")


def test_configure_jeb_validates_and_writes_url(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"version": 1, "mcp": {"rea": {"env": {}, "state": "connected"}}, "mounts": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(rea_setup, "scan_rea_setup", lambda _workspace=None: {"ready": True})

    rea_setup.configure_component(
        "jeb",
        {"jeb_mcp_url": "http://127.0.0.1:8425/mcp"},
        workspace,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["mcp"]["rea"]["env"]["REA_JEB_MCP_URL"] == "http://127.0.0.1:8425/mcp"

    try:
        rea_setup.configure_component("jeb", {"jeb_mcp_url": "not-a-url"}, workspace)
    except ValueError as exc:
        assert "http://" in str(exc)
    else:
        raise AssertionError("invalid JEB URL must be rejected")


def test_configure_component_validates_required_any_and_directory_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    state_path = workspace / "mcp" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mcp": {"rea": {"env": {}, "state": "connected"}},
                "mounts": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rea_setup, "scan_rea_setup", lambda _workspace=None: {"ready": True})

    try:
        rea_setup.configure_component("ida", {}, workspace)
    except ValueError as exc:
        assert "至少填写一个" in str(exc)
    else:
        raise AssertionError("empty IDA configuration must be rejected")

    wrong_ghidra = tmp_path / "ghidra"
    wrong_ghidra.mkdir()
    try:
        rea_setup.configure_component("ghidra", {"ghidra_dir": str(wrong_ghidra)}, workspace)
    except ValueError as exc:
        assert "不像是正确的安装目录" in str(exc)
    else:
        raise AssertionError("Ghidra directory without analyzeHeadless must be rejected")

    valid_ghidra = tmp_path / "ghidra-public"
    (valid_ghidra / "support").mkdir(parents=True)
    (valid_ghidra / "support" / "analyzeHeadless").write_text("", encoding="utf-8")
    result = rea_setup.configure_component(
        "ghidra",
        {"ghidra_dir": str(valid_ghidra)},
        workspace,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["ready"] is True
    assert state["mcp"]["rea"]["env"]["GHIDRA_INSTALL_DIR"] == str(valid_ghidra.resolve())


def test_managed_install_reports_download_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifact = tmp_path / "fixture.jar"
    artifact.write_bytes(b"fixture")
    component = {
        "id": "jadx",
        "display_name": "JADX / Android",
        "install_mode": "managed",
        "default_selected": True,
        "artifact": {"size_bytes": 7},
    }
    monkeypatch.setattr(rea_setup, "rea_mcp_record", lambda _workspace=None: None)
    monkeypatch.setattr(rea_setup, "_manifest_components", lambda: {"jadx": component})

    def fake_download(_component, _workspace, _artifact, progress_callback):
        progress_callback(
            0.5,
            {
                "phase": "downloading",
                "source": "https://example.invalid/jadx.jar",
                "downloaded_bytes": 4,
                "total_bytes": 7,
                "bytes_per_second": 2,
                "eta_seconds": 2,
            },
        )
        return artifact

    monkeypatch.setattr(rea_setup, "_download_component_artifact", fake_download)
    monkeypatch.setattr(
        rea_setup,
        "_apply_component_files",
        lambda *_args: {"REA_JADX_MCP_JAR": str(artifact)},
    )
    progress: list[tuple[str, float, str, dict | None]] = []

    result = rea_setup.install_managed_enhancements(
        tmp_path / "workspace",
        progress_callback=lambda stage, percent, message, details: progress.append(
            (stage, percent, message, details)
        ),
    )

    stages = [item[0] for item in progress]
    assert "component_jadx_download" in stages
    assert "component_jadx_verify" not in stages
    assert "component_jadx_install" in stages
    assert "component_jadx_complete" in stages
    download = next(item for item in progress if item[0] == "component_jadx_download")
    assert "4 B / 7 B" in download[2]
    assert download[3]["eta_seconds"] == 2
    assert result["env"]["REA_JADX_MCP_JAR"] == str(artifact)
    assert result["results"][0]["source"] == "https://example.invalid/jadx.jar"


def test_download_detail_marks_mirror_fallback() -> None:
    detail = rea_setup._download_detail(
        phase="downloading",
        source="https://mirror.example/archive.zip",
        downloaded_bytes=50,
        total_bytes=100,
        started_at=time.monotonic() - 1,
        source_index=1,
        source_count=3,
    )

    assert detail["mirror_fallback"] is True
    assert detail["source_index"] == 1
    assert detail["source_count"] == 3


def test_download_sources_prefer_verified_domestic_mirrors(monkeypatch) -> None:
    sources = [
        "https://github.com/example/project/release.zip",
        "https://1.na.dl.wireshark.org/osx/Wireshark.dmg",
        "https://ghfast.top/https://github.com/example/project/release.zip",
        "https://mirrors.cloud.tencent.com/tools/release.zip",
        "https://mirrors.tuna.tsinghua.edu.cn/tools/release.zip",
        "https://gh-proxy.com/https://github.com/example/project/release.zip",
    ]
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")

    assert rea_setup._preferred_download_sources(sources) == [
        "https://mirrors.tuna.tsinghua.edu.cn/tools/release.zip",
        "https://mirrors.cloud.tencent.com/tools/release.zip",
        "https://ghfast.top/https://github.com/example/project/release.zip",
        "https://gh-proxy.com/https://github.com/example/project/release.zip",
        "https://github.com/example/project/release.zip",
        "https://1.na.dl.wireshark.org/osx/Wireshark.dmg",
    ]

    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")
    assert rea_setup._preferred_download_sources(sources) == [
        "https://mirrors.tuna.tsinghua.edu.cn/tools/release.zip",
        "https://mirrors.cloud.tencent.com/tools/release.zip",
        "https://ghfast.top/https://github.com/example/project/release.zip",
        "https://gh-proxy.com/https://github.com/example/project/release.zip",
        "https://github.com/example/project/release.zip",
        "https://1.na.dl.wireshark.org/osx/Wireshark.dmg",
    ]


def test_download_supports_http11_user_agent_without_range_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = b"hopper-dmg"
    seen: list[list[str]] = []

    class FakeProcess:
        def __init__(self, argv, **_kwargs):
            seen.append(argv)
            Path(argv[argv.index("--output") + 1]).write_bytes(payload)
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", ""

    artifact = {
        "url": "https://example.invalid/hopper.dmg",
        "sha1": hashlib.sha1(payload).hexdigest(),
        "size_bytes": len(payload),
        "destination": "cache/hopper.dmg",
        "max_bytes": 1024,
        "user_agent": "Workswarm-Hopper-Test",
        "resume": False,
        "http1_1": True,
    }
    monkeypatch.setattr(rea_setup.shutil, "which", lambda _name: "/usr/bin/curl")
    monkeypatch.setattr(rea_setup.subprocess, "Popen", FakeProcess)

    downloaded = rea_setup._download_component_artifact(
        {"artifact": artifact},
        tmp_path,
        artifact,
    )

    assert downloaded.read_bytes() == payload
    assert "--http1.1" in seen[0]
    assert "--continue-at" not in seen[0]
    assert seen[0][seen[0].index("--user-agent") + 1] == "Workswarm-Hopper-Test"


def test_managed_executable_selection_prefers_declared_primary_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "rea" / "tools" / "cutter" / "2.5.0"
    wrong = root / "share" / "cons" / "cutter"
    expected = root / "Cutter-v2.5.0-Windows-x86_64" / "cutter.exe"

    def fake_extract(_archive: Path, destination: Path, _archive_type: str) -> None:
        for path in (wrong, expected):
            portable = destination / path.relative_to(root)
            portable.parent.mkdir(parents=True, exist_ok=True)
            portable.write_text("", encoding="utf-8")

    monkeypatch.setattr(rea_setup, "_extract_archive_safely", fake_extract)
    component = {
        "id": "cutter",
        "env_key": "REA_CUTTER_COMMAND",
        "executable_names": ["Cutter.exe", "Cutter", "cutter"],
        "extract_to": "rea/tools/cutter/2.5.0",
    }

    result = rea_setup._apply_component_files(
        component,
        {"archive_type": "zip"},
        tmp_path / "cutter.zip",
        workspace,
        {},
    )

    assert result["REA_CUTTER_COMMAND"] == str(expected)


def test_cutter_bridge_install_adds_release_bridge_and_windows_runtime_fix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "rea-agents"
    bridge = package / "bridge" / "cutter" / "rea_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("class ReaCutterPlugin: pass\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"version": "6.8.0"}),
        encoding="utf-8",
    )
    cutter = (
        tmp_path
        / "Cutter-v2.5.0-Windows-x86_64"
        / "cutter.exe"
    )
    cutter.parent.mkdir(parents=True)
    cutter.write_text("", encoding="utf-8")
    plugin_dir = tmp_path / "plugins" / "python"

    monkeypatch.setattr(rea_setup, "_rea_skill_source_candidates", lambda: [package])
    monkeypatch.setattr(rea_setup, "_cutter_user_plugin_dir", lambda: plugin_dir)
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "windows-x64")

    def fake_libffi(_workspace: Path, cutter_root: Path) -> Path:
        destination = cutter_root / "libffi-8.dll"
        destination.write_bytes(b"fixture")
        return destination

    monkeypatch.setattr(rea_setup, "_ensure_cutter_libffi", fake_libffi)

    installed = rea_setup._install_cutter_bridge(cutter, tmp_path / "workspace")

    assert (plugin_dir / "rea_bridge.py").is_file()
    assert (plugin_dir / "workswarm_rea_bridge.py").is_file()
    assert (cutter.parent / "libffi-8.dll").is_file()
    assert installed["bootstrap"] == str(plugin_dir / "workswarm_rea_bridge.py")
    ready, reason = rea_setup._cutter_bridge_readiness(cutter)
    assert ready is True
    assert "启动 Cutter 后可连接" in reason


def test_cutter_bridge_install_adds_macos_lifecycle_fix_without_windows_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "rea-agents"
    bridge = package / "bridge" / "cutter" / "rea_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("class ReaCutterPlugin: pass\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"version": "6.8.0"}),
        encoding="utf-8",
    )
    app = tmp_path / "Cutter.app"
    cutter = app / "Contents" / "MacOS" / "Cutter"
    cutter.parent.mkdir(parents=True)
    cutter.write_text("", encoding="utf-8")
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "2.5.0"}, stream)
    plugin_dir = tmp_path / "plugins" / "python"

    monkeypatch.setattr(rea_setup, "_rea_skill_source_candidates", lambda: [package])
    monkeypatch.setattr(rea_setup, "_cutter_user_plugin_dir", lambda: plugin_dir)
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")
    monkeypatch.setattr(
        rea_setup,
        "_ensure_cutter_libffi",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not download Windows runtime")),
    )

    installed = rea_setup._install_cutter_bridge(cutter, tmp_path / "workspace")

    assert installed["bootstrap"] == str(plugin_dir / "workswarm_rea_bridge.py")
    assert installed["libffi"] is None
    ready, reason = rea_setup._cutter_bridge_readiness(cutter)
    assert ready is True
    assert "启动 Cutter 后可连接" in reason


def test_dmg_extraction_uses_ditto_only_for_app_bundles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "destination"
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        command = argv[0]
        if command == "hdiutil" and argv[1] == "attach":
            mount = Path(argv[argv.index("-mountpoint") + 1])
            app = mount / "Cutter.app" / "Contents" / "MacOS"
            app.mkdir(parents=True)
            (app / "Cutter").write_text("", encoding="utf-8")
            (mount / ".background").mkdir()
            (mount / "Applications").symlink_to("/Applications", target_is_directory=True)
        elif command == "ditto":
            target = Path(argv[2])
            (target / "Contents" / "MacOS").mkdir(parents=True)
            (target / "Contents" / "MacOS" / "Cutter").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rea_setup, "_run", fake_run)

    rea_setup._extract_archive_safely(
        tmp_path / "Cutter.dmg",
        destination,
        "dmg",
    )

    assert (destination / "Cutter.app" / "Contents" / "MacOS" / "Cutter").is_file()
    assert not (destination / "Applications").exists()
    assert not (destination / ".background").exists()
    ditto_calls = [call for call in calls if call[0] == "ditto"]
    assert ditto_calls == [["ditto", str(destination / ".mount" / "Cutter.app"), str(destination / "Cutter.app")]]


def test_pkg_extraction_uses_nonexistent_staging_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "tools" / "rizin" / "0.9.1"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")

    def fake_run(argv, **_kwargs):
        assert argv[:2] == ["pkgutil", "--expand-full"]
        staging = Path(argv[-1])
        assert not staging.exists()
        (staging / "payload" / "bin").mkdir(parents=True)
        (staging / "payload" / "bin" / "rizin").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rea_setup, "_run", fake_run)

    rea_setup._extract_archive_safely(
        tmp_path / "rizin.pkg",
        destination,
        "pkg",
    )

    assert (destination / "payload" / "bin" / "rizin").is_file()
    assert not (destination / "old.txt").exists()
    assert not list(destination.parent.glob(".0.9.1.*"))


def test_zip_extraction_preserves_unix_executable_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / "tool.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        member = zipfile.ZipInfo("support/analyzeHeadless")
        member.external_attr = 0o100755 << 16
        zipped.writestr(member, "#!/bin/sh\n")
    monkeypatch.setattr(rea_setup.os, "name", "posix")

    rea_setup._extract_zip_safely(archive, tmp_path / "tool")

    executable = tmp_path / "tool" / "support" / "analyzeHeadless"
    assert executable.stat().st_mode & 0o111 == 0o111
    executable.chmod(0o644)

    rea_setup._restore_zip_file_modes(archive, tmp_path / "tool")

    assert executable.stat().st_mode & 0o111 == 0o111


def test_macos_rizin_uses_private_dylib_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    rizin = (
        workspace
        / "rea"
        / "tools"
        / "rizin"
        / "0.9.1"
        / "rizin.pkg"
        / "Payload"
        / "usr"
        / "local"
        / "bin"
        / "rizin"
    )

    def fake_extract(_archive: Path, _destination: Path, _archive_type: str) -> None:
        rizin.parent.mkdir(parents=True)
        rizin.write_text("#!/bin/sh\necho 'rizin 0.9.1'\n", encoding="utf-8")
        rizin.chmod(0o755)

    monkeypatch.setattr(rea_setup, "_extract_archive_safely", fake_extract)
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")
    component = {
        "id": "rizin",
        "env_key": "REA_RIZIN_COMMAND",
        "executable_names": ["rizin.exe", "rizin"],
        "extract_to": "rea/tools/rizin/0.9.1",
    }

    result = rea_setup._apply_component_files(
        component,
        {"archive_type": "pkg"},
        tmp_path / "rizin.pkg",
        workspace,
        {},
    )

    launcher = Path(result["REA_RIZIN_COMMAND"])
    launched = subprocess.run([str(launcher)], check=False, capture_output=True, text=True)
    assert launched.stdout.strip() == "rizin 0.9.1"


def test_cutter_libffi_is_extracted_with_inner_hash_check(tmp_path: Path) -> None:
    payload = b"official-libffi-fixture"
    archive = tmp_path / "python-embed.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("libffi-8.dll", payload)
    destination = tmp_path / "cutter" / "libffi-8.dll"

    rea_setup._extract_zip_member_atomically(
        archive,
        "libffi-8.dll",
        destination,
        hashlib.sha256(payload).hexdigest(),
    )

    assert destination.read_bytes() == payload


def test_cutter_verification_rejects_incomplete_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime = _runtime(runtime_root)
    cutter = tmp_path / "cutter.exe"
    cutter.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        rea_setup,
        "_cutter_bridge_readiness",
        lambda _path: (False, "REA Cutter 桥接插件尚未安装"),
    )

    result = rea_setup._verify_component(
        "cutter",
        runtime,
        {"REA_CUTTER_COMMAND": str(cutter)},
    )

    assert result["ok"] is False
    assert result["code"] == "cutter_bridge_incomplete"
    assert "桥接插件尚未安装" in result["detail"]


def test_client_verification_reports_rea_error_instead_of_success_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime = _runtime(runtime_root)
    monkeypatch.setattr(
        rea_setup,
        "_rea_cli_json",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": {
                "code": "capability_unavailable",
                "message": "Execution denied (EACCES)",
            },
        },
    )

    result = rea_setup._verify_component(
        "adb",
        runtime,
        {"REA_ADB_PATH": str(tmp_path / "adb")},
    )

    assert result["ok"] is False
    assert result["code"] == "capability_unavailable"
    assert "EACCES" in result["detail"]


def test_hopper_verification_uses_scoped_doctor_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    launcher = str(tmp_path / "hopper")
    monkeypatch.setattr(
        rea_setup,
        "_rea_cli_json",
        lambda *_args, **_kwargs: {
            "ok": True,
            "data": {
                "healthy": True,
                "hopperPath": launcher,
                "scope_checks": [
                    {"name": "node", "ok": True},
                    {"name": "hopper", "ok": True, "detail": launcher},
                ],
                "providerInspections": [],
            },
        },
    )

    result = rea_setup._verify_component(
        "hopper",
        runtime,
        {"HOPPER_LAUNCHER_PATH": launcher},
    )

    assert result["ok"] is True
    assert result["code"] is None
    assert launcher in result["detail"]


def test_provider_verification_reads_doctor_error_envelope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        rea_setup,
        "_rea_cli_json",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": {
                "providerInspections": [
                    {
                        "id": "ghidra",
                        "available": False,
                        "providerVersion": "12.1.4",
                        "checks": [
                            {
                                "name": "headless",
                                "ok": False,
                                "code": "executable_missing",
                                "detail": "support/analyzeHeadless is not executable",
                            }
                        ],
                    }
                ]
            },
        },
    )

    result = rea_setup._verify_component("ghidra", runtime, {})

    assert result["ok"] is False
    assert result["code"] == "executable_missing"
    assert "not executable" in result["detail"]


def test_managed_executable_can_configure_parent_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "rea" / "tools" / "wabt" / "1.0.42"
    expected = root / "wabt-1.0.42" / "bin" / "wasm-validate.exe"

    def fake_extract(_archive: Path, destination: Path, _archive_type: str) -> None:
        portable = destination / expected.relative_to(root)
        portable.parent.mkdir(parents=True)
        portable.write_text("", encoding="utf-8")

    monkeypatch.setattr(rea_setup, "_extract_archive_safely", fake_extract)
    component = {
        "id": "wabt",
        "env_key": "REA_WABT_BIN_DIRECTORY",
        "executable_names": ["wasm-validate.exe", "wasm-validate"],
        "env_directory": True,
        "extract_to": "rea/tools/wabt/1.0.42",
    }

    result = rea_setup._apply_component_files(
        component,
        {"archive_type": "tar.gz"},
        tmp_path / "wabt.tar.gz",
        workspace,
        {},
    )

    assert result["REA_WABT_BIN_DIRECTORY"] == str(expected.parent)


def test_managed_executable_map_marks_binary_executable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    android = workspace / "rea" / "tools" / "adb" / "37.0.1" / "platform-tools" / "adb"

    def fake_extract(_archive: Path, _destination: Path, _archive_type: str) -> None:
        android.parent.mkdir(parents=True)
        android.write_text("", encoding="utf-8")

    monkeypatch.setattr(rea_setup, "_extract_archive_safely", fake_extract)
    monkeypatch.setattr(rea_setup, "_platform_id", lambda: "macos-arm64")
    monkeypatch.setattr(rea_setup.os, "name", "posix")
    component = {
        "id": "adb",
        "env_key": "REA_ADB_PATH",
        "executables": {"macos-arm64": "platform-tools/adb"},
        "extract_to": "rea/tools/adb/37.0.1",
    }

    result = rea_setup._apply_component_files(
        component,
        {"archive_type": "zip"},
        tmp_path / "platform-tools.zip",
        workspace,
        {},
    )

    assert result["REA_ADB_PATH"] == str(android)
    assert os.access(android, os.X_OK)


def test_pwntools_environment_reuses_only_exact_package_versions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    outputs = iter(
        [
            '{"pwntools": "4.15.0", "pyelftools": "0.33", "unicorn": "2.1.2"}',
            '{"pwntools": "4.14.0", "pyelftools": "0.33", "unicorn": "2.1.2"}',
        ]
    )
    monkeypatch.setattr(
        rea_setup,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            next(outputs),
            "",
        ),
    )

    assert rea_setup._python_component_compatible("pwntools", python) is True
    assert rea_setup._python_component_compatible("pwntools", python) is False


def test_managed_python_component_writes_rea_pwntools_python(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    uv = tmp_path / "uv.exe"
    uv.write_text("", encoding="utf-8")
    component = rea_setup._manifest_components()["pwntools"]

    def fake_run(argv, **_kwargs):
        if "venv" in argv:
            python = workspace / "rea" / "tools" / "python" / "pwntools" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rea_setup, "ensure_uv_runtime", lambda *_args, **_kwargs: uv)
    monkeypatch.setattr(rea_setup, "_run", fake_run)
    monkeypatch.setattr(rea_setup.os, "name", "nt")

    env = rea_setup._install_python_component(
        "pwntools",
        component,
        workspace,
        None,
        component_start=20,
        component_end=40,
        cancel_check=None,
    )

    assert env == {
        "REA_PWNTOOLS_PYTHON": str(
            workspace / "rea" / "tools" / "python" / "pwntools" / "Scripts" / "python.exe"
        )
    }
