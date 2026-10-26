# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instance configuration, constants, name validation, and port management.

This module provides:
- Data classes: InstanceConfig, InstanceStatus, InstancesYamlError
- Constants: BASE_PORTS, PORT_TYPES, INSTANCE_NAME_PATTERN, RESERVED_NAMES
- Name validation: validate_instance_name, is_valid_instance_name
- Port management: compute_auto_port, calculate_instance_ports, is_port_available, etc.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class InstancesYamlError(Exception):
    """Custom exception for instances.yaml parsing/validation errors.

    Provides user-friendly error messages with actionable fix suggestions.
    """

    def __init__(self, message: str):
        # Prepend consistent header for CLI display
        super().__init__(f"[instances.yaml] {message}")


# Instance name validation rules
INSTANCE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
RESERVED_NAMES = frozenset({'default', 'config', 'tmp', 'jiuwenswarm', 'all'})

# Base ports for default instance (index=0)
BASE_PORTS = {
    "agent_server": 18092,
    "web": 19000,
    "gateway": 19001,
    "frontend": 5173,
    "http_api": 8766,
}

# Port types that must be unique across instances
PORT_TYPES = ("agent_server", "web", "gateway", "frontend", "http_api")

# PID file name in workspace directory
PID_FILENAME = ".instance.pid"


@dataclass
class InstanceConfig:
    """Configuration for a named instance.

    Attributes:
        name: Instance name (unique identifier)
        workspace: Path to instance workspace directory
        ports: Dict of port assignments for each service type
    """
    name: str
    workspace: Path
    ports: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Expand and resolve workspace path."""
        self.workspace = Path(self.workspace).expanduser().resolve()

    def get_pid_file_path(self) -> Path:
        """Get the PID file path for this instance."""
        return self.workspace / PID_FILENAME

    def get_bootstrap_env_path(self) -> Path:
        """Get the bootstrap .env file path for this instance."""
        return self.workspace / ".env"


@dataclass
class InstanceStatus:
    """Runtime status of an instance.

    Attributes:
        name: Instance name
        running: Whether the instance is currently running
        pid: Process ID if running, None otherwise
        workspace: Path to instance workspace
        ports: Dict of port assignments
        started_at: Startup timestamp if running, None otherwise
    """
    name: str
    running: bool
    pid: Optional[int]
    workspace: Path
    ports: Dict[str, int]
    started_at: Optional[float] = None


def validate_instance_name(name: str) -> Optional[str]:
    """Validate instance name, return error message or None if valid.

    Rules:
    - Length: 1-64 characters
    - Pattern: letters, digits, underscore, hyphen only
    - No leading dot
    - Not a reserved name
    """
    if not name or not isinstance(name, str):
        return "Instance name must be a non-empty string"
    if len(name) < 1 or len(name) > 64:
        return "Instance name must be 1-64 characters"
    if not INSTANCE_NAME_PATTERN.match(name):
        return "Instance name must contain only letters, digits, underscore, hyphen"
    if name.startswith('.'):
        return "Instance name cannot start with dot"
    if name.lower() in RESERVED_NAMES:
        return f"Instance name '{name}' is reserved"
    return None


def is_valid_instance_name(name: str) -> bool:
    """Check if instance name is valid (returns bool)."""
    return validate_instance_name(name) is None


# Environment variable names that override BASE_PORTS, enabling Docker /
# container deployments and ad-hoc base-port customization. When unset,
# behavior is identical to the historical fixed defaults (backward compatible).
PORT_ENV_OVERRIDES = {
    "agent_server": "JIUWENSWARM_AGENT_SERVER_PORT",
    "web": "JIUWENSWARM_WEB_PORT",
    "gateway": "JIUWENSWARM_GATEWAY_PORT",
    "frontend": "JIUWENSWARM_FRONTEND_PORT",
    "http_api": "JIUWENSWARM_HTTP_API_PORT",
}


def _resolved_base_port(port_type: str) -> int:
    """Return the effective base port for a port type.

    Honors ``JIUWENSWARM_<TYPE>_PORT`` env-var overrides (Phase 2 / Docker),
    falling back to the static ``BASE_PORTS`` default. Malformed env values are
    ignored with a debug log so a typo never breaks startup.
    """
    env_key = PORT_ENV_OVERRIDES.get(port_type)
    if env_key:
        raw = os.getenv(env_key)
        if raw:
            try:
                return int(raw)
            except ValueError:
                logger.debug(
                    "Ignoring invalid %s=%r (not an int), using default",
                    env_key, raw,
                )
    return BASE_PORTS.get(port_type, 10000)


def compute_auto_port(port_type: str, index: int) -> int:
    """Compute auto-allocated port for an instance.

    Algorithm: base_port + index * 1000
    - index 0 reserved for default instance
    - Named instances start from index 1

    The base_port honors ``JIUWENSWARM_<TYPE>_PORT`` env-var overrides
    (see ``PORT_ENV_OVERRIDES``); when unset the historical fixed default is
    used, so existing deployments are unaffected.

    Args:
        port_type: Service type (agent_server, web, gateway, frontend)
        index: Instance index (0 for default, 1+ for named)

    Returns:
        Computed port number
    """
    return _resolved_base_port(port_type) + index * 1000


def calculate_instance_ports(index: int) -> Dict[str, int]:
    """Calculate ports for an instance: base_port + index * 1000.

    Base ports honor env-var overrides (see ``compute_auto_port``).
    """
    return {k: _resolved_base_port(k) + index * 1000 for k in BASE_PORTS}


def _bind_listen_probe(host: str, port: int, family: int) -> bool | None:
    """Try bind()+listen() on ``host:port``.

    Returns:
        True if the port can be bound, False if occupied, None if this address
        family / host is unsupported on the machine (caller should ignore).
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return None
    # Intentionally NOT setting SO_REUSEADDR: on Windows it permits multiple
    # sockets to bind the same port, which would mask an occupied port and
    # reproduce the very 10048 crash we are trying to avoid. The real services
    # do not set it either.
    try:
        sock.bind((host, port))
        sock.listen(1)
        return True
    except OSError as exc:
        # Windows: WinError 10022/10049; POSIX: EADDRNOTAVAIL / EAFNOSUPPORT
        # when IPv6 is disabled — treat as "family unavailable", not occupied.
        winerr = getattr(exc, "winerror", None)
        errno = getattr(exc, "errno", None)
        if winerr in (10022, 10049) or errno in (
            getattr(socket, "EADDRNOTAVAIL", 99),
            getattr(socket, "EAFNOSUPPORT", 97),
            getattr(socket, "EPROTONOSUPPORT", 93),
        ):
            return None
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def is_port_available(host: str, port: int) -> bool:
    """Check if a port is available for binding on the given host.

    Probes by attempting to ``bind()``+``listen()`` (without SO_REUSEADDR) and
    immediately closing. This mirrors how the real services (AgentServer /
    Gateway / Web / Frontend) actually acquire their ports, so it correctly
    detects:

    - A live service listening on the port (bind fails -> occupied).
    - A *stuck/zombie* listener that is in LISTENING state but no longer
      accept()ing connections (connect()-based probes falsely report these as
      free because the connect times out, but bind() still fails).

    Earlier connect()-based probes mis-detected stuck listeners as available,
    which made the fallback logic pick an index whose ports were actually
    held by a dead-but-listening socket, causing the real service to crash
    with ``OSError [Errno 10048]`` on its own bind.

    When ``host`` is IPv4 loopback / wildcard, also probes ``::1``: Vite on
    Windows often listens on ``[::1]:5173`` only. An IPv4-only probe would
    report the port free, skip ``jiuwenswarm-start`` port-group fallback, and
    then ``strictPort: true`` would fail with "Port 5173 is already in use".

    Args:
        host: Host address to check
        port: Port number to check

    Returns:
        True if the port can be bound (available), False if occupied.
    """
    family = socket.AF_INET6 if ":" in host and host.count(":") >= 2 else socket.AF_INET
    primary = _bind_listen_probe(host, port, family)
    if primary is False:
        return False
    if primary is None and family == socket.AF_INET6:
        # Explicit IPv6 host but stack unavailable — fall back to IPv4 view.
        return bool(_bind_listen_probe("127.0.0.1", port, socket.AF_INET))

    # Dual-stack localhost: Vite / Node frequently bind ::1 only.
    if host in ("127.0.0.1", "0.0.0.0", "", "localhost"):
        ipv6 = _bind_listen_probe("::1", port, socket.AF_INET6)
        if ipv6 is False:
            return False
    return primary is not False


def check_port_conflicts(
    ports: Dict[str, int],
    host: str = "127.0.0.1",
    existing_ports: Optional[Sequence[int]] = None,
) -> List[int]:
    """Check for port conflicts.

    Args:
        ports: Dict of port assignments to check
        host: Host address to check
        existing_ports: Ports already used by other instances

    Returns:
        List of conflicting port numbers
    """
    conflicts: List[int] = []
    check_set = set(existing_ports or [])

    for port_type, port in ports.items():
        if port in check_set:
            conflicts.append(port)
            continue
        if not is_port_available(host, port):
            conflicts.append(port)

    return conflicts


def collect_all_ports(exclude_name: Optional[str] = None) -> List[int]:
    """Collect all ports used by all instances for conflict detection.

    Args:
        exclude_name: Instance name to exclude from collection

    Returns:
        List of port numbers used by other instances
    """
    # Import locally to avoid circular dependency
    from jiuwenswarm.instance_manager.yaml import load_instances_yaml

    ports: List[int] = []

    # Default instance ports
    if exclude_name != "default":
        for port_type in PORT_TYPES:
            ports.append(compute_auto_port(port_type, 0))

    # Named instance ports from instances.yaml
    try:
        data = load_instances_yaml()
        instances = data.get("instances", {})
        for name, inst_data in instances.items():
            if name == exclude_name:
                continue
            index = list(instances.keys()).index(name) + 1
            ports_config = (inst_data or {}).get("ports", {})
            for port_type in PORT_TYPES:
                if port_type in ports_config:
                    ports.append(ports_config[port_type])
                else:
                    ports.append(compute_auto_port(port_type, index))
    except Exception as exc:
        logger.debug(
            "Failed to load instance ports from yaml (ignored): %s", exc
        )

    return ports


def _get_system_executable(name: str) -> str:
    """Get absolute path for system executable.

    Uses System32 path on Windows for common system utilities.
    Falls back to shutil.which() for cross-platform resolution.

    Args:
        name: Executable name (e.g., 'tasklist', 'netstat', 'lsof')

    Returns:
        Absolute path to executable, or name if not found
    """
    import shutil

    system = platform.system().lower()
    if system == "windows":
        # Windows system executables are in System32
        system32 = os.path.join(
            os.environ.get("SystemRoot", "C:\\Windows"), "System32"
        )
        exe_path = os.path.join(system32, f"{name}.exe")
        if os.path.exists(exe_path):
            return exe_path
    # Fallback to shutil.which for cross-platform resolution
    resolved = shutil.which(name)
    return resolved or name


# Port-type to bootstrap .env variable name mapping. Kept here (next to the
# port allocation logic) so fallback persistence and bootstrap .env creation
# stay in sync. Must match jiuwenswarm/instance_manager/bootstrap.py.
PORT_ENV_NAMES = {
    "agent_server": "AGENT_SERVER_PORT",
    "web": "WEB_PORT",
    "gateway": "GATEWAY_PORT",
    "frontend": "FRONTEND_PORT",
    # http_api 参与端口分配与冲突扫描（见 BASE_PORTS / PORT_TYPES），因此也必须
    # 下发到进程 —— 否则实例预留了端口却收不到，开启 HTTP 时会回落到默认 8766。
    # 是否真正监听由 config.yaml 的 http_server.enabled 决定，与本端口无关。
    "http_api": "AGENT_HTTP_PORT",
}


def find_available_ports(
    base_index: int = 0,
    host: str = "127.0.0.1",
    scan_range: int = 10,
    exclude_ports: Optional[Sequence[int]] = None,
) -> Optional[tuple]:
    """Scan upward for the first fully-available port group.

    Starting at ``base_index``, walks ``scan_range`` consecutive instance
    indices. For each index it computes the full 4-port group via
    ``calculate_instance_ports``; the group is usable only when *every* port
    is available on ``host`` AND not in ``exclude_ports`` (ports already
    claimed by other live instances).

    Args:
        base_index: Instance index to start scanning from (typically the
            instance's own index).
        host: Host address to probe.
        scan_range: Number of consecutive indices to try.
        exclude_ports: Ports to treat as occupied regardless of probe result
            (avoids colliding with other configured instances).

    Returns:
        ``(ports_dict, actual_index)`` for the first usable group, or ``None``
        if the entire scan range is exhausted.
    """
    exclude_set = set(exclude_ports or [])

    # scan_range=0 means "scan nothing" → immediately None. (Previously this
    # was clamped via max(1, scan_range), which silently scanned one index
    # even when the caller asked for zero — making the exhausted-range error
    # message in start_services.py ("scanned indices base..base+range-1")
    # describe a range that did not match what was actually scanned.)
    for offset in range(scan_range):
        index = base_index + offset
        candidate = calculate_instance_ports(index)

        if all(
            p not in exclude_set and is_port_available(host, p)
            for p in candidate.values()
        ):
            return candidate, index

    return None


def _upsert_env_ports(
    env_path: Path,
    ports: Dict[str, int],
) -> None:
    """Idempotently write port assignments into a .env file.

    For each port type in ``PORT_ENV_NAMES``: if a ``KEY=...`` line exists it
    is replaced in place, otherwise the line is appended. All other lines
    (API keys, model config, etc.) are preserved untouched. Creates the file
    if missing.

    This is the persistence mechanism for the default instance (whose ports
    are read from ``~/.jiuwenswarm/config/.env`` via ``app.py``'s
    ``load_dotenv(get_env_file(), override=True)``). Named instances persist
    via ``instances.yaml`` + ``create_bootstrap_env`` instead.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)

    target_keys = {env_name: str(ports[pt]) for pt, env_name in PORT_ENV_NAMES.items() if pt in ports}

    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    seen: set = set()
    updated: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in target_keys:
                updated.append(f"{key}={target_keys[key]}")
                seen.add(key)
                continue
        updated.append(line)

    # Append any port keys not already present in the file
    for key, val in target_keys.items():
        if key not in seen:
            updated.append(f"{key}={val}")

    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    logger.debug("Upserted port env vars into %s: %s", env_path, list(target_keys))


def _format_url_hint(ports: Dict[str, int]) -> str:
    """Build the TUI/CLI connection hint shown after a port fallback.

    Emits ``jiuwenswarm-tui --url ...`` (explicit, works even if the TUI cannot
    read the persisted GATEWAY_PORT) and ``jiuwenswarm chat`` (which reads
    GATEWAY_PORT from the loaded .env automatically). Returns a multi-line
    string without a trailing newline.
    """
    gateway_port = ports.get("gateway", 0)
    lines = [
        "[start_services] TUI/CLI connect with:",
        f"  jiuwenswarm-tui --url ws://127.0.0.1:{gateway_port}/tui",
        "  jiuwenswarm chat   (auto-reads GATEWAY_PORT)",
    ]
    return "\n".join(lines)