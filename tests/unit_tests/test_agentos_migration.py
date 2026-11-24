"""AgentOS 一体机 Gateway 配置存量迁移测试。"""

from pathlib import Path

import yaml

from jiuwenswarm.agentos_migration import (
    apply_deploy_owned,
    migrate,
    project_user_config,
)


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _legacy_config() -> dict:
    return {
        "gateway": {
            "session_map_scope": "per_chat_bot",
            "agent_client": {
                "type": "agentos_router",
                "frontend_endpoint": "http://old-vip:8888",
                "function_version_urn": "old-function",
                "agent_timeout_s": 30,
                "agent_namespace": "old-ns",
            },
            "agentos": {
                "registry": {
                    "endpoint": "http://old-vip:4003",
                    "node": "old-node",
                },
                "auth_service_url": "http://old-vip:8090",
                "timeout": 5,
            },
            "cron": {
                "store_backend": "etcd",
                "etcd_endpoints": "http://old-etcd:32379",
                "etcd_prefix": "/old/cron/",
            },
        },
        "channels": {
            "feishu": {
                "apps": [{"app_id": "cli_old", "enabled": True}],
                "send_file_allowed": True,
            }
        },
        "hooks": {"disable_all_hooks": True},
        "code_graph": {"enabled": "old"},
    }


def _rendered_template() -> dict:
    return {
        "gateway": {
            "session_map_scope": "per_chat_bot_user",
            "agent_client": {
                "type": "agentos_router",
                "frontend_endpoint": "http://new-vip:8888",
                "function_version_urn": "new-function",
                "agent_timeout_s": 300,
                "agent_namespace": "default",
            },
            "agentos": {
                "registry": {
                    "endpoint": "http://new-vip:4003",
                    "node": "new-node",
                },
                "auth_service_url": "http://new-vip:8090",
                "timeout": 10,
            },
            "cron": {
                "store_backend": "etcd",
                "etcd_endpoints": "",
                "etcd_prefix": "/jiuwenswarm/cron/jobs/",
            },
        },
        "channels": {
            "web": {"send_file_allowed": True},
            "feishu": {"apps": [], "send_file_allowed": True},
        },
        "hooks": {"disable_all_hooks": False},
        "code_graph": {"enabled": "new"},
        "extensions": {"extension_dirs": "${EXTENSION_DIRS:-agent_gateway/extensions}"},
    }


def test_project_user_config_keeps_template_shape_and_legacy_values() -> None:
    projected = project_user_config(_rendered_template(), _legacy_config())

    assert projected["channels"]["feishu"]["apps"][0]["app_id"] == "cli_old"
    assert projected["hooks"]["disable_all_hooks"] is True
    # 旧配置里模板没有的键不会进入结果。
    assert "models" not in projected


def test_apply_deploy_owned_overrides_even_empty_values() -> None:
    rendered = _rendered_template()
    migrated = {
        "gateway": {
            "cron": {"etcd_endpoints": "http://old-etcd:32379"},
            "agent_client": {"frontend_endpoint": "http://old-vip:8888"},
        }
    }

    final = apply_deploy_owned(migrated, rendered)

    assert final["gateway"]["cron"]["etcd_endpoints"] == ""
    assert final["gateway"]["agent_client"]["frontend_endpoint"] == "http://new-vip:8888"


def test_migrate_moves_user_config_and_overwrites_deploy_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    legacy = _write(workspace / "config" / "config.yaml", _legacy_config())
    legacy_env = workspace / "config" / ".env"
    legacy_env.write_text("API_KEY=old\n", encoding="utf-8")
    rendered = _write(tmp_path / "rendered.yaml", _rendered_template())
    gateway_dir = workspace / "infra" / "config"

    report = migrate(
        workspace=workspace,
        rendered_template=rendered,
        gateway_dir=gateway_dir,
    )

    assert report["status"] == "migrated"
    target = _read(gateway_dir / "config.yaml")
    assert target["channels"]["feishu"]["apps"][0]["app_id"] == "cli_old"
    assert target["hooks"]["disable_all_hooks"] is True
    assert target["gateway"]["agent_client"]["frontend_endpoint"] == "http://new-vip:8888"
    assert target["gateway"]["agent_client"]["function_version_urn"] == "new-function"
    assert target["gateway"]["agentos"]["registry"]["endpoint"] == "http://new-vip:4003"
    assert target["gateway"]["cron"]["etcd_endpoints"] == ""
    assert target["code_graph"]["enabled"] == "new"

    # AS 旧配置不被改写；env 保持共用一份，不拆到 Gateway 目录。
    assert _read(legacy)["gateway"]["agent_client"]["frontend_endpoint"] == "http://old-vip:8888"
    assert report["env"] == str(legacy_env)
    assert legacy_env.read_text(encoding="utf-8") == "API_KEY=old\n"
    assert not (gateway_dir / ".env").exists()
    assert list((workspace / "config").glob("config.yaml.bak.*"))


def test_migrate_forces_agentos_router_even_from_stock_legacy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    legacy = _legacy_config()
    legacy["gateway"]["agent_client"]["type"] = "websocket"
    _write(workspace / "config" / "config.yaml", legacy)
    rendered = _write(tmp_path / "rendered.yaml", _rendered_template())

    migrate(
        workspace=workspace,
        rendered_template=rendered,
        gateway_dir=workspace / "infra" / "config",
    )

    target = _read(workspace / "infra" / "config" / "config.yaml")
    assert target["gateway"]["agent_client"]["type"] == "agentos_router"


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "config" / "config.yaml", _legacy_config())
    rendered = _write(tmp_path / "rendered.yaml", _rendered_template())
    gateway_dir = workspace / "infra" / "config"

    assert migrate(
        workspace=workspace,
        rendered_template=rendered,
        gateway_dir=gateway_dir,
    )["status"] == "migrated"
    first = (gateway_dir / "config.yaml").read_text(encoding="utf-8")

    second = migrate(
        workspace=workspace,
        rendered_template=rendered,
        gateway_dir=gateway_dir,
    )

    assert second["status"] == "skipped"
    assert (gateway_dir / "config.yaml").read_text(encoding="utf-8") == first


def test_migrate_overwrites_stock_gateway_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "config" / "config.yaml", _legacy_config())
    rendered = _write(tmp_path / "rendered.yaml", _rendered_template())
    gateway_dir = workspace / "infra" / "config"
    _write(gateway_dir / "config.yaml", {"gateway": {"agent_client": {"type": "websocket"}}})

    report = migrate(
        workspace=workspace,
        rendered_template=rendered,
        gateway_dir=gateway_dir,
    )

    assert report["status"] == "migrated"
    assert _read(gateway_dir / "config.yaml")["gateway"]["agent_client"]["type"] == "agentos_router"


def test_migrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "config" / "config.yaml", _legacy_config())
    rendered = _write(tmp_path / "rendered.yaml", _rendered_template())
    gateway_dir = workspace / "infra" / "config"

    report = migrate(
        workspace=workspace,
        rendered_template=rendered,
        gateway_dir=gateway_dir,
        dry_run=True,
    )

    assert report["status"] == "dry-run"
    assert not (gateway_dir / "config.yaml").exists()
