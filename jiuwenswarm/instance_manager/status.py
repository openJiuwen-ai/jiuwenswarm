# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instance status query and process control.

This module provides:
- Status query: get_instance_status, get_default_instance_status, list_all_instances
- Formatting: format_status_line
- Config loading: get_instance_config, load_all_instance_configs
- Process control: stop_process_by_pid, stop_instance_process
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from jiuwenswarm.instance_manager.config import (
    InstanceConfig,
    InstanceStatus,
    PORT_TYPES,
    compute_auto_port,
    _get_system_executable,
)
from jiuwenswarm.instance_manager.lock import (
    delete_pid_file,
    is_process_alive,
    read_pid_file,
)
from jiuwenswarm.instance_manager.yaml import (
    get_instance_workspace_path,
    get_instances_yaml_path,
    load_instances_yaml,
)

logger = logging.getLogger(__name__)


def get_instance_status(config: InstanceConfig) -> InstanceStatus:
    """Get runtime status of an instance.

    Args:
        config: InstanceConfig for the instance

    Returns:
        InstanceStatus with current state
    """
    pid_data = read_pid_file(config)

    if pid_data is None:
        return InstanceStatus(
            name=config.name,
            running=False,
            pid=None,
            workspace=config.workspace,
            ports=config.ports,
            started_at=None,
        )

    pid = pid_data.get("pid", 0)
    started_at = pid_data.get("started_at")

    if not isinstance(pid, int) or pid <= 0:
        running = False
    else:
        running = is_process_alive(pid)

    return InstanceStatus(
        name=config.name,
        running=running,
        pid=pid if running else None,
        workspace=config.workspace,
        ports=config.ports,
        started_at=started_at if running else None,
    )


def get_default_instance_status() -> InstanceStatus:
    """Get status of the default instance (workspace at ~/.jiuwenswarm).

    The default instance uses base ports (index 0) and standard workspace.
    For default instance, we check port availability to determine running status
    since PID file management may not be used.

    Returns:
        InstanceStatus for default instance
    """
    from jiuwenswarm.common.utils import get_user_workspace_dir

    workspace = get_user_workspace_dir()
    ports = {pt: compute_auto_port(pt, 0) for pt in PORT_TYPES}

    config = InstanceConfig(name="default", workspace=workspace, ports=ports)

    # First check PID file (if exists from previous named start)
    pid_data = read_pid_file(config)

    running = False
    pid = None
    started_at = None

    if pid_data is not None:
        pid = pid_data.get("pid", 0)
        started_at = pid_data.get("started_at")
        if isinstance(pid, int) and pid > 0 and is_process_alive(pid):
            running = True

    # If not running via PID file, check port availability
    # Default instance may be started without PID file management
    if not running:
        # Check if any of the main ports are occupied
        from jiuwenswarm.instance_manager.config import is_port_available

        for port_type in ["agent_server", "gateway", "frontend"]:
            port_num = ports.get(port_type, 0)
            if port_num > 0 and not is_port_available("127.0.0.1", port_num):
                pid = _find_pid_by_port(port_num)
                running = True
                break

    return InstanceStatus(
        name="default",
        running=running,
        pid=pid if running else None,
        workspace=workspace,
        ports=ports,
        started_at=started_at if running else None,
    )


def _find_pid_by_port(port: int) -> Optional[int]:
    """Find process PID that is listening on the given port.

    Args:
        port: Port number to check

    Returns:
        PID if found, None otherwise
    """
    if port <= 0:
        return None

    system = platform.system().lower()

    try:
        if system == "windows":
            # Use netstat to find PID
            result = subprocess.run(
                [
                    _get_system_executable("netstat"),
                    "-ano", "-p", "tcp"
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Look for lines like: "TCP    0.0.0.0:18092    0.0.0.0:0    LISTENING    12345"
            # or: "TCP    127.0.0.1:18092    0.0.0.0:0    LISTENING    12345"
            for line in result.stdout.splitlines():
                if "LISTENING" in line:
                    # Check for both 0.0.0.0 and 127.0.0.1 bindings
                    if f"0.0.0.0:{port}" in line or f"127.0.0.1:{port}" in line:
                        parts = line.split()
                        if parts:
                            last_part = parts[-1]
                            try:
                                return int(last_part)
                            except ValueError:
                                # Not a valid PID, continue to next line
                                continue
        else:
            # Unix: use lsof or ss
            try:
                result = subprocess.run(
                    [
                        _get_system_executable("lsof"),
                        "-i", f":{port}", "-t"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.stdout.strip():
                    return int(result.stdout.strip().split()[0])
            except (subprocess.SubprocessError, ValueError) as exc:
                logger.debug(
                    "lsof lookup failed for port %d (ignored): %s", port, exc
                )
    except Exception as exc:
        logger.debug("PID lookup by port %d failed (ignored): %s", port, exc)

    return None


def list_all_instances(include_default: bool = True) -> List[InstanceStatus]:
    """List status of all instances.

    Args:
        include_default: Whether to include default instance in the list

    Returns:
        List of InstanceStatus for all configured instances
    """
    statuses: List[InstanceStatus] = []

    # Add default instance first
    if include_default:
        statuses.append(get_default_instance_status())

    # Add named instances from instances.yaml
    yaml_path = get_instances_yaml_path()
    if yaml_path.exists():
        try:
            configs = load_all_instance_configs(yaml_path)
            for name, config in configs.items():
                statuses.append(get_instance_status(config))
        except Exception as exc:
            logger.warning("Failed to load instances.yaml: %s", exc)

    return statuses


def format_status_line(status: InstanceStatus) -> str:
    """Format an instance status for display.

    Args:
        status: InstanceStatus to format

    Returns:
        Formatted string for display
    """
    name = status.name

    if status.running:
        state = "running"
        pid_str = str(status.pid or "-")
    else:
        state = "stopped"
        pid_str = "-"

    workspace_str = str(status.workspace)

    ports_str = "/".join(
        str(status.ports.get(pt, 0)) for pt in PORT_TYPES if status.ports.get(pt)
    )

    return f"{name:<12} {state:<8} {pid_str:<7} {workspace_str:<40} {ports_str}"


def get_instance_config(name: str) -> Optional[InstanceConfig]:
    """Load instance configuration from instances.yaml.

    Args:
        name: Instance name to load

    Returns:
        InstanceConfig if instance exists, None otherwise
    """
    data = load_instances_yaml()
    if name not in data.get("instances", {}):
        return None

    instance_data = data["instances"][name] or {}
    instances = list(data.get("instances", {}).keys())
    index = instances.index(name) + 1

    # Determine workspace path
    workspace_str = instance_data.get("workspace")
    if workspace_str:
        workspace = Path(workspace_str)
    else:
        workspace = get_instance_workspace_path(name)

    # Determine ports
    ports_config = instance_data.get("ports", {})
    ports: Dict[str, int] = {}
    for port_type in PORT_TYPES:
        if port_type in ports_config:
            ports[port_type] = ports_config[port_type]
        else:
            ports[port_type] = compute_auto_port(port_type, index)

    return InstanceConfig(name=name, workspace=workspace, ports=ports)


def load_all_instance_configs(
    path: Optional[Path] = None
) -> Dict[str, InstanceConfig]:
    """Load all instance configurations from instances.yaml.

    Args:
        path: Path to instances.yaml, defaults to standard location

    Returns:
        Dict mapping instance name to InstanceConfig
    """
    if path is None:
        path = get_instances_yaml_path()

    if not path.exists():
        return {}

    data = load_instances_yaml()
    instances_data = data.get("instances", {})

    configs: Dict[str, InstanceConfig] = {}

    for index, (name, inst_data) in enumerate(instances_data.items(), start=1):
        # Determine workspace
        workspace_raw = (inst_data or {}).get("workspace")
        if workspace_raw:
            workspace = Path(workspace_raw).expanduser()
        else:
            workspace = get_instance_workspace_path(name)

        # Determine ports (auto-allocate if not specified)
        ports_data = (inst_data or {}).get("ports", {})
        ports: Dict[str, int] = {}
        for port_type in PORT_TYPES:
            if port_type in ports_data:
                ports[port_type] = ports_data[port_type]
            else:
                ports[port_type] = compute_auto_port(port_type, index)

        configs[name] = InstanceConfig(
            name=name,
            workspace=workspace,
            ports=ports,
        )

    return configs


def stop_process_by_pid(pid: int, timeout: float = 10.0) -> bool:
    """Stop a process by its PID directly.

    Args:
        pid: Process ID to stop
        timeout: Seconds to wait for graceful shutdown

    Returns:
        True if process stopped successfully, False otherwise
    """
    if pid <= 0:
        return True

    if not is_process_alive(pid):
        logger.info("Process %d already dead", pid)
        return True

    system = platform.system().lower()
    logger.info("Stopping process with PID %d", pid)

    if system == "windows":
        # Use taskkill to terminate process tree
        try:
            subprocess.run(
                [
                    _get_system_executable("taskkill"),
                    "/PID", str(pid), "/T", "/F"
                ],
                capture_output=True,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("taskkill failed for PID %d: %s", pid, exc)
    else:
        # Unix: SIGTERM then SIGKILL
        try:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(2)
            if is_process_alive(pid):
                os.kill(pid, 9)  # SIGKILL
        except OSError as exc:
            logger.warning("kill failed for PID %d: %s", pid, exc)

    # Wait for process to exit
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.5)

    logger.info("Process %d stopped", pid)
    return True


def stop_instance_process(config: InstanceConfig, timeout: float = 10.0) -> bool:
    """Stop a running instance process.

    Args:
        config: InstanceConfig for the instance
        timeout: Seconds to wait for graceful shutdown

    Returns:
        True if process stopped successfully, False otherwise
    """
    pid_data = read_pid_file(config)
    if pid_data is None:
        logger.info(
            "No PID file found for instance '%s', already stopped", config.name
        )
        return True

    pid = pid_data.get("pid", 0)
    if not isinstance(pid, int) or pid <= 0:
        delete_pid_file(config)
        return True

    if not is_process_alive(pid):
        logger.info(
            "Process %d already dead for instance '%s'", pid, config.name
        )
        delete_pid_file(config)
        return True

    system = platform.system().lower()
    logger.info("Stopping instance '%s' with PID %d", config.name, pid)

    if system == "windows":
        # Use taskkill to terminate process tree
        try:
            subprocess.run(
                [
                    _get_system_executable("taskkill"),
                    "/PID", str(pid), "/T", "/F"
                ],
                capture_output=True,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("taskkill failed for PID %d: %s", pid, exc)
    else:
        # Unix: SIGTERM then SIGKILL
        try:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(2)
            if is_process_alive(pid):
                os.kill(pid, 9)  # SIGKILL
        except OSError as exc:
            logger.warning("kill failed for PID %d: %s", pid, exc)

    # Wait for process to exit
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.5)

    # Cleanup PID file
    delete_pid_file(config)
    logger.info("Instance '%s' stopped", config.name)
    return True