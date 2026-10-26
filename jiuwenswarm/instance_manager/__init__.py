# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instance manager for multi-instance isolation.

Provides utilities for:
- Instance name validation
- instances.yaml configuration management
- Bootstrap .env file creation
- Port allocation and conflict detection
- PID file management
- Instance status querying
- Instance startup locking for concurrency safety

Design reference: multi-instance-isolation-design.md

This package is organized into submodules:
- config: Data classes, constants, name validation, port management
- yaml: YAML configuration parsing and writing
- lock: Instance lock and PID file management
- status: Status query, config loading, process control
- bootstrap: Bootstrap .env creation

All public APIs are re-exported from this module for backward compatibility.
"""

from __future__ import annotations

# Data classes and constants from config
from jiuwenswarm.instance_manager.config import (
    InstanceConfig,
    InstanceStatus,
    InstancesYamlError,
    BASE_PORTS,
    INSTANCE_NAME_PATTERN,
    PID_FILENAME,
    PORT_TYPES,
    RESERVED_NAMES,
)

# Name validation from config
from jiuwenswarm.instance_manager.config import (
    is_valid_instance_name,
    validate_instance_name,
)

# Port management from config
from jiuwenswarm.instance_manager.config import (
    calculate_instance_ports,
    check_port_conflicts,
    collect_all_ports,
    compute_auto_port,
    find_available_ports,
    is_port_available,
    PORT_ENV_NAMES,
    PORT_ENV_OVERRIDES,
)

# YAML management from yaml
from jiuwenswarm.instance_manager.yaml import (
    create_instances_yaml_template,
    get_instance_index,
    get_instance_workspace_path,
    get_instances_dir,
    get_instances_yaml_path,
    load_instances_yaml,
    save_instances_yaml,
    update_instances_yaml,
)

# Instance lock and PID management from lock
from jiuwenswarm.instance_manager.lock import (
    InstanceLock,
    LOCK_FILENAME,
    STALE_LOCK_TIMEOUT,
    check_instance_running,
    delete_pid_file,
    is_process_alive,
    read_pid_file,
    write_pid_file,
)

# Status query and process control from status
from jiuwenswarm.instance_manager.status import (
    format_status_line,
    get_default_instance_status,
    get_instance_config,
    get_instance_status,
    list_all_instances,
    load_all_instance_configs,
    stop_instance_process,
    stop_process_by_pid,
)

# Bootstrap .env creation from bootstrap
from jiuwenswarm.instance_manager.bootstrap import (
    create_bootstrap_env,
    create_bootstrap_env_for_name,
    load_instance_bootstrap_by_name,
)

__all__ = [
    # Data classes
    "InstanceConfig",
    "InstanceStatus",
    "InstancesYamlError",
    # Constants
    "BASE_PORTS",
    "INSTANCE_NAME_PATTERN",
    "PID_FILENAME",
    "PORT_TYPES",
    "RESERVED_NAMES",
    "LOCK_FILENAME",
    "STALE_LOCK_TIMEOUT",
    # Name validation
    "validate_instance_name",
    "is_valid_instance_name",
    # Port management
    "compute_auto_port",
    "calculate_instance_ports",
    "is_port_available",
    "check_port_conflicts",
    "collect_all_ports",
    "find_available_ports",
    "PORT_ENV_NAMES",
    "PORT_ENV_OVERRIDES",
    # YAML management
    "get_instances_yaml_path",
    "get_instances_dir",
    "get_instance_workspace_path",
    "load_instances_yaml",
    "save_instances_yaml",
    "create_instances_yaml_template",
    "update_instances_yaml",
    "get_instance_index",
    # Instance lock and PID
    "InstanceLock",
    "write_pid_file",
    "read_pid_file",
    "delete_pid_file",
    "is_process_alive",
    "check_instance_running",
    # Status query
    "get_instance_status",
    "get_default_instance_status",
    "list_all_instances",
    "format_status_line",
    "get_instance_config",
    "load_all_instance_configs",
    # Process control
    "stop_process_by_pid",
    "stop_instance_process",
    # Bootstrap .env
    "create_bootstrap_env",
    "create_bootstrap_env_for_name",
    "load_instance_bootstrap_by_name",
]