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
import signal
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
    InstanceLock,
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
        # Legacy records stored wall-clock write time, not kernel creation
        # time.  Only schema v2 has a trustworthy birth-time identity.
        running = is_process_alive(pid) and (
            pid_data.get("schema_version") != 2
            or _pid_matches_record(pid, started_at)
        )

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

    stopped = not is_process_alive(pid)
    logger.info("Process %d %s", pid, "stopped" if stopped else "still alive")
    return stopped


def _pid_matches_record(pid: int, started_at: object) -> bool:
    """Check that a live PID is the process recorded in the PID file.

    PID values are reusable.  A successful ``kill(pid, 0)`` alone is not
    ownership proof, so POSIX group signalling is allowed only when psutil's
    process creation time agrees with our recorded launcher start time.
    """
    if not isinstance(started_at, (int, float)):
        return False
    try:
        import psutil

        return abs(psutil.Process(pid).create_time() - float(started_at)) < 3.0
    except Exception:
        return False


def _has_dedicated_session(pid_data: Dict[str, object], pid: int) -> bool:
    """Return whether launch-time metadata proves this is a private session."""
    launcher = pid_data.get("launcher")
    return (
        pid_data.get("schema_version") == 2
        and pid_data.get("dedicated_session") is True
        and pid_data.get("pgid") == pid
        and pid_data.get("sid") == pid
        and isinstance(launcher, dict)
        and launcher.get("pid") == pid
        and launcher.get("create_time") == pid_data.get("started_at")
    )


def _command_has_exact_dotenv(cmdline: list[str], expected_dotenv: object) -> bool:
    """Check the token following ``--dotenv`` against the recorded path."""
    if not isinstance(expected_dotenv, str):
        return False
    for index, token in enumerate(cmdline[:-1]):
        if token == "--dotenv" and os.path.realpath(cmdline[index + 1]) == expected_dotenv:
            return True
    return False


def _has_live_group_witness(pid_data: Dict[str, object], pid: int) -> bool:
    """Prove a dead launcher's private group still contains an owned child."""
    if not _has_dedicated_session(pid_data, pid):
        return False
    witnesses = pid_data.get("witnesses")
    if not isinstance(witnesses, list):
        return False
    try:
        import psutil

        for witness in witnesses:
            if not isinstance(witness, dict):
                continue
            witness_pid = witness.get("pid")
            create_time = witness.get("create_time")
            module = witness.get("module")
            if (
                not isinstance(witness_pid, int)
                or not isinstance(create_time, (int, float))
                or not isinstance(module, str)
            ):
                continue
            process = psutil.Process(witness_pid)
            cmdline = process.cmdline()
            if abs(process.create_time() - create_time) >= 3.0:
                continue
            if os.getpgid(witness_pid) != pid or os.getsid(witness_pid) != pid:
                continue
            if not any(
                cmdline[index:index + 2] == ["-m", module]
                for index in range(len(cmdline) - 1)
            ):
                continue
            if _command_has_exact_dotenv(cmdline, witness.get("dotenv")):
                return True
    except (OSError, psutil.Error):
        return False
    return False


def _listener_pids(config: InstanceConfig) -> list[int]:
    """Return PIDs listening on this instance's configured ports."""
    try:
        import psutil

        ports = {port for port in config.ports.values() if isinstance(port, int) and port > 0}
        pids: set[int] = set()
        for conn in psutil.net_connections(kind="inet"):
            laddr = getattr(conn, "laddr", None)
            if conn.status != "LISTEN" or not laddr:
                continue
            if getattr(laddr, "port", None) not in ports:
                continue
            if conn.pid:
                pids.add(conn.pid)
        return sorted(pids)
    except Exception as exc:  # platform permissions can prevent inspection
        logger.warning("Cannot inspect ports for instance '%s': %s", config.name, exc)
        return []


def _listener_matches_instance(
    pid: int, config: InstanceConfig, expected_pgid: Optional[int] = None
) -> bool:
    """Return whether a listener is provably one of this named instance's services."""
    try:
        import psutil

        cmdline = " ".join(psutil.Process(pid).cmdline())
        if "jiuwenswarm" not in cmdline:
            return False
        if str(config.get_bootstrap_env_path()) not in cmdline:
            return False
        return expected_pgid is None or os.getpgid(pid) == expected_pgid
    except Exception:
        # Failure to inspect ownership is not permission to kill.
        return False


def _find_owned_listener_pids(
    config: InstanceConfig, expected_pgid: Optional[int] = None
) -> list[int]:
    return [
        pid for pid in _listener_pids(config)
        if _listener_matches_instance(pid, config, expected_pgid)
    ]


def _ports_are_clear(config: InstanceConfig) -> bool:
    from jiuwenswarm.instance_manager.config import is_port_available

    return all(
        is_port_available("127.0.0.1", port)
        for port in config.ports.values()
        if isinstance(port, int) and port > 0
    )


def _stop_process_group(pgid: int, timeout: float) -> bool:
    """Terminate an already ownership-validated POSIX process group."""
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        logger.warning("SIGTERM to process group %d failed: %s", pgid, exc)
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        time.sleep(0.3)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        logger.warning("SIGKILL to process group %d failed: %s", pgid, exc)
        return False

    # SIGKILL is asynchronous.  Give the kernel a bounded reaping interval
    # before treating a still-visible group as a failed stop.
    kill_deadline = time.time() + 3.0
    while time.time() < kill_deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        time.sleep(0.1)
    return False


def _stop_owned_listeners(config: InstanceConfig, timeout: float) -> bool:
    """Surgically stop proven instance listeners when no group is available."""
    result = True
    for listener_pid in _find_owned_listener_pids(config):
        result = stop_process_by_pid(listener_pid, timeout=timeout) and result
    return result


def stop_instance_process(config: InstanceConfig, timeout: float = 10.0) -> bool:
    """Stop a named instance without signalling processes we cannot identify.

    Args:
        config: InstanceConfig for the instance
        timeout: Seconds to wait for graceful shutdown

    Returns:
        True if process stopped successfully, False otherwise

    The launcher holds ``InstanceLock`` for its lifetime, so stop must not
    acquire it: doing so would make a healthy instance impossible to stop.
    ``delete_pid_file(..., expected_data=...)`` below prevents a concurrent
    newer launch record from being removed after this stop finishes.
    """
    pid_data = read_pid_file(config)
    if pid_data is None:
        stopped = _stop_owned_listeners(config, timeout)
        return stopped and _ports_are_clear(config)

    pid = pid_data.get("pid", 0)
    if not isinstance(pid, int) or pid <= 0:
        return False

    system = platform.system().lower()
    logger.info("Stopping instance '%s' with PID %d", config.name, pid)

    current_format = pid_data.get("schema_version") == 2
    started_at = pid_data.get("started_at")
    if (
        current_format
        and is_process_alive(pid)
        and not _pid_matches_record(pid, started_at)
    ):
        logger.warning("PID %d does not match instance '%s' record", pid, config.name)
        return False

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
        stopped = not is_process_alive(pid)
    else:
        if is_process_alive(pid):
            if not current_format:
                # Legacy records cannot prove a dedicated process group.  Keep
                # their historical single-PID behavior for a live launcher.
                stopped = stop_process_by_pid(pid, timeout)
            else:
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    return False
                stopped = (
                    _stop_process_group(pgid, timeout)
                    if (
                        _has_dedicated_session(pid_data, pid)
                        and pgid == pid
                        and os.getsid(pid) == pid
                    )
                    else stop_process_by_pid(pid, timeout)
                )
        elif _has_live_group_witness(pid_data, pid):
            # The external ``setsid`` launcher has exited, but a listener
            # recorded at launch proves this exact instance still owns its
            # old dedicated group.  This avoids global port enumeration.
            stopped = _stop_process_group(pid, timeout)
        else:
            # There is no proven group.  A listener with the exact bootstrap
            # path can still be stopped individually; strangers are untouched.
            stopped = _stop_owned_listeners(config, timeout)

    success = stopped and _ports_are_clear(config)
    if success:
        # The lifetime launcher lock is released when the old group exits.
        # Take it only for final record retirement so a concurrent new start
        # cannot write a PID file between our equality check and unlink.
        lock = InstanceLock(config)
        if not lock.acquire(timeout=5.0):
            logger.warning("Could not acquire final lifecycle lock for instance '%s'", config.name)
            return False
        try:
            if not delete_pid_file(config, expected_data=pid_data):
                logger.warning("Instance '%s' PID record changed during stop", config.name)
                return False
        finally:
            lock.release()
        logger.info("Instance '%s' stopped", config.name)
    else:
        logger.warning("Instance '%s' could not be confirmed stopped", config.name)
    return success
