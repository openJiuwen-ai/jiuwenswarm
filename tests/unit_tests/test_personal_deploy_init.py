# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

import yaml

from jiuwenswarm.common import utils
from jiuwenswarm.common.utils import CopyDiffResult


def _write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_install_gateway_config_projects_legacy_values(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    legacy = tmp_path / "legacy.yaml"
    _write_yaml(template, {
        "gateway": {"agent_client": {"type": "websocket"}, "cron": {"store_backend": "file"}},
        "hooks": {"disable_all_hooks": False},
    })
    _write_yaml(legacy, {
        "gateway": {"agent_client": {"type": "http"}, "cron": {"store_backend": "etcd"}},
        "hooks": {"disable_all_hooks": True},
        "models": {"defaults": [{"api_key": "secret"}]},
    })
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    workspace = tmp_path / "workspace"
    utils._install_gateway_config(
        workspace,
        legacy,
        overwrite=False,
        cumulative_diff=CopyDiffResult([], [], []),
    )

    generated = yaml.safe_load(
        (workspace / "config" / "config-gateway.yaml").read_text(encoding="utf-8")
    )
    assert generated["gateway"]["agent_client"]["type"] == "http"
    assert generated["gateway"]["cron"]["store_backend"] == "etcd"
    assert generated["hooks"]["disable_all_hooks"] is True
    assert "models" not in generated


def test_install_gateway_config_migrates_only_gateway_hooks(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    legacy = tmp_path / "legacy.yaml"
    _write_yaml(template, {"hooks": {"disable_all_hooks": False}})
    _write_yaml(legacy, {
        "hooks": {
            "disable_all_hooks": True,
            "SessionStart": [{"matcher": "*", "hooks": [{"type": "command"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "prompt"}]}],
        },
    })
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    workspace = tmp_path / "workspace"
    utils._install_gateway_config(
        workspace,
        legacy,
        overwrite=False,
        cumulative_diff=CopyDiffResult([], [], []),
    )

    generated = yaml.safe_load(
        (workspace / "config" / "config-gateway.yaml").read_text(encoding="utf-8")
    )
    assert generated["hooks"]["disable_all_hooks"] is True
    assert generated["hooks"]["SessionStart"] == [
        {"matcher": "*", "hooks": [{"type": "command"}]}
    ]
    assert "PreToolUse" not in generated["hooks"]


def test_install_gateway_config_preserves_gateway_owned_keys(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    legacy = tmp_path / "legacy.yaml"
    _write_yaml(template, {
        "extensions": {"extension_dirs": ""},
        "hooks": {"disable_all_hooks": False},
        "modes": {"team": {"jiuwen_team": {"enable_swarmflow": False}}},
    })
    _write_yaml(legacy, {
        "extensions": {
            "install_dir": "/old/extensions",
            "data_dir": "/old/data",
            "store_dir": "/old/store",
            "state_dir": "/old/state",
            "install_report": {"poll_interval_ms": 1000},
        },
        "models": {"login_model_settings": {"m": {"context_window": 8192}}},
        "modes": {"team": {"jiuwen_team": {
            "swarmflow_budget": 500000,
            "external_cli_path_histories": ["/old/bin"],
        }}},
    })
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    workspace = tmp_path / "workspace"
    utils._install_gateway_config(
        workspace,
        legacy,
        overwrite=False,
        cumulative_diff=CopyDiffResult([], [], []),
    )

    generated = yaml.safe_load(
        (workspace / "config" / "config-gateway.yaml").read_text(encoding="utf-8")
    )
    assert generated["extensions"]["install_dir"] == "/old/extensions"
    assert generated["extensions"]["data_dir"] == "/old/data"
    assert generated["extensions"]["install_report"] == {"poll_interval_ms": 1000}
    assert generated["models"]["login_model_settings"] == {
        "m": {"context_window": 8192}
    }
    assert generated["modes"]["team"]["jiuwen_team"]["swarmflow_budget"] == 500000
    assert generated["modes"]["team"]["jiuwen_team"]["external_cli_path_histories"] == [
        "/old/bin"
    ]


def test_install_gateway_config_keeps_existing_gateway_file(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    _write_yaml(template, {"gateway": {"agent_client": {"type": "websocket"}}})
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    workspace = tmp_path / "workspace"
    existing = workspace / "config" / "config-gateway.yaml"
    _write_yaml(existing, {"gateway": {"agent_client": {"type": "http"}}})

    utils._install_gateway_config(
        workspace,
        None,
        overwrite=False,
        cumulative_diff=CopyDiffResult([], [], []),
    )

    generated = yaml.safe_load(existing.read_text(encoding="utf-8"))
    assert generated["gateway"]["agent_client"]["type"] == "http"


def test_install_gateway_config_merges_template_keys(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    _write_yaml(template, {
        "gateway": {"agent_client": {"type": "websocket"}},
        "proactive_recommendation": {"enabled": False},
    })
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    workspace = tmp_path / "workspace"
    existing = workspace / "config" / "config-gateway.yaml"
    _write_yaml(existing, {
        "gateway": {"agent_client": {"type": "http"}},
        "custom_operator_key": "keep",
    })

    utils._install_gateway_config(
        workspace,
        None,
        overwrite=False,
        cumulative_diff=CopyDiffResult([], [], []),
    )

    generated = yaml.safe_load(existing.read_text(encoding="utf-8"))
    assert generated["gateway"]["agent_client"]["type"] == "http"
    assert generated["proactive_recommendation"]["enabled"] is False
    assert generated["custom_operator_key"] == "keep"


def test_ensure_gateway_config_creates_missing_file(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    stock_dirs = "${EXTENSION_DIRS:-agent_gateway/extensions}"
    _write_yaml(template, {
        "gateway": {"agent_client": {"type": "websocket"}},
        "extensions": {"extension_dirs": stock_dirs},
    })
    workspace = tmp_path / "workspace"
    _write_yaml(workspace / "config" / "config.yaml", {"models": {}})
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    assert utils.ensure_gateway_config(workspace) is True
    generated = yaml.safe_load(
        (workspace / "config" / "config-gateway.yaml").read_text(encoding="utf-8")
    )
    assert generated["gateway"]["agent_client"]["type"] == "websocket"
    assert generated["extensions"]["extension_dirs"] == f"{stock_dirs};jiuwenswarm/extensions"


def test_ensure_gateway_config_merges_existing_file(
    tmp_path: Path, monkeypatch,
) -> None:
    template = tmp_path / "gateway-template.yaml"
    _write_yaml(template, {
        "gateway": {"agent_client": {"type": "websocket"}},
        "proactive_recommendation": {"enabled": False},
    })
    workspace = tmp_path / "workspace"
    existing = workspace / "config" / "config-gateway.yaml"
    _write_yaml(existing, {"gateway": {"agent_client": {"type": "http"}}})
    monkeypatch.setattr(utils, "get_gateway_config_template", lambda: template)

    assert utils.ensure_gateway_config(workspace) is True
    generated = yaml.safe_load(existing.read_text(encoding="utf-8"))
    assert generated["gateway"]["agent_client"]["type"] == "http"
    assert generated["proactive_recommendation"]["enabled"] is False


def test_ensure_gateway_config_skips_agentos(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(utils, "is_agentos_runtime", lambda: True)

    assert utils.ensure_gateway_config(workspace) is False
    assert not (workspace / "config" / "config-gateway.yaml").exists()


def test_gateway_config_file_uses_shared_config_dir(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.common import paths

    monkeypatch.setattr(paths, "get_user_workspace_dir", lambda: tmp_path)
    assert utils.get_gateway_config_file() == tmp_path / "config" / "config-gateway.yaml"


def test_gateway_config_dir_honors_environment_override(
    tmp_path: Path, monkeypatch,
) -> None:
    custom = tmp_path / "custom-gateway"
    monkeypatch.setenv("JIUWENSWARM_GATEWAY_CONFIG_DIR", str(custom))

    assert utils.get_gateway_config_file() == custom / "config-gateway.yaml"
