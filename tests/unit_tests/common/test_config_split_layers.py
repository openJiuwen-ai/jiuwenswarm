# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""升级按模板哈希戳 copy2；白名单键写回 config.yaml；遗留 overlay 折进 yaml。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import (
    get_builtin_rules_file,
    get_package_config_file,
    get_package_resources_dir,
)


def test_package_resources_resolve_to_repo_templates() -> None:
    res = get_package_resources_dir()
    assert res is not None
    assert (res / "config.yaml").is_file()
    assert get_package_config_file() == res / "config.yaml"
    assert get_builtin_rules_file().name == "builtin_rules.yaml"


def test_get_config_reads_yaml_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "execution_guard": {"llm_retry_rail": {"enabled": True}},
                "sandbox": {"enabled": True, "policy_file": "windows-policy.yaml"},
                "permissions": {
                    "enabled": True,
                    "permission_mode": "normal",
                    "approval_overrides": [{"id": "curl_post", "action": "allow"}],
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (user_dir / "config.user.yaml").write_text(
        yaml.safe_dump(
            {
                "sandbox": {"enabled": False},
                "channels": {"xiaoyi": {"enabled": True}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_file", lambda: user_dir / "config.yaml"
    )

    cfg = get_config()
    assert cfg["sandbox"]["enabled"] is True
    assert cfg["sandbox"]["policy_file"] == "windows-policy.yaml"
    assert cfg["permissions"]["enabled"] is True
    assert cfg["permissions"]["approval_overrides"] == [
        {"id": "curl_post", "action": "allow"}
    ]
    assert cfg["execution_guard"]["llm_retry_rail"]["enabled"] is True
    assert "xiaoyi" not in (cfg.get("channels") or {})


def test_extract_allowlist_keeps_ui_knobs_not_auto_patches() -> None:
    from jiuwenswarm.common.config_split import extract_user_keep_from_legacy

    package = {
        "preferred_language": "zh",
        "logging": {"level": "INFO", "path": "app.log"},
        "auto_memory_enabled": True,
        "channels": {
            "xiaoyi": {"enabled": False, "ws_url1": "wss://old"},
        },
        "permissions": {
            "enabled": True,
            "permission_mode": "normal",
            "tools": {"bash": "allow"},
            "file_guard": {"defaults": {"read": "allow", "write": "allow"}},
            "rules": [{"id": "shell_allow_ls", "pattern": "ls *"}],
        },
        "sandbox": {"enabled": False},
        "mcp": {"servers": []},
    }
    user = {
        "preferred_language": "en",
        "auto_memory_enabled": False,
        "channels": {
            "xiaoyi": {"enabled": True, "ws_url1": "np://claw-relay"},
        },
        "permissions": {
            "enabled": True,
            "permission_mode": "strict",
            "tools": {"bash": "ask"},
            "file_guard": {
                "defaults": {"read": "ask", "write": "ask"},
                "paths": [{"path": "C:/docs", "write": "allow"}],
            },
            "approval_overrides": [
                {"id": "user_curl", "tools": ["bash"], "action": "allow"}
            ],
            "rules": [
                {"id": "shell_allow_ls", "pattern": "ls *"},
                {"id": "ui_rule_1", "pattern": "curl *"},
            ],
        },
        "sandbox": {"enabled": True},
        "mcp": {"servers": [{"name": "gausspd-memory"}]},
    }
    keep = extract_user_keep_from_legacy(user, package)
    assert "preferred_language" not in keep
    assert "auto_memory_enabled" not in keep
    assert "channels" not in keep
    assert "mcp" not in keep
    assert keep["sandbox"]["enabled"] is True
    assert keep["permissions"]["approval_overrides"] == [
        {"id": "user_curl", "tools": ["bash"], "action": "allow"}
    ]
    assert keep["permissions"]["file_guard"]["paths"] == [
        {"path": "C:/docs", "write": "allow"}
    ]
    assert "rules" not in keep["permissions"]
    assert "enabled" not in keep["permissions"]
    assert "permission_mode" not in keep["permissions"]
    assert "tools" not in keep["permissions"]


def test_copy_if_missing_or_changed(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import copy_if_missing_or_changed

    src = tmp_path / "src.yaml"
    dest = tmp_path / "dest.yaml"
    src.write_text("a: 1\n", encoding="utf-8")
    assert copy_if_missing_or_changed(src, dest) is True
    assert dest.read_text(encoding="utf-8") == "a: 1\n"
    assert copy_if_missing_or_changed(src, dest) is False
    src.write_text("a: 2\n", encoding="utf-8")
    assert copy_if_missing_or_changed(src, dest) is True
    assert dest.read_text(encoding="utf-8") == "a: 2\n"


def test_upgrade_restores_allowlist_onto_yaml_and_drops_overlay(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import sync_system_files_from_package

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    pkg.write_text(
        "sandbox:\n  enabled: false\nchannels:\n  xiaoyi:\n    enabled: false\n"
        "permissions:\n  file_guard:\n    defaults:\n      read: allow\n",
        encoding="utf-8",
    )
    user.write_text("sandbox:\n  enabled: false\nstale: true\n", encoding="utf-8")
    overlay.write_text(
        "sandbox:\n  enabled: true\nchannels:\n  xiaoyi:\n    enabled: true\n"
        "permissions:\n  approval_overrides:\n    - id: curl_post\n      action: allow\n",
        encoding="utf-8",
    )
    assert sync_system_files_from_package(
        user_yaml=user, overlay_yaml=overlay, package_yaml=pkg
    ) is True
    restored = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert restored["sandbox"]["enabled"] is True
    assert restored["permissions"]["approval_overrides"] == [
        {"id": "curl_post", "action": "allow"}
    ]
    assert restored["permissions"]["file_guard"]["defaults"]["read"] == "allow"
    assert "stale" not in restored
    assert restored["channels"]["xiaoyi"]["enabled"] is False
    assert not overlay.is_file()


def test_dump_writes_yaml_not_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    legacy = user_dir / "config.yaml"
    overlay = user_dir / "config.user.yaml"
    legacy.write_text("logging:\n  level: INFO\n", encoding="utf-8")
    overlay.write_text("sandbox:\n  enabled: false\n", encoding="utf-8")

    monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", legacy)

    loaded = load_yaml_round_trip(legacy)
    assert loaded["logging"]["level"] == "INFO"
    loaded["preferred_language"] = "en"
    dump_yaml_round_trip(legacy, loaded)
    assert yaml.safe_load(legacy.read_text(encoding="utf-8"))["preferred_language"] == "en"
    assert yaml.safe_load(overlay.read_text(encoding="utf-8"))["sandbox"]["enabled"] is False


def test_skip_system_file_sync_folds_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common import config_split as split

    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    pkg = tmp_path / "pkg.yaml"
    pkg.write_text("sandbox:\n  enabled: false\n", encoding="utf-8")
    user.write_text("stale: true\n", encoding="utf-8")
    overlay.write_text(
        "sandbox:\n  enabled: true\nauto_memory_enabled: false\n"
        "permissions:\n  approval_overrides:\n    - id: curl_post\n      action: allow\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(split, "get_config_file", lambda: user)
    monkeypatch.setattr(split, "get_user_overlay_file", lambda: overlay)
    monkeypatch.setattr(split, "get_package_config_file", lambda: pkg)
    monkeypatch.setenv("JIUWENSWARM_SKIP_SYSTEM_FILE_SYNC", "1")
    monkeypatch.setenv("JIUWENSWARM_ALLOW_OVERLAY_EXTRACT", "1")
    assert split.maybe_fold_legacy_overlay() is True
    data = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert data["stale"] is True
    assert data["sandbox"]["enabled"] is True
    assert data["permissions"]["approval_overrides"] == [
        {"id": "curl_post", "action": "allow"}
    ]
    assert "auto_memory_enabled" not in data
    assert not overlay.is_file()


def test_write_template_stamp_uses_lf_and_reads_crlf(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import (
        compute_template_stamp,
        read_template_stamp,
        write_template_stamp,
    )

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    pkg.write_text("sandbox:\n  enabled: false\n", encoding="utf-8")
    user.write_text("sandbox:\n  enabled: false\n", encoding="utf-8")
    write_template_stamp(user, pkg, None)
    stamp = user.with_name(".template.sha256")
    raw = stamp.read_bytes()
    assert b"\r\n" not in raw
    expected = compute_template_stamp(pkg, None)
    assert raw.decode("utf-8") == expected
    stamp.write_bytes(expected.replace("\n", "\r\n").encode("utf-8"))
    assert read_template_stamp(user) == expected


def test_restart_same_stamp_does_not_copy_and_keeps_last(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import (
        compute_template_stamp,
        sync_system_files_from_package,
        template_stamp_path,
    )

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    pkg.write_text(
        "sandbox:\n  enabled: false\nchannels:\n  xiaoyi:\n    enabled: false\n",
        encoding="utf-8",
    )
    user_text = "\n".join(
        [
            "sandbox:",
            "  enabled: false",
            "channels:",
            "  xiaoyi:",
            "    enabled: true",
            "    last_session_id: sess-keep",
            "    last_task_id: task-keep",
            "    last_message_id: msg-keep",
            "    push_id: push-keep",
            "stale: true",
            "",
        ]
    )
    user.write_text(user_text, encoding="utf-8")
    stamp = template_stamp_path(user)
    stamp.write_text(compute_template_stamp(pkg, None), encoding="utf-8")
    stamp_text = stamp.read_text(encoding="utf-8")

    assert sync_system_files_from_package(
        user_yaml=user, overlay_yaml=overlay, package_yaml=pkg
    ) is False
    assert user.read_text(encoding="utf-8") == user_text
    assert stamp.read_text(encoding="utf-8") == stamp_text


def test_restart_folds_leftover_overlay_without_copy(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import (
        compute_template_stamp,
        sync_system_files_from_package,
        template_stamp_path,
    )

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    pkg.write_text(
        "sandbox:\n  enabled: false\nchannels:\n  xiaoyi:\n    enabled: false\n",
        encoding="utf-8",
    )
    user.write_text(
        "sandbox:\n  enabled: false\nchannels:\n  xiaoyi:\n    last_session_id: sess-keep\n",
        encoding="utf-8",
    )
    overlay.write_text(
        "sandbox:\n  enabled: true\nchannels:\n  xiaoyi:\n    enabled: true\n",
        encoding="utf-8",
    )
    stamp = template_stamp_path(user)
    stamp.write_text(compute_template_stamp(pkg, None), encoding="utf-8")

    assert sync_system_files_from_package(
        user_yaml=user, overlay_yaml=overlay, package_yaml=pkg
    ) is True
    data = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert data["sandbox"]["enabled"] is True
    assert data["channels"]["xiaoyi"]["last_session_id"] == "sess-keep"
    assert "enabled" not in data["channels"]["xiaoyi"]
    assert not overlay.is_file()


def test_upgrade_copies_and_restores_keep_set(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import (
        compute_template_stamp,
        sync_system_files_from_package,
        template_stamp_path,
    )

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    old_pkg = (
        "sandbox:\n  enabled: false\nevolution:\n  enabled: true\n"
        "channels:\n  xiaoyi:\n    enabled: false\n    apps:\n"
        "      - name: default\n        api_id: api-1\n"
    )
    new_pkg = (
        "sandbox:\n  enabled: false\nevolution:\n  enabled: false\n"
        "channels:\n  xiaoyi:\n    enabled: false\n    apps:\n"
        "      - name: default\n        api_id: api-1\n"
        "permissions:\n  file_guard:\n    defaults:\n      read: allow\n"
    )
    pkg.write_text(old_pkg, encoding="utf-8")
    user.write_text(
        "\n".join(
            [
                "sandbox:",
                "  enabled: false",
                "evolution:",
                "  enabled: true",
                "channels:",
                "  xiaoyi:",
                "    enabled: true",
                "    last_session_id: sess-1",
                "    last_task_id: task-1",
                "    last_message_id: msg-1",
                "    push_id: hook-1",
                "    apps:",
                "      - name: default",
                "        api_id: api-1",
                "        push_id: app-hook",
                "",
            ]
        ),
        encoding="utf-8",
    )
    overlay.write_text(
        "\n".join(
            [
                "sandbox:",
                "  enabled: true",
                "permissions:",
                "  enabled: false",
                "  file_guard:",
                "    paths:",
                "      - path: C:/docs",
                "        write: allow",
                "",
            ]
        ),
        encoding="utf-8",
    )
    stamp = template_stamp_path(user)
    stamp.write_text(compute_template_stamp(pkg, None), encoding="utf-8")
    pkg.write_text(new_pkg, encoding="utf-8")

    assert sync_system_files_from_package(
        user_yaml=user, overlay_yaml=overlay, package_yaml=pkg
    ) is True
    restored = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert restored["evolution"]["enabled"] is False
    assert restored["sandbox"]["enabled"] is True
    assert restored["channels"]["xiaoyi"]["last_session_id"] == "sess-1"
    assert restored["channels"]["xiaoyi"]["last_task_id"] == "task-1"
    assert restored["channels"]["xiaoyi"]["last_message_id"] == "msg-1"
    assert restored["channels"]["xiaoyi"]["push_id"] == "hook-1"
    assert restored["channels"]["xiaoyi"]["apps"][0]["push_id"] == "app-hook"
    assert restored["permissions"]["file_guard"]["paths"] == [
        {"path": "C:/docs", "write": "allow"}
    ]
    assert restored["permissions"]["file_guard"]["defaults"]["read"] == "allow"
    assert "enabled" not in restored.get("permissions", {})
    assert not overlay.is_file()
    assert stamp.read_text(encoding="utf-8") == compute_template_stamp(pkg, None)


def test_missing_stamp_with_existing_yaml_is_upgrade(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import (
        compute_template_stamp,
        sync_system_files_from_package,
        template_stamp_path,
    )

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    pkg.write_text(
        "sandbox:\n  enabled: false\nevolution:\n  enabled: false\n",
        encoding="utf-8",
    )
    user.write_text(
        "\n".join(
            [
                "sandbox:",
                "  enabled: false",
                "evolution:",
                "  enabled: true",
                "channels:",
                "  xiaoyi:",
                "    last_session_id: sess-legacy",
                "    push_id: push-legacy",
                "stale: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    overlay.write_text("sandbox:\n  enabled: true\n", encoding="utf-8")

    assert not template_stamp_path(user).is_file()
    assert sync_system_files_from_package(
        user_yaml=user, overlay_yaml=overlay, package_yaml=pkg
    ) is True
    restored = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert restored["evolution"]["enabled"] is False
    assert restored["sandbox"]["enabled"] is True
    assert "stale" not in restored
    assert restored["channels"]["xiaoyi"]["last_session_id"] == "sess-legacy"
    assert restored["channels"]["xiaoyi"]["push_id"] == "push-legacy"
    assert not overlay.is_file()
    assert template_stamp_path(user).read_text(
        encoding="utf-8"
    ) == compute_template_stamp(pkg, None)


def test_removed_from_allowlist_follows_template_on_upgrade(tmp_path: Path) -> None:
    """名单外键（含昨日 overlay 管道）升级后跟模板，不写回。"""
    from jiuwenswarm.common.config_split import sync_system_files_from_package

    pkg = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    pkg.write_text("sandbox:\n  enabled: false\nauto_memory_enabled: true\n", encoding="utf-8")
    user.write_text("sandbox:\n  enabled: false\nauto_memory_enabled: false\n", encoding="utf-8")
    overlay.write_text("auto_memory_enabled: false\n", encoding="utf-8")
    assert sync_system_files_from_package(
        user_yaml=user, overlay_yaml=overlay, package_yaml=pkg
    ) is True
    restored = yaml.safe_load(user.read_text(encoding="utf-8"))
    assert restored["auto_memory_enabled"] is True
    assert restored["sandbox"]["enabled"] is False
    assert not overlay.is_file()
