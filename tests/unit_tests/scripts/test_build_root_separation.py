# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Nested release scripts must use the selected sources, not their own checkout."""
import ast
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_nested_tui_build_uses_packaging_script_and_source_inputs(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "application sources"
    release = tmp_path / "release scripts"
    (release / "scripts").mkdir(parents=True)
    entry = source / "jiuwenswarm/channels/tui/frontend/src/index.ts"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('fixture');", encoding="utf-8")
    nested_script = release / "scripts/build_tui.py"
    shutil.copyfile(root / "scripts/build_tui.py", nested_script)
    monkeypatch.setenv("WORKSWARM_SOURCE_ROOT", str(source))
    monkeypatch.setenv("WORKSWARM_PACKAGING_ROOT", str(release))
    parent = runpy.run_path(str(root / "scripts/build_python_packages.py"))
    calls = []

    def invoke_nested(command, cwd, env=None):
        assert command[:2] == [sys.executable, str(nested_script)]
        assert cwd == source
        assert not (source / "scripts").exists()
        nested = runpy.run_path(command[1])

        def compile_bun(args, cwd, check):
            calls.append((args, cwd))
            assert cwd == source
            assert args[-1] == str(entry)
            output = Path(args[args.index("--outfile") + 1])
            assert output.is_relative_to(source / "packages/jiuwenswarm-tui")
            output.write_bytes(b"fixture native binary")

        monkeypatch.setattr(nested["subprocess"], "run", compile_bun)
        monkeypatch.setattr(nested["shutil"], "which", lambda _: "fixture-bun")
        monkeypatch.setattr(sys, "argv", command[1:])
        nested["main"]()

    monkeypatch.setitem(parent["build_tui_binary"].__globals__, "run", invoke_nested)
    parent["build_tui_binary"]("windows-x64", clean=False)
    assert len(calls) == 1
    output = source / "packages/jiuwenswarm-tui/jiuwenswarm_tui/resources/tui-bin/windows-x64/jiuwenswarm-tui.exe"
    assert output.read_bytes() == b"fixture native binary"
    assert not (release / "jiuwenswarm").exists()


def _verifier_fixture(tmp_path):
    source = tmp_path / "old source"
    release = tmp_path / "release scripts"
    output = tmp_path / "new build"
    (source / "scripts").mkdir(parents=True)
    (release / "scripts").mkdir(parents=True)
    verifier = release / "scripts/verify_computer_use_bundle.py"
    verifier.write_text("import json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['GATE_REPORT']).write_text(json.dumps({'verifier':str(Path(__file__).resolve()),"
        "'receiver':os.environ.get('GATE_RECEIVER',sys.executable)}),encoding='utf-8')\n", encoding="utf-8")
    (source / "scripts/verify_computer_use_bundle.py").write_text(
        "raise RuntimeError('The source checkout verifier must not run')\n", encoding="utf-8")
    return source, release, output, verifier


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch command expansion")
def test_batch_computer_use_gate_runs_new_output_and_release_verifier(tmp_path):
    source, release, output, verifier = _verifier_fixture(tmp_path)
    receiver = output / "dist/backend/workswarm.exe"
    receiver.parent.mkdir(parents=True)
    shutil.copyfile(getattr(sys, "_base_executable", sys.executable), receiver)
    dll_name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    shutil.copyfile(Path(sys.base_prefix) / dll_name, receiver.parent / dll_name)
    original = Path(__file__).resolve().parents[3] / "scripts/build-exe.bat"
    command = next(line for line in original.read_text(encoding="utf-8").splitlines()
                   if line.startswith('start "" /wait') and "verify_computer_use_bundle.py" in line)
    script = tmp_path / "gate.cmd"
    script.write_text("@echo off\n" + command.replace('start "" /wait', 'start "" /b /wait', 1)
                      + "\nexit /b %errorlevel%\n", encoding="utf-8")
    report = tmp_path / "gate.json"
    env = {**os.environ, "WORKSWARM_BUILD_ROOT": str(output), "WORKSWARM_PACKAGING_ROOT": str(release),
           "BUILD_DIST_DIR_NAME": "backend", "BUILD_EXECUTABLE_NAME_WINDOWS": "workswarm.exe",
           "PYTHONHOME": sys.base_prefix, "GATE_REPORT": str(report)}
    subprocess.run(["cmd.exe", "/d", "/c", str(script)], cwd=source, env=env, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=20)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert Path(result["receiver"]).resolve() == receiver.resolve()
    assert Path(result["verifier"]).resolve() == verifier.resolve()


@pytest.mark.parametrize("wrapper", ["build-macos.sh", "build-electron-exe.sh"])
def test_shell_computer_use_gate_uses_release_verifier(tmp_path, wrapper):
    bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is unavailable")
    source, release, output, verifier = _verifier_fixture(tmp_path)
    receiver = output / "WorkSwarm.app/Contents/MacOS/workswarm"
    receiver.parent.mkdir(parents=True)
    receiver.write_text('#!/bin/sh\nexport GATE_RECEIVER="$0"\nexec "$GATE_PYTHON" "$@"\n', encoding="utf-8")
    receiver.chmod(0o755)
    original = Path(__file__).resolve().parents[3] / "scripts" / wrapper
    command = next(line.strip() for line in original.read_text(encoding="utf-8").splitlines()
                   if "verify_computer_use_bundle.py" in line)
    report = tmp_path / "gate.json"
    env = {**os.environ, "PROJECT_ROOT": source.as_posix(), "PACKAGING_ROOT": release.as_posix(),
           "APP_PATH": (output / "WorkSwarm.app").as_posix(), "BACKEND_DIST": receiver.parent.as_posix(),
           "BUILD_EXECUTABLE_NAME": "workswarm", "GATE_REPORT": str(report),
           "GATE_PYTHON": Path(sys.executable).as_posix()}
    subprocess.run([str(bash), "-c", command], cwd=source, env=env, check=True, capture_output=True, timeout=20)
    result = json.loads(report.read_text(encoding="utf-8"))
    assert Path(result["receiver"]).resolve() == receiver.resolve()
    assert Path(result["verifier"]).resolve() == verifier.resolve()


def test_spec_loads_computer_use_builder_from_release_with_source_data(tmp_path):
    source = tmp_path / "application sources"
    release = tmp_path / "release scripts"
    (release / "scripts").mkdir(parents=True)
    source.mkdir()
    (release / "scripts/build_computer_use.py").write_text(
        "def bundle_data(root):\n    return [(str(root / 'native'), 'cua')]\n", encoding="utf-8",
    )
    original = Path(__file__).resolve().parents[3] / "scripts/jiuwenswarm.spec"
    tree = ast.parse(original.read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "computer_use_build" for target in node.targets))
    data_node = next(node for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "computer_use_data" for target in node.targets))
    namespace = {"os": os, "runpy": runpy, "Path": Path,
                 "packaging_root": str(release), "project_root": str(source)}
    exec(compile(ast.Module(body=[node, data_node], type_ignores=[]), str(original), "exec"), namespace)
    assert namespace["computer_use_data"] == [(str(source / "native"), "cua")]
    assert not (source / "scripts").exists()


def test_metadata_contract_is_shared_by_separate_release_and_frozen_builds(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "application sources"
    release = tmp_path / "release scripts"
    helper_path = source / "jiuwenswarm/common/worker_build_identity.py"
    helper_path.parent.mkdir(parents=True)
    shutil.copyfile(root / "jiuwenswarm/common/worker_build_identity.py", helper_path)
    config_path = release / "scripts/build_config.py"
    config_path.parent.mkdir(parents=True)
    shutil.copyfile(root / "scripts/build_config.py", config_path)
    monkeypatch.setenv("WORKSWARM_SOURCE_ROOT", str(source))
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
    compatibility = tmp_path / "compatibility.json"
    runpy.run_path(str(config_path))["write_build_metadata"](compatibility, "fixture-version")

    tree = ast.parse((root / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8"))
    stamp = next(node for node in tree.body if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Subscript)
                 and isinstance(node.value.func.slice, ast.Constant)
                 and node.value.func.slice.value == "write_build_metadata")
    frozen = tmp_path / "frozen.json"
    namespace = {"_identity_helper": runpy.run_path(str(helper_path)), "_build_metadata_file": frozen,
                 "build_config": SimpleNamespace(version="fixture-version")}
    exec(compile(ast.Module(body=[stamp], type_ignores=[]), "frozen-metadata", "exec"), namespace)
    assert json.loads(frozen.read_text(encoding="utf-8")) == {
        "version": "fixture-version", "build_date": "2026-01-01",
    }
    assert frozen.read_bytes() == compatibility.read_bytes()
    assert not (source / "scripts").exists()
