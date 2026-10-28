# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Nested release scripts must use the selected sources, not their own checkout."""
import runpy
import shutil
import sys
from pathlib import Path


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
