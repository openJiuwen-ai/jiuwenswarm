"""AgentOS（一体机）Gateway 配置存量迁移。

个人版在运行期通过 ``prepare_workspace`` / ``ensure_gateway_config`` 完成
“旧共享配置 -> AS/Gateway 两份配置”的迁移；AgentOS 下这些自动路径被
``is_agentos_runtime()`` 关闭，因此需要部署期显式迁移。

本模块只处理 Gateway 侧：

1. 用新部署值渲染好的 ``config.agentos.yaml`` 作为模板骨架；
2. 从旧共享 ``<workspace>/config/config.yaml`` 投影用户配置；
3. 用新渲染值重新覆盖部署归属字段（VIP / etcd / function ID 等）；
4. 备份旧配置后原子写入 ``<gateway_config_dir>/config.yaml``；
5. ``.env`` 不搬家：Gateway 和 AS 共用 ``<workspace>/config/.env``。

AS 配置不在这里迁移：部署脚本用不带 ``-f`` 的 ``jiuwenswarm-init`` 保留
旧 AS 值并补齐新模板键。
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from jiuwenswarm.common.paths import get_gateway_config_dir
from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

#: 这些字段由部署参数决定，迁移时必须使用新渲染值，不能沿用旧值。
DEPLOY_OWNED_PATHS: tuple[str, ...] = (
    "gateway.session_map_scope",
    "gateway.agent_client.type",
    "gateway.agent_client.frontend_endpoint",
    "gateway.agent_client.function_version_urn",
    "gateway.agent_client.agent_timeout_s",
    "gateway.agent_client.agent_namespace",
    "gateway.agentos.registry.endpoint",
    "gateway.agentos.registry.node",
    "gateway.agentos.auth_service_url",
    "gateway.agentos.timeout",
    "gateway.cron.store_backend",
    "gateway.cron.etcd_endpoints",
    "gateway.cron.etcd_prefix",
    "code_graph",
    "sandbox",
)

_MISSING = object()


def _yaml() -> YAML:
    parser = YAML(typ="safe")
    parser.default_flow_style = False
    parser.sort_base_mapping_type_on_output = False
    return parser


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = _yaml().load(stream)
    if not isinstance(data, dict):
        raise TypeError(f"config root must be an object: {path}")
    return data


def dump_yaml_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp.open("w", encoding="utf-8") as stream:
        _yaml().dump(dict(data), stream)
    os.replace(temp, path)


def project_user_config(template: Any, legacy: Any) -> Any:
    """Project legacy values onto the template shape.

    语义与个人版 ``_project_gateway_config`` 保持一致：模板决定键集合，
    旧值只填充模板已有的键；列表整体替换，不做逐项合并。
    """
    if isinstance(template, dict):
        source = legacy if isinstance(legacy, dict) else {}
        return {
            key: project_user_config(default, source.get(key, _MISSING))
            for key, default in template.items()
        }
    if isinstance(template, list):
        return deepcopy(legacy if isinstance(legacy, list) else template)
    if legacy is _MISSING or legacy is None:
        return deepcopy(template)
    return deepcopy(legacy)


def _get_path(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = deepcopy(value)


def apply_deploy_owned(
    migrated: dict[str, Any],
    rendered: Mapping[str, Any],
    paths: Sequence[str] = DEPLOY_OWNED_PATHS,
) -> dict[str, Any]:
    """Overwrite deployment-owned fields with freshly rendered values.

    按“键是否存在”判断，而不是 truthiness：新部署值可能本来就是空字符串
    （例如 ``etcd_endpoints``），空值也必须覆盖旧值。
    """
    result = deepcopy(migrated)
    for dotted in paths:
        value = _get_path(rendered, dotted)
        if value is _MISSING:
            continue
        _set_path(result, dotted, value)
    return result


def is_agentos_gateway_config(path: Path) -> bool:
    try:
        data = load_yaml(path)
    except (OSError, ValueError, TypeError):
        return False
    gateway = data.get("gateway")
    if not isinstance(gateway, Mapping):
        return False
    client = gateway.get("agent_client")
    return isinstance(client, Mapping) and client.get("type") == "agentos_router"


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def migrate(
    *,
    workspace: Path,
    rendered_template: Path,
    gateway_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    workspace = Path(workspace)
    legacy_path = workspace / "config" / "config.yaml"
    if not legacy_path.is_file():
        return {
            "status": "skipped",
            "reason": "legacy config not found",
            "legacy": str(legacy_path),
        }

    target_dir = Path(gateway_dir) if gateway_dir else get_gateway_config_dir(workspace)
    target_path = target_dir / "config-gateway.yaml"
    if target_path.resolve() == legacy_path.resolve():
        raise ValueError(
            "gateway config dir points at the legacy shared config: "
            f"{target_path}; refusing to rewrite the AgentServer config in place"
        )
    if target_path.is_file() and is_agentos_gateway_config(target_path) and not force:
        return {
            "status": "skipped",
            "reason": "gateway config already migrated",
            "target": str(target_path),
        }

    rendered = load_yaml(rendered_template)
    legacy = load_yaml(legacy_path)
    migrated = project_user_config(rendered, legacy)
    final = apply_deploy_owned(migrated, rendered)

    if dry_run:
        return {
            "status": "dry-run",
            "legacy": str(legacy_path),
            "target": str(target_path),
            "deploy_owned": list(DEPLOY_OWNED_PATHS),
        }

    legacy_backup = _backup(legacy_path)
    target_backup = _backup(target_path)
    dump_yaml_atomic(target_path, final)

    # Gateway 和 AS 共用 <workspace>/config/.env，迁移不复制、不拆出第二份。
    shared_env = workspace / "config" / ".env"

    return {
        "status": "migrated",
        "legacy": str(legacy_path),
        "legacy_backup": str(legacy_backup) if legacy_backup else None,
        "target": str(target_path),
        "target_backup": str(target_backup) if target_backup else None,
        "env": str(shared_env),
        "env_copied": False,
    }


def refresh_deploy_owned(
    *,
    workspace: Path,
    rendered_template: Path,
    gateway_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Refresh deploy-owned fields in an existing AgentOS Gateway config.

    User-owned fields stay untouched; only ``DEPLOY_OWNED_PATHS`` are replaced
    with the freshly rendered values. This is the deploy-time path for an
    already migrated Gateway config after ``.env.custom`` changed.
    """
    workspace = Path(workspace)
    target_dir = Path(gateway_dir) if gateway_dir else get_gateway_config_dir(workspace)
    target_path = target_dir / "config-gateway.yaml"
    if not target_path.is_file():
        return {
            "status": "skipped",
            "reason": "gateway config not found",
            "target": str(target_path),
        }
    if not is_agentos_gateway_config(target_path):
        return {
            "status": "skipped",
            "reason": "gateway config is not agentos_router",
            "target": str(target_path),
        }

    rendered = load_yaml(rendered_template)
    current = load_yaml(target_path)
    updated = apply_deploy_owned(current, rendered)

    if dry_run:
        return {
            "status": "dry-run",
            "target": str(target_path),
            "deploy_owned": list(DEPLOY_OWNED_PATHS),
        }

    backup = _backup(target_path)
    dump_yaml_atomic(target_path, updated)
    return {
        "status": "refreshed",
        "target": str(target_path),
        "target_backup": str(backup) if backup else None,
        "deploy_owned": list(DEPLOY_OWNED_PATHS),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate AgentOS legacy shared config to Gateway config",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace root; defaults to JIUWENSWARM_DATA_DIR or ~/.jiuwenswarm",
    )
    parser.add_argument(
        "--template",
        type=Path,
        required=True,
        help="rendered AgentOS Gateway template (config.agentos.yaml)",
    )
    parser.add_argument(
        "--gateway-dir",
        type=Path,
        default=None,
        help="Gateway config directory; defaults to the data root (config-gateway.yaml)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing agentos_router config",
    )
    parser.add_argument(
        "--refresh-deploy-owned",
        action="store_true",
        help="refresh deploy-owned fields in an existing agentos_router config",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[agentos-migrate] %(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    args = _parse_args(argv)
    workspace = args.workspace or get_user_workspace_dir()
    try:
        if args.refresh_deploy_owned:
            report = refresh_deploy_owned(
                workspace=workspace,
                rendered_template=args.template,
                gateway_dir=args.gateway_dir,
                dry_run=args.dry_run,
            )
        else:
            report = migrate(
                workspace=workspace,
                rendered_template=args.template,
                gateway_dir=args.gateway_dir,
                force=args.force,
                dry_run=args.dry_run,
            )
    except Exception as exc:  # noqa: BLE001 - CLI should surface a stable message
        logger.error("%s", exc)
        return 2
    logger.info("%s", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
