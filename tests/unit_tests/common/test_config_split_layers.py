# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""用户 yaml 为基底 + overlay 稀疏合并；系统文件按内容覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import get_config, merge_config_layers
from jiuwenswarm.common.utils import (
    get_builtin_rules_file,
    get_package_config_file,
    get_package_resources_dir,
)


def test_merge_overlay_leaf_wins_and_keeps_base_and_extra() -> None:
    base = {
        "logging": {"level": "INFO", "file": "a.log"},
        "tools": ["read_file", "bash"],
        "sandbox": {"enabled": False, "policy_file": "windows-policy.yaml"},
    }
    overlay = {
        "logging": {"level": "DEBUG"},
        "tools": ["read_file"],
        "preferred_language": "zh",
    }
    merged = merge_config_layers(base, overlay)
    assert merged["logging"]["level"] == "DEBUG"
    assert merged["logging"]["file"] == "a.log"
    assert merged["tools"] == ["read_file"]
    assert merged["sandbox"]["enabled"] is False
    assert merged["preferred_language"] == "zh"
    assert base["logging"]["level"] == "INFO"


def test_package_resources_resolve_to_repo_templates() -> None:
    res = get_package_resources_dir()
    assert res is not None
    assert (res / "config.yaml").is_file()
    assert get_package_config_file() == res / "config.yaml"
    assert get_builtin_rules_file().name == "builtin_rules.yaml"


def test_get_config_merges_user_yaml_with_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "execution_guard": {"llm_retry_rail": {"enabled": True}},
                "logging": {"level": "INFO"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (user_dir / "config.user.yaml").write_text(
        yaml.safe_dump({"logging": {"level": "ERROR"}}, allow_unicode=True),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_file", lambda: user_dir / "config.yaml"
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_user_overlay_file",
        lambda: user_dir / "config.user.yaml",
    )

    cfg = get_config()
    assert cfg["logging"]["level"] == "ERROR"
    assert cfg["execution_guard"]["llm_retry_rail"]["enabled"] is True


def test_get_config_prefers_sparse_user_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "config.yaml").write_text("logging:\n  level: WARN\n", encoding="utf-8")
    (user_dir / "config.user.yaml").write_text(
        "logging:\n  level: DEBUG\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_file", lambda: user_dir / "config.yaml"
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_user_overlay_file",
        lambda: user_dir / "config.user.yaml",
    )

    cfg = get_config()
    assert cfg["logging"]["level"] == "DEBUG"


def test_extract_allowlist_keeps_ui_knobs_not_system_lists() -> None:
    from jiuwenswarm.common.config_split import extract_overlay_from_legacy

    package = {
        "preferred_language": "zh",
        "logging": {"level": "INFO", "path": "app.log"},
        "progressive_tool_always_visible_tools": ["todo_create"],
        "auto_memory_enabled": True,
        "channels": {
            "desktop": {"send_file_allowed": True},
            "xiaoyi": {"enabled": False, "ws_url1": "wss://old", "file_upload_url": ""},
            "feishu": {"enabled": False, "app_id": ""},
        },
        "permissions": {
            "enabled": True,
            "permission_mode": "normal",
            "tools": {"bash": "allow", "write": "allow"},
            "file_guard": {"defaults": {"read": "allow", "write": "allow", "exec": "allow"}},
        },
        "sandbox": {"enabled": False},
        "mcp": {"servers": []},
    }
    user = {
        "preferred_language": "en",
        "logging": {"level": "DEBUG", "path": "app.log"},
        "progressive_tool_always_visible_tools": ["bash"],
        "auto_memory_enabled": False,
        "channels": {
            "desktop": {"send_file_allowed": True},
            "xiaoyi": {
                "enabled": True,
                "ws_url1": "np://claw-relay",
                "file_upload_url": "http://up",
            },
            "feishu": {"enabled": True, "app_id": "cli_user"},
        },
        "permissions": {
            "enabled": True,
            "permission_mode": "strict",
            "tools": {"bash": "ask", "write": "deny"},
            "file_guard": {"defaults": {"read": "ask", "write": "ask", "exec": "deny"}},
        },
        "sandbox": {"enabled": False},
        "mcp": {
            "servers": [
                {
                    "name": "gausspd-memory",
                    "transport": "stdio",
                    "command": "${GSPD_MCP_EXE}",
                    "enabled": True,
                }
            ]
        },
        "execution_guard": {"llm_retry_rail": {"enabled": False}},
    }
    overlay = extract_overlay_from_legacy(user, package)
    assert "preferred_language" not in overlay
    assert "logging" not in overlay
    assert "progressive_tool_always_visible_tools" not in overlay
    assert "execution_guard" not in overlay
    assert overlay["auto_memory_enabled"] is False
    assert overlay["channels"]["xiaoyi"]["enabled"] is True
    assert overlay["channels"]["xiaoyi"]["ws_url1"] == "np://claw-relay"
    assert "feishu" not in overlay.get("channels", {})
    assert "permissions" not in overlay
    assert "sandbox" not in overlay
    assert overlay["mcp"]["servers"][0]["name"] == "gausspd-memory"
    assert list(overlay["mcp"]["servers"][0].keys()) == [
        "name",
        "transport",
        "command",
        "enabled",
    ]


def test_drop_permissions_from_overlay(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import drop_permissions_from_overlay

    overlay = tmp_path / "config.user.yaml"
    overlay.write_text(
        "auto_memory_enabled: false\npermissions:\n  enabled: false\n",
        encoding="utf-8",
    )
    assert drop_permissions_from_overlay(overlay) is True
    data = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert data["auto_memory_enabled"] is False
    assert "permissions" not in data
    assert drop_permissions_from_overlay(overlay) is False


def test_extract_user_overlay_is_idempotent_and_keeps_legacy_yaml(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import extract_user_overlay

    package = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    package.write_text("auto_memory_enabled: true\n", encoding="utf-8")
    user.write_text("auto_memory_enabled: false\nlogging:\n  level: ERROR\n", encoding="utf-8")
    assert extract_user_overlay(user_yaml=user, overlay_yaml=overlay, package_yaml=package) is True
    first = overlay.read_text(encoding="utf-8")
    assert user.is_file()
    assert extract_user_overlay(user_yaml=user, overlay_yaml=overlay, package_yaml=package) is False
    assert overlay.read_text(encoding="utf-8") == first
    data = yaml.safe_load(first)
    assert data["auto_memory_enabled"] is False
    assert "logging" not in data


def test_extract_keeps_mcp_server_key_order_and_four_space_list(tmp_path: Path) -> None:
    from jiuwenswarm.common.config_split import extract_user_overlay

    package = tmp_path / "pkg.yaml"
    user = tmp_path / "config.yaml"
    overlay = tmp_path / "config.user.yaml"
    package.write_text("auto_memory_enabled: true\nmcp:\n  servers: []\n", encoding="utf-8")
    user.write_text(
        "\n".join(
            [
                "auto_memory_enabled: false",
                "mcp:",
                "  servers:",
                "    - name: gausspd-memory",
                "      transport: stdio",
                "      command: ${GSPD_MCP_EXE}",
                "      enabled: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert extract_user_overlay(user_yaml=user, overlay_yaml=overlay, package_yaml=package) is True
    text = overlay.read_text(encoding="utf-8")
    name_i = text.index("name: gausspd-memory")
    assert text.index("transport: stdio") > name_i
    assert text.index("command:") > text.index("transport: stdio")
    assert text.index("enabled: true") > text.index("command:")
    assert "    - name: gausspd-memory" in text
    assert not any(line.startswith("  - ") for line in text.splitlines())


def test_get_config_reads_user_yaml_only_when_no_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "config.yaml").write_text("sandbox:\n  enabled: false\n", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config_file", lambda: user_dir / "config.yaml"
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_user_overlay_file",
        lambda: user_dir / "config.user.yaml",
    )

    cfg = get_config()
    assert cfg["sandbox"]["enabled"] is False


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


def test_patch_user_config_and_dump_write_overlay_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.config import (
        dump_yaml_round_trip,
        get_user_config,
        load_yaml_round_trip,
        patch_user_config,
    )

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    legacy = user_dir / "config.yaml"
    overlay = user_dir / "config.user.yaml"
    legacy.write_text("logging:\n  level: INFO\n", encoding="utf-8")
    overlay.write_text("logging:\n  level: DEBUG\n", encoding="utf-8")

    monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", legacy)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_user_overlay_file", lambda: overlay
    )

    loaded = load_yaml_round_trip(legacy)
    assert loaded["logging"]["level"] == "DEBUG"
    loaded["preferred_language"] = "en"
    dump_yaml_round_trip(legacy, loaded)
    assert "preferred_language" not in yaml.safe_load(legacy.read_text(encoding="utf-8"))
    assert yaml.safe_load(overlay.read_text(encoding="utf-8"))["preferred_language"] == "en"

    patch_user_config(lambda data: {**data, "auto_memory_enabled": False})
    assert get_user_config()["auto_memory_enabled"] is False


def test_sparse_merge_keeps_system_lists_and_index_merges_temperature() -> None:
    base = {
        "progressive_tool_always_visible_tools": ["todo_create", "bash"],
        "modes": {"agent": {"tools": ["read_file", "write_file"]}},
        "models": {
            "defaults": [
                {
                    "model_client_config": {"timeout": 360},
                    "model_config_obj": {"temperature": 0.95},
                }
            ]
        },
        "mcp": {"servers": []},
    }
    overlay = {
        "progressive_tool_always_visible_tools": ["only_user"],
        "modes": {"agent": {"tools": ["bash"]}},
        "models": {"defaults": [{"model_config_obj": {"temperature": 0.1}}]},
        "mcp": {"servers": [{"name": "gausspd-memory"}]},
    }
    merged = merge_config_layers(base, overlay, sparse=True)
    assert merged["progressive_tool_always_visible_tools"] == ["todo_create", "bash"]
    assert merged["modes"]["agent"]["tools"] == ["read_file", "write_file"]
    assert merged["models"]["defaults"][0]["model_client_config"]["timeout"] == 360
    assert merged["models"]["defaults"][0]["model_config_obj"]["temperature"] == 0.1
    assert merged["mcp"]["servers"][0]["name"] == "gausspd-memory"
