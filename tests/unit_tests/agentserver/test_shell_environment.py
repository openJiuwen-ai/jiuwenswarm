# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shell prompt discovery must not monopolize an agent's event loop."""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from jiuwenswarm.agents.harness.common.prompt import shell_environment as shell_module


@pytest.fixture
def capability_cache(monkeypatch):
    cache = shell_module._ShellCapabilityCache()
    monkeypatch.setattr(shell_module, "_CAPABILITY_CACHE", cache)
    monkeypatch.setattr(shell_module, "_PROBE_WAIT_SECONDS", 0.1)
    return cache


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "background probe did not reach the expected state"
        await asyncio.sleep(0.005)


async def _get_detected(os_type: str):
    deadline = time.monotonic() + 2.0
    while True:
        result = await shell_module.get_shell_capabilities(os_type)
        if result.detected:
            return result
        assert time.monotonic() < deadline, "released probe did not produce verified capabilities"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_concurrent_members_share_and_reuse_one_probe(monkeypatch, capability_cache):
    calls = []
    expected = shell_module.ShellCapabilities(detected=True, powershell="powershell.exe")

    def detect(os_type):
        calls.append(os_type)
        return expected

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)

    results = await asyncio.gather(*(shell_module.get_shell_capabilities("win32") for _ in range(20)))
    for _ in range(3):
        results.append(await shell_module.get_shell_capabilities("win32"))

    assert all(result == expected for result in results)
    assert calls == ["win32"]


@pytest.mark.asyncio
async def test_slow_probe_leaves_loop_responsive_and_does_not_spawn_replacements(
    monkeypatch, capability_cache,
):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def detect(os_type):
        calls.append(os_type)
        started.set()
        try:
            release.wait(5.0)
            return shell_module.ShellCapabilities(detected=True, powershell="powershell.exe")
        finally:
            finished.set()

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    ticks = []

    async def heartbeat():
        while not release.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        first = await shell_module.get_shell_capabilities("win32")
        assert started.is_set()
        assert not first.detected
        assert len(ticks) >= 2
        more = await asyncio.gather(*(shell_module.get_shell_capabilities("win32") for _ in range(20)))
        assert all(not result.detected for result in more)
        assert calls == ["win32"]
    finally:
        release.set()
        await heartbeat_task
        await _wait_until(finished.is_set)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_probe(monkeypatch, capability_cache):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []
    expected = shell_module.ShellCapabilities(detected=True, powershell="powershell.exe")

    def detect(os_type):
        calls.append(os_type)
        started.set()
        try:
            release.wait(5.0)
            return expected
        finally:
            finished.set()

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    monkeypatch.setattr(shell_module, "_PROBE_WAIT_SECONDS", 2.0)
    cancelled = asyncio.create_task(shell_module.get_shell_capabilities("win32"))
    remaining = None
    try:
        await _wait_until(started.is_set)
        remaining = asyncio.create_task(shell_module.get_shell_capabilities("win32"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        assert await remaining == expected
        assert await shell_module.get_shell_capabilities("win32") == expected
        assert calls == ["win32"]
    finally:
        release.set()
        for task in (cancelled, remaining):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (cancelled, remaining) if task is not None), return_exceptions=True)
        await _wait_until(finished.is_set)


def test_probe_survives_waiter_loop_closing_and_is_reused_by_new_loop(monkeypatch, capability_cache):
    release = threading.Event()
    calls = []
    expected = shell_module.ShellCapabilities(detected=True, posix_shell="/bin/sh")

    def detect(os_type):
        calls.append(os_type)
        release.wait(5.0)
        return expected

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    try:
        assert not asyncio.run(shell_module.get_shell_capabilities("linux")).detected
        release.set()
        assert asyncio.run(_get_detected("linux")) == expected
        assert asyncio.run(shell_module.get_shell_capabilities("linux")) == expected
        assert calls == ["linux"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_environment_change_invalidates_completed_probe(monkeypatch, capability_cache):
    calls = []

    def detect(os_type):
        path = os.environ["GIT_BASH_PATH"]
        calls.append((os_type, path))
        return shell_module.ShellCapabilities(detected=True, git_bash=path)

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    monkeypatch.setenv("GIT_BASH_PATH", "first-bash.exe")
    first = await shell_module.get_shell_capabilities("win32")
    monkeypatch.setenv("GIT_BASH_PATH", "second-bash.exe")
    second = await shell_module.get_shell_capabilities("win32")

    assert first.git_bash == "first-bash.exe"
    assert second.git_bash == "second-bash.exe"
    assert calls == [("win32", "first-bash.exe"), ("win32", "second-bash.exe")]


@pytest.mark.asyncio
async def test_missing_path_and_explicit_empty_path_use_different_snapshots(monkeypatch, capability_cache):
    calls = []

    def detect(os_type):
        path_state = ("PATH" in os.environ, os.environ.get("PATH"))
        calls.append(path_state)
        # Missing PATH lets which() search the default path; an explicitly
        # empty PATH disables that search, so these are distinct capabilities.
        return shell_module.ShellCapabilities(
            detected=True, posix_shell=None if path_state[0] else "/bin/sh",
        )

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    monkeypatch.delenv("PATH", raising=False)
    missing = await shell_module.get_shell_capabilities("linux")
    monkeypatch.setenv("PATH", "")
    empty = await shell_module.get_shell_capabilities("linux")

    assert missing.detected and missing.posix_shell == "/bin/sh"
    assert empty.detected and empty.posix_shell is None
    assert calls == [(False, None), (True, "")]


@pytest.mark.asyncio
async def test_environment_change_during_probe_does_not_start_another_thread(
    monkeypatch, capability_cache,
):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def detect(os_type):
        path = os.environ["GIT_BASH_PATH"]
        calls.append(path)
        started.set()
        try:
            if path == "first-bash.exe":
                release.wait(5.0)
            return shell_module.ShellCapabilities(detected=True, git_bash=path)
        finally:
            finished.set()

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    monkeypatch.setenv("GIT_BASH_PATH", "first-bash.exe")
    try:
        assert not (await shell_module.get_shell_capabilities("win32")).detected
        assert started.is_set()
        monkeypatch.setenv("GIT_BASH_PATH", "second-bash.exe")
        assert not (await shell_module.get_shell_capabilities("win32")).detected
        assert calls == ["first-bash.exe"]
        release.set()
        await _wait_until(finished.is_set)
        result = await _get_detected("win32")
        assert result.detected
        assert result.git_bash == "second-bash.exe"
        assert calls == ["first-bash.exe", "second-bash.exe"]
    finally:
        release.set()
        await _wait_until(finished.is_set)


@pytest.mark.asyncio
async def test_failed_probe_cools_down_before_retry(monkeypatch, capability_cache):
    calls = []

    def detect(os_type):
        calls.append(os_type)
        raise OSError("shell discovery unavailable")

    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    monkeypatch.setattr(shell_module, "_PROBE_RETRY_SECONDS", 0.3)
    first = await shell_module.get_shell_capabilities("win32")
    repeated = await asyncio.gather(*(shell_module.get_shell_capabilities("win32") for _ in range(10)))
    assert not first.detected
    assert all(not result.detected for result in repeated)
    assert calls == ["win32"]

    await asyncio.sleep(0.35)
    assert not (await shell_module.get_shell_capabilities("win32")).detected
    assert calls == ["win32", "win32"]


@pytest.mark.asyncio
async def test_failed_thread_start_cools_down_then_retries(monkeypatch, capability_cache):
    original_start = threading.Thread.start
    starts = []
    probes = []
    expected = shell_module.ShellCapabilities(detected=True, powershell="powershell.exe")

    def start(thread):
        starts.append(thread)
        if len(starts) == 1:
            raise RuntimeError("thread resources temporarily unavailable")
        original_start(thread)

    def detect(os_type):
        probes.append(os_type)
        return expected

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", detect)
    monkeypatch.setattr(shell_module, "_PROBE_RETRY_SECONDS", 0.3)

    assert not (await shell_module.get_shell_capabilities("win32")).detected
    repeated = await asyncio.gather(*(shell_module.get_shell_capabilities("win32") for _ in range(10)))
    assert all(not result.detected for result in repeated)
    assert len(starts) == 1
    assert probes == []

    await asyncio.sleep(0.35)
    assert await shell_module.get_shell_capabilities("win32") == expected
    assert len(starts) == 2
    assert probes == ["win32"]


@pytest.mark.parametrize("os_type", ["win32", "linux"])
@pytest.mark.parametrize("language, phrase", [("cn", "尚未确认"), ("en", "not yet verified")])
def test_unknown_capabilities_are_not_reported_as_unavailable(os_type, language, phrase):
    prompt = shell_module.build_shell_environment_prompt(
        language, os_type, capabilities=shell_module.ShellCapabilities(),
    )
    assert phrase in prompt
    assert "- PowerShell: unavailable" not in prompt
    assert "- PowerShell：不可用" not in prompt
    assert "- Bash/sh: unavailable" not in prompt
    assert "- Bash/sh：不可用" not in prompt


@pytest.mark.parametrize("language", ["cn", "en"])
def test_renderer_uses_snapshot_without_probing(monkeypatch, language):
    def unexpected_probe(*args, **kwargs):
        raise AssertionError("rendering a capability snapshot must not access the filesystem")

    monkeypatch.setattr(shell_module.shutil, "which", unexpected_probe)
    monkeypatch.setattr(shell_module, "_detect_shell_capabilities", unexpected_probe)
    snapshot = shell_module.ShellCapabilities(
        detected=True,
        path_bash=r"C:\Windows\System32\bash.exe",
        powershell=r"C:\PowerShell\pwsh.exe",
        git_bash=r"C:\Git\bin\bash.exe",
    )
    prompt = shell_module.build_shell_environment_prompt(language, "win32", capabilities=snapshot)
    assert snapshot.powershell in prompt
    assert snapshot.git_bash in prompt
    assert snapshot.path_bash in prompt
    assert "WSL stub" in prompt


def test_verified_missing_shell_is_reported_as_unavailable():
    prompt = shell_module.build_shell_environment_prompt(
        "en", "linux", capabilities=shell_module.ShellCapabilities(detected=True),
    )
    assert "- Bash/sh: unavailable" in prompt
    assert "not yet verified" not in prompt
