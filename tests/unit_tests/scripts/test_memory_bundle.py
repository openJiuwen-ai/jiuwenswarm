# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression coverage for the frozen memory SDK verification gate."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import memory_bundle, verify_memory_bundle as verifier


@pytest.mark.asyncio
async def test_real_memory_sdk_registers_video_tools_only_when_configured(tmp_path, monkeypatch):
    for key in list(verifier.os.environ):
        if key.startswith(("JIUWEN_MEMORY_", "EMBED_")):
            monkeypatch.delenv(key)
    await verifier.verify_memory_runtime(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["initialize", "text_tools", "video_tools"])
async def test_verifier_rejects_sdk_failures_and_closes_provider(monkeypatch, tmp_path, failure):
    instances = []

    class Provider:
        def __init__(self, config_dict, **kwargs):
            self.video = "local_media_roots" in config_dict["globals"]
            self.is_initialized = False
            self.closed = False
            instances.append(self)

        async def initialize(self, **kwargs):
            self.is_initialized = failure != "initialize"

        def get_tool_schemas(self):
            names = {"agent_memory_search", "agent_memory_conclude", "agent_memory_read_md_file"}
            if failure == "text_tools":
                names.remove("agent_memory_search")
            if self.video and failure != "video_tools":
                names.update({"agent_memory_video_submit", "agent_memory_video_status"})
            return [{"name": name} for name in names]

        async def shutdown(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider", SimpleNamespace(
        AgentMemoryMemoryProvider=Provider,
    ))
    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder", SimpleNamespace(
        _build_security_provider=lambda org, user: object(),
    ))
    with pytest.raises(RuntimeError):
        await verifier.verify_memory_runtime(tmp_path)
    assert instances and all(provider.closed for provider in instances)


def test_launcher_isolates_config_and_model_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "real-user"))
    monkeypatch.setenv("JIUWEN_MEMORY_LOCAL_MEDIA_ROOTS", '[{"root":"private"}]')
    monkeypatch.setenv("EMBED_API_KEY", "private")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_WORKSPACE_READY", "1")
    calls = []

    def run(command, cwd, env, **kwargs):
        assert cwd != tmp_path / "real-user"
        assert env["JIUWENSWARM_DATA_DIR"] == str(cwd)
        assert "JIUWEN_MEMORY_LOCAL_MEDIA_ROOTS" not in env
        assert "EMBED_API_KEY" not in env
        assert "JIUWENSWARM_RUNTIME_WORKSPACE_READY" not in env
        assert (cwd / "config").is_dir()
        calls.append(cwd)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(verifier.subprocess, "run", run)
    assert verifier.run_isolated(tmp_path / "backend.exe") == 7
    assert len(calls) == 1 and not calls[0].exists()


def test_collection_scope_covers_security_and_excludes_optional_model_backends():
    packages = memory_bundle.MEMORY_RUNTIME_PACKAGES
    modules = memory_bundle.MEMORY_RUNTIME_MODULES
    assert "jiuwen_memory.common.security" in packages
    assert "jiuwen_memory.common.normalizer.normalizer_impl" in packages
    assert "jiuwen_memory.storage.shadow_impl.sqlite_shadow_index" in modules
    assert "jiuwen_memory.storage.watchdog_impl.local_watchdog" in modules
    assert "jiuwen_memory.storage.fs_impl.encrypted_fs_store" in modules
    assert "jiuwen_memory.storage.kv_impl.encrypted_kv_store" in modules
    assert "jiuwen_memory" not in packages
    assert not any("bge" in name or "spacy" in name or "milvus" in name for name in modules)


def test_frozen_failure_preserves_diagnostics_without_logging(monkeypatch, tmp_path):
    async def fail(root):
        raise RuntimeError("missing frozen implementation")

    monkeypatch.setattr(verifier.sys, "frozen", True, raising=False)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(verifier, "verify_memory_runtime", fail)
    assert verifier.main() == 1
    assert "missing frozen implementation" in (tmp_path / "memory-bundle-check.log").read_text(encoding="utf-8")


def test_all_desktop_build_entries_verify_memory():
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    for name in (
        "build-exe.bat",
        "build-exe.ps1",
        "build-macos.sh",
        "build-electron-exe.ps1",
        "build-electron-exe.sh",
    ):
        assert "verify_memory_bundle.py" in (scripts / name).read_text(encoding="utf-8"), name


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_batch_memory_gate_stops_before_next_verifier(tmp_path, exit_code):
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    source = (scripts / "build-exe.bat").read_text(encoding="utf-8")
    gate = source.split("echo Verifying frozen JiuwenMemory", 1)[1]
    gate = "echo Verifying frozen JiuwenMemory" + gate.split("echo Verifying frozen A2UI", 1)[0]
    (tmp_path / "uv.cmd").write_text(
        '@echo off\necho %* > invoked.txt\nexit /b %MEMORY_TEST_EXIT%\n', encoding="utf-8",
    )
    probe = tmp_path / "probe.cmd"
    probe.write_text('@echo off\n' + gate + '\necho passed > passed.txt\nexit /b 0\n', encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "MEMORY_TEST_EXIT": str(exit_code),
        "BUILD_DIST_DIR_NAME": "bundle with spaces",
        "BUILD_EXECUTABLE_NAME_WINDOWS": "backend.exe",
    })
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(probe)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == (1 if exit_code else 0)
    assert (tmp_path / "passed.txt").exists() == (exit_code == 0)
    invocation = (tmp_path / "invoked.txt").read_text(encoding="utf-8")
    assert "--no-sync python scripts\\verify_memory_bundle.py --executable" in invocation
    assert f'"{tmp_path}\\dist\\bundle with spaces\\backend.exe"' in invocation
