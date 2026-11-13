# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""YAML configuration management for instances.

This module provides:
- Path functions: get_instances_yaml_path, get_instances_dir, get_instance_workspace_path
- YAML parsing: load_instances_yaml with validation
- YAML writing: save_instances_yaml, update_instances_yaml, create_instances_yaml_template
- Index lookup: get_instance_index
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from jiuwenswarm.instance_manager.config import (
    InstancesYamlError,
    PORT_TYPES,
    validate_instance_name,
)
from jiuwenswarm.common.utils import get_user_home

logger = logging.getLogger(__name__)


def get_instances_yaml_path() -> Path:
    """Return path to instances.yaml: ~/.jiuwenswarm/instances.yaml"""
    return get_user_home() / ".jiuwenswarm" / "instances.yaml"


def get_instances_dir() -> Path:
    """Return base directory for named instance workspaces: ~/.jiuwenswarm-instances/"""
    return get_user_home() / ".jiuwenswarm-instances"


def get_instance_workspace_path(name: str) -> Path:
    """Return workspace path for a named instance: ~/.jiuwenswarm-instances/<name>/"""
    return get_instances_dir() / name


def _read_yaml_file(path: Path) -> dict:
    """Read and parse YAML file with error handling.

    Returns:
        Parsed YAML data

    Raises:
        InstancesYamlError: If file cannot be read or parsed
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except IOError as exc:
        raise InstancesYamlError(
            f"Cannot read file: {exc}\n"
            f"  Path: {path}\n"
            f"  Please check file permissions."
        ) from exc

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise InstancesYamlError(
            f"YAML format error:\n"
            f"  {exc}\n"
            f"  Path: {path}\n"
            f"  Fix suggestions:\n"
            f"    - Check YAML syntax (indentation, quotes, colons)\n"
            f"    - Ensure no duplicate keys\n"
            f"    - Run 'jiuwenswarm-init' to recreate a valid template"
        ) from exc

    return data


def _validate_yaml_structure(data: Any, path: Path) -> dict:
    """Validate YAML top-level structure.

    Returns:
        Validated dict with 'instances' key

    Raises:
        InstancesYamlError: If structure is invalid
    """
    if data is None:
        return {"instances": {}}

    if not isinstance(data, dict):
        raise InstancesYamlError(
            f"Invalid structure: expected dict, got {type(data).__name__}\n"
            f"  Path: {path}\n"
            f"  The file should start with 'instances:' key."
        )

    if "instances" not in data:
        raise InstancesYamlError(
            f"Missing 'instances' key\n"
            f"  Path: {path}\n"
            f"  Add 'instances: {{}}' to create an empty configuration."
        )

    instances_data = data.get("instances")
    if instances_data is None:
        data["instances"] = {}

    return data


def _validate_instance_entry(name: str, inst_data: Any, path: Path) -> None:
    """Validate a single instance entry in instances.yaml.

    Note: YAML dict keys are always strings, so name type check is skipped.

    Raises:
        InstancesYamlError: If instance entry is invalid
    """
    # Validate instance name format
    error = validate_instance_name(name)
    if error:
        raise InstancesYamlError(
            f"Invalid instance name '{name}': {error}\n"
            f"  Path: {path}\n"
            f"  Name rules: 1-64 chars, alphanumeric/underscore/hyphen only, "
            f"no leading dot, not reserved."
        )

    # Validate instance data structure
    if inst_data is not None and not isinstance(inst_data, dict):
        raise InstancesYamlError(
            f"Instance '{name}' has invalid data: expected dict or null, "
            f"got {type(inst_data).__name__}\n"
            f"  Path: {path}"
        )

    if inst_data is None:
        return

    # Validate ports
    if "ports" in inst_data:
        _validate_ports_config(name, inst_data["ports"], path)

    # Validate workspace
    if "workspace" in inst_data:
        workspace = inst_data["workspace"]
        if not isinstance(workspace, str):
            raise InstancesYamlError(
                f"Instance '{name}': 'workspace' must be a string path, "
                f"got {type(workspace).__name__}\n"
                f"  Path: {path}"
            )


def _validate_ports_config(name: str, ports: Any, path: Path) -> None:
    """Validate ports configuration for an instance.

    Raises:
        InstancesYamlError: If ports config is invalid
    """
    if not isinstance(ports, dict):
        raise InstancesYamlError(
            f"Instance '{name}': 'ports' must be a dict\n"
            f"  Path: {path}\n"
            f"  Example: ports: {{agent_server: 28092, web: 29000}}"
        )

    for port_type, port_val in ports.items():
        if port_type not in PORT_TYPES:
            raise InstancesYamlError(
                f"Instance '{name}': unknown port type '{port_type}'\n"
                f"  Valid types: {', '.join(PORT_TYPES)}\n"
                f"  Path: {path}"
            )

        if not isinstance(port_val, int):
            raise InstancesYamlError(
                f"Instance '{name}': port '{port_type}' must be integer, "
                f"got {type(port_val).__name__}\n"
                f"  Path: {path}"
            )

        if port_val < 1 or port_val > 65535:
            raise InstancesYamlError(
                f"Instance '{name}': port '{port_type}' must be 1-65535, "
                f"got {port_val}\n"
                f"  Path: {path}"
            )


def load_instances_yaml() -> dict:
    """Load instances.yaml with comprehensive error handling.

    Returns:
        Dict with 'instances' key containing instance configurations

    Raises:
        InstancesYamlError: If file cannot be read, parsed, or validated
            with user-friendly message and fix suggestions
    """
    path = get_instances_yaml_path()
    if not path.exists():
        return {"instances": {}}

    # Read and parse YAML
    data = _read_yaml_file(path)

    # Validate structure
    data = _validate_yaml_structure(data, path)

    # Validate each instance entry
    instances_data = data.get("instances", {})
    for name, inst_data in instances_data.items():
        _validate_instance_entry(name, inst_data, path)

    return data


def save_instances_yaml(data: dict) -> None:
    """Save instances.yaml file atomically.

    Writes to a sibling temp file then ``os.replace`` swaps it into place.
    This avoids a partially-written (corrupt) instances.yaml if the process
    is interrupted mid-write, and narrows the read-modify-write race window
    when two named instances start concurrently and both call
    ``update_instances_yaml``: a reader can never observe a half-written
    file. (Interleaved writes from two concurrent updaters can still lose
    one update — that's guarded per-instance by ``InstanceLock``; this
    function guarantees the file is never left corrupted.)
    """
    import os
    import tempfile

    path = get_instances_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the SAME directory (so os.replace is atomic
    # on POSIX and a same-volume rename on Windows).
    fd, tmp_name = tempfile.mkstemp(
        prefix=".instances.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the orphan temp file on any failure; never leave it behind.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def create_instances_yaml_template() -> Path:
    """Create a minimal instances.yaml template file if not exists."""
    path = get_instances_yaml_path()
    if path.exists():
        return path

    template = """# JiuWenSwarm instances configuration
# Each instance has its own workspace, ports, and processes
#
# Example:
# instances:
#   alice:
#     # workspace: /custom/path/alice  # optional, defaults to ~/.jiuwenswarm-instances/alice
#     ports:
#       agent_server: 28092
#       web: 29000
#       gateway: 29001
#       frontend: 29173
#   bob:  # minimal declaration, all auto-allocated
#
# Instance name rules:
# - 1-64 alphanumeric/underscore/hyphen characters
# - Cannot start with dot
# - Reserved names: default, config, tmp
#
# Port auto-allocation: base + index * 1000
# - index 0: default instance (18092/19000/19001/5173)
# - index 1+: named instances

instances: {}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding='utf-8')
    logger.info("Created instances.yaml template: %s", path)
    return path


def update_instances_yaml(
    name: str, workspace: Path, ports: dict | None = None
) -> None:
    """Add or update instance entry in instances.yaml with full configuration.

    Args:
        name: Instance name
        workspace: Workspace path
        ports: Optional ports dict (if None, will be auto-allocated)
    """
    # Import locally to avoid circular dependency
    from jiuwenswarm.instance_manager.config import calculate_instance_ports

    data = load_instances_yaml()
    if "instances" not in data:
        data["instances"] = {}

    # Calculate ports if not provided
    if ports is None:
        # Get index for port calculation
        existing_names = list(data.get("instances", {}).keys())
        if name in existing_names:
            index = existing_names.index(name) + 1
        else:
            index = len(existing_names) + 1
        ports = calculate_instance_ports(index)

    # Write full configuration to YAML
    data["instances"][name] = {
        "workspace": str(workspace),
        "ports": ports,
    }
    save_instances_yaml(data)


def get_instance_index(name: str) -> int:
    """Get instance declaration order index (starts from 1, 0 reserved for default)."""
    data = load_instances_yaml()
    instances = list(data.get("instances", {}).keys())
    try:
        return instances.index(name) + 1
    except ValueError:
        # New instance not yet in yaml, will be appended
        return len(instances) + 1