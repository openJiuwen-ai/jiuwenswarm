"""Persistent requests see host Shell capabilities without republishing a persona."""

import sys
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common.prompt.shell_environment import ShellCapabilities
from jiuwenswarm.server.jiuwenmate import bash_runtime, execution_driver
from tests.unit_tests.server.jiuwenmate.test_session_scheduler import host, settle, stub_invocation_builder


@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.parametrize("os_type", ["win32", "darwin", "linux"])
async def test_persistent_prompt_uses_host_capabilities_and_explicit_shell(monkeypatch, os_type, language):
    caps = ShellCapabilities(detected=True, git_bash="C:/Git/bin/bash.exe", powershell="powershell.exe")
    if os_type != "win32":
        caps = ShellCapabilities(detected=True, path_bash="/bin/bash", posix_shell="/bin/bash")
    probe = AsyncMock(return_value=caps)
    monkeypatch.setattr(bash_runtime, "get_shell_capabilities", probe, raising=False)
    monkeypatch.setattr(sys, "platform", os_type)
    prompt = await bash_runtime.shell_environment_guidance(language)
    probe.assert_awaited_once_with(os_type)
    assert os_type in prompt
    assert 'shell_type="bash"' in prompt and 'shell_type="powershell"' in prompt
    if os_type == "win32":
        assert "C:/Git/bin/bash.exe" in prompt and "cmd" in prompt
    else:
        assert "/bin/bash" in prompt and "/bin/sh" in prompt
        assert "C:/Git" not in prompt
    assert ("渠道" in prompt) if language == "cn" else ("channel" in prompt)


async def test_unverified_shell_capabilities_are_not_reported_as_installed(monkeypatch):
    monkeypatch.setattr(bash_runtime, "get_shell_capabilities", AsyncMock(return_value=ShellCapabilities()),
                        raising=False)
    prompt = await bash_runtime.shell_environment_guidance("cn")
    assert "尚未确认" in prompt and "不要假定" in prompt


async def test_runtime_prompt_reaches_invocation_without_republishing(host, monkeypatch):
    stub_invocation_builder(monkeypatch)
    guidance = AsyncMock(side_effect=["HOST-SHELL-ONE", "HOST-SHELL-TWO"])
    monkeypatch.setattr(execution_driver, "shell_environment_guidance", guidance, raising=False)
    for text, expected in (("first-host-environment", "HOST-SHELL-ONE"),
                           ("next-host-environment", "HOST-SHELL-TWO")):
        host.core.release(text)
        state = await host.send(text)
        await settle(lambda: text in host.core.ended)
        await host.scheduler.drain()
        assert host.status(state) == ("ended", "completed")
        context = host.core.contexts[text]
        assert expected in context.system_prompt_addendum
        assert context.system_prompt_addendum.count(expected) == 1
        assert context.execution.config_fingerprint == host.core.specs[text].fingerprint()
    assert guidance.await_count == 2
