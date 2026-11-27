"""The path guard does not ask about the agent's own skill files or a redirect to /dev/null."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.harness.security.permission_engine.models import PermissionLevel

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

SHELL = ["bash", "powershell", "mcp_exec_command", "create_terminal"]


def _permissions() -> dict:
    """The permissions Swarm ships (resources/config.yaml), switched on in manual mode."""
    return {
        "enabled": True,
        "mode": "manual",
        "defaults": {"*": "allow"},
        "tools": {"bash": "allow", "write_file": "allow"},
        "rules": [{"id": "shell_allow_ls", "tools": SHELL, "pattern": "ls *", "severity": "LOW"}],
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "workspace": {"read": "allow", "write": "allow", "exec": "ask"},
            "paths": [
                {"path": "**/.ssh/**", "match": "glob", "read": "ask", "write": "ask", "exec": "ask"},
                {"path": "**/.env*", "match": "glob", "read": "ask", "write": "ask", "exec": "ask"},
            ],
        },
        "external_directory": {"*": "ask"},
    }


@pytest.fixture
def guard(tmp_path):
    work = tmp_path / "work"
    skill = tmp_path / "agent" / "skills" / "workswarm-builtin"
    guide = skill / "references" / "capabilities" / "xlsx" / "references" / "create.md"
    config = tmp_path / "config" / "config.yaml"
    for path in (guide, config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    work.mkdir()
    (skill / ".env.local").write_text("x\n")
    (skill / "linked.yaml").symlink_to(config)
    rail = build_permission_rail({"permissions": _permissions()}, session_id="guard", workspace_root=work)
    return SimpleNamespace(rail=rail, work=work, skills=skill.parent, skill=skill, guide=guide, config=config,
                           tmp=tmp_path)


async def _level(h, tool: str, args: dict) -> PermissionLevel:
    return (await h.rail._engine.check_permission(tool, args)).permission  # pylint: disable=protected-access


async def test_read_under_listed_skill_folder_is_not_asked(guard):
    assert await _level(guard, "read_file", {"file_path": str(guard.guide)}) == PermissionLevel.ASK

    guard.rail.set_skill_read_roots(lambda: [str(guard.skills)])

    assert await _level(guard, "read_file", {"file_path": str(guard.guide)}) == PermissionLevel.ALLOW
    assert await _level(guard, "bash", {"command": f"cat {guard.guide}"}) == PermissionLevel.ALLOW


async def test_redirect_to_dev_null_is_not_asked(guard):
    assert await _level(guard, "bash", {"command": "ls 2>/dev/null"}) == PermissionLevel.ALLOW
    assert await _level(guard, "bash", {"command": "ls >/dev/null 2>&1"}) == PermissionLevel.ALLOW


async def test_every_other_question_is_kept(guard):
    guard.rail.set_skill_read_roots(lambda: [str(guard.skills)])
    asked = [
        ("write_file", {"file_path": str(guard.skill / "new.md"), "content": "x"}),
        ("bash", {"command": f"python3 {guard.skill}/scripts/run.py"}),
        ("read_file", {"file_path": str(guard.config)}),
        ("read_file", {"file_path": f"{guard.skill}/../../../config/config.yaml"}),
        ("read_file", {"file_path": str(guard.skill / "linked.yaml")}),
        ("bash", {"command": f"ls > {guard.tmp / 'out.txt'}"}),
        ("bash", {"command": f"cat {guard.guide} {guard.config}"}),
        ("bash", {"command": "cat /dev/null"}),
    ]
    for tool, args in asked:
        assert await _level(guard, tool, args) == PermissionLevel.ASK, (tool, args)
    assert await _level(guard, "read_file", {"file_path": str(guard.skill / ".env.local")}) != PermissionLevel.ALLOW


def _with_rules(defaults=None, paths=()) -> dict:
    permissions = _permissions()
    if defaults:
        permissions["file_guard"]["defaults"].update(defaults)
    permissions["file_guard"]["paths"].extend(paths)
    return permissions


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        pytest.param({"paths": [{"path": "{skill}", "match": "prefix", "read": "ask"}]},
                     PermissionLevel.ASK, id="prefix-ask-on-the-skill-folder"),
        pytest.param({"paths": [{"path": "**/capabilities/**", "match": "glob", "read": "ask"}]},
                     PermissionLevel.ASK, id="glob-ask-on-the-skill-file"),
        pytest.param({"paths": [{"path": "{skill}", "match": "prefix", "read": "deny"}]},
                     PermissionLevel.DENY, id="prefix-deny-on-the-skill-folder"),
        pytest.param({"paths": [{"path": "**/capabilities/**", "match": "glob", "read": "deny"}]},
                     PermissionLevel.DENY, id="glob-deny-on-the-skill-file"),
        pytest.param({"defaults": {"read": "deny"}}, PermissionLevel.DENY, id="default-deny"),
    ],
)
async def test_an_explicit_rule_or_a_deny_on_a_skill_file_is_kept(guard, rules, expected):
    """The exemption only lifts the question that file_guard.defaults asks; it changes no other rule."""
    paths = [{**rule, "path": rule["path"].format(skill=guard.skill)} for rule in rules.get("paths", ())]
    guard.rail.set_skill_read_roots(lambda: [str(guard.skills)])
    guard.rail.update_config(_with_rules(rules.get("defaults"), paths))

    assert await _level(guard, "read_file", {"file_path": str(guard.guide)}) == expected
    assert await _level(guard, "bash", {"command": f"cat {guard.guide}"}) == expected


async def test_a_deny_on_dev_null_is_kept(guard):
    """A redirect to /dev/null is exempt only from the default question; a rule that denies it is kept."""
    guard.rail.update_config(_with_rules(paths=[{"path": "/dev/null", "match": "prefix", "write": "deny"}]))

    assert await _level(guard, "bash", {"command": "ls 2>/dev/null"}) == PermissionLevel.DENY


async def test_exemption_survives_a_config_update(guard):
    guard.rail.set_skill_read_roots(lambda: [str(guard.skills)])
    guard.rail.update_config(_permissions())

    assert await _level(guard, "read_file", {"file_path": str(guard.guide)}) == PermissionLevel.ALLOW


async def test_roots_are_read_when_the_guard_asks(guard):
    roots: list[str] = []
    guard.rail.set_skill_read_roots(lambda: roots)
    assert await _level(guard, "read_file", {"file_path": str(guard.guide)}) == PermissionLevel.ASK

    roots.append(str(guard.skills))

    assert await _level(guard, "read_file", {"file_path": str(guard.guide)}) == PermissionLevel.ALLOW


def test_adapter_lists_the_skill_rail_folders():
    adapter = SimpleNamespace(_skill_rail=SimpleNamespace(skills_dir=["/a/skills", Path("/b/skills")]))
    assert JiuWenSwarmDeepAdapter._skill_read_roots(adapter) == ["/a/skills", "/b/skills"]
    assert JiuWenSwarmDeepAdapter._skill_read_roots(SimpleNamespace(_skill_rail=None)) == []

    bound = []
    rail = SimpleNamespace(set_skill_read_roots=bound.append)
    adapter = SimpleNamespace(_permission_rail=rail, _skill_read_roots=lambda: [])
    JiuWenSwarmDeepAdapter._bind_skill_read_roots(adapter)
    assert bound == [adapter._skill_read_roots]
