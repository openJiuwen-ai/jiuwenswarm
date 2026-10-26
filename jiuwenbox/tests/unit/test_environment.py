"""Configured inheritance excludes parent credentials and service loader paths."""

import json
import sys
from types import SimpleNamespace

import pytest

from jiuwenbox.server.environment import ENV_ALLOWLIST, inherited_environment


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("for_runner", [False, True])
def test_inheritance_matches_platform_key_case(monkeypatch, platform, for_runner):
    from jiuwenbox.server import environment

    # Use an ordinary mapping so mixed-case source keys survive on every host.
    source = {
        ENV_ALLOWLIST: '["TOOL_SETTING", "EMPTY_SETTING"]',
        "Path": "tool-path", "SystemRoot": "system-root", "windir": "windows-dir",
        "ComSpec": "command-shell", "tool_setting": "tool-value", "empty_setting": "",
        "JiuwenBox_Worker_Limit": "16", "OPENAI_API_KEY": "must-not-be-forwarded",
    }
    monkeypatch.setattr(environment, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(environment.os, "environ", source)
    env = inherited_environment(for_runner=for_runner)
    assert env["SystemRoot"] == "system-root"
    assert env["windir"] == "windows-dir"
    assert "OPENAI_API_KEY" not in env
    if platform == "win32":
        assert env["PATH"] == "tool-path"
        assert env["COMSPEC"] == "command-shell"
        assert env["TOOL_SETTING"] == "tool-value"
        assert env["EMPTY_SETTING"] == ""
        assert ("JiuwenBox_Worker_Limit" in env) is for_runner
    else:
        assert "PATH" not in env
        assert "COMSPEC" not in env
        assert "TOOL_SETTING" not in env
        assert "EMPTY_SETTING" not in env
        assert "JiuwenBox_Worker_Limit" not in env


def test_inheritance_without_allowlist_excludes_tool_and_secret_keys(monkeypatch):
    monkeypatch.delenv(ENV_ALLOWLIST, raising=False)
    for key in ("CLAW_SKILL_TOKEN", "OPENAI_API_KEY", "DATABASE_URL", "PYTHONPATH", "NODE_OPTIONS", "PYTHONPYCACHEPREFIX"):
        monkeypatch.setenv(key, "test-parent-value")
    env = inherited_environment()
    for key in ("CLAW_SKILL_TOKEN", "OPENAI_API_KEY", "DATABASE_URL", "PYTHONPATH", "NODE_OPTIONS", "PYTHONPYCACHEPREFIX"):
        assert key not in env


@pytest.mark.skipif(sys.platform != "win32", reason="Windows profile variables")
def test_profile_paths_are_not_inherited_even_when_allowlisted(monkeypatch):
    monkeypatch.setenv(ENV_ALLOWLIST, '["home", "USERPROFILE", "TMPDIR"]')
    monkeypatch.setenv("HOME", "C:/host-profile")
    monkeypatch.setenv("USERPROFILE", "C:/host-profile")
    monkeypatch.setenv("TMPDIR", "C:/host-temp")
    for for_runner in (False, True):
        env = inherited_environment(for_runner=for_runner)
        assert not {key.upper() for key in env} & {"HOME", "USERPROFILE", "TMPDIR"}


@pytest.mark.parametrize("raw", ["bad-json", '"PATH"', '["*"]', '[1]'])
def test_invalid_allowlist_fails_closed(monkeypatch, raw):
    monkeypatch.setenv(ENV_ALLOWLIST, raw)
    with pytest.raises(ValueError, match="JIUWENBOX_ENV_ALLOWLIST"):
        inherited_environment()


def test_linux_bwrap_clears_parent_env_and_sets_only_allowed_keys(monkeypatch):
    from jiuwenbox.models.policy import SecurityPolicy
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    monkeypatch.setenv(ENV_ALLOWLIST, json.dumps(["CUSTOM_TOOL_SETTING", "EMPTY_TOOL_SETTING"]))
    monkeypatch.setenv("CUSTOM_TOOL_SETTING", "inherited-setting")
    monkeypatch.setenv("EMPTY_TOOL_SETTING", "")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-forwarded")
    monkeypatch.setenv("PYTHONPATH", "service-loader-path")
    monkeypatch.setenv("NODE_OPTIONS", "service-loader-options")
    monkeypatch.setattr(BwrapConfig, "from_policy", lambda policy, command: BwrapConfig(
        command=command, env={"POLICY_SETTING": "policy-value"},
    ))
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runtime._policy_binds = {}
    runtime._launcher_dirs = {}
    monkeypatch.setattr(runtime, "_needs_privdrop_for_process_identity", lambda *a, **k: False)
    args = runtime._build_sandbox_bwrap_args(
        "sb", SecurityPolicy(), ["echo", "ok"], workdir=None,
        sandbox_env={"CUSTOM_TOOL_SETTING": "explicit-override"},
        netns_attached=False, seccomp_fd=None,
    )
    assert "--clearenv" in args
    env = {args[i + 1]: args[i + 2] for i, arg in enumerate(args) if arg == "--setenv"}
    assert env["CUSTOM_TOOL_SETTING"] == "explicit-override"
    assert env["EMPTY_TOOL_SETTING"] == ""
    assert env["POLICY_SETTING"] == "policy-value"
    assert "OPENAI_API_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "PYTHONPATH" not in env
    assert "NODE_OPTIONS" not in env
