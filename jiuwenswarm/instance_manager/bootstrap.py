# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap .env file creation for instances.

This module provides:
- create_bootstrap_env: Create bootstrap .env for an InstanceConfig
- create_bootstrap_env_for_name: Create bootstrap .env for a named instance
- _create_basic_bootstrap_env: Early-stage bootstrap creation (independent)
- load_instance_bootstrap_by_name: Load bootstrap .env after argparse parsing
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from jiuwenswarm.instance_manager.config import (
    InstanceConfig,
    BASE_PORTS,
    calculate_instance_ports,
    validate_instance_name,
)
from jiuwenswarm.instance_manager.status import get_instance_config
from jiuwenswarm.instance_manager.yaml import get_instance_index

logger = logging.getLogger(__name__)


def create_bootstrap_env(config: InstanceConfig) -> Path:
    """Create bootstrap .env file for an instance.

    The bootstrap .env contains:
    - JIUWENSWARM_DATA_DIR: instance workspace path
    - JIUWENSWARM_INSTANCE: instance name
    - Port assignments for each service

    Args:
        config: InstanceConfig for the instance

    Returns:
        Path to the created .env file
    """
    env_path = config.get_bootstrap_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Bootstrap .env for instance: {config.name}",
        f"JIUWENSWARM_DATA_DIR={config.workspace}",
        f"JIUWENSWARM_INSTANCE={config.name}",
    ]

    # Add port assignments
    port_env_mapping = {
        "agent_server": "AGENT_SERVER_PORT",
        "web": "WEB_PORT",
        "gateway": "GATEWAY_PORT",
        "frontend": "FRONTEND_PORT",
    }

    for port_type, env_name in port_env_mapping.items():
        if port_type in config.ports:
            lines.append(f"{env_name}={config.ports[port_type]}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(
        "Created bootstrap .env for instance '%s': %s", config.name, env_path
    )
    return env_path


def create_bootstrap_env_for_name(name: str, workspace: Path) -> Path:
    """Create bootstrap .env file for a named instance (legacy interface)."""
    index = get_instance_index(name)
    ports = calculate_instance_ports(index)
    config = InstanceConfig(name=name, workspace=workspace, ports=ports)
    return create_bootstrap_env(config)


# --- Early-stage bootstrap functions (from dotenv_early.py) ---


def _create_basic_bootstrap_env(
    name: str, workspace: Path, component_name: str
) -> None:
    """Create a basic bootstrap .env file during early parsing.

    This is needed when instance was created but bootstrap .env is missing.
    Uses auto-port allocation based on instance index.

    IMPORTANT: This function must NOT depend on any jiuwenswarm modules
    because it is called during early parsing before any imports.

    Args:
        name: Instance name
        workspace: Workspace path
        component_name: Component name for logging
    """
    # Early logger for startup diagnostics
    _early_logger = logging.getLogger("jiuwenswarm.early")
    if not _early_logger.handlers:
        import sys
        _early_logger.addHandler(logging.StreamHandler(sys.stderr))
        _early_logger.setLevel(logging.WARNING)

    # Get instance index from instances.yaml order
    user_home = os.environ.get("JIUWENSWARM_HOME") or Path.home()
    yaml_path = Path(user_home) / ".jiuwenswarm" / "instances.yaml"

    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        instances = list(data.get("instances", {}).keys()) if data else []
        index = instances.index(name) + 1 if name in instances else 1
    except Exception:
        index = 1

    # Auto-allocated ports (same logic as config.BASE_PORTS)
    ports = {k: v + index * 1000 for k, v in BASE_PORTS.items()}

    # Write bootstrap .env
    bootstrap_env = workspace / ".env"
    lines = [
        f"# Bootstrap .env for instance: {name}",
        f"JIUWENSWARM_DATA_DIR={workspace}",
        f"JIUWENSWARM_INSTANCE={name}",
        f"AGENT_SERVER_PORT={ports['agent_server']}",
        f"WEB_PORT={ports['web']}",
        f"GATEWAY_PORT={ports['gateway']}",
        f"FRONTEND_PORT={ports['frontend']}",
    ]
    bootstrap_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _early_logger.info(
        "[%s] Created bootstrap .env: %s", component_name, bootstrap_env
    )


def load_instance_bootstrap_by_name(name: str) -> Path | None:
    """Load bootstrap .env for a named instance after argparse parsing.

    This function is called from CLI main() when --name is specified
    but --dotenv was not parsed early. It loads the instance's bootstrap
    .env file to set JIUWENSWARM_DATA_DIR and port environment variables.

    Args:
        name: Instance name (must exist in instances.yaml)

    Returns:
        Path to loaded .env if successful, None otherwise

    Error handling:
        - Invalid instance name: prints error and returns None
        - Instance not in instances.yaml: prints error and returns None
        - Workspace not found: prints error with init hint and returns None

    Usage in CLI entry points:
        def main():
            parser.add_argument("--name", ...)
            args = parser.parse_args()

            if args.name:
                from jiuwenswarm.instance_manager.bootstrap import (
                    load_instance_bootstrap_by_name
                )
                env_path = load_instance_bootstrap_by_name(args.name)
                if env_path is None:
                    raise SystemExit(1)
    """
    _logger = logging.getLogger(__name__)

    # Validate instance name
    error = validate_instance_name(name)
    if error:
        _logger.error("ERROR: %s", error)
        return None

    # Load instance config from instances.yaml
    config = get_instance_config(name)
    if config is None:
        _logger.error("ERROR: Instance '%s' not found in instances.yaml", name)
        _logger.error("Run 'jiuwenswarm-init --name %s' to create it.", name)
        return None

    # Check workspace directory exists
    if not config.workspace.exists():
        _logger.error(
            "ERROR: Workspace directory not found: %s", config.workspace
        )
        _logger.error("Run 'jiuwenswarm-init --name %s' to create it.", name)
        return None

    # Get or create bootstrap .env
    env_path = config.get_bootstrap_env_path()
    if not env_path.exists():
        env_path = create_bootstrap_env(config)

    # Load the .env file with override=True
    from dotenv import load_dotenv
    load_dotenv(env_path, override=True)

    return env_path