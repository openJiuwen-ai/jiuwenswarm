# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deny-only project policy, shell-network policy and the Code guard rail.

The project file is never merged into the permission engine; the guard rail
enforces it on its own. These tests cover the schema, the loader, path
normalisation, the engine's shell-network rule (user/global config only) and
the rail's basic behaviour.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
    evaluate_tiered_policy,
)

import jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail as guard_mod
from jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail import (
    ShellPolicyGuardRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions import project_policy as pp
from jiuwenswarm.agents.harness.common.rails.permissions.project_policy import (
    SHELL_NETWORK_RULE_ID,
    ask_rule_can_loosen,
    command_segments,
    detect_network_command,
    load_project_policy,
    normalize_deny_path,
    normalize_project_policy,
    path_within_roots,
    resolve_shell_network_policy,
    with_shell_network_rule,
)


class _ForwardToCaplog(logging.Handler):
    """Forward ``jiuwenswarm`` records to caplog's current handler (see below)."""

    def __init__(self, caplog: pytest.LogCaptureFixture, source: logging.Logger) -> None:
        super().__init__()
        self._caplog = caplog
        self._source = source

    def emit(self, record: logging.LogRecord) -> None:
        target = self._caplog.handler
        # Root already delivers, or pytest >= 9.1 attached caplog here itself.
        if self._source.propagate or target in self._source.handlers:
            return
        if record.levelno >= target.level:
            target.handle(record)


@pytest.fixture(autouse=True)
def _capture_jiuwenswarm_logs(caplog):
    """Let caplog see the ``jiuwenswarm`` logger on every pytest version.

    ``setup_logger()`` sets ``propagate = False`` on ``jiuwenswarm``. pytest 9.1
    attaches caplog to non-propagating loggers, but the uv.lock pin (9.0) only
    listens on root, so the log assertions here saw nothing (and the ``not in
    caplog.text`` ones passed vacuously). caplog swaps its handler per test
    phase, so the forwarder looks the current one up when a record arrives.
    """
    source = logging.getLogger("jiuwenswarm")
    forwarder = _ForwardToCaplog(caplog, source)
    source.addHandler(forwarder)
    try:
        yield
    finally:
        source.removeHandler(forwarder)

ROOT = Path(__file__).resolve().parents[4]

NETWORK = [
    "curl -s https://example.org",
    "bash -c 'curl example.org'",
    'sh -c "wget -qO- example.org"',
    "FOO=1 /usr/bin/curl example.org",
    "echo $(wget -qO- example.org)",
    "cd /app && npm install",
    "ls; curl example.org",
    "sudo apt-get install jq",
    "pip install requests",
    "python -m pip install requests",
    "uv pip install requests",
    "git clone https://example.org/r.git",
    "git -C /app fetch origin",
    "git push origin main",
    "npx create-thing",
    "docker pull alpine",
    "scp file user@host:/tmp",
    "rsync -a build/ deploy@host:/srv/",
    "go get example.org/x",
]
LOCAL = [
    "pytest -q",
    "python -m pytest tests/test_curl.py",
    "npm test",
    "uv run pytest",
    "git status",
    "git diff HEAD",
    "git log --all --oneline",
    "grep -rn 'https://' src",
    "cat README.md | grep curl",
    "ls lib/curl",
    "echo pip install is disabled",
    "rsync -a build/ dist/",
]


@pytest.mark.parametrize("command", NETWORK)
def test_network_commands_are_detected(command: str) -> None:
    assert detect_network_command(command)


@pytest.mark.parametrize("command", LOCAL)
def test_local_commands_are_not_flagged(command: str) -> None:
    assert detect_network_command(command) is None


def _project(root: Path, data) -> Path:
    d = root / ".jiuwenswarm"
    d.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    (d / "permissions.yaml").write_text(text, encoding="utf-8")
    pp._CACHE.clear()
    return root


# --------------------------------------------------------------------------
# Schema: deny-only
# --------------------------------------------------------------------------
def test_project_file_is_deny_only(tmp_path: Path, caplog) -> None:
    root = _project(tmp_path.resolve(), {"permissions": {
        "deny_paths": ["secrets", {"path": "build", "axes": ["write"]}],
        "deny_read": [".env*"],
        "deny_exec": ["scripts/danger.sh"],
        "deny_commands": ["rm -rf *", "re:^docker\\b"],
        "shell_network": "deny",
        "writable_roots": ["src"],
        # everything below is ignored with a warning
        "tools": {"bash": "allow", "write_file": "ask"},
        "rules": [{"pattern": "ls *", "action": "allow"}],
        "file_guard": {"paths": [{"path": "tmp", "read": "allow"}]},
        "net_guard": {"urls": {"*.example.org": "deny"}},
        "allow_loosen": True,
    }})
    pol = load_project_policy(root)
    assert [(d.raw, sorted(d.axes)) for d in pol.deny_paths] == [
        ("secrets", ["exec", "read", "write"]), ("build", ["write"]), (".env*", ["read"]),
        ("scripts/danger.sh", ["exec"])]
    assert pol.deny_commands == ["rm -rf *", "re:^docker\\b"]
    assert pol.shell_network == "deny"
    assert pol.writable_roots == [str(root / "src")]
    assert {i.split(":")[0] for i in pol.ignored} == {
        "tools", "rules", "file_guard", "net_guard", "allow_loosen"}
    assert "project files are deny-only" in caplog.text


@pytest.mark.parametrize("value", ["allow", "ask", True])
def test_shell_network_other_than_deny_is_ignored(tmp_path: Path, value) -> None:
    pol = normalize_project_policy({"shell_network": value}, project_root=tmp_path)
    assert pol.shell_network is None and pol.ignored and pol.error is None


@pytest.mark.parametrize("value", ["maybe", 42, ["deny"]])
def test_shell_network_garbage_fails_closed(tmp_path: Path, value) -> None:
    pol = normalize_project_policy({"shell_network": value}, project_root=tmp_path)
    assert pol.error and "shell_network" in pol.error


def test_shell_network_false_means_deny(tmp_path: Path) -> None:
    assert normalize_project_policy({"shell_network": False},
                                    project_root=tmp_path).shell_network == "deny"


@pytest.mark.parametrize("entry", [
    {"path": "x", "axes": ["read", "allow"]}, {"path": "x", "axes": []},
    {"path": "x", "read": "deny"}, {"path": "x", "action": "deny"}, {"axes": ["read"]},
    42, "", "src/*/../x",
])
def test_malformed_deny_entries_fail_closed(tmp_path: Path, entry) -> None:
    # A malformed entry would silently drop a deny: the whole file is unusable.
    pol = normalize_project_policy({"deny_paths": [entry, "kept"]}, project_root=tmp_path)
    assert pol.error and "deny_paths" in pol.error


def test_bad_regex_in_deny_commands_fails_closed(tmp_path: Path) -> None:
    pol = normalize_project_policy({"deny_commands": ["re:(", "make deploy"]},
                                   project_root=tmp_path)
    assert pol.error and "bad regex" in pol.error


# --------------------------------------------------------------------------
# Path normalisation (pathlib; the old ``lstrip("./")`` turned ``.env*`` into ``env*``)
# --------------------------------------------------------------------------
def test_relative_globs_keep_their_dots(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "proj"
    root.mkdir()
    assert normalize_deny_path(".env*", root) == (f"{root.as_posix()}/.env*", True)
    assert normalize_deny_path("./.env*", root) == (f"{root.as_posix()}/.env*", True)
    assert normalize_deny_path("../**", root) == (f"{root.parent.as_posix()}/**", True)
    assert normalize_deny_path("./src/../secret/**", root) == (f"{root.as_posix()}/secret/**", True)
    assert normalize_deny_path("secret/", root) == ((root / "secret").as_posix(), False)
    assert normalize_deny_path("**/*.pem", root) == (f"{root.as_posix()}/**/*.pem", True)
    with pytest.raises(ValueError):
        normalize_deny_path("*/../x", root)


def test_dot_env_glob_matches_dotfiles_only(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    pol = normalize_project_policy({"deny_read": [".env*"]}, project_root=root)
    assert pol.path_denied(root / ".env", "read")
    assert pol.path_denied(root / ".env.local", "read")
    assert pol.path_denied(root / ".env.d" / "x", "read")  # below a denied directory
    assert pol.path_denied(root / "env.py", "read") is None
    assert pol.path_denied(root / "src" / ".env", "read") is None
    assert pol.path_denied(root / ".env", "write") is None


def test_parent_glob_covers_the_parent_tree(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "proj"
    root.mkdir()
    pol = normalize_project_policy({"deny_write": ["../**"]}, project_root=root)
    assert pol.path_denied(tmp_path.resolve() / "sibling" / "x", "write")
    assert pol.path_denied(root / "src" / "a.py", "write")
    assert pol.path_denied(Path("/usr/local/x"), "write") is None


def test_prefix_and_symlinked_anchor(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "real" / "keys").mkdir(parents=True)
    (root / "alias").symlink_to(root / "real")
    pol = normalize_project_policy({"deny_paths": ["alias/keys", "/etc/**"]}, project_root=root)
    assert pol.path_denied(root / "real" / "keys" / "k.pem", "read")
    assert pol.path_denied(root / "real" / "keysX", "read") is None
    assert pol.path_denied(Path("/etc").resolve() / "hosts", "write")


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------
def test_loader_caches_by_mtime_and_size(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path.resolve(), {"deny_paths": ["secret"]})
    first = load_project_policy(root)
    import yaml as yaml_mod

    def boom(*_a, **_k):
        raise AssertionError("re-parsed an unchanged policy")

    monkeypatch.setattr(yaml_mod, "safe_load", boom)
    assert load_project_policy(root) is first
    monkeypatch.undo()
    _project(root, {"deny_paths": ["secret", "other"]})
    assert [d.raw for d in load_project_policy(root).deny_paths] == ["secret", "other"]


@pytest.mark.parametrize("text", ["deny_paths: [unclosed", "- a\n- b\n", "!!python/object:os.system x"])
def test_unusable_project_file_is_flagged(tmp_path: Path, text: str) -> None:
    pol = load_project_policy(_project(tmp_path.resolve(), text))
    # valid YAML with the wrong shape fails closed like unparseable YAML
    assert pol.error and "\n" not in pol.error


def test_disabled_or_missing_project_file(tmp_path: Path) -> None:
    root = _project(tmp_path.resolve(), {"shell_network": "deny"})
    assert load_project_policy(root, config={"project_policy": {"enabled": False}}).empty
    assert load_project_policy(tmp_path.resolve() / "nowhere").empty


def test_shell_network_resolution(tmp_path: Path) -> None:
    project_deny = normalize_project_policy({"shell_network": "deny"}, project_root=tmp_path)
    assert resolve_shell_network_policy({}) == "allow"
    assert resolve_shell_network_policy({}, headless=True) == "deny"
    assert resolve_shell_network_policy({"headless": {"shell_network": "ask"}}, headless=True) == "ask"
    assert resolve_shell_network_policy({}, project=project_deny) == "deny"
    assert resolve_shell_network_policy({"shell_network": "ask"}) == "ask"


# --------------------------------------------------------------------------
# Engine: the shell-network rule comes from user/global config only
# --------------------------------------------------------------------------
@pytest.mark.parametrize("level", ["deny", "ask"])
@pytest.mark.parametrize("shell_cats", [None, ["bash"], ["bash", "powershell", "mcp_exec_command"]])
def test_shell_network_rule_is_applied_by_the_engine(level: str, shell_cats) -> None:
    base = {"enabled": True, "schema": "tiered_policy", "defaults": {"*": "allow"},
            "tools": {"bash": "allow"}, "rules": []}
    if shell_cats is not None:
        base["categories"] = {"shell": shell_cats}
    cfg = with_shell_network_rule(base, {"shell_network": level})
    assert base["rules"] == []  # input not mutated
    for command in ("bash -c 'curl example.org'", "cd x && pip install requests",
                    "git -C /app fetch origin", "CURL example.org"):
        got, rule = evaluate_tiered_policy(cfg, "bash", {"command": command})
        assert got.value == level, (command, got, rule)
        assert SHELL_NETWORK_RULE_ID in rule
    got, _ = evaluate_tiered_policy(cfg, "bash", {"command": "python -m pytest -q"})
    assert got.value == "allow"


def test_allow_adds_nothing_and_rule_is_not_duplicated() -> None:
    base = {"enabled": True, "rules": []}
    assert with_shell_network_rule(base, {}) is base
    once = with_shell_network_rule(base, {"shell_network": "deny"})
    twice = with_shell_network_rule(once, {"shell_network": "deny"})
    assert [r["id"] for r in twice["rules"]] == [SHELL_NETWORK_RULE_ID]


@pytest.mark.parametrize("tools,defaults,loosens", [
    ({}, {"*": "deny"}, True),                        # B1: default deny, no tool level
    ({"bash": {"mode": "x"}}, {"*": "deny"}, True),   # dict form without "*": no baseline
    ({"bash": {"*": "allow"}, "powershell": "ask", "mcp_exec_command": "allow",
      "cmd": "allow"}, {"*": "deny"}, False),
    ({}, {"*": "allow"}, False),
    ({}, {}, False),                                   # engine fallback is ask
])
def test_ask_rule_is_only_added_when_it_cannot_loosen(tools, defaults, loosens) -> None:
    cfg = {"enabled": True, "tools": tools, "defaults": defaults, "rules": [],
           "categories": {"shell": ["bash", "powershell", "mcp_exec_command", "cmd"]}}
    assert ask_rule_can_loosen(cfg) is loosens
    out = with_shell_network_rule(cfg, {"shell_network": "ask"})
    assert (out is cfg) is loosens
    for command in ("curl example.org", "ls"):
        before, _ = evaluate_tiered_policy(cfg, "bash", {"command": command})
        after, _ = evaluate_tiered_policy(out, "bash", {"command": command})
        assert pp._STRICTNESS[after.value] >= pp._STRICTNESS[before.value], (command, before, after)


def test_deny_rule_never_loosens() -> None:
    for cfg in ({"defaults": {"*": "deny"}}, {"tools": {"bash": {"*": "deny"}}},
                {"defaults": {"*": "allow"}, "rules": [{"id": "u", "tools": ["bash"],
                                                        "pattern": "curl *", "action": "allow"}]}):
        out = with_shell_network_rule({"enabled": True, **cfg}, {"shell_network": "deny"})
        for command in ("curl example.org", "ls", "pip install x"):
            before, _ = evaluate_tiered_policy({"enabled": True, **cfg}, "bash", {"command": command})
            after, _ = evaluate_tiered_policy(out, "bash", {"command": command})
            assert pp._STRICTNESS[after.value] >= pp._STRICTNESS[before.value]


def test_command_segments() -> None:
    assert command_segments("sudo -E rm -rf /tmp/x") == ["rm -rf /tmp/x"]
    assert "rm -rf x" in command_segments("bash -c 'rm -rf x'")
    assert command_segments("FOO=1 /bin/rm -f a && ls") == ["rm -f a", "ls"]
    assert "docker ps" in command_segments("echo $(docker ps)")


# --------------------------------------------------------------------------
# Rail basics
# --------------------------------------------------------------------------
def test_writable_roots_resolve_symlinks_and_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    roots = [str(root / "src")]
    assert path_within_roots(str(root / "src" / "a.py"), roots)
    assert path_within_roots("src/a.py", roots, base=str(root))
    assert not path_within_roots("src/../../outside/x", roots, base=str(root))
    assert not path_within_roots(str(root / "link" / "x"), [str(root)])


def _ctx(tool_name: str, args: dict) -> SimpleNamespace:
    tool_call = SimpleNamespace(id="call-1", name=tool_name, arguments=args)
    return SimpleNamespace(
        inputs=ToolCallInputs(tool_call=tool_call, tool_name=tool_name, tool_args=args,
                              tool_result=None),
        extra={},
    )


def _policy(ctx: SimpleNamespace) -> str | None:
    if not ctx.extra.get("_skip_tool_calls", {}).get("call-1"):
        return None
    return json.loads(ctx.inputs.tool_msg.content)["policy"]


def _use_root(monkeypatch, root) -> None:
    value = str(root) if root else None
    monkeypatch.setattr(pp, "current_project_root", lambda: value)
    monkeypatch.setattr(guard_mod, "current_project_root", lambda: value)
    pp._CACHE.clear()


@pytest.fixture
def no_project(monkeypatch):
    _use_root(monkeypatch, None)


async def test_headless_guard_refuses_network_commands(no_project) -> None:
    rail = ShellPolicyGuardRail(permissions={}, headless=True)
    ctx = _ctx("bash", {"command": "FOO=1 /usr/bin/curl example.org"})
    await rail.before_tool_call(ctx)
    payload = json.loads(ctx.inputs.tool_msg.content)
    assert payload["policy"] == "shell_network" and payload["command_class"] == "curl"
    local = _ctx("bash", {"command": "python -m pytest -q"})
    await rail.before_tool_call(local)
    assert _policy(local) is None


async def test_interactive_default_is_unchanged(no_project) -> None:
    ctx = _ctx("bash", {"command": "pip install requests"})
    await ShellPolicyGuardRail(permissions={}, headless=False).before_tool_call(ctx)
    assert _policy(ctx) is None


@pytest.mark.parametrize("engine", [False, True])
async def test_user_deny_is_enforced_by_the_rail_in_every_mode(no_project, engine) -> None:
    for headless in (False, True):
        ctx = _ctx("bash", {"command": "curl example.org"})
        await ShellPolicyGuardRail(permissions={"enabled": engine, "shell_network": "deny"},
                                   headless=headless).before_tool_call(ctx)
        assert _policy(ctx) == "shell_network"


@pytest.mark.parametrize("engine,headless,refused", [
    (False, False, False),  # no approval channel: not enforced (logged once)
    (True, False, False),   # the engine's ask rule prompts
    (False, True, True), (True, True, True),  # nobody to answer
])
async def test_user_ask(no_project, engine, headless, refused) -> None:
    ctx = _ctx("bash", {"command": "curl example.org"})
    await ShellPolicyGuardRail(permissions={"enabled": engine, "shell_network": "ask"},
                               headless=headless).before_tool_call(ctx)
    assert (_policy(ctx) == "shell_network") is refused


@pytest.mark.parametrize("engine", [False, True])
async def test_project_file_denies_network_in_every_mode(tmp_path, monkeypatch, engine) -> None:
    root = _project(tmp_path.resolve(), {"shell_network": "deny"})
    _use_root(monkeypatch, root)
    ctx = _ctx("bash", {"command": "git fetch origin"})
    await ShellPolicyGuardRail(permissions={"enabled": engine}, headless=False).before_tool_call(ctx)
    assert _policy(ctx) == "shell_network"
    assert "the project's .jiuwenswarm/permissions.yaml" in json.loads(
        ctx.inputs.tool_msg.content)["detail"]


async def test_writable_roots_guard_file_tools(tmp_path, monkeypatch) -> None:
    root = _project(tmp_path.resolve(), {"writable_roots": ["src"]})
    (root / "src").mkdir()
    _use_root(monkeypatch, root)
    rail = ShellPolicyGuardRail(permissions={}, headless=False)
    inside = _ctx("write_file", {"file_path": "src/a.py", "content": "x"})
    await rail.before_tool_call(inside)
    assert _policy(inside) is None
    outside = _ctx("edit_file", {"file_path": os.path.join(str(tmp_path), "..", "etc", "x")})
    await rail.before_tool_call(outside)
    assert _policy(outside) == "writable_roots"


async def test_unusable_project_file_refuses_file_and_shell_tools(tmp_path, monkeypatch) -> None:
    root = _project(tmp_path.resolve(), "deny_paths: [unclosed")
    _use_root(monkeypatch, root)
    rail = ShellPolicyGuardRail(permissions={}, headless=False)
    for tool, args in (("read_file", {"file_path": "a"}), ("bash", {"command": "ls"})):
        ctx = _ctx(tool, args)
        await rail.before_tool_call(ctx)
        assert _policy(ctx) == "project_policy_invalid"
    other = _ctx("todo_write", {"todos": []})
    await rail.before_tool_call(other)
    assert _policy(other) is None


async def test_guard_never_raises(no_project) -> None:
    rail = ShellPolicyGuardRail(permissions={}, headless=True)
    await rail.before_tool_call(SimpleNamespace(inputs=None, extra={}))
    await rail.before_tool_call(_ctx("bash", {"command": None}))


def test_config_templates_carry_the_keys() -> None:
    for name in ("config.yaml", "config.team.distributed.leader.yaml",
                 "config.team.distributed.teammate.yaml"):
        perms = yaml.safe_load((ROOT / "jiuwenswarm" / "resources" / name).read_text())["permissions"]
        assert perms["shell_network"] == "allow", name
        assert perms["headless"]["shell_network"] == "deny", name
        assert perms["project_policy"] == {"enabled": True}, name


def test_code_adapter_mounts_the_guard_headless_only_for_process_cli() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter.__new__(JiuwenSwarmCodeAdapter)
    adapter._config_base_cache = {"permissions": {}}
    adapter._channel_id = "process_cli"
    assert adapter._build_shell_policy_guard_rail()._headless is True
    adapter._channel_id = "web"
    assert adapter._build_shell_policy_guard_rail()._headless is False
    assert "ShellPolicyGuardRail" in JiuwenSwarmCodeAdapter._FIXED_RAIL_NAMES
