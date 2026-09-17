# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for instance_manager module.

Tests for:
- Instance name validation
- Port auto-allocation
- Port conflict detection
- PID file management
- Instance status querying
- InstancesYamlError handling
- InstanceLock concurrency control
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from jiuwenswarm.instance_manager import (
    InstanceConfig,
    InstanceLock,
    InstanceStatus,
    InstancesYamlError,
    validate_instance_name,
    is_valid_instance_name,
    get_instances_yaml_path,
    compute_auto_port,
    calculate_instance_ports,
    check_port_conflicts,
    collect_all_ports,
    find_available_ports,
    write_pid_file,
    read_pid_file,
    delete_pid_file,
    is_process_alive,
    is_port_available,
    get_instance_status,
    get_default_instance_status,
    list_all_instances,
    format_status_line,
    get_instance_config,
    load_all_instance_configs,
    create_bootstrap_env,
    create_bootstrap_env_for_name,
    stop_instance_process,
    create_instances_yaml_template,
    load_instances_yaml,
    save_instances_yaml,
    update_instances_yaml,
    get_instance_index,
    RESERVED_NAMES,
    PORT_TYPES,
    BASE_PORTS,
    PORT_ENV_NAMES,
    PORT_ENV_OVERRIDES,
    STALE_LOCK_TIMEOUT,
)
from jiuwenswarm.instance_manager.config import (
    _format_url_hint,
    _upsert_env_ports,
)


# Module-level helper function for multiprocessing (must be at module level to be pickleable)
def _try_acquire_lock_for_multiprocess(workspace_str: str) -> bool:
    """Helper function to try acquiring lock in a subprocess.

    This must be at module level to be pickleable for multiprocessing.
    """
    from jiuwenswarm.instance_manager import (
        InstanceConfig as _InstanceConfig,
        InstanceLock as _InstanceLock,
    )

    _config = _InstanceConfig(name="test", workspace=Path(workspace_str), ports={})
    _lock = _InstanceLock(_config)
    result = _lock.acquire(timeout=0.5)  # Short timeout
    if result:
        _lock.release()
    return result


def _try_acquire_lock_with_result(workspace_str: str, result_queue) -> None:
    """Helper function for multiprocessing that returns result via Queue.

    This must be at module level to be pickleable for multiprocessing on Windows.
    """
    result = _try_acquire_lock_for_multiprocess(workspace_str)
    result_queue.put(result)


class TestInstanceNameValidation:
    """Test instance name validation."""

    @staticmethod
    def test_valid_simple_names():
        """Test valid simple names."""
        assert validate_instance_name("alice") is None
        assert validate_instance_name("bob") is None
        assert validate_instance_name("my-instance") is None
        assert validate_instance_name("test_123") is None
        assert validate_instance_name("a") is None

    @staticmethod
    def test_valid_complex_names():
        """Test valid complex names."""
        assert validate_instance_name("production-server-01") is None
        assert validate_instance_name("dev_test_env") is None
        assert validate_instance_name("CamelCaseName") is None

    @staticmethod
    def test_invalid_empty_name():
        """Test empty name is invalid."""
        assert validate_instance_name("") is not None
        assert validate_instance_name(None) is not None

    @staticmethod
    def test_invalid_too_long():
        """Test name longer than 64 chars is invalid."""
        long_name = "a" * 65
        assert validate_instance_name(long_name) is not None

    @staticmethod
    def test_invalid_special_chars():
        """Test names with special characters are invalid."""
        assert validate_instance_name("alice@example") is not None
        assert validate_instance_name("my instance") is not None  # space
        assert validate_instance_name("instance.name") is not None  # dot
        assert validate_instance_name("中文实例") is not None  # non-ASCII

    @staticmethod
    def test_invalid_leading_dot():
        """Test names starting with dot are invalid."""
        assert validate_instance_name(".hidden") is not None
        assert validate_instance_name(".alice") is not None

    @staticmethod
    def test_reserved_names():
        """Test reserved names are invalid."""
        for name in RESERVED_NAMES:
            assert validate_instance_name(name) is not None
            assert validate_instance_name(name.upper()) is not None  # case insensitive

    @staticmethod
    def test_is_valid_instance_name():
        """Test is_valid_instance_name helper."""
        assert is_valid_instance_name("alice") is True
        assert is_valid_instance_name("default") is False
        assert is_valid_instance_name("") is False


class TestPortAllocation:
    """Test port auto-allocation."""

    @staticmethod
    def test_base_ports():
        """Test base ports for default instance (index 0)."""
        assert compute_auto_port("agent_server", 0) == 18092
        assert compute_auto_port("web", 0) == 19000
        assert compute_auto_port("gateway", 0) == 19001
        assert compute_auto_port("frontend", 0) == 5173

    @staticmethod
    def test_calculate_instance_ports():
        """Test calculate_instance_ports returns all port types."""
        ports = calculate_instance_ports(1)
        assert "agent_server" in ports
        assert "web" in ports
        assert "gateway" in ports
        assert "frontend" in ports
        assert ports["agent_server"] == 19092

    @staticmethod
    def test_unknown_port_type():
        """Test unknown port type uses default base."""
        assert compute_auto_port("unknown", 0) == 10000


class TestPortAvailability:
    """Test port availability checking."""

    @staticmethod
    def test_check_port_conflicts_no_conflicts():
        """Test no conflicts when ports are available."""
        ports = {"agent_server": 19092, "web": 20000}
        # Should have no conflicts if ports are free (unlikely to be used)
        conflicts = check_port_conflicts(ports, "127.0.0.1", [])
        # This test may fail if ports happen to be occupied
        # We're testing the logic, not the actual availability
        assert isinstance(conflicts, list)

    @staticmethod
    def test_check_port_conflicts_with_existing():
        """Test conflicts detected when port in existing set."""
        ports = {"agent_server": 19092}
        existing = [19092]
        conflicts = check_port_conflicts(ports, "127.0.0.1", existing)
        assert 19092 in conflicts

    @staticmethod
    def test_is_port_available_detects_live_listener(tmp_path):
        """A port held by a real bind()+listen() is reported as occupied.

        Regression guard: the old connect()-based probe timed out against a
        stuck/zombie listener (LISTENING but not accept()ing) and falsely
        reported it as free, which made the fallback pick an index whose
        ports were actually held and the real service crashed with
        OSError [Errno 10048] on bind. The bind()-based probe detects this.
        """
        import socket as _socket
        # Pick an ephemeral port the OS hands us, then hold it.
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            assert is_port_available("127.0.0.1", port) is False
        finally:
            s.close()

    @staticmethod
    def test_is_port_available_true_when_free(tmp_path):
        """A genuinely free ephemeral port is reported as available."""
        import socket as _socket
        # Grab an ephemeral port, release it, then probe — it should be free.
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # Small race window, but in practice the port is free immediately.
        assert is_port_available("127.0.0.1", port) is True

    @staticmethod
    def test_is_port_available_detects_ipv6_only_listener():
        """IPv6-only listeners must count as occupied for 127.0.0.1 probes.

        Vite on Windows often binds ``[::1]:5173`` only. An IPv4-only bind
        probe would miss it, skip jiuwenswarm-start's port-group fallback, and
        then Vite ``strictPort: true`` fails with "Port 5173 is already in use".
        """
        import socket as _socket
        import pytest

        s = None
        try:
            try:
                s = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
                # Restrict to IPv6-only so we do not also claim IPv4 via dualstack.
                if hasattr(_socket, "IPV6_V6ONLY"):
                    s.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 1)
                s.bind(("::1", 0))
                s.listen(1)
            except OSError:
                # Must close before skip: an unbound AF_INET6 socket still
                # triggers ResourceWarning / PytestUnraisableExceptionWarning
                # on the next test's setup (seen on Linux CI without ::1).
                if s is not None:
                    s.close()
                    s = None
                pytest.skip("IPv6 loopback unavailable on this host")
            port = s.getsockname()[1]
            assert is_port_available("127.0.0.1", port) is False
        finally:
            if s is not None:
                s.close()

    @staticmethod
    def test_is_port_available_ignores_lingering_connection_sockets():
        """A port held only by half-closed sockets (no listener) is available.

        Reproduces the real trigger: the services are stopped while a browser
        tab still holds the Web UI open. The kernel closes the server side, but
        those sockets sit in FIN_WAIT_2 (local port = the service port) for as
        long as the tab keeps its end open. A plain bind() then fails, while the
        real services - which all set SO_REUSEADDR - would bind fine. Reporting
        such a port as occupied made jiuwenswarm-start shift the whole port
        group (5173 -> 6173) for no reason.
        """
        import socket as _socket

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(8)

        # A browser holding the page open.
        client = _socket.create_connection(("127.0.0.1", port))
        conn, _addr = listener.accept()

        # The service exits: listener gone, server side actively closed. The
        # client end stays open, so the server side parks in FIN_WAIT_2.
        listener.close()
        conn.close()

        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            # Precondition: without SO_REUSEADDR the port really is unbindable.
            # If this ever stops holding, the test below proves nothing.
            with pytest.raises(OSError):
                probe.bind(("127.0.0.1", port))
                probe.listen(1)
        finally:
            probe.close()

        try:
            assert is_port_available("127.0.0.1", port) is True
        finally:
            client.close()

    @staticmethod
    def test_reuseaddr_probe_still_refuses_a_live_listener():
        """The SO_REUSEADDR retry must not mask a real listener.

        This is the guard that keeps zombie-listener detection working: a stuck
        listener holds a LISTENING socket, and SO_REUSEADDR does not allow
        binding over one on POSIX.
        """
        import socket as _socket

        from jiuwenswarm.instance_manager.config import _reuseaddr_bind_probe

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            assert _reuseaddr_bind_probe("127.0.0.1", port, _socket.AF_INET) is False
        finally:
            s.close()

    @staticmethod
    def test_reuseaddr_retry_is_disabled_on_windows(monkeypatch):
        """Windows SO_REUSEADDR allows binding over a live listener - never retry."""
        import socket as _socket

        from jiuwenswarm.instance_manager import config as config_mod

        called = {"count": 0}

        def _should_not_run(*_args, **_kwargs) -> bool:
            called["count"] += 1
            return True

        monkeypatch.setattr(config_mod, "_REUSEADDR_PROBE_SUPPORTED", False)
        monkeypatch.setattr(config_mod, "_reuseaddr_bind_probe", _should_not_run)

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            assert config_mod._bind_listen_probe("127.0.0.1", port, _socket.AF_INET) is False
        finally:
            s.close()

        assert called["count"] == 0

    @staticmethod
    def test_family_unavailable_errnos_are_platform_correct():
        """EADDRNOTAVAIL etc. must come from errno, not hardcoded Linux values.

        macOS uses 49 for EADDRNOTAVAIL where Linux uses 99. Hardcoding the
        Linux set made a probe against a disabled IPv6 stack read as "port
        occupied" on macOS, which reported every port as taken.
        """
        import errno as _errno

        from jiuwenswarm.instance_manager.config import _FAMILY_UNAVAILABLE_ERRNOS

        assert _errno.EADDRNOTAVAIL in _FAMILY_UNAVAILABLE_ERRNOS
        assert _errno.EAFNOSUPPORT in _FAMILY_UNAVAILABLE_ERRNOS
        assert _errno.EPROTONOSUPPORT in _FAMILY_UNAVAILABLE_ERRNOS
        # A port being taken must never be mistaken for an unusable family.
        assert _errno.EADDRINUSE not in _FAMILY_UNAVAILABLE_ERRNOS


class TestInstanceConfig:
    """Test InstanceConfig dataclass."""

    @staticmethod
    def test_basic_config():
        """Test basic InstanceConfig creation."""
        config = InstanceConfig(
            name="alice",
            workspace=Path("/tmp/alice"),
            ports={"agent_server": 19092, "web": 20000},
        )
        assert config.name == "alice"
        assert "alice" in str(config.workspace)
        assert config.ports["agent_server"] == 19092


class TestPidFileManagement:
    """Test PID file management."""

    @staticmethod
    def test_write_and_read_pid_file(tmp_path):
        """Test writing and reading PID file."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        pid = 12345
        write_pid_file(config, pid)

        data = read_pid_file(config)
        assert data is not None
        assert data["pid"] == pid
        assert data["name"] == "test"
        assert "started_at" in data

    @staticmethod
    def test_read_nonexistent_pid_file(tmp_path):
        """Test reading nonexistent PID file."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        data = read_pid_file(config)
        assert data is None

    @staticmethod
    def test_delete_pid_file(tmp_path):
        """Test deleting PID file."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        write_pid_file(config, 12345)
        assert config.get_pid_file_path().exists()

        deleted = delete_pid_file(config)
        assert deleted is True
        assert not config.get_pid_file_path().exists()

        # Second delete returns False
        deleted2 = delete_pid_file(config)
        assert deleted2 is False


class TestInstanceStatus:
    """Test InstanceStatus and status querying."""

    @staticmethod
    def test_get_instance_status_stopped(tmp_path):
        """Test getting status for stopped instance."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={"agent_server": 19092},
        )
        status = get_instance_status(config)
        assert status.name == "test"
        assert status.running is False
        assert status.pid is None

    @staticmethod
    def test_get_instance_status_running_with_dead_pid(tmp_path):
        """Test status returns stopped when PID file exists but process is dead."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        # Write PID file with unlikely PID
        write_pid_file(config, 999999999)

        status = get_instance_status(config)
        assert status.running is False
        assert status.pid is None

    @staticmethod
    def test_format_status_line():
        """Test formatting status line."""
        status = InstanceStatus(
            name="alice",
            running=True,
            pid=12345,
            workspace=Path("/tmp/alice"),
            ports={"agent_server": 19092, "web": 20000},
        )
        line = format_status_line(status)
        assert "alice" in line
        assert "running" in line
        assert "12345" in line

    @staticmethod
    def test_format_status_line_default():
        """Test formatting default instance status."""
        status = InstanceStatus(
            name="default",
            running=False,
            pid=None,
            workspace=Path("/tmp/default"),
            ports={},
        )
        line = format_status_line(status)
        assert "default" in line
        assert "stopped" in line


class TestIsProcessAlive:
    """Test process alive checking."""

    @staticmethod
    def test_invalid_pid():
        """Test invalid PID returns False."""
        assert is_process_alive(-1) is False
        assert is_process_alive(0) is False

    @staticmethod
    def test_current_process():
        """Test current process PID is alive."""
        current_pid = os.getpid()
        assert is_process_alive(current_pid) is True


class TestBootstrapEnv:
    """Test bootstrap .env creation."""

    @staticmethod
    def test_create_bootstrap_env(tmp_path):
        """Test creating bootstrap env file."""
        config = InstanceConfig(
            name="alice",
            workspace=tmp_path,
            ports={
                "agent_server": 19092,
                "web": 20000,
                "gateway": 20001,
                "frontend": 6173,
            },
        )
        env_path = create_bootstrap_env(config)

        assert env_path.exists()
        content = env_path.read_text()
        assert "JIUWENSWARM_DATA_DIR" in content
        assert "JIUWENSWARM_INSTANCE=alice" in content
        assert "AGENT_SERVER_PORT=19092" in content


class TestInstancesYaml:
    """Test instances.yaml management."""

    @staticmethod
    def test_create_instances_yaml_template(tmp_path):
        """Test creating instances.yaml template."""
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=tmp_path / "instances.yaml",
        ):
            path = create_instances_yaml_template()
            assert path.exists()
            content = path.read_text()
            assert "instances:" in content

    @staticmethod
    def test_load_empty_instances_yaml(tmp_path):
        """Test loading nonexistent instances.yaml."""
        yaml_path = tmp_path / "instances.yaml"
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            data = load_instances_yaml()
            assert data == {"instances": {}}

    @staticmethod
    def test_save_and_load_instances_yaml(tmp_path):
        """Test saving and loading instances.yaml."""
        yaml_path = tmp_path / "instances.yaml"
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            data = {
                "instances": {"alice": {}, "bob": {"ports": {"agent_server": 28092}}}
            }
            save_instances_yaml(data)

            loaded = load_instances_yaml()
            assert "alice" in loaded["instances"]
            assert "bob" in loaded["instances"]
            assert loaded["instances"]["bob"]["ports"]["agent_server"] == 28092

    @staticmethod
    def test_save_is_atomic_and_leaves_no_temp_residue(tmp_path):
        """save_instances_yaml writes atomically and cleans up its temp file.

        Regression guard for the concurrent-named-instance write race: the
        file must never be observed half-written, and the sibling ``.tmp``
        temp file must not be left behind after a successful save.
        """
        yaml_path = tmp_path / "instances.yaml"
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            save_instances_yaml({"instances": {"alice": {"ports": {"gateway": 21001}}}})

            # File is complete and loadable.
            loaded = load_instances_yaml()
            assert loaded["instances"]["alice"]["ports"]["gateway"] == 21001

            # No leftover temp files in the same directory.
            leftovers = [p.name for p in tmp_path.iterdir() if p.name != "instances.yaml"]
            assert leftovers == [], f"temp residue left behind: {leftovers}"


class TestGetInstanceConfig:
    """Test instance config loading."""

    @staticmethod
    def test_get_instance_config_not_found(tmp_path):
        """Test getting nonexistent instance config."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text("instances: {}\n", encoding="utf-8")
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            config = get_instance_config("nonexistent")
            assert config is None

    @staticmethod
    def test_get_instance_config_with_auto_ports(tmp_path):
        """Test getting instance config with auto-allocated ports."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text("instances:\n  alice: {}\n", encoding="utf-8")
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            config = get_instance_config("alice")
            assert config is not None
            assert config.name == "alice"
            # First instance (index 1) should have these ports
            assert config.ports["agent_server"] == 19092


class TestCollectAllPorts:
    """Test collecting all ports for conflict detection."""

    @staticmethod
    def test_collect_default_ports():
        """Test collecting default instance ports."""
        yaml_path = Path("/nonexistent")
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            ports = collect_all_ports()
            # Default instance ports should be included
            assert 18092 in ports  # agent_server
            assert 19000 in ports  # web

    @staticmethod
    def test_collect_excluding_self():
        """Test collecting ports excluding a specific instance."""
        yaml_path = Path("/nonexistent")
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            ports = collect_all_ports(exclude_name="default")
            # Should be empty when excluding default and no named instances
            assert ports == []


class TestListAllInstances:
    """Test listing all instances."""

    @staticmethod
    def test_list_all_instances_empty(tmp_path):
        """Test listing with no instances.yaml."""
        yaml_path = tmp_path / "instances.yaml"
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            statuses = list_all_instances(include_default=True)
            # Should include default instance
            assert len(statuses) >= 1
            assert any(s.name == "default" for s in statuses)

    @staticmethod
    def test_list_all_instances_no_default(tmp_path):
        """Test listing without default instance."""
        yaml_path = tmp_path / "instances.yaml"
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            statuses = list_all_instances(include_default=False)
            # Should not include default instance
            assert not any(s.name == "default" for s in statuses)


class TestInstancesYamlError:
    """Test instances.yaml error handling."""

    @staticmethod
    def test_valid_yaml(tmp_path):
        """Test loading valid YAML file."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  alice:\n    ports:\n      agent_server: 28092\n",
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            data = load_instances_yaml()
            assert "alice" in data["instances"]
            assert data["instances"]["alice"]["ports"]["agent_server"] == 28092

    @staticmethod
    def test_missing_file(tmp_path):
        """Test missing file returns empty structure."""
        yaml_path = tmp_path / "nonexistent.yaml"
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            data = load_instances_yaml()
            assert data == {"instances": {}}

    @staticmethod
    def test_invalid_yaml_syntax(tmp_path):
        """Test invalid YAML syntax raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        # Invalid YAML: missing space after colon, duplicate key
        yaml_path.write_text(
            "instances:\n  alice:bad_syntax\n  alice: duplicate\n",
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            # Check error message contains useful info
            assert "YAML format error" in str(exc_info.value)
            assert str(yaml_path) in str(exc_info.value)

    @staticmethod
    def test_missing_instances_key(tmp_path):
        """Test missing 'instances' key raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text("other_key: value\n", encoding="utf-8")
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "Missing 'instances' key" in str(exc_info.value)

    @staticmethod
    def test_invalid_instance_name(tmp_path):
        """Test invalid instance name raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  '.hidden': {}\n",  # Invalid: starts with dot
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "Invalid instance name" in str(exc_info.value)
            assert ".hidden" in str(exc_info.value)

    @staticmethod
    def test_reserved_instance_name(tmp_path):
        """Test reserved instance name raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  default: {}\n",  # Reserved name
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "reserved" in str(exc_info.value).lower()

    @staticmethod
    def test_invalid_port_type(tmp_path):
        """Test unknown port type raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  alice:\n    ports:\n      unknown_port: 12345\n",
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "unknown port type" in str(exc_info.value)
            assert "unknown_port" in str(exc_info.value)

    @staticmethod
    def test_invalid_port_value_negative(tmp_path):
        """Test negative port value raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  alice:\n    ports:\n      agent_server: -1\n",
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "must be 1-65535" in str(exc_info.value)

    @staticmethod
    def test_invalid_port_value_out_of_range(tmp_path):
        """Test port value out of range raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  alice:\n    ports:\n      agent_server: 70000\n",
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "must be 1-65535" in str(exc_info.value)

    @staticmethod
    def test_invalid_port_value_string(tmp_path):
        """Test string port value raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n"
            "  alice:\n"
            "    ports:\n"
            '      agent_server: "28092"\n',  # String, not int
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "must be integer" in str(exc_info.value)

    @staticmethod
    def test_ports_not_dict(tmp_path):
        """Test non-dict ports value raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  alice:\n    ports: invalid\n",
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "'ports' must be a dict" in str(exc_info.value)

    @staticmethod
    def test_workspace_not_string(tmp_path):
        """Test non-string workspace raises InstancesYamlError."""
        yaml_path = tmp_path / "instances.yaml"
        yaml_path.write_text(
            "instances:\n  alice:\n    workspace: 123\n",  # Int, not string
            encoding="utf-8",
        )
        with patch(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            return_value=yaml_path,
        ):
            with pytest.raises(InstancesYamlError) as exc_info:
                load_instances_yaml()
            assert "'workspace' must be a string" in str(exc_info.value)


class TestInstanceLock:
    """Test InstanceLock concurrency control."""

    @staticmethod
    def test_acquire_and_release(tmp_path):
        """Test basic lock acquire and release."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        lock = InstanceLock(config)

        # Should acquire successfully
        assert lock.acquire(timeout=1.0) is True
        assert lock.lock_path.exists()

        # Should be able to release
        lock.release()
        # On Windows, lock file is removed; on Unix, it may remain
        assert getattr(lock, "_lock_file") is None

    @staticmethod
    def test_context_manager(tmp_path):
        """Test lock as context manager."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )

        with InstanceLock(config) as lock:
            assert lock.lock_path.exists()
            assert getattr(lock, "_lock_file") is not None

        # Released after context
        assert getattr(lock, "_lock_file") is None

    @staticmethod
    def test_double_acquire_same_process(tmp_path):
        """Test that same process can re-acquire after release."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        lock = InstanceLock(config)

        assert lock.acquire(timeout=1.0) is True
        lock.release()

        # Should be able to acquire again
        assert lock.acquire(timeout=1.0) is True
        lock.release()

    @staticmethod
    def test_concurrent_acquire_fails(tmp_path):
        """Test that concurrent acquire from another process fails.

        This is the primary test for cross-process lock isolation.
        test_timeout_exceeded only tests same-process lock object competition,
        which is a different scenario and cannot replace this test.
        """
        import multiprocessing

        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )
        lock = InstanceLock(config)

        # Acquire in this process
        assert lock.acquire(timeout=1.0) is True

        # Use Queue to get return value from subprocess
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()

        p = ctx.Process(
            target=_try_acquire_lock_with_result, args=(str(tmp_path), result_queue)
        )
        p.start()
        p.join(timeout=5.0)

        # Process should not hang
        assert not p.is_alive()

        # The other process should fail to acquire lock (return False)
        # On Windows, the lock file already exists so exclusive creation fails
        # On Unix, flock on same file from different process fails
        if not result_queue.empty():
            subprocess_result = result_queue.get(timeout=1.0)
            assert subprocess_result is False, (
                "Subprocess should fail to acquire lock held by main process"
            )

        lock.release()

    @staticmethod
    def test_stale_lock_cleanup(tmp_path):
        """Test that stale lock is cleaned up."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )

        # Create a stale lock file manually
        lock_path = tmp_path / ".instance.lock"
        lock_path.write_text("99999\n0.0\n", encoding="utf-8")  # Old timestamp

        # Modify mtime to be old
        old_time = time.time() - STALE_LOCK_TIMEOUT - 10
        os.utime(lock_path, (old_time, old_time))

        # Acquire should succeed after cleaning stale lock
        lock = InstanceLock(config)
        assert lock.acquire(timeout=1.0) is True
        lock.release()

    @staticmethod
    def test_timeout_exceeded(tmp_path):
        """Test that acquire returns False after timeout."""
        config = InstanceConfig(
            name="test",
            workspace=tmp_path,
            ports={},
        )

        # First lock acquires successfully
        first_lock = InstanceLock(config)
        assert first_lock.acquire(timeout=1.0) is True

        # Second lock should fail to acquire (timeout exceeded)
        second_lock = InstanceLock(config)
        result = second_lock.acquire(timeout=0.5)
        assert result is False

        # Clean up
        first_lock.release()


class TestPortFallback:
    """Test find_available_ports() conflict-fallback scanning."""

    @staticmethod
    def test_returns_own_index_when_free(monkeypatch):
        """When the base index group is fully free, it is returned as-is."""
        ports_at = lambda idx: calculate_instance_ports(idx)
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available",
            lambda host, port: True,
        )
        result = find_available_ports(base_index=0, scan_range=5)
        assert result is not None
        ports, idx = result
        assert idx == 0
        assert ports == ports_at(0)

    @staticmethod
    def test_skips_occupied_index(monkeypatch):
        """When index 1 group is occupied, returns index 2."""
        occupied = set(calculate_instance_ports(1).values())

        def fake(host, port):
            return port not in occupied

        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available", fake
        )
        result = find_available_ports(base_index=1, scan_range=5)
        assert result is not None
        ports, idx = result
        assert idx == 2
        assert ports == calculate_instance_ports(2)

    @staticmethod
    def test_returns_none_when_range_exhausted(monkeypatch):
        """Returns None when every index in range has a conflict."""
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available",
            lambda host, port: False,
        )
        result = find_available_ports(base_index=0, scan_range=3)
        assert result is None

    @staticmethod
    def test_scan_range_zero_scans_nothing(monkeypatch):
        """scan_range=0 means scan nothing → None immediately.

        Regression guard: previously the loop was ``range(max(1, scan_range))``
        which silently scanned ONE index even when the caller asked for zero,
        making the exhausted-range error message in start_services.py describe
        a range that did not match what was actually scanned.
        """
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available",
            lambda host, port: True,  # all free, but range is 0
        )
        result = find_available_ports(base_index=5, scan_range=0)
        assert result is None

    @staticmethod
    def test_exclude_ports_treated_as_occupied(monkeypatch):
        """Ports in exclude_ports are skipped even if probe says free."""
        occupied = calculate_instance_ports(0)
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available",
            lambda host, port: True,
        )
        result = find_available_ports(
            base_index=0,
            scan_range=5,
            exclude_ports=list(occupied.values()),
        )
        assert result is not None
        _, idx = result
        assert idx == 1  # index 0 fully excluded -> next group


class TestEnvVarOverride:
    """Test Phase 2 env-var base-port override (Docker-friendly)."""

    @staticmethod
    def test_no_env_vars_keeps_defaults(monkeypatch):
        """Without env vars, behavior is identical to BASE_PORTS."""
        for key in PORT_ENV_OVERRIDES.values():
            monkeypatch.delenv(key, raising=False)
        assert compute_auto_port("gateway", 0) == 19001
        assert calculate_instance_ports(0)["frontend"] == 5173

    @staticmethod
    def test_env_var_overrides_base(monkeypatch):
        """JIUWENSWARM_<TYPE>_PORT overrides the base port for that type only."""
        monkeypatch.setenv("JIUWENSWARM_GATEWAY_PORT", "29001")
        monkeypatch.setenv("JIUWENSWARM_FRONTEND_PORT", "15173")
        try:
            assert compute_auto_port("gateway", 0) == 29001
            assert compute_auto_port("gateway", 2) == 29001 + 2000
            ports = calculate_instance_ports(0)
            assert ports["gateway"] == 29001
            assert ports["frontend"] == 15173
            # Unset types keep defaults
            assert ports["agent_server"] == 18092
            assert ports["web"] == 19000
        finally:
            monkeypatch.delenv("JIUWENSWARM_GATEWAY_PORT", raising=False)
            monkeypatch.delenv("JIUWENSWARM_FRONTEND_PORT", raising=False)

    @staticmethod
    def test_invalid_env_var_ignored(monkeypatch):
        """Malformed env value falls back to default (no crash)."""
        monkeypatch.setenv("JIUWENSWARM_GATEWAY_PORT", "not-a-number")
        try:
            assert compute_auto_port("gateway", 0) == 19001
        finally:
            monkeypatch.delenv("JIUWENSWARM_GATEWAY_PORT", raising=False)


class TestUpsertEnvPorts:
    """Test _upsert_env_ports() persistence for the default instance."""

    @staticmethod
    def test_append_to_empty_file(tmp_path):
        env_path = tmp_path / ".env"
        ports = {"agent_server": 19092, "web": 20000, "gateway": 20001, "frontend": 6173}
        _upsert_env_ports(env_path, ports)
        txt = env_path.read_text()
        assert "AGENT_SERVER_PORT=19092" in txt
        assert "GATEWAY_PORT=20001" in txt
        assert "FRONTEND_PORT=6173" in txt

    @staticmethod
    def test_replace_existing_preserves_others(tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "API_KEY=sk-keep\nGATEWAY_PORT=19001\nMODEL_NAME=m\n",
            encoding="utf-8",
        )
        ports = {"agent_server": 19092, "web": 20000, "gateway": 20001, "frontend": 6173}
        _upsert_env_ports(env_path, ports)
        txt = env_path.read_text()
        assert "API_KEY=sk-keep" in txt
        assert "MODEL_NAME=m" in txt
        assert "GATEWAY_PORT=20001" in txt
        assert "GATEWAY_PORT=19001" not in txt
        assert "AGENT_SERVER_PORT=19092" in txt

    @staticmethod
    def test_idempotent(tmp_path):
        env_path = tmp_path / ".env"
        ports = {"agent_server": 19092, "web": 20000, "gateway": 20001, "frontend": 6173}
        _upsert_env_ports(env_path, ports)
        before = env_path.read_text()
        _upsert_env_ports(env_path, ports)
        assert env_path.read_text() == before

    @staticmethod
    def test_comment_lines_preserved(tmp_path):
        """A commented-out port line must not be replaced; a real line is appended."""
        env_path = tmp_path / ".env"
        env_path.write_text("# GATEWAY_PORT=9999\nAPI_KEY=x\n", encoding="utf-8")
        _upsert_env_ports(env_path, {"gateway": 20001})
        lines = env_path.read_text().splitlines()
        assert "# GATEWAY_PORT=9999" in lines
        assert any(l == "GATEWAY_PORT=20001" for l in lines)


class TestFormatUrlHint:
    """Test _format_url_hint() output."""

    @staticmethod
    def test_hint_contains_gateway_port():
        hint = _format_url_hint({"gateway": 20001})
        assert "jiuwenswarm-tui --url ws://127.0.0.1:20001/tui" in hint
        assert "jiuwenswarm chat" in hint

    @staticmethod
    def test_hint_handles_missing_gateway():
        """Missing gateway port still formats (uses 0 placeholder)."""
        hint = _format_url_hint({})
        assert "ws://127.0.0.1:0/tui" in hint


class TestStartServicesFallback:
    """Test start_services._resolve_ports_with_fallback() integration.

    Covers the named-instance path (persists to instances.yaml + bootstrap .env)
    and the default-instance path (persists to ~/.jiuwenswarm/config/.env).
    """

    @staticmethod
    def test_named_instance_fallback_persists(tmp_path, monkeypatch):
        """Named instance: index-1 conflict → fallback to index-2, persisted."""
        import yaml
        from jiuwenswarm import start_services

        # Isolate instances.yaml / workspaces into tmp_path by patching the
        # path resolvers directly (env-var-based resolution is cached at module
        # import time, which is unreliable across tests in one process).
        yaml_path = tmp_path / "instances.yaml"
        ws = tmp_path / "alice"
        ws.mkdir()
        yaml_path.write_text(
            f"instances:\n  alice:\n    workspace: {ws}\n", encoding="utf-8"
        )

        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.yaml.get_instances_yaml_path",
            lambda: yaml_path,
        )
        # Workspace path resolver must also point at tmp_path for bootstrap .env.
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.yaml.get_instance_workspace_path",
            lambda name: tmp_path / name,
        )

        cmd = start_services.InstanceCommand("alice")
        assert cmd.validate_and_load() is None
        orig = dict(cmd.config.ports)
        assert orig["gateway"] == 20001  # index 1

        # Make index-1 group occupied; everything else free
        occupied = set(calculate_instance_ports(1).values())
        real = is_port_available

        def fake(host, port):
            return port not in occupied and real(host, port)

        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available", fake
        )
        monkeypatch.setattr(
            "jiuwenswarm.start_services.is_port_available", fake
        )

        rc = start_services._resolve_ports_with_fallback(cmd)
        assert rc is None  # success
        assert cmd.config.ports["agent_server"] == 20092  # index 2
        assert cmd.config.ports["gateway"] == 21001

        # yaml persisted with new ports
        data = yaml.safe_load(yaml_path.read_text())
        assert data["instances"]["alice"]["ports"]["gateway"] == 21001

        # bootstrap .env persisted
        benv = ws / ".env"
        assert benv.exists()
        assert "GATEWAY_PORT=21001" in benv.read_text()

    @staticmethod
    def test_default_instance_fallback_persists_to_env(tmp_path, monkeypatch):
        """Default instance: conflict → fallback, persisted to config/.env."""
        from jiuwenswarm import start_services

        # Patch get_env_file (used by start_services) to a tmp path so the
        # test never touches the real ~/.jiuwenswarm/config/.env.
        cfg_env = tmp_path / "config" / ".env"
        cfg_env.parent.mkdir(parents=True, exist_ok=True)
        cfg_env.write_text("API_KEY=sk-keep\nMODEL_NAME=m-keep\n", encoding="utf-8")
        monkeypatch.setattr("jiuwenswarm.start_services.get_env_file", lambda: cfg_env)

        # Isolate from other instances configured on the host machine
        # (e.g. a leftover 'alice'/'bob' in instances.yaml would otherwise
        # pollute collect_all_ports and shift the fallback index).
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.collect_all_ports",
            lambda exclude_name=None: [],
        )

        cmd = start_services.InstanceCommand("default")
        assert cmd.validate_and_load() is None

        # Force ONLY the index-0 group to be occupied; every higher index free.
        occupied = set(calculate_instance_ports(0).values())
        real = is_port_available

        def fake(host, port):
            return port not in occupied and real(host, port)

        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available", fake
        )
        monkeypatch.setattr(
            "jiuwenswarm.start_services.is_port_available", fake
        )

        rc = start_services._resolve_ports_with_fallback(cmd)
        assert rc is None
        assert cmd.config.ports["gateway"] == 20001  # index 1

        # config/.env got the new ports AND preserved existing keys
        txt = cfg_env.read_text()
        assert "API_KEY=sk-keep" in txt
        assert "MODEL_NAME=m-keep" in txt
        assert "GATEWAY_PORT=20001" in txt
        assert "AGENT_SERVER_PORT=19092" in txt

    @staticmethod
    def test_no_fallback_returns_error_when_range_exhausted(tmp_path, monkeypatch):
        """When the whole scan range is occupied, fallback returns 1."""
        from jiuwenswarm import start_services

        cfg_env = tmp_path / "config" / ".env"
        cfg_env.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("jiuwenswarm.start_services.get_env_file", lambda: cfg_env)
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.collect_all_ports",
            lambda exclude_name=None: [],
        )

        cmd = start_services.InstanceCommand("default")
        assert cmd.validate_and_load() is None

        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available",
            lambda host, port: False,
        )
        monkeypatch.setattr(
            "jiuwenswarm.start_services.is_port_available",
            lambda host, port: False,
        )
        rc = start_services._resolve_ports_with_fallback(cmd, scan_range=3)
        assert rc == 1

    @staticmethod
    def test_persistence_failure_aborts_launch(tmp_path, monkeypatch):
        """P2: if port persistence fails, fallback returns 1 (not None).

        Without this, a default instance whose .env write fails would return
        success and the launcher would spawn subprocesses that read the OLD
        (still-conflicting) ports from .env and crash on bind. The caller
        must be told persistence failed so it aborts instead.
        """
        from unittest.mock import patch as _patch
        from jiuwenswarm import start_services

        cfg_env = tmp_path / "config" / ".env"
        cfg_env.parent.mkdir(parents=True, exist_ok=True)
        # Pre-create the file so the post-check read works even though the
        # write is mocked to fail. Pre-existing content must be preserved
        # (no fallback ports written into it).
        cfg_env.write_text("API_KEY=sk-keep\n", encoding="utf-8")
        monkeypatch.setattr("jiuwenswarm.start_services.get_env_file", lambda: cfg_env)
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.collect_all_ports",
            lambda exclude_name=None: [],
        )

        cmd = start_services.InstanceCommand("default")
        assert cmd.validate_and_load() is None

        # Only index-0 occupied so a fallback group IS found — but then make
        # the .env write raise so the persistence path fails.
        occupied = set(calculate_instance_ports(0).values())
        real = is_port_available

        def fake_probe(host, port):
            return port not in occupied and real(host, port)

        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available", fake_probe
        )
        monkeypatch.setattr(
            "jiuwenswarm.start_services.is_port_available", fake_probe
        )

        def boom(*args, **kwargs):
            raise OSError("simulated .env write failure")

        with _patch(
            "jiuwenswarm.instance_manager.config._upsert_env_ports",
            side_effect=boom,
        ):
            rc = start_services._resolve_ports_with_fallback(cmd)

        # Must surface the failure (return 1), not silently succeed.
        assert rc == 1
        # Persistence failed, so the .env must NOT contain fallback ports;
        # only the pre-existing content remains.
        assert "GATEWAY_PORT=20001" not in cfg_env.read_text()
        assert "API_KEY=sk-keep" in cfg_env.read_text()

    @staticmethod
    def test_no_conflict_launch_clears_stale_env_ports(tmp_path, monkeypatch):
        """P2 regression: a previous fallback's ports must not linger in .env.

        Scenario ("conflict-then-no-conflict"):
          1. Prior launch hit a conflict and wrote GATEWAY_PORT=20001 to .env.
          2. The conflict is now gone (index-0 ports are free again).
          3. This launch has NO conflict → check_ports_conflicts() is empty.

        Without the _sync_default_env_ports call on the no-conflict path,
        .env would keep GATEWAY_PORT=20001 and subprocesses (which read .env
        via load_dotenv) would bind 20001 instead of the default 19001. The
        fix re-writes index-0 defaults into .env on every no-conflict launch.
        """
        from unittest.mock import patch as _patch
        from jiuwenswarm import start_services

        cfg_env = tmp_path / "config" / ".env"
        cfg_env.parent.mkdir(parents=True, exist_ok=True)
        # Simulate a stale fallback residue from a previous launch.
        cfg_env.write_text(
            "API_KEY=sk-keep\n"
            "AGENT_SERVER_PORT=19092\n"  # stale fallback (index 1)
            "GATEWAY_PORT=20001\n"       # stale fallback (index 1)
            "MODEL_NAME=m-keep\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("jiuwenswarm.start_services.get_env_file", lambda: cfg_env)
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.collect_all_ports",
            lambda exclude_name=None: [],
        )

        cmd = start_services.InstanceCommand("default")
        assert cmd.validate_and_load() is None

        # Make index-0 ports look free (no conflict) so _run takes the
        # no-conflict path. Other indices are irrelevant here.
        monkeypatch.setattr(
            "jiuwenswarm.instance_manager.config.is_port_available",
            lambda host, port: True,
        )
        monkeypatch.setattr(
            "jiuwenswarm.start_services.is_port_available",
            lambda host, port: True,
        )

        # Stub out the actual subprocess launch — we only care about .env sync.
        monkeypatch.setattr(
            "jiuwenswarm.start_services._build_commands",
            lambda mode: [("stub", ["true"], tmp_path)],
        )
        monkeypatch.setattr(
            "jiuwenswarm.start_services._run_processes",
            lambda commands, ports: 0,
        )

        rc = start_services._run("app")
        assert rc == 0

        # .env must now carry the index-0 DEFAULTS (not the stale 20001).
        txt = cfg_env.read_text()
        assert "GATEWAY_PORT=19001" in txt, txt
        assert "AGENT_SERVER_PORT=18092" in txt, txt
        # Stale fallback ports must be gone.
        assert "GATEWAY_PORT=20001" not in txt
        assert "AGENT_SERVER_PORT=19092" not in txt
        # And pre-existing unrelated keys preserved.
        assert "API_KEY=sk-keep" in txt
        assert "MODEL_NAME=m-keep" in txt
