from pathlib import Path

from openjiuwen.harness.security.permission_engine.models import PermissionLevel
from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
    evaluate_tiered_policy,
)

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    apply_permission_trusted_dirs,
    build_permission_rail,
    build_trusted_dirs_permission_config,
)
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist


def _base_permissions() -> dict:
    return {
        "enabled": True,
        "schema": "tiered_policy",
        "defaults": {"*": "allow"},
        "tools": {
            "bash": "allow",
            "powershell": "ask",
            "mcp_exec_command": "ask",
            "create_terminal": "ask",
        },
        "file_guard": {
            "enabled": True,
            "defaults": {"read": "allow", "write": "allow", "exec": "ask"},
            "paths": [],
        },
    }


class _FakePermissionRail:
    def __init__(self, config):
        self._engine = type("Engine", (), {"config": config})()
        self.trusted_dirs = None

    def update_config(self, config):
        self._engine.config = config

    def set_trusted_dirs(self, trusted_dirs):
        self.trusted_dirs = trusted_dirs


def test_apply_permission_trusted_dirs_replaces_stale_runtime_entries(tmp_path):
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    project_dir = tmp_path / "project"
    rail = _FakePermissionRail(
        build_trusted_dirs_permission_config(_base_permissions(), [str(old_dir)])
    )

    merged = apply_permission_trusted_dirs(
        rail, [str(new_dir)], project_dir=str(project_dir)
    )

    expected = [
        str(new_dir.resolve()).replace("\\", "/"),
        str(project_dir.resolve()).replace("\\", "/"),
    ]
    assert merged == expected
    assert rail.trusted_dirs == expected
    assert [
        p["path"] for p in rail._engine.config["file_guard"]["paths"][:2]
    ] == expected
    assert len(
        [
            rule
            for rule in rail._engine.config["rules"]
            if rule["id"].startswith("jiuwenswarm_runtime_trusted_script_")
        ]
    ) == 2
    old_path = str(old_dir.resolve()).replace("\\", "/")
    assert all(
        old_path not in str(item)
        for item in rail._engine.config["file_guard"]["paths"]
    )


def test_apply_permission_trusted_dirs_clears_stale_runtime_entries(tmp_path):
    old_dir = tmp_path / "old"
    rail = _FakePermissionRail(
        build_trusted_dirs_permission_config(_base_permissions(), [str(old_dir)])
    )

    merged = apply_permission_trusted_dirs(rail, [], project_dir=None)

    assert merged == []
    assert rail.trusted_dirs == []
    assert all(
        not entry.get("_jiuwenswarm_runtime_trusted_dir")
        for entry in rail._engine.config["file_guard"]["paths"]
    )
    assert all(
        not rule.get("_jiuwenswarm_runtime_trusted_dir")
        for rule in rail._engine.config["rules"]
    )


def test_permission_snapshot_reapplies_runtime_project_dir(tmp_path, monkeypatch):
    base_permissions = _base_permissions()
    monkeypatch.setattr(
        permissions_persist,
        "get_permissions_with_session_overlay",
        lambda session_id=None: base_permissions,
    )
    rail = build_permission_rail({"permissions": _base_permissions()})
    assert rail is not None
    project_dir = tmp_path / "project"
    expected = str(project_dir.resolve()).replace("\\", "/")

    apply_permission_trusted_dirs(rail, [], project_dir=str(project_dir))
    refreshed = rail._host.get_permissions_snapshot(session_id="test-session")

    project_rule = next(
        entry
        for entry in refreshed["file_guard"]["paths"]
        if entry.get("path") == expected
    )
    assert project_rule["exec"] == "allow"


def test_session_overlay_does_not_persist_request_scoped_runtime_path(tmp_path):
    runtime_dir = str((tmp_path / "project").resolve()).replace("\\", "/")
    merged = build_trusted_dirs_permission_config(
        _base_permissions(), [runtime_dir]
    )

    delta = permissions_persist.extract_session_overlay_delta(
        _base_permissions(), merged
    )

    assert delta == {}


def test_persisted_trusted_dir_allows_file_execution(tmp_path, monkeypatch):
    stored = {"permissions": {"file_guard": {"enabled": True, "paths": []}}}
    monkeypatch.setattr(
        permissions_persist,
        "_load_config_yaml_round_trip",
        lambda: (stored, tmp_path / "config.yaml"),
    )
    monkeypatch.setattr(
        permissions_persist, "_dump_config_yaml_round_trip", lambda *_: None
    )

    result = permissions_persist.persist_cli_trusted_directory(
        str(tmp_path / "trusted")
    )

    assert result["ok"] is True
    assert stored["permissions"]["file_guard"]["paths"][0]["exec"] == "allow"


def test_persisted_shell_override_is_limited_to_direct_scripts(tmp_path, monkeypatch):
    stored = {
        "permissions": {
            "schema": "tiered_policy",
            "tools": {"powershell": "ask"},
            "defaults": {"*": "allow"},
            "file_guard": {"enabled": True, "paths": []},
            "approval_overrides": [],
        }
    }
    monkeypatch.setattr(
        permissions_persist,
        "_load_config_yaml_round_trip",
        lambda: (stored, tmp_path / "config.yaml"),
    )
    monkeypatch.setattr(
        permissions_persist, "_dump_config_yaml_round_trip", lambda *_: None
    )

    trusted_dir = tmp_path / "trusted"
    result = permissions_persist.persist_cli_trusted_directory_with_overrides(
        str(trusted_dir)
    )

    assert result["ok"] is True
    override = stored["permissions"]["approval_overrides"][0]
    assert "powershell" in override["tools"]
    assert "interpreter_sink" not in override["pattern"]
    allowed, _ = evaluate_tiered_policy(
        stored["permissions"],
        "powershell",
        {"command": f"powershell -File {trusted_dir / 'build.ps1'}"},
    )
    assert allowed == PermissionLevel.ALLOW
    unsafe, _ = evaluate_tiered_policy(
        stored["permissions"],
        "powershell",
        {"command": f"powershell -Command Remove-Item {trusted_dir / 'a.txt'}"},
    )
    assert unsafe == PermissionLevel.ASK


def test_trusted_dirs_get_file_exec_allow_and_script_command_allow(tmp_path):
    trusted_dir = (tmp_path / "trusted").resolve()
    trusted_dir.mkdir()
    project_dir = (tmp_path / "project").resolve()
    project_dir.mkdir()
    other_dir = (tmp_path / "other").resolve()
    other_dir.mkdir()

    permissions = build_trusted_dirs_permission_config(
        _base_permissions(), [str(trusted_dir)], project_dir=str(project_dir)
    )

    trusted_path = str(trusted_dir).replace("\\", "/")
    project_path = str(project_dir).replace("\\", "/")
    path_rules = permissions["file_guard"]["paths"]
    trusted_rule = next(rule for rule in path_rules if rule["path"] == trusted_path)
    assert trusted_rule["read"] == "allow"
    assert trusted_rule["write"] == "allow"
    assert trusted_rule["exec"] == "allow"
    project_rule = next(rule for rule in path_rules if rule["path"] == project_path)
    assert project_rule["read"] == "allow"
    assert project_rule["write"] == "allow"
    assert project_rule["exec"] == "allow"

    allow_rules = permissions["rules"]
    runtime_rules = [
        rule
        for rule in allow_rules
        if rule["id"].startswith("jiuwenswarm_runtime_trusted_script_")
    ]
    assert len(runtime_rules) == 2
    trusted_rule = runtime_rules[0]
    assert "powershell" in trusted_rule["tools"]
    assert trusted_rule["action"] == "allow"

    allowed, _ = evaluate_tiered_policy(
        permissions,
        "powershell",
        {"command": f"powershell -File {trusted_dir / 'build.ps1'}"},
    )
    assert allowed == PermissionLevel.ALLOW

    project_script, _ = evaluate_tiered_policy(
        permissions,
        "powershell",
        {"command": f"powershell -File {project_dir / 'build.ps1'}"},
    )
    assert project_script == PermissionLevel.ALLOW

    outside, _ = evaluate_tiered_policy(
        permissions,
        "powershell",
        {"command": f"powershell -File {other_dir / 'build.ps1'}"},
    )
    assert outside == PermissionLevel.ASK


def test_trusted_dir_script_allow_uses_agent_core_interpreter_set(tmp_path):
    trusted_dir = (tmp_path / "trusted").resolve()
    trusted_dir.mkdir()
    permissions = build_trusted_dirs_permission_config(
        _base_permissions(), [str(trusted_dir)]
    )

    allowed, _ = evaluate_tiered_policy(
        permissions,
        "powershell",
        {"command": f"python3 {trusted_dir / 'build.py'}"},
    )
    assert allowed == PermissionLevel.ALLOW


def test_trusted_dir_script_allow_does_not_allow_interpreter_sink_command(tmp_path):
    trusted_dir = (tmp_path / "trusted").resolve()
    trusted_dir.mkdir()
    permissions = build_trusted_dirs_permission_config(
        _base_permissions(), [str(trusted_dir)]
    )

    result, matched_rule = evaluate_tiered_policy(
        permissions,
        "powershell",
        {
            "command": "echo a | python -c pass"
        },
    )
    assert result == PermissionLevel.ASK
    assert "interpreter_sink" in matched_rule


def test_unknown_structure_shell_guard_can_be_disabled_without_disabling_sink_guard():
    permissions = _base_permissions()
    permissions["shell_guard"] = {
        "unknown_structure": False,
        "interpreter_sink": True,
    }

    complex_command, _ = evaluate_tiered_policy(
        permissions,
        "bash",
        {"command": "echo a | while read x; do echo $x; done"},
    )
    assert complex_command == PermissionLevel.ALLOW


def test_product_configs_disable_shell_guard_interception():
    import yaml

    repo_root = Path(__file__).resolve().parents[4]
    config_paths = [
        repo_root / "jiuwenswarm/resources/config.yaml",
        repo_root / "jiuwenswarm/resources/config.team.distributed.leader.yaml",
        repo_root / "jiuwenswarm/resources/config.team.distributed.teammate.yaml",
        repo_root / "deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml",
    ]

    for config_path in config_paths:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        shell_guard = data["permissions"]["shell_guard"]
        assert shell_guard["unknown_structure"] is False
        assert shell_guard["interpreter_sink"] is False
        assert set(shell_guard) <= {
            "unknown_structure",
            "interpreter_sink",
            "builtin_rules_enabled",
        }
        if "builtin_rules_enabled" in shell_guard:
            assert shell_guard["builtin_rules_enabled"] is True
