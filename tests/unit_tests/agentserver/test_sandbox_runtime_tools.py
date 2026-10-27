"""Common runtime declarations preserve interpreter selection and narrow ACL roots."""

import json
import os
import shutil
import sys

import pytest

from jiuwenswarm.server.runtime import pip_env
from jiuwenswarm.server.sandbox import runtime_tools

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime declarations")


@pytest.fixture(autouse=True)
def host_pins(monkeypatch):
    pins = {}
    monkeypatch.setattr(pip_env, "read_python_base_pin", lambda root: pins.get(root))
    monkeypatch.setattr(pip_env, "write_python_base_pin", lambda root, base, **kwargs: pins.__setitem__(root, base))
    monkeypatch.setattr(pip_env, "_venv_ready", None)
    for key in list(os.environ):
        if key.startswith(("JIUWENBOX_", "JIUWENCLAW_")) or key == "CLAW_PYTHON_HOME":
            monkeypatch.delenv(key)
    return pins


def python(root):
    executable = root / "python.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    return executable


def venv(root, base):
    executable = python(root / "Scripts")
    (root / "pyvenv.cfg").write_text(f"home = {base.parent}\nexecutable = {base}\n", encoding="utf-8")
    return executable


def test_auto_declaration_uses_managed_venv_before_runner_and_covers_its_actual_base(tmp_path, host_pins):
    base = python(tmp_path / "runtime" / "python-win")
    managed = venv(tmp_path / "dependencies" / "venv", base)
    host_pins[managed.parent.parent] = base
    runner = python(tmp_path / "runner-python")
    node = tmp_path / "runtime" / "node-win"
    node.mkdir()
    (node / "node.exe").touch()
    (node / "npm.cmd").touch()
    env = {"PATH": str(node), "JIUWENBOX_VENV_DIR": str(managed.parent.parent),
           "JIUWENBOX_RUNNER_PYTHON": str(runner)}
    runtime_tools.declare_windows_runtime_tools(env)
    declared = json.loads(env["JIUWENBOX_TOOL_PATHS"])
    assert declared["executable_dirs"] == [str(managed.parent), str(node)]
    assert set(declared["read_roots"]) == {str(managed.parent.parent), str(base.parent), str(runner.parent), str(node)}
    assert str(tmp_path) not in declared["read_roots"]
    assert str(node.parent) not in declared["read_roots"]


def test_existing_venv_base_wins_over_current_source_interpreter(tmp_path, monkeypatch, host_pins):
    base = python(tmp_path / "python-win")
    existing = venv(tmp_path / "venv", base)
    host_pins[existing.parent.parent] = base
    source = venv(tmp_path / "source" / ".venv", python(tmp_path / "source-base"))
    monkeypatch.delenv("JIUWENCLAW_BASE_PYTHON", raising=False)
    monkeypatch.setenv("CLAW_PYTHON_HOME", str(source.parent.parent))
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: existing.parent.parent)
    monkeypatch.setattr(sys, "executable", str(source))
    assert pip_env.resolve_base_python() == base


@pytest.mark.parametrize("selection", ["current", "override", "home"])
def test_selected_venv_resolves_full_base_not_scripts(tmp_path, monkeypatch, selection):
    base = python(tmp_path / "base")
    selected = venv(tmp_path / "selected", base)
    monkeypatch.delenv("JIUWENCLAW_BASE_PYTHON", raising=False)
    monkeypatch.delenv("CLAW_PYTHON_HOME", raising=False)
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr(sys, "executable", str(selected))
    if selection == "override":
        monkeypatch.setenv("JIUWENCLAW_BASE_PYTHON", str(selected))
    elif selection == "home":
        monkeypatch.setenv("CLAW_PYTHON_HOME", str(selected.parent.parent))
    assert pip_env.resolve_base_python() == base


@pytest.mark.parametrize("config", ["home = missing", "executable = {self}"])
def test_invalid_or_cyclic_venv_base_is_not_treated_as_a_runtime(tmp_path, config):
    selected = python(tmp_path / "venv" / "Scripts")
    (selected.parent.parent / "pyvenv.cfg").write_text(config.format(self=selected), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot resolve base Python"):
        pip_env.resolve_python_base(selected)


def test_bare_python_resolves_declared_venv_even_with_a_different_runner(tmp_path, monkeypatch, host_pins):
    SecurityPolicy = pytest.importorskip("jiuwenbox.models.policy").SecurityPolicy
    _resolve_tool_paths = pytest.importorskip("jiuwenbox.server.policy_reader")._resolve_tool_paths
    _build_windows_exec_env = pytest.importorskip("jiuwenbox.server.sandbox_manager")._build_windows_exec_env

    base = python(tmp_path / "python-win")
    managed = venv(tmp_path / "venv", base)
    host_pins[managed.parent.parent] = base
    runner = python(tmp_path / "runner")
    env = {"PATH": str(runner.parent), "PATHEXT": ".EXE;.CMD", "JIUWENBOX_ENV_ALLOWLIST": "[]",
           "JIUWENBOX_PATH_ALLOWLIST": "[]", "JIUWENBOX_VENV_DIR": str(managed.parent.parent),
           "JIUWENBOX_RUNNER_PYTHON": str(runner)}
    runtime_tools.declare_windows_runtime_tools(env)
    with monkeypatch.context() as context:
        for name in list(os.environ):
            context.delenv(name)
        for name, value in env.items():
            context.setenv(name, value)
        policy = _resolve_tool_paths(SecurityPolicy(version=1))
        result = _build_windows_exec_env(None, policy.windows.filesystem.tool_paths)
    assert result["PATH"].split(os.pathsep)[0] == str(managed.parent)
    assert shutil.which("python.exe", path=result["PATH"]) == str(managed)


def test_unknown_node_layout_requires_explicit_declaration(tmp_path):
    node = tmp_path / "user-data" / "node.exe"
    node.parent.mkdir()
    node.touch()
    env = {"PATH": str(node.parent)}
    runtime_tools.declare_windows_runtime_tools(env)
    assert "JIUWENBOX_TOOL_PATHS" not in env


@pytest.mark.parametrize("key", ["home", "executable", "base-executable"])
def test_standard_and_virtualenv_base_metadata_are_supported(tmp_path, key):
    base = python(tmp_path / "base")
    selected = python(tmp_path / "venv" / "Scripts")
    value = base.parent if key == "home" else base
    (selected.parent.parent / "pyvenv.cfg").write_text(f"{key} = {value}\n", encoding="utf-8")
    assert pip_env.resolve_python_base(selected) == base


def test_explicit_tool_interpreter_selection_is_preserved(tmp_path):
    tools = {"read_roots": [str(tmp_path / "skill-venv")],
             "executable_dirs": [str(tmp_path / "skill-venv" / "Scripts")]}
    encoded = json.dumps(tools)
    env = {"JIUWENBOX_TOOL_PATHS": encoded, "JIUWENBOX_VENV_DIR": str(tmp_path / "isolation-venv")}
    runtime_tools.declare_windows_runtime_tools(env)
    assert env["JIUWENBOX_TOOL_PATHS"] == encoded


def test_node_root_does_not_include_sibling_user_data(tmp_path):
    node = tmp_path / "runtime" / "node-win"
    node.mkdir(parents=True)
    (node / "node.exe").touch()
    (node / "npm.cmd").touch()
    env = {"PATH": str(node)}
    runtime_tools.declare_windows_runtime_tools(env)
    assert json.loads(env["JIUWENBOX_TOOL_PATHS"]) == {
        "read_roots": [str(node)], "executable_dirs": [str(node)],
    }


def test_cwd_and_relative_path_do_not_implicitly_authorize_tools(tmp_path, monkeypatch):
    node = tmp_path / "node.exe"
    node.touch()
    (tmp_path / "npm.cmd").touch()
    monkeypatch.chdir(tmp_path)
    env = {"PATH": "."}
    runtime_tools.declare_windows_runtime_tools(env)
    assert "JIUWENBOX_TOOL_PATHS" not in env


@pytest.mark.parametrize("initial_roots", ["", "  ", "[]"])
def test_service_sources_are_readonly_and_do_not_replace_explicit_tools(tmp_path, initial_roots):
    source = tmp_path / "repo" / "jiuwenbox" / "src"
    skills = tmp_path / "agent" / "workspace" / "skills"
    unrelated = tmp_path / "private"
    for folder in (source, skills, unrelated):
        folder.mkdir(parents=True)
    env = {"JIUWENBOX_TOOL_PATHS": "{}", "JIUWENBOX_SKILLS_DIR": str(skills),
           "JIUWENBOX_SERVICE_READ_ROOTS": initial_roots,
           "PYTHONPATH": str(unrelated)}
    runtime_tools.declare_windows_service_read_roots(env, source)
    runtime_tools.declare_windows_runtime_tools(env)
    assert env["JIUWENBOX_TOOL_PATHS"] == "{}"
    assert json.loads(env["JIUWENBOX_SERVICE_READ_ROOTS"]) == [str(source)]
    assert str(skills) not in json.loads(env["JIUWENBOX_SERVICE_READ_ROOTS"])
    assert str(source.parent) not in json.loads(env["JIUWENBOX_SERVICE_READ_ROOTS"])
    assert str(unrelated) not in json.loads(env["JIUWENBOX_SERVICE_READ_ROOTS"])


def test_skill_directory_alone_does_not_create_a_service_readonly_grant(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    env = {"JIUWENBOX_SKILLS_DIR": str(skills)}
    runtime_tools.declare_windows_service_read_roots(env, None)
    assert "JIUWENBOX_SERVICE_READ_ROOTS" not in env


def test_service_source_discovery_does_not_grant_relative_or_missing_paths(tmp_path):
    env = {"JIUWENBOX_SKILLS_DIR": "relative-skills", "PYTHONPATH": str(tmp_path)}
    runtime_tools.declare_windows_service_read_roots(env, tmp_path / "missing")
    assert "JIUWENBOX_SERVICE_READ_ROOTS" not in env


def test_service_read_roots_reject_invalid_explicit_json(tmp_path):
    env = {"JIUWENBOX_SERVICE_READ_ROOTS": '{}'}
    with pytest.raises(ValueError, match="list of paths"):
        runtime_tools.declare_windows_service_read_roots(env, tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows case-insensitive service paths")
def test_service_read_roots_deduplicate_windows_case_and_separators(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    supplied = str(source.resolve()).upper().replace("\\", "/")
    env = {"JIUWENBOX_SERVICE_READ_ROOTS": json.dumps([supplied, str(source.resolve())])}
    runtime_tools.declare_windows_service_read_roots(env, source)
    assert json.loads(env["JIUWENBOX_SERVICE_READ_ROOTS"]) == [supplied]


@pytest.mark.asyncio
async def test_bootstrap_declares_the_existing_venv_base_instead_of_another_python(tmp_path, monkeypatch, host_pins):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server import agent_ws_server as server

    base = python(tmp_path / "base")
    managed = venv(tmp_path / "managed", base)
    host_pins[managed.parent.parent] = base
    other = python(tmp_path / "other")
    monkeypatch.setattr(pip_env, "resolve_base_python", lambda: other)
    monkeypatch.setattr(pip_env, "ensure_runtime_venv", lambda: managed.parent.parent)
    monkeypatch.setattr(server, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_select_runner_python", AsyncMock(return_value=str(other)))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    result = await server._build_sandbox_env()
    assert result["JIUWENBOX_BUNDLED_PYTHON"] == str(base.parent)
    assert result["JIUWENCLAW_BASE_PYTHON"] == str(base)
    assert result["JIUWENBOX_RUNNER_PYTHON"] == str(other)


@pytest.mark.parametrize("key", ["home", "executable", "base-executable"])
def test_writable_venv_cannot_change_host_pin_to_another_python(tmp_path, host_pins, key):
    base = python(tmp_path / "trusted-base")
    managed = venv(tmp_path / "managed", base)
    host_pins[managed.parent.parent] = base
    forged = python(tmp_path / "private-user-data")
    value = forged.parent if key == "home" else forged
    (managed.parent.parent / "pyvenv.cfg").write_text(f"{key} = {value}\n", encoding="utf-8")
    env = {"JIUWENBOX_VENV_DIR": str(managed.parent.parent)}
    with pytest.raises(pip_env.RuntimePythonTrustError, match="host Python pin"):
        runtime_tools.declare_windows_runtime_tools(env)
    assert "JIUWENBOX_TOOL_PATHS" not in env
    assert host_pins[managed.parent.parent] == base


@pytest.mark.parametrize("filename", ["NTUSER.DAT", "credentials.json"])
def test_base_metadata_must_reference_a_python_executable(tmp_path, filename):
    victim = tmp_path / "user-profile" / filename
    victim.parent.mkdir()
    victim.touch()
    managed = venv(tmp_path / "managed", python(tmp_path / "base"))
    (managed.parent.parent / "pyvenv.cfg").write_text(f"base-executable = {victim}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cannot resolve base Python"):
        pip_env.resolve_python_base(managed)


def test_unpinned_existing_venv_must_match_independent_host_selection(tmp_path, monkeypatch, host_pins):
    base = python(tmp_path / "host-base")
    managed = venv(tmp_path / "managed", python(tmp_path / "forged-base"))
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: managed.parent.parent)
    monkeypatch.setattr(sys, "executable", str(base))
    monkeypatch.setattr(pip_env, "_ensure_runtime_pip", lambda path: pytest.fail("untrusted interpreter executed"))
    with pytest.raises(pip_env.RuntimePythonTrustError):
        pip_env.ensure_runtime_venv()
    assert host_pins == {}


def test_existing_venv_is_pinned_only_after_matching_host_base(tmp_path, monkeypatch, host_pins):
    base = python(tmp_path / "base")
    managed = venv(tmp_path / "managed", base)
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: managed.parent.parent)
    monkeypatch.setattr(sys, "executable", str(base))
    monkeypatch.setattr(pip_env, "_ensure_runtime_pip", lambda path: None)
    assert pip_env.ensure_runtime_venv() == managed.parent.parent
    assert host_pins == {managed.parent.parent: base}
    (managed.parent.parent / "pyvenv.cfg").write_text("home = missing\n", encoding="utf-8")
    with pytest.raises(pip_env.RuntimePythonTrustError):
        pip_env.ensure_runtime_venv()  # The in-process ready cache must not bypass validation.


def test_new_venv_is_pinned_before_its_python_is_executed(tmp_path, monkeypatch, host_pins):
    base = python(tmp_path / "base")
    managed = tmp_path / "managed"
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: managed)
    monkeypatch.setattr(sys, "executable", str(base))
    monkeypatch.setattr(pip_env, "_create_runtime_venv_dir", lambda root: venv(root, base))

    def check_pip(path):
        assert path == managed / "Scripts" / "python.exe"
        assert host_pins == {managed: base}

    monkeypatch.setattr(pip_env, "_ensure_runtime_pip", check_pip)
    assert pip_env.ensure_runtime_venv() == managed


@pytest.mark.parametrize("source", ["path", "bundled", "runner"])
def test_invalid_optional_python_keeps_managed_venv_node_and_git(tmp_path, host_pins, source, monkeypatch):
    from unittest.mock import Mock

    warning = Mock()
    monkeypatch.setattr(runtime_tools.logger, "warning", warning)
    base = python(tmp_path / "base")
    managed = venv(tmp_path / "managed", base)
    host_pins[managed.parent.parent] = base
    invalid = python(tmp_path / "broken" / "Scripts")
    node = tmp_path / "node"
    node.mkdir()
    (node / "node.exe").touch()
    (node / "npm.cmd").touch()
    git = tmp_path / "git"
    for name in ("cmd/git.exe", "usr/bin/bash.exe"):
        file = git / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    env = {"PATH": os.pathsep.join([str(invalid.parent), str(node), str(git / "cmd")])}
    # PATH is consulted only when the managed venv has not selected its interpreter.
    if source != "path":
        env["JIUWENBOX_VENV_DIR"] = str(managed.parent.parent)
        env["JIUWENBOX_BUNDLED_PYTHON" if source == "bundled" else "JIUWENBOX_RUNNER_PYTHON"] = (
            str(invalid.parent) if source == "bundled" else str(invalid))
    runtime_tools.declare_windows_runtime_tools(env)
    roots = json.loads(env["JIUWENBOX_TOOL_PATHS"])["read_roots"]
    assert str(node) in roots and str(git) in roots
    if source != "path":
        assert str(managed.parent.parent) in roots
    assert str(invalid.parent) not in roots
    warning.assert_called_once()
    assert "Skipping optional Python" in warning.call_args.args[0]


@pytest.mark.asyncio
async def test_bootstrap_refuses_tampered_venv_before_publishing_environment(tmp_path, monkeypatch, host_pins):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server import agent_ws_server as server

    base = python(tmp_path / "base")
    managed = venv(tmp_path / "managed", python(tmp_path / "private-data"))
    host_pins[managed.parent.parent] = base
    monkeypatch.setattr(pip_env, "ensure_runtime_venv", lambda: managed.parent.parent)
    select = AsyncMock()
    monkeypatch.setattr(server, "_select_runner_python", select)
    with pytest.raises(pip_env.RuntimePythonTrustError):
        await server._build_sandbox_env()
    select.assert_not_awaited()
    assert "JIUWENBOX_BUNDLED_PYTHON" not in os.environ


@pytest.mark.parametrize("recovery", ["override", "removed_base", "recreated_venv", "rebuilt_venv"])
def test_host_can_recover_runtime_pin(tmp_path, monkeypatch, host_pins, recovery):
    old = python(tmp_path / "old")
    new = python(tmp_path / "new")
    managed = tmp_path / "managed"
    host_pins[managed] = old
    if recovery != "recreated_venv":
        venv(managed, new if recovery == "rebuilt_venv" else old)
    if recovery == "override":
        monkeypatch.setenv("JIUWENCLAW_BASE_PYTHON", str(new))
        monkeypatch.setattr(pip_env, "_venv_ready", managed)  # Explicit selection bypasses the ready cache.
    elif recovery == "removed_base":
        old.unlink()
    monkeypatch.setattr(sys, "executable", str(new))
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: managed)
    monkeypatch.setattr(pip_env, "_create_runtime_venv_dir", lambda root: venv(root, new))

    def check_pip(path):
        assert host_pins[managed] == new
        assert pip_env.verified_runtime_python_base(managed) == new

    monkeypatch.setattr(pip_env, "_ensure_runtime_pip", check_pip)
    assert pip_env.ensure_runtime_venv() == managed
    assert pip_env.verified_runtime_python_base(managed) == new
    env = {"JIUWENBOX_VENV_DIR": str(managed)}
    runtime_tools.declare_windows_runtime_tools(env)
    roots = json.loads(env["JIUWENBOX_TOOL_PATHS"])["read_roots"]
    assert str(new.parent) in roots
    assert str(old.parent) not in roots


def test_recovery_rejects_venv_that_does_not_match_host_selection(tmp_path, monkeypatch, host_pins):
    old = python(tmp_path / "old")
    new = python(tmp_path / "new")
    forged = python(tmp_path / "forged")
    managed = tmp_path / "managed"
    host_pins[managed] = old
    monkeypatch.setattr(sys, "executable", str(new))
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: managed)
    monkeypatch.setattr(pip_env, "_create_runtime_venv_dir", lambda root: venv(root, forged))
    monkeypatch.setattr(pip_env, "_ensure_runtime_pip", lambda path: pytest.fail("unverified Python executed"))
    with pytest.raises(pip_env.RuntimePythonTrustError):
        pip_env.ensure_runtime_venv()
    assert host_pins[managed] == old


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("disk full"), PermissionError("venv is read-only")])
async def test_bootstrap_keeps_host_base_on_provisioning_failure(tmp_path, monkeypatch, failure):
    from unittest.mock import AsyncMock, Mock

    from jiuwenswarm.server import agent_ws_server as server

    base = python(tmp_path / "host-base")
    monkeypatch.setattr(pip_env, "ensure_runtime_venv", Mock(side_effect=failure))
    monkeypatch.setattr(pip_env, "resolve_base_python", lambda: base)
    monkeypatch.setattr(server, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_select_runner_python", AsyncMock(return_value=str(base)))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    result = await server._build_sandbox_env()
    assert result["JIUWENBOX_BUNDLED_PYTHON"] == str(base.parent)
    assert result["JIUWENCLAW_BASE_PYTHON"] == str(base)
    assert "JIUWENBOX_VENV_DIR" not in result
    runtime_tools.declare_windows_runtime_tools(result)
    assert str(base.parent) in json.loads(result["JIUWENBOX_TOOL_PATHS"])["read_roots"]
