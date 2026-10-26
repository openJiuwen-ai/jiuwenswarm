# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Integration tests for box-server API endpoints."""

import copy
import ipaddress
import json
import logging
import posixpath
import re
import socket
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import pytest
import yaml

from jiuwenbox.bundled_configs import default_policy_path
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import JOB_ID_FORMAT_MESSAGE, SANDBOX_ID_FORMAT_MESSAGE
from jiuwenbox.supervisor import network as network_module
from jiuwenbox.supervisor.bwrap import BwrapConfig

_DEFAULT_POLICY = yaml.safe_load(
    default_policy_path().read_text(encoding="utf-8")
)
_DEFAULT_FILESYSTEM_POLICY = _DEFAULT_POLICY["filesystem_policy"]

SYSTEM_BIND_MOUNTS = copy.deepcopy(_DEFAULT_FILESYSTEM_POLICY["bind_mounts"])
DEVICE_MOUNTS = copy.deepcopy(_DEFAULT_FILESYSTEM_POLICY["device"])
DEFAULT_FILES = copy.deepcopy(_DEFAULT_FILESYSTEM_POLICY.get("files", []))
SANDBOX_WORKSPACE = "/root/.jiuwenbox"
DIRECTORIES = copy.deepcopy(_DEFAULT_FILESYSTEM_POLICY["directories"])

logger = logging.getLogger(__name__)


class SandboxTrackingClient:
    """Track sandboxes created during a test and clean them up afterwards."""

    def __init__(self, client):
        self._client = client
        self._created_ids: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def post(self, url, *args, **kwargs):
        response = self._client.post(url, *args, **kwargs)
        if str(url).rstrip("/") == "/api/v1/sandboxes" and response.status_code == 201:
            try:
                sandbox_id = response.json().get("id")
            except Exception:
                sandbox_id = None
            if sandbox_id:
                self._created_ids.append(sandbox_id)
        return response

    def delete(self, url, *args, **kwargs):
        response = self._client.delete(url, *args, **kwargs)
        sandbox_id = self._sandbox_id_from_delete_url(url)
        if sandbox_id and response.status_code in (200, 202, 204, 404):
            self._created_ids = [item for item in self._created_ids if item != sandbox_id]
        return response

    def cleanup_sandboxes(self) -> None:
        for sandbox_id in reversed(self._created_ids):
            try:
                self._client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            except Exception as exc:
                logger.warning("Failed to cleanup sandbox %s: %s", sandbox_id, exc)
        self._created_ids.clear()

    @staticmethod
    def _sandbox_id_from_delete_url(url) -> str | None:
        path = str(url).split("?", 1)[0].rstrip("/")
        prefix = "/api/v1/sandboxes/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        if "/" in suffix:
            return None
        return suffix or None


_UDS_SCHEME = "unix://"
# httpx UDS transport 仍要求一个合法 absolute base_url 才能拼相对路径; 实际
# 请求由 socket transport 接管, 这个 host 字段不会被解析。与
# ``tests/integration/conftest.py`` 中的 ``_UDS_PLACEHOLDER_BASE_URL`` 保持一致。
_UDS_PLACEHOLDER_BASE_URL = "http://jiuwenbox"


def _is_uds_endpoint(endpoint: str) -> bool:
    return endpoint.startswith(_UDS_SCHEME)


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint if "://" in endpoint else f"http://{endpoint}"


def _build_httpx_client(endpoint: str, *, timeout: float = 30.0) -> httpx.Client:
    """按 endpoint scheme 构造 httpx.Client (与 conftest._build_httpx_client 同语义).

    存在的原因: 本文件原先有一份本地 ``client`` fixture, 覆盖了 conftest 里
    UDS-aware 的版本。保留本地 fixture 是为了让用例显式可见, 但实现委托给
    这个工厂, 避免再写一份 base_url + transport 的分叉逻辑。
    """
    if _is_uds_endpoint(endpoint):
        uds_path = endpoint[len(_UDS_SCHEME):]
        if not uds_path.startswith("/"):
            raise ValueError(f"unix endpoint requires absolute path: {endpoint!r}")
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=uds_path),
            base_url=_UDS_PLACEHOLDER_BASE_URL,
            timeout=timeout,
        )
    return httpx.Client(base_url=_normalize_endpoint(endpoint), timeout=timeout)


def _sandbox_health_url(server_endpoint: str) -> str:
    """构造一个"从沙箱内 urlopen 回服务端 /health"的 URL.

    这个 helper 只在两处用到, 都是"isolated 沙箱不能反向连服务端"类的网络
    隔离断言。UDS 模式下服务端根本没暴露 TCP listener, 沙箱无论隔离与否
    都拿不到一个可拨号的 host:port, 该断言失去意义—— ``pytest.skip`` 而
    不是返回一个 ``unix://`` URL (urllib 会以 ``unknown url type`` 提前
    报错, 看上去像通过了, 实际并未验证隔离)。
    """
    if _is_uds_endpoint(server_endpoint):
        pytest.skip(
            "sandbox-to-host TCP isolation test needs a TCP server endpoint; "
            f"got {server_endpoint!r}"
        )
    return f"{_normalize_endpoint(server_endpoint).rstrip('/')}/health"


def _host_network_ip_from_sandbox(client, sandbox_id: str) -> str:
    script = textwrap.dedent(
        """
        import re
        import subprocess
        import sys

        def run_ip(args):
            try:
                return subprocess.check_output(
                    ["ip", *args],
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                return ""

        route = run_ip(["-4", "route", "get", "1.1.1.1"])
        match = re.search(r"\\bsrc\\s+(\\d+\\.\\d+\\.\\d+\\.\\d+)", route)
        if match and not match.group(1).startswith("127."):
            print(match.group(1))
            sys.exit(0)

        addresses = run_ip(["-4", "-o", "addr", "show", "scope", "global"])
        for address in re.findall(r"\\binet\\s+(\\d+\\.\\d+\\.\\d+\\.\\d+)/", addresses):
            if not address.startswith("127."):
                print(address)
                sys.exit(0)

        print("failed to resolve host-network IPv4 address from sandbox", file=sys.stderr)
        sys.exit(1)
        """
    ).strip()
    response = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
        "command": ["python3", "-c", script],
        "timeout_seconds": 5,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["exit_code"] == 0, data
    return data["stdout"].strip()


def _unused_host_network_tcp_port_from_sandbox(client, sandbox_id: str) -> int:
    script = textwrap.dedent(
        """
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", 0))
            print(sock.getsockname()[1])
        """
    ).strip()
    response = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
        "command": ["python3", "-c", script],
        "timeout_seconds": 5,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["exit_code"] == 0, data
    return int(data["stdout"].strip())


def _isolated_network_policy(*, uplink_subnet: str = "") -> dict:
    network = {
        "mode": "isolated",
        "egress": {"default": "allow"},
    }
    if uplink_subnet:
        network["uplink"] = {"subnet": uplink_subnet}
    return {
        "name": "uplink-test-policy",
        "filesystem_policy": {
            "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
            "read_write": ["/tmp"],
        },
        "network": network,
    }


_UPLINK_INFO_SCRIPT = textwrap.dedent(
    """
    import ipaddress
    import json
    import re
    import subprocess
    import sys

    def run_ip(args):
        try:
            return subprocess.check_output(
                ["ip", *args],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return ""

    default_route = run_ip(["-4", "route", "show", "default"])
    dev_match = re.search(r"dev\\s+(\\S+)", default_route)
    if not dev_match:
        sys.exit(1)
    device = dev_match.group(1)

    addr_output = run_ip(["-4", "-o", "addr", "show", "dev", device])
    inet_match = re.search(r"inet\\s+(\\d+\\.\\d+\\.\\d+\\.\\d+)/(\\d+)", addr_output)
    if not inet_match:
        sys.exit(2)
    sandbox_ip = ipaddress.IPv4Address(inet_match.group(1))
    prefix_len = int(inet_match.group(2))

    gateway_match = re.search(r"via\\s+(\\d+\\.\\d+\\.\\d+\\.\\d+)", default_route)
    if not gateway_match:
        sys.exit(3)
    gateway = ipaddress.IPv4Address(gateway_match.group(1))

    block = ipaddress.ip_network(f"{sandbox_ip}/{prefix_len}", strict=False)
    print(json.dumps({
        "block": str(block),
        "sandbox_ip": str(sandbox_ip),
        "gateway": str(gateway),
    }))
    """
).strip()


def _uplink_info_from_sandbox(
    client,
    sandbox_id: str,
) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Address, ipaddress.IPv4Address]:
    """Read uplink /30 details from inside the sandbox (works when server runs in a container)."""
    response = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
        "command": ["python3", "-c", _UPLINK_INFO_SCRIPT],
        "timeout_seconds": 5,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["exit_code"] == 0, data
    payload = json.loads(data["stdout"])
    block = ipaddress.ip_network(payload["block"], strict=False)
    sandbox_ip = ipaddress.IPv4Address(payload["sandbox_ip"])
    gateway = ipaddress.IPv4Address(payload["gateway"])
    assert block.prefixlen == 30, block
    assert sandbox_ip == block.network_address + 2, (sandbox_ip, block)
    assert gateway == block.network_address + 1, (gateway, block)
    return block, sandbox_ip, gateway


def _capability_check_script(cap_bit: int) -> str:
    return textwrap.dedent(
        f"""
        cap_eff = 0
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    cap_eff = int(line.split()[1], 16)
                    break
        print("yes" if cap_eff & (1 << {cap_bit}) else "no")
        """
    ).strip()


def _loopback_ingress_script(expect_success: bool) -> str:
    connect_block = textwrap.dedent(
        """
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        conn, _ = srv.accept()
        conn.sendall(b"ingress-ok")
        conn.close()
        print(sock.recv(64).decode())
        sock.close()
        """
    ).strip()
    if not expect_success:
        connect_block = textwrap.dedent(
            """
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                conn, _ = srv.accept()
                conn.sendall(b"ingress-ok")
                conn.close()
                print(sock.recv(64).decode())
                sock.close()
                print("unexpected-success")
                sys.exit(0)
            except Exception as exc:
                print(type(exc).__name__)
                sys.exit(7)
            """
        ).strip()

    return "\n".join([
        "import socket",
        "import sys",
        "",
        "port = int(sys.argv[1])",
        "",
        "srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)",
        'srv.bind(("127.0.0.1", port))',
        "srv.listen(1)",
        connect_block,
        "srv.close()",
        "",
    ])


def _has_directory(directories: list, path: str) -> bool:
    for directory in directories:
        if isinstance(directory, str) and directory == path:
            return True
        if isinstance(directory, dict) and directory.get("path") == path:
            return True
    return False


def _has_bind_mount(bind_mounts: list, sandbox_path: str) -> bool:
    return any(mount.get("sandbox_path") == sandbox_path for mount in bind_mounts)


def _with_runtime_support(policy: dict) -> dict:
    runtime_policy = copy.deepcopy(policy)
    filesystem_policy = runtime_policy.setdefault("filesystem_policy", {})
    bind_mounts = filesystem_policy.setdefault("bind_mounts", [])
    for mount in SYSTEM_BIND_MOUNTS:
        if mount not in bind_mounts:
            bind_mounts.append(mount.copy())

    directories = filesystem_policy.setdefault("directories", [])
    for directory_entry in DIRECTORIES:
        directories.append(directory_entry.copy())

    return runtime_policy


def _has_mount(args: list[str], flag: str, source: str, target: str) -> bool:
    for index, value in enumerate(args[:-2]):
        if value == flag and args[index + 1] == source and args[index + 2] == target:
            return True
    return False


def _has_arg_pair(args: list[str], flag: str, value: str) -> bool:
    for index, item in enumerate(args[:-1]):
        if item == flag and args[index + 1] == value:
            return True
    return False


@pytest.fixture
def client(server_endpoint):
    with _build_httpx_client(server_endpoint, timeout=30.0) as external:
        tracking = SandboxTrackingClient(external)
        try:
            yield tracking
        finally:
            tracking.cleanup_sandboxes()


@pytest.fixture
def create_sandbox_with_policy(client):
    def factory(
        *,
        policy: dict,
        policy_mode: str = "override",
    ) -> dict:
        response = client.post("/api/v1/sandboxes", json={
            "policy_mode": policy_mode,
            "policy": _with_runtime_support(policy),
        })
        assert response.status_code == 201, response.text
        sandbox = response.json()
        assert sandbox["phase"] == "ready", sandbox
        return sandbox

    return factory


class TestHealthEndpoint:
    @staticmethod
    def test_health(client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "landlock_supported" in data
        assert "sandboxes_active" in data


class TestSandboxCRUD:
    @staticmethod
    def test_list_sandboxes_empty(client):
        resp = client.get("/api/v1/sandboxes")
        assert resp.status_code == 200
        assert resp.json() == []

    @staticmethod
    def test_create_sandbox(client):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "name" not in data
        assert "command" not in data
        assert "workdir" not in data
        assert data["phase"] in ("provisioning", "ready", "error")

    @staticmethod
    def test_list_sandboxes_after_create(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        resp = client.get("/api/v1/sandboxes")
        assert resp.status_code == 200
        data = resp.json()
        assert any(item["id"] == sandbox_id for item in data)
        assert len(data) == 1

    @staticmethod
    def test_get_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sandbox_id
        assert "name" not in data

    @staticmethod
    def test_get_nonexistent_sandbox(client):
        resp = client.get("/api/v1/sandboxes/nonexistent")
        assert resp.status_code == 404

    @staticmethod
    def test_delete_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert resp.status_code == 204

        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert resp.status_code == 404

    @staticmethod
    def test_create_sandbox_auto_generated_id_format(client):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, resp.text
        sandbox_id = resp.json()["id"]
        assert re.fullmatch(r"^[0-9a-f]{8}-[0-9a-f]{3}$", sandbox_id), sandbox_id

    @staticmethod
    def test_create_sandbox_with_custom_id(client):
        custom_id = f"my-sb_{uuid.uuid4().hex[:6]}"
        resp = client.post("/api/v1/sandboxes", json={"sandbox_id": custom_id})
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == custom_id

    @staticmethod
    def test_create_sandbox_with_custom_id_abcd(client):
        custom_id = f"abcd-{uuid.uuid4().hex[:4]}"
        resp = client.post("/api/v1/sandboxes", json={"sandbox_id": custom_id})
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == custom_id

    @staticmethod
    def test_create_sandbox_empty_sandbox_id_generates(client):
        resp = client.post("/api/v1/sandboxes", json={"sandbox_id": ""})
        assert resp.status_code == 201, resp.text
        sandbox_id = resp.json()["id"]
        assert re.fullmatch(r"^[0-9a-f]{8}-[0-9a-f]{3}$", sandbox_id), sandbox_id

    @staticmethod
    @pytest.mark.parametrize(
        "invalid_id",
        [
            "abc",
            "ABC123",
            "my sb",
            " abcd ",
            "a" * 41,
            "id!",
        ],
    )
    def test_create_sandbox_rejects_invalid_custom_id(client, invalid_id):
        resp = client.post("/api/v1/sandboxes", json={"sandbox_id": invalid_id})
        assert resp.status_code == 400, resp.text
        assert SANDBOX_ID_FORMAT_MESSAGE in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_duplicate_id(client):
        custom_id = f"dup-{uuid.uuid4().hex[:4]}"
        first = client.post("/api/v1/sandboxes", json={"sandbox_id": custom_id})
        assert first.status_code == 201, first.text

        second = client.post("/api/v1/sandboxes", json={"sandbox_id": custom_id})
        assert second.status_code == 409, second.text
        assert custom_id in second.json()["error"]

    @staticmethod
    def test_create_sandbox_concurrent_duplicate_id(server_endpoint):
        custom_id = f"race-{uuid.uuid4().hex[:4]}"

        def _create() -> httpx.Response:
            with _build_httpx_client(server_endpoint, timeout=30.0) as thread_client:
                return thread_client.post(
                    "/api/v1/sandboxes",
                    json={"sandbox_id": custom_id},
                )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(_create) for _ in range(2)]
                statuses = sorted(f.result().status_code for f in as_completed(futures))

            assert statuses == [201, 409], statuses
        finally:
            with _build_httpx_client(server_endpoint, timeout=30.0) as cleanup_client:
                cleanup_client.delete(f"/api/v1/sandboxes/{custom_id}")


class TestSandboxLifecycle:
    @staticmethod
    def test_start_stopped_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        stop_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert stop_resp.status_code == 200
        assert stop_resp.json()["phase"] == "stopped"

        start_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/start")
        assert start_resp.status_code == 200
        assert start_resp.json()["phase"] == "ready", start_resp.json()

    @staticmethod
    def test_stop_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "stopped"

    @staticmethod
    def test_restart_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/restart")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "ready"

    @staticmethod
    def test_sandbox_process_cannot_target_sandbox_daemon(client):
        # The long-running daemon shares the sandbox PID namespace with
        # user-spawned children.  bwrap's monitor is typically PID 1 and
        # the daemon is PID 2; seccomp blocks kill-family syscalls that
        # target those PIDs (plus broadcast/process-group forms), and each
        # exec runs in its own session so kill(0) cannot reach the daemon.
        create_resp = client.post("/api/v1/sandboxes", json={})
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        assert sandbox["phase"] == "ready", sandbox
        sandbox_id = sandbox["id"]

        kill_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": [
                "python3",
                "-c",
                textwrap.dedent(
                    """
                    import os
                    import signal
                    import sys
                    import time

                    targets = [signal.SIGTERM, signal.SIGINT, signal.SIGKILL,
                               signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2]
                    delivered = []
                    for pid in (1, 2):
                        for sig in targets:
                            try:
                                os.kill(pid, sig)
                            except ProcessLookupError:
                                delivered.append(f"missing:{pid}:{sig}")
                            except PermissionError:
                                continue
                            except OSError as exc:
                                delivered.append(f"error:{pid}:{sig}:{exc.errno}")
                    for pid in (0, -1, -2):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            delivered.append(f"missing:{pid}")
                        except PermissionError:
                            continue
                        except OSError as exc:
                            delivered.append(f"error:{pid}:{exc.errno}")
                    time.sleep(0.5)

                    for pid in (1, 2):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            print(f"daemon-killed:{pid}")
                            sys.exit(1)
                        except PermissionError:
                            pass

                    if delivered:
                        print(f"unexpected:{','.join(delivered)}")
                        sys.exit(2)
                    print("daemon-survived")
                    """
                ).strip(),
            ],
            "timeout_seconds": 10,
        })
        assert kill_resp.status_code == 200
        kill_data = kill_resp.json()
        assert kill_data["exit_code"] == 0, kill_data
        assert kill_data["stdout"].strip() == "daemon-survived"

        shell_kill_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": [
                "sh",
                "-c",
                "kill -9 1 2>/dev/null; kill -9 2 2>/dev/null; "
                "kill -9 0 2>/dev/null; kill -9 -1 2>/dev/null; "
                "kill -9 -2 2>/dev/null; echo daemon-survived",
            ],
            "timeout_seconds": 10,
        })
        assert shell_kill_resp.status_code == 200
        shell_kill_data = shell_kill_resp.json()
        assert shell_kill_data["exit_code"] == 0, shell_kill_data
        assert shell_kill_data["stdout"].strip() == "daemon-survived"

        status_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["phase"] == "ready", status_resp.json()

        exec_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": ["echo", "daemon-alive"],
            "timeout_seconds": 5,
        })
        assert exec_resp.status_code == 200
        exec_data = exec_resp.json()
        assert exec_data["exit_code"] == 0, exec_data
        assert exec_data["stdout"].strip() == "daemon-alive"

    @staticmethod
    def test_sandbox_process_cannot_inspect_sandbox_daemon_memory(client):
        # The sandbox infrastructure occupies PID 1/2 (bwrap monitor and
        # daemon). Reading either process' address space requires
        # CAP_SYS_PTRACE (stripped by the default policy), and ptrace attach is
        # blocked by seccomp. Together these prevent a sandboxed process from
        # extracting secrets from or hijacking the long-running daemon.
        create_resp = client.post("/api/v1/sandboxes", json={})
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        assert sandbox["phase"] == "ready", sandbox
        sandbox_id = sandbox["id"]

        script = textwrap.dedent(
            """
            import ctypes
            import os
            import sys

            for pid in (1, 2):
                try:
                    fd = os.open(f'/proc/{pid}/mem', os.O_RDONLY)
                except PermissionError:
                    continue
                except FileNotFoundError:
                    continue
                else:
                    try:
                        os.read(fd, 16)
                    except PermissionError:
                        pass
                    except OSError:
                        pass
                    else:
                        os.close(fd)
                        print(f'infrastructure-memory-readable:{pid}')
                        sys.exit(2)
                    os.close(fd)

            libc = ctypes.CDLL('libc.so.6', use_errno=True)
            PTRACE_ATTACH = 16
            for pid in (1, 2):
                try:
                    rc = libc.ptrace(PTRACE_ATTACH, pid, 0, 0)
                    if rc == 0:
                        print(f'infrastructure-ptraceable:{pid}')
                        sys.exit(3)
                except OSError:
                    pass

            print('daemon-protected')
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": ["python3", "-c", script],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip() == "daemon-protected"

    @staticmethod
    def test_default_policy_seccomp_allows_sandbox_startup(client):
        # End-to-end regression for the seccomp BPF chain: the default policy
        # blocks many syscalls and uses isolated networking. A malformed filter
        # used to make bwrap exit with SIGSEGV (code 139) before the daemon
        # printed anything, leaving the sandbox stuck in ``error``.
        create_resp = client.post("/api/v1/sandboxes", json={})
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        assert sandbox["phase"] == "ready", sandbox
        assert sandbox.get("error_message") in (None, ""), sandbox

        exec_resp = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["echo", "seccomp-ok"],
            "timeout_seconds": 5,
        })
        assert exec_resp.status_code == 200
        exec_data = exec_resp.json()
        assert exec_data["exit_code"] == 0, exec_data
        assert exec_data["stdout"].strip() == "seccomp-ok"

    @staticmethod
    def test_default_policy_blocked_syscall_is_enforced(client):
        # Prove the installed default-policy seccomp filter still denies blocked
        # syscalls at exec time (not only at sandbox startup).
        create_resp = client.post("/api/v1/sandboxes", json={})
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        assert sandbox["phase"] == "ready", sandbox
        sandbox_id = sandbox["id"]

        response = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": [
                "python3",
                "-c",
                textwrap.dedent(
                    """
                    import ctypes
                    import errno
                    import platform
                    import sys

                    syscall_numbers = {
                        "x86_64": 165,
                        "AMD64": 165,
                        "aarch64": 40,
                    }
                    nr = syscall_numbers.get(platform.machine())
                    if nr is None:
                        print(f"unsupported-arch:{platform.machine()}")
                        sys.exit(2)

                    libc = ctypes.CDLL("libc.so.6", use_errno=True)
                    libc.syscall.restype = ctypes.c_long
                    ctypes.set_errno(0)
                    result = libc.syscall(nr)
                    err = ctypes.get_errno()
                    if result == -1 and err == errno.EPERM:
                        print("syscall-blocked")
                        sys.exit(7)

                    print(f"unexpected-success:{result}:{err}")
                    """
                ).strip(),
            ],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 7, data
        assert "syscall-blocked" in data["stdout"]
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_sandbox_process_can_kill_unprotected_child(client):
        # Seccomp only guards infrastructure PIDs (0/1/2 and broadcast forms).
        # User payloads must still be able to signal their own children.
        create_resp = client.post("/api/v1/sandboxes", json={})
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        assert sandbox["phase"] == "ready", sandbox
        sandbox_id = sandbox["id"]

        kill_child_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": [
                "sh",
                "-c",
                "sleep 30 & pid=$!; "
                "kill -0 $pid || exit 11; "
                "kill -9 $pid || exit 12; "
                "wait $pid; "
                "echo killed:$pid",
            ],
            "timeout_seconds": 10,
        })
        assert kill_child_resp.status_code == 200
        kill_child_data = kill_child_resp.json()
        assert kill_child_data["exit_code"] == 0, kill_child_data
        assert kill_child_data["stdout"].strip().startswith("killed:")
        assert "Operation not permitted" not in kill_child_data["stderr"]

        status_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["phase"] == "ready", status_resp.json()

    @staticmethod
    def test_get_logs(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}/logs")
        assert resp.status_code == 200


class TestPolicyAPI:
    @staticmethod
    def test_get_sandbox_policy(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 1
        assert data["name"] == "server-default"
        assert data["environment"] == {}
        assert "sandbox_workspace" not in data
        assert "resources" not in data
        assert data["filesystem_policy"]["directories"] == [{'path': '/home', 'permissions': '0777'},
                                                            {'path': '/tmp', 'permissions': '1777'}]
        assert data["filesystem_policy"]["read_only"] == [
            "/",
            "/bin",
            "/sbin",
            "/usr",
            "/lib",
            "/lib64",
            "/etc",
            "/opt",
        ]
        assert data["filesystem_policy"]["read_write"] == ["/home", "/tmp"]
        assert data["filesystem_policy"]["bind_mounts"] == SYSTEM_BIND_MOUNTS
        assert data["filesystem_policy"]["device"] == DEVICE_MOUNTS
        assert data["filesystem_policy"]["files"] == DEFAULT_FILES
        assert data["process"]["run_as_user"] == "sandbox"
        assert data["process"]["run_as_group"] == "sandbox"
        assert data["namespace"] == {
            "user": True,
            "pid": True,
            "ipc": True,
            "cgroup": True,
            "uts": True,
        }
        assert data["capabilities"] == {"add": [], "drop": []}
        assert data["landlock"]["compatibility"] == "best_effort"
        assert data["network"]["mode"] == "isolated"
        assert data["network"]["egress"]["allowed_domains"] == ["baidu.com"]
        assert data["network"]["egress"]["allowed_ips"] == ["127.0.0.1/32", "::1/128"]
        assert data["network"]["egress"]["blocked_ips"] == ["169.254.169.254/32"]
        assert data["network"]["egress"]["blocked_ports"] == [22]
        assert data["network"]["egress"]["default"] == "allow"
        assert data["network"]["egress"]["blocked_domains"] == ["ip.me"]
        assert data["network"]["egress"]["allowed_ports"] == [443, 80]
        assert data["network"]["ingress"]["default"] == "allow"
        assert data["network"]["ingress"]["allowed_domains"] == ["localhost"]
        assert data["network"]["ingress"]["allowed_ips"] == ["127.0.0.1/32", "::1/128"]
        assert data["network"]["ingress"]["blocked_ips"] == []
        assert data["network"]["ingress"]["allowed_ports"] == [8080]
        assert data["network"]["ingress"]["blocked_ports"] == []
        assert "profile" not in data["syscall"]
        assert "blocked" not in data["syscall"]
        assert "mount" in data["syscall"]["x86_64"]["blocked"]
        assert "kexec_file_load" in data["syscall"]["x86_64"]["blocked"]
        assert "mount" in data["syscall"]["arm64"]["blocked"]
        assert "kexec_file_load" in data["syscall"]["arm64"]["blocked"]

    @staticmethod
    def test_append_policy_merges_with_server_default(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "policy_mode": "append",
            "policy": {
                "name": "appended-policy",
                "environment": {
                    "JIUWENBOX_APPEND_ENV": "append-ok",
                },
                "filesystem_policy": {
                    "directories": [{"path": "/tmp/appended-dir", "permissions": "0700"}],
                    "read_only": ["/var/log"],
                    "read_write": ["/var/tmp"],
                    "bind_mounts": [{
                        "host_path": "/tmp",
                        "sandbox_path": "/tmp",
                        "mode": "rw",
                    }],
                },
                "network": {
                    "egress": {
                        "allowed_domains": ["extra.example.com"],
                        "allowed_ips": ["203.0.113.10/32"],
                    },
                    "ingress": {
                        "allowed_ips": ["10.0.0.0/8"],
                        "allowed_ports": [9090],
                    },
                },
                "process": {
                    "run_as_user": "root",
                    "run_as_group": "root",
                },
                "namespace": {
                    "pid": False,
                    "uts": False,
                },
                "capabilities": {
                    "add": ["CAP_NET_RAW"],
                    "drop": [],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "syscall": {
                    "x86_64": {"blocked": ["getpid"]},
                    "arm64": {"blocked": ["getpid"]},
                },
            },
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "appended-policy"
        assert data["environment"] == {"JIUWENBOX_APPEND_ENV": "append-ok"}
        assert "sandbox_workspace" not in data
        assert data["network"]["egress"]["allowed_domains"] == [
            "baidu.com",
            "extra.example.com",
        ]
        assert data["network"]["egress"]["allowed_ips"] == [
            "127.0.0.1/32",
            "::1/128",
            "203.0.113.10/32",
        ]
        assert data["network"]["egress"]["blocked_ips"] == ["169.254.169.254/32"]
        assert data["network"]["egress"]["blocked_ports"] == [22]
        assert data["network"]["ingress"]["allowed_domains"] == ["localhost"]
        assert data["network"]["ingress"]["allowed_ips"] == [
            "127.0.0.1/32",
            "::1/128",
            "10.0.0.0/8",
        ]
        assert data["network"]["ingress"]["allowed_ports"] == [8080, 9090]
        assert data["filesystem_policy"]["read_only"] == [
            "/",
            "/bin",
            "/sbin",
            "/usr",
            "/lib",
            "/lib64",
            "/etc",
            "/opt",
            "/var/log",
        ]
        assert data["filesystem_policy"]["read_write"] == ["/home", "/tmp", "/var/tmp"]
        assert data["filesystem_policy"]["directories"] == [{'path': '/home', 'permissions': '0777'},
                                                            {'path': '/tmp', 'permissions': '1777'},
                                                            {"path": "/tmp/appended-dir", "permissions": "0700"}]
        assert data["filesystem_policy"]["bind_mounts"] == SYSTEM_BIND_MOUNTS + [{
            "host_path": "/tmp",
            "sandbox_path": "/tmp",
            "mode": "rw",
        }]
        assert data["filesystem_policy"]["device"] == DEVICE_MOUNTS
        assert data["filesystem_policy"]["files"] == DEFAULT_FILES
        assert data["process"]["run_as_user"] == "root"
        assert data["process"]["run_as_group"] == "root"
        assert data["namespace"] == {
            "user": True,
            "pid": False,
            "ipc": True,
            "cgroup": True,
            "uts": False,
        }
        assert data["capabilities"]["add"] == ["CAP_NET_RAW"]
        assert data["capabilities"]["drop"] == []
        assert data["landlock"]["compatibility"] == "disabled"
        assert "getpid" in data["syscall"]["x86_64"]["blocked"]
        assert "mount" in data["syscall"]["x86_64"]["blocked"]
        assert "getpid" in data["syscall"]["arm64"]["blocked"]
        assert "mount" in data["syscall"]["arm64"]["blocked"]

    @staticmethod
    def test_override_policy_replaces_server_default(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "policy_mode": "override",
            "policy": {
                "name": "override-policy",
                "environment": {
                    "JIUWENBOX_OVERRIDE_ENV": "override-ok",
                },
                "filesystem_policy": {
                    "directories": [{
                        "path": "/tmp/override-dir",
                        "permissions": "0700",
                    }],
                    "read_only": ["/usr"],
                    "read_write": ["/var/tmp"],
                    "bind_mounts": SYSTEM_BIND_MOUNTS,
                },
                "network": {
                    "mode": "host",
                    "egress": {
                        "default": "deny",
                        "allowed_domains": ["override.example.com"],
                        "allowed_ips": ["198.51.100.10/32"],
                        "blocked_ips": ["198.51.100.11/32"],
                        "allowed_ports": [80],
                        "blocked_ports": [25],
                    },
                    "ingress": {
                        "default": "allow",
                        "allowed_ips": ["10.0.0.0/8"],
                        "blocked_ips": ["10.0.5.0/24"],
                        "allowed_ports": [9090],
                        "blocked_ports": [22],
                    },
                },
                "process": {
                    "run_as_user": "root",
                    "run_as_group": "root",
                },
                "namespace": {
                    "user": True,
                    "pid": False,
                    "ipc": False,
                    "cgroup": False,
                    "uts": False,
                },
                "capabilities": {
                    "add": ["CAP_NET_RAW"],
                    "drop": [],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "syscall": {
                    "x86_64": {"blocked": ["getppid"]},
                    "arm64": {"blocked": ["getppid"]},
                },
            },
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "override-policy"
        assert data["environment"] == {"JIUWENBOX_OVERRIDE_ENV": "override-ok"}
        assert "sandbox_workspace" not in data
        assert data["network"]["mode"] == "host"
        assert data["network"]["egress"]["allowed_domains"] == ["override.example.com"]
        assert data["network"]["egress"]["allowed_ips"] == ["198.51.100.10/32"]
        assert data["network"]["egress"]["blocked_ips"] == ["198.51.100.11/32"]
        assert data["network"]["egress"]["blocked_ports"] == [25]
        assert data["network"]["ingress"]["default"] == "allow"
        assert data["network"]["ingress"]["allowed_ips"] == ["10.0.0.0/8"]
        assert data["network"]["ingress"]["blocked_ips"] == ["10.0.5.0/24"]
        assert data["network"]["ingress"]["allowed_ports"] == [9090]
        assert data["network"]["ingress"]["blocked_ports"] == [22]
        assert data["filesystem_policy"]["read_only"] == ["/usr"]
        assert data["filesystem_policy"]["read_write"] == ["/var/tmp"]
        assert data["filesystem_policy"]["bind_mounts"] == SYSTEM_BIND_MOUNTS
        assert data["filesystem_policy"]["device"] == []
        assert data["filesystem_policy"]["files"] == []
        assert data["filesystem_policy"]["directories"] == [{
            "path": "/tmp/override-dir",
            "permissions": "0700",
        }]
        assert data["process"]["run_as_user"] == "root"
        assert data["process"]["run_as_group"] == "root"
        assert data["namespace"] == {
            "user": True,
            "pid": False,
            "ipc": False,
            "cgroup": False,
            "uts": False,
        }
        assert data["capabilities"] == {"add": ["CAP_NET_RAW"], "drop": []}
        assert data["landlock"]["compatibility"] == "disabled"
        assert data["syscall"]["x86_64"]["blocked"] == ["getppid"]
        assert data["syscall"]["arm64"]["blocked"] == ["getppid"]

    @staticmethod
    def test_get_nonexistent_policy(client):
        resp = client.get("/api/v1/policies/nonexistent")
        assert resp.status_code == 404

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_bind_mount(client):
        resp = client.post("/api/v1/sandboxes", json={
            "policy": {
                "name": "bad-sandbox-mount-policy",
                "filesystem_policy": {
                    "bind_mounts": [{
                        "host_path": f"{SANDBOX_WORKSPACE}/manual",
                        "sandbox_path": "/tmp/manual",
                        "mode": "rw",
                    }],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert SANDBOX_WORKSPACE in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_device_mount(client):
        resp = client.post("/api/v1/sandboxes", json={
            "policy": {
                "name": "bad-sandbox-device-policy",
                "filesystem_policy": {
                    "device": [{
                        "host_path": f"{SANDBOX_WORKSPACE}/manual-device",
                        "sandbox_path": "/dev/manual-device",
                    }],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert SANDBOX_WORKSPACE in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_path(client):
        resp = client.post("/api/v1/sandboxes", json={
            "policy": {
                "name": "bad-sandbox-path-policy",
                "filesystem_policy": {
                    "read_write": [f"{SANDBOX_WORKSPACE}/manual"],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert SANDBOX_WORKSPACE in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_directory(client):
        resp = client.post("/api/v1/sandboxes", json={
            "policy": {
                "name": "bad-sandbox-dir-policy",
                "filesystem_policy": {
                    "directories": [{
                        "path": f"{SANDBOX_WORKSPACE}/manual",
                        "permissions": "0700",
                    }],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert SANDBOX_WORKSPACE in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_file(client):
        resp = client.post("/api/v1/sandboxes", json={
            "policy": {
                "name": "bad-sandbox-file-policy",
                "filesystem_policy": {
                    "files": [{
                        "path": f"{SANDBOX_WORKSPACE}/manual-file",
                        "permissions": "0600",
                    }],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert SANDBOX_WORKSPACE in resp.json()["error"]


@pytest.fixture
def restore_timeout(client):
    """Snapshot the server-level timeout config and restore it on teardown.

    Required because integration tests share one jiuwenbox-server process
    for the whole session: a test that mutates ``mgr.policy.timeout`` must
    not leak that state into the next test (in particular, leaving an
    aggressive idle_timeout active would silently reap sandboxes created
    by unrelated tests).

    Yields the captured snapshot so the test can sanity-check the starting
    state if it cares.
    """
    snapshot_resp = client.get("/api/v1/timeout")
    assert snapshot_resp.status_code == 200, snapshot_resp.text
    snapshot = snapshot_resp.json()
    try:
        yield snapshot
    finally:
        # ``idle_timeout`` may legitimately be ``None`` (= reaping disabled),
        # which the PUT endpoint accepts. ``idle_check_interval`` is always
        # numeric on the manager side, so the restore body is unambiguous.
        client.put("/api/v1/timeout", json={
            "idle_timeout": snapshot["idle_timeout"],
            "idle_check_interval": snapshot["idle_check_interval"],
        })


class TestTimeoutAPI:
    """Cover the ``GET /timeout`` / ``PUT /timeout`` administrative endpoints
    that drive the server-level idle-sandbox reaper.

    Each test takes the ``restore_timeout`` fixture so the global
    ``mgr.policy.timeout`` is rolled back even when the test asserts halfway
    through.
    """

    @staticmethod
    def test_get_timeout_returns_current_config(client, restore_timeout):
        resp = client.get("/api/v1/timeout")
        assert resp.status_code == 200
        data = resp.json()
        # Schema only: don't hardcode "reaping disabled by default" because
        # ``default-policy.yaml`` could legitimately be changed to opt in.
        assert set(data.keys()) == {"idle_timeout", "idle_check_interval"}
        assert data["idle_check_interval"] is not None
        assert data["idle_check_interval"] > 0
        if data["idle_timeout"] is not None:
            assert data["idle_timeout"] > 0

    @staticmethod
    def test_put_timeout_full_update_round_trips(client, restore_timeout):
        resp = client.put("/api/v1/timeout", json={
            "idle_timeout": 600,
            "idle_check_interval": 30,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"idle_timeout": 600.0, "idle_check_interval": 30.0}
        # GET must reflect the PUT immediately (single in-memory source of truth).
        assert client.get("/api/v1/timeout").json() == {
            "idle_timeout": 600.0,
            "idle_check_interval": 30.0,
        }

    @staticmethod
    def test_put_timeout_partial_updates_only_idle_timeout(client, restore_timeout):
        client.put("/api/v1/timeout", json={
            "idle_timeout": 600,
            "idle_check_interval": 30,
        })
        # Omit ``idle_check_interval`` -> server keeps the prior value.
        resp = client.put("/api/v1/timeout", json={"idle_timeout": 1200})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["idle_timeout"] == 1200.0
        assert data["idle_check_interval"] == 30.0

    @staticmethod
    def test_put_timeout_partial_updates_only_idle_check_interval(
        client, restore_timeout,
    ):
        client.put("/api/v1/timeout", json={
            "idle_timeout": 600,
            "idle_check_interval": 30,
        })
        resp = client.put("/api/v1/timeout", json={"idle_check_interval": 120})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["idle_timeout"] == 600.0
        assert data["idle_check_interval"] == 120.0

    @staticmethod
    def test_put_timeout_disables_reaping_via_null(client, restore_timeout):
        client.put("/api/v1/timeout", json={
            "idle_timeout": 600,
            "idle_check_interval": 30,
        })
        # Explicit null -> reaping disabled, but check_interval is preserved.
        resp = client.put("/api/v1/timeout", json={"idle_timeout": None})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["idle_timeout"] is None
        assert data["idle_check_interval"] == 30.0

    @staticmethod
    def test_put_timeout_disables_reaping_via_zero(client, restore_timeout):
        # Zero / negative idle_timeout is normalized to None by TimeoutPolicy
        # (lets operators flip the feature off without removing the key).
        client.put("/api/v1/timeout", json={
            "idle_timeout": 600,
            "idle_check_interval": 30,
        })
        resp = client.put("/api/v1/timeout", json={"idle_timeout": 0})
        assert resp.status_code == 200, resp.text
        assert resp.json()["idle_timeout"] is None

    @staticmethod
    def test_put_timeout_rejects_non_positive_check_interval(
        client, restore_timeout,
    ):
        for bad in (0, -1, -0.5):
            resp = client.put(
                "/api/v1/timeout", json={"idle_check_interval": bad},
            )
            assert resp.status_code == 400, (bad, resp.text)

    @staticmethod
    def test_put_timeout_rejects_null_check_interval(client, restore_timeout):
        # ``idle_check_interval: null`` is ambiguous: omitting the key already
        # means "don't touch this field", so an explicit null can only mean
        # "set it to None" -- which TimeoutPolicy forbids. Reject up-front
        # with a 400 instead of leaking a generic pydantic ValidationError.
        resp = client.put(
            "/api/v1/timeout", json={"idle_check_interval": None},
        )
        assert resp.status_code == 400

    @staticmethod
    def test_put_timeout_empty_body_is_noop(client, restore_timeout):
        before = client.get("/api/v1/timeout").json()
        resp = client.put("/api/v1/timeout", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == before
        assert client.get("/api/v1/timeout").json() == before

    @staticmethod
    def test_timeout_reaper_deletes_idle_sandbox_end_to_end(
        client, restore_timeout,
    ):
        """End-to-end: configure aggressive timeout, observe reaper deleting
        an untouched sandbox.

        Uses ``idle_timeout=1.5s`` / ``idle_check_interval=0.5s`` so the
        whole test finishes well under 10s. The sandbox is intentionally
        *not* touched after creation -- any exec / file IO would refresh
        ``last_active_at`` and reset the clock.
        """
        resp = client.put("/api/v1/timeout", json={
            "idle_timeout": 1.5,
            "idle_check_interval": 0.5,
        })
        assert resp.status_code == 200, resp.text

        create = client.post("/api/v1/sandboxes", json={})
        assert create.status_code == 201, create.text
        sandbox_id = create.json()["id"]
        # Sanity: sandbox visible right after create.
        listing = client.get("/api/v1/sandboxes").json()
        assert any(item["id"] == sandbox_id for item in listing), listing

        # idle_timeout (1.5s) + worst-case check_interval delay (0.5s) +
        # generous slack for ProcessRuntime.cleanup -> 8s deadline.
        deadline = time.monotonic() + 8.0
        reaped = False
        while time.monotonic() < deadline:
            still_present = any(
                item["id"] == sandbox_id
                for item in client.get("/api/v1/sandboxes").json()
            )
            if not still_present:
                reaped = True
                break
            time.sleep(0.25)
        assert reaped, (
            f"reaper did not delete idle sandbox {sandbox_id} within 8s "
            f"(idle_timeout=1.5s, idle_check_interval=0.5s)"
        )


class TestPolicyEnforcement:
    @staticmethod
    def test_filesystem_read_write_rule_allows_upload_and_download(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "fs-rw-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                    "egress": {"default": "allow"},
                },
            },
        )

        upload = client.post(
            f"/api/v1/sandboxes/{sandbox['id']}/upload",
            params={"sandbox_path": "/tmp/policy-ok.txt"},
            files={"file": ("policy-ok.txt", b"hello-policy", "text/plain")},
        )
        assert upload.status_code == 204

        download = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/download",
            params={"sandbox_path": "/tmp/policy-ok.txt"},
        )
        assert download.status_code == 200
        assert download.content == b"hello-policy"

    @staticmethod
    def test_filesystem_read_only_rule_rejects_upload(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "fs-ro-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                    "egress": {"default": "allow"},
                },
            },
        )

        upload = client.post(
            f"/api/v1/sandboxes/{sandbox['id']}/upload",
            params={"sandbox_path": "/etc/policy-denied.txt"},
            files={"file": ("policy-denied.txt", b"nope", "text/plain")},
        )
        assert upload.status_code == 409

    @staticmethod
    def test_filesystem_directories_rule_creates_directory(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "fs-dir-policy",
                "filesystem_policy": {
                    "directories": [{
                        "path": "/policy-created",
                        "permissions": 700,
                    }],
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python",
                "-c",
                (
                    "import os, stat; "
                    "from pathlib import Path; "
                    "path = Path('/policy-created'); "
                    "print(path.is_dir()); "
                    "print(oct(stat.S_IMODE(os.stat(path).st_mode)))"
                ),
            ],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["True", "0o700"]

    @staticmethod
    def test_filesystem_directories_rule_creates_nested_directory(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "fs-nested-dir-policy",
                "filesystem_policy": {
                    "directories": [{
                        "path": "/policy-created/level1/level2",
                        "permissions": "0711",
                    }],
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python3",
                "-c",
                (
                    "import os, stat; "
                    "from pathlib import Path; "
                    "parent = Path('/policy-created/level1'); "
                    "path = parent / 'level2'; "
                    "print(Path('/policy-created').is_dir()); "
                    "print(parent.is_dir()); "
                    "print(path.is_dir()); "
                    "print(oct(stat.S_IMODE(os.stat(path).st_mode)))"
                ),
            ],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["True", "True", "True", "0o711"]

    @staticmethod
    def test_filesystem_files_rule_creates_nested_empty_file(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "fs-file-policy",
                "filesystem_policy": {
                    "files": [{
                        "path": "/policy-created/level1/marker.txt",
                        "permissions": "0640",
                    }],
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python3",
                "-c",
                (
                    "import os, stat; "
                    "from pathlib import Path; "
                    "parent = Path('/policy-created/level1'); "
                    "path = parent / 'marker.txt'; "
                    "print(Path('/policy-created').is_dir()); "
                    "print(parent.is_dir()); "
                    "print(path.is_file()); "
                    "print(path.read_text()); "
                    "print(oct(stat.S_IMODE(os.stat(path).st_mode)))"
                ),
            ],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        lines = data["stdout"].splitlines()
        assert lines[:4] == ["True", "True", "True", ""]
        assert lines[4] in {"0o640", "0o646"}, lines

    @staticmethod
    def test_filesystem_directories_rule_creates_directory_under_home(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "fs-home-dir-policy",
                "filesystem_policy": {
                    "directories": [{
                        "path": "/home",
                        "permissions": "0755",
                    }],
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        upload = client.post(
            f"/api/v1/sandboxes/{sandbox['id']}/upload",
            params={"sandbox_path": "/home/upload-created/file.txt"},
            files={"file": ("file.txt", b"hello-home-upload", "text/plain")},
        )
        assert upload.status_code == 204, upload.text

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python3",
                "-c",
                (
                    "import os; "
                    "from pathlib import Path; "
                    "home = Path('/home'); "
                    "exec_path = home / 'exec-created'; "
                    "exec_path.mkdir(); "
                    "(exec_path / 'marker.txt').write_text('hello-home-exec'); "
                    "print(home.is_dir()); "
                    "print((home / 'upload-created').is_dir()); "
                    "print((home / 'upload-created/file.txt').read_text()); "
                    "print(exec_path.is_dir())"
                ),
            ],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["True", "True", "hello-home-upload", "True"]

        download = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/download",
            params={"sandbox_path": "/home/exec-created/marker.txt"},
        )
        assert download.status_code == 200, download.text
        assert download.content == b"hello-home-exec"

    @staticmethod
    def test_exec_applies_workdir_env_and_stdin(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "exec-options-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        script = (
            "import os, pathlib, sys; "
            "print(os.environ['BOX_TEST']); "
            "print(pathlib.Path.cwd()); "
            "print(sys.stdin.read())"
        )
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script],
            "workdir": "/tmp",
            "env": {"BOX_TEST": "env-ok"},
            "stdin": "stdin-ok",
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["env-ok", "/tmp", "stdin-ok"]

    @staticmethod
    def test_policy_environment_applies_to_all_exec_processes(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "policy-env-policy",
                "environment": {
                    "JIUWENBOX_POLICY_ENV": "policy-env-ok",
                    "JIUWENBOX_SHARED_ENV": "from-policy",
                },
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        script = (
            "import os; "
            "print(os.environ['JIUWENBOX_POLICY_ENV']); "
            "print(os.environ['JIUWENBOX_SHARED_ENV'])"
        )
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["policy-env-ok", "from-policy"]

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script],
            "env": {"JIUWENBOX_SHARED_ENV": "from-exec"},
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["policy-env-ok", "from-exec"]

    @staticmethod
    def test_exec_runs_javascript_code(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "exec-js-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        js_code = (
            "const label = process.env.BOX_JS_TEST || 'missing'; "
            "const sum = [1, 2, 3, 4].reduce((total, value) => total + value, 0); "
            "console.log(label); "
            "console.log(`sum=${sum}`);"
        )
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["node", "-e", js_code],
            "env": {"BOX_JS_TEST": "js-ok"},
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].splitlines() == ["js-ok", "sum=10"]
        assert data["stderr"] == ""

    @staticmethod
    def test_download_missing_file_returns_404(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "download-missing-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/download",
            params={"sandbox_path": "/tmp/not-found.txt"},
        )
        assert response.status_code == 404

    @staticmethod
    def test_download_directory_returns_409(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "download-dir-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/download",
            params={"sandbox_path": "/tmp"},
        )
        assert response.status_code == 409

    @staticmethod
    def test_list_files_endpoint_returns_files_and_directories(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "list-files-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        setup = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('/tmp/list-api/sub').mkdir(parents=True, exist_ok=True); "
                    "Path('/tmp/list-api/a.txt').write_text('a'); "
                    "Path('/tmp/list-api/sub/b.log').write_text('b')"
                ),
            ],
            "timeout_seconds": 5,
        })
        assert setup.status_code == 200
        assert setup.json()["exit_code"] == 0

        response = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/files",
            params={"sandbox_path": "/tmp/list-api", "recursive": True},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        paths = {item["path"] for item in items}
        assert "/tmp/list-api/a.txt" in paths
        assert "/tmp/list-api/sub" in paths
        assert "/tmp/list-api/sub/b.log" in paths
        assert any(item["name"] == "sub" and item["is_directory"] for item in items)

        files_only = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/files",
            params={
                "sandbox_path": "/tmp/list-api",
                "recursive": True,
                "include_dirs": False,
            },
        )
        assert files_only.status_code == 200
        assert all(not item["is_directory"] for item in files_only.json()["items"])

    @staticmethod
    def test_search_files_endpoint_filters_matches(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "search-files-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        setup = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('/tmp/search-api').mkdir(parents=True, exist_ok=True); "
                    "Path('/tmp/search-api/keep.py').write_text('print(1)'); "
                    "Path('/tmp/search-api/drop.py').write_text('print(2)'); "
                    "Path('/tmp/search-api/readme.md').write_text('# hi')"
                ),
            ],
            "timeout_seconds": 5,
        })
        assert setup.status_code == 200
        assert setup.json()["exit_code"] == 0

        response = client.get(
            f"/api/v1/sandboxes/{sandbox['id']}/search",
            params=[
                ("sandbox_path", "/tmp/search-api"),
                ("pattern", "*.py"),
                ("exclude_patterns", "drop.py"),
            ],
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["name"] for item in items] == ["keep.py"]

    @staticmethod
    def test_process_user_and_group_policy_is_applied(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "process-root-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "process": {
                    "run_as_user": "root",
                    "run_as_group": "root",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["id", "-u"],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        uid_data = response.json()
        assert uid_data["exit_code"] == 0, uid_data
        assert uid_data["stdout"].strip() == "0"

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["id", "-g"],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        gid_data = response.json()
        assert gid_data["exit_code"] == 0, gid_data
        assert gid_data["stdout"].strip() == "0"

    @staticmethod
    def test_syscall_blocked_rule_is_applied(
        client,
        create_sandbox_with_policy,
    ):
        # Use ``mount`` rather than ``getpid``: seccomp is installed once for the
        # whole sandbox lifecycle (daemon + user exec children), so blocking a
        # syscall the daemon needs during startup would prevent the sandbox from
        # reaching ``ready``.
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "syscall-block-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "syscall": {
                    "x86_64": {"blocked": ["mount"]},
                    "arm64": {"blocked": ["mount"]},
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "python3",
                "-c",
                textwrap.dedent(
                    """
                    import ctypes
                    import errno
                    import platform
                    import sys

                    syscall_numbers = {
                        "x86_64": 165,
                        "AMD64": 165,
                        "aarch64": 40,
                    }
                    nr = syscall_numbers.get(platform.machine())
                    if nr is None:
                        print(f"unsupported-arch:{platform.machine()}")
                        sys.exit(2)

                    libc = ctypes.CDLL("libc.so.6", use_errno=True)
                    libc.syscall.restype = ctypes.c_long
                    ctypes.set_errno(0)
                    result = libc.syscall(nr)
                    err = ctypes.get_errno()
                    if result == -1 and err == errno.EPERM:
                        print("syscall-blocked")
                        sys.exit(7)

                    print(f"unexpected-success:{result}:{err}")
                    """
                ).strip(),
            ],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 7, data
        assert "syscall-blocked" in data["stdout"]
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_pid_namespace_policy_is_applied(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "pid-ns-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "namespace": {
                    "pid": True,
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", "import os; print(os.getpid())"],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert int(data["stdout"].strip()) <= 5

    @staticmethod
    def test_capability_drop_removes_net_raw(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "cap-drop-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "process": {
                    "run_as_user": "root",
                    "run_as_group": "root",
                },
                "capabilities": {
                    "add": ["CAP_NET_RAW"],
                    "drop": ["CAP_NET_RAW"],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python", "-c", _capability_check_script(13)],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip() == "no"

    @staticmethod
    def test_capability_add_net_raw_sets_effective_capability(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "cap-add-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "process": {
                    "run_as_user": "root",
                    "run_as_group": "root",
                },
                "capabilities": {
                    "add": ["CAP_NET_RAW"],
                    "drop": [],
                },
                "landlock": {
                    "compatibility": "disabled",
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python", "-c", _capability_check_script(13)],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip() == "yes"

    @staticmethod
    def test_landlock_hard_requirement_policy_is_enforced(
        client,
    ):
        create_resp = client.post("/api/v1/sandboxes", json={
            "policy": _with_runtime_support({
                "name": "landlock-hard-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {
                    "compatibility": "hard_requirement",
                },
                "network": {
                    "mode": "host",
                },
            }),
        })
        assert create_resp.status_code == 201
        data = create_resp.json()
        if data["phase"] == "ready":
            assert data["phase"] == "ready", data
        else:
            assert data["phase"] == "error", data
            assert "landlock" in (data.get("error_message") or "").lower()

    @staticmethod
    def test_landlock_rules_allow_policy_paths_and_deny_other_mounted_paths(
        client,
    ):
        create_resp = client.post("/api/v1/sandboxes", json={
            "name": "landlock-rules",
            "policy": _with_runtime_support({
                "name": "landlock-rules-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {
                    "compatibility": "hard_requirement",
                },
                "network": {
                    "mode": "host",
                },
            }),
        })
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        if sandbox["phase"] == "error":
            assert "landlock" in (sandbox.get("error_message") or "").lower()
            return
        assert sandbox["phase"] == "ready", sandbox

        script = textwrap.dedent(
            """
            from pathlib import Path
            import sys

            allowed = Path("/tmp/landlock-allowed.txt")
            allowed.write_text("landlock-allowed")
            assert allowed.read_text() == "landlock-allowed"

            try:
                Path("/jiuwenbox/landlock-launcher.py").read_text()
            except PermissionError:
                print("landlock-denied")
                sys.exit(7)

            print("unexpected-success")
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python", "-c", script],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 7, data
        assert "landlock-denied" in data["stdout"]
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_network_mode_isolated_allows_external_http_requests(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "net-isolated-uplink-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "isolated",
                    "egress": {"default": "allow"},
                },
            },
        )

        script = textwrap.dedent(
            """
            import sys
            import urllib.request

            request = urllib.request.Request(
                sys.argv[1],
                headers={"User-Agent": "jiuwenbox-integration-test"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                print(response.status)
                print(response.geturl())
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script, "https://www.baidu.com/"],
            "timeout_seconds": 15,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert "baidu.com" in data["stdout"].lower()

    @staticmethod
    def test_network_mode_isolated_blocked_ip_rejects_egress(
        client,
        create_sandbox_with_policy,
    ):
        blocked_ip = socket.gethostbyname("www.baidu.com")
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "net-isolated-block-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "isolated",
                    "egress": {
                        "default": "allow",
                        "blocked_ips": [f"{blocked_ip}/32"],
                    },
                },
            },
        )

        script = textwrap.dedent(
            """
            import sys
            import socket

            ip = sys.argv[1]
            with socket.create_connection((ip, 443), timeout=10):
                pass
            print("unexpected-success")
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script, blocked_ip],
            "timeout_seconds": 15,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] != 0, data
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_network_mode_host_allows_http_requests(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "net-host-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                    "egress": {"default": "allow"},
                },
            },
        )

        script = textwrap.dedent(
            """
            import sys
            import urllib.request

            request = urllib.request.Request(
                sys.argv[1],
                headers={"User-Agent": "jiuwenbox-integration-test"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                print(response.status)
                print(response.geturl())
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script, "https://www.huawei.com/"],
            "timeout_seconds": 15,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0
        assert "huawei.com" in data["stdout"].lower()

    @staticmethod
    def test_host_network_allows_external_tcp_connection_to_sandbox_process(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "net-host-listener-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "host",
                    "egress": {"default": "allow"},
                    "ingress": {"default": "allow"},
                },
            },
        )
        host = _host_network_ip_from_sandbox(client, sandbox["id"])
        port = _unused_host_network_tcp_port_from_sandbox(client, sandbox["id"])
        script = textwrap.dedent(
            """
            import socket
            import sys

            port = int(sys.argv[1])
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", port))
            server.listen(1)
            print("server-listening", flush=True)
            while True:
                conn, _ = server.accept()
                data = conn.recv(64)
                print("received:" + data.decode(), flush=True)
                conn.sendall(b"pong-from-sandbox")
                conn.close()
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec_background", json={
            "command": ["python3", "-c", script, str(port)],
            "timeout_seconds": 10,
        })
        assert response.status_code == 200, response.text
        background = response.json()
        assert background["started"] is True, background
        assert isinstance(background.get("job_id"), str) and background["job_id"]
        assert isinstance(background["pid"], int), background
        assert background["error_message"] is None

        deadline = time.monotonic() + 8
        last_error = None
        payload = b""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5) as sock:
                    sock.sendall(b"ping-from-host")
                    payload = sock.recv(64)
                    break
            except OSError as exc:
                last_error = exc
                time.sleep(0.1)
        else:
            raise AssertionError(
                f"sandbox tcp server did not accept connections: {last_error}; "
                f"background={background}"
            )

        assert payload == b"pong-from-sandbox"
        for index in range(5):
            with socket.create_connection((host, port), timeout=0.5) as sock:
                sock.sendall(f"ping-{index}".encode())
                assert sock.recv(64) == b"pong-from-sandbox"
            time.sleep(0.5)

    @staticmethod
    def test_default_policy_allows_network_https(client):
        response = client.post("/api/v1/sandboxes", json={})
        assert response.status_code == 201, response.text
        sandbox = response.json()
        assert sandbox["phase"] == "ready", sandbox

        script = textwrap.dedent(
            """
            import sys
            import urllib.request

            request = urllib.request.Request(
                sys.argv[1],
                headers={"User-Agent": "jiuwenbox-integration-test"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                print(response.status)
                print(response.geturl())
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script, "https://www.huawei.com/"],
            "timeout_seconds": 15,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert "huawei.com" in data["stdout"].lower()

    @staticmethod
    def test_default_policy_blocks_access_to_box_server_health(client, server_endpoint):
        response = client.post("/api/v1/sandboxes", json={})
        assert response.status_code == 201, response.text
        sandbox = response.json()
        assert sandbox["phase"] == "ready", sandbox

        script = textwrap.dedent(
            """
            import sys
            import urllib.request

            try:
                urllib.request.urlopen(sys.argv[1], timeout=3).read()
            except Exception as exc:
                print(type(exc).__name__)
                sys.exit(7)

            print("unexpected-success")
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script, _sandbox_health_url(server_endpoint)],
            "timeout_seconds": 10,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 7, data
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_default_policy_does_not_expose_sensitive_etc_files(client):
        response = client.post("/api/v1/sandboxes", json={})
        assert response.status_code == 201, response.text
        sandbox = response.json()
        assert sandbox["phase"] == "ready", sandbox

        script = textwrap.dedent(
            """
            from pathlib import Path
            import sys

            sensitive_paths = [
                "/etc/passwd",
                "/etc/shadow",
                "/etc/group",
                "/etc/gshadow",
            ]

            for sensitive_path in sensitive_paths:
                try:
                    content = Path(sensitive_path).read_text()
                except (FileNotFoundError, PermissionError):
                    print(f"denied:{sensitive_path}")
                    continue

                print(f"leaked:{sensitive_path}:{content[:80]!r}")
                sys.exit(1)
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", script],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert "leaked:" not in data["stdout"]
        assert "root:" not in data["stdout"]
        assert data["stdout"].splitlines() == [
            "denied:/etc/passwd",
            "denied:/etc/shadow",
            "denied:/etc/group",
            "denied:/etc/gshadow",
        ]

    @staticmethod
    def test_ingress_allowed_port_accepts_loopback_connection(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "ingress-allow-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "isolated",
                    "egress": {"default": "allow"},
                    "ingress": {
                        "default": "deny",
                        "allowed_ips": ["127.0.0.1/32"],
                        "allowed_ports": [18081],
                    },
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", _loopback_ingress_script(True), "18081"],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert "ingress-ok" in data["stdout"]

    @staticmethod
    def test_ingress_blocked_port_rejects_loopback_connection(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "ingress-block-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "isolated",
                    "egress": {"default": "allow"},
                    "ingress": {
                        "default": "deny",
                        "allowed_ips": ["127.0.0.1/32"],
                        "allowed_ports": [18081],
                        "blocked_ports": [18082],
                    },
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", _loopback_ingress_script(False), "18082"],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] != 0
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_isolated_sandbox_policy_persists_after_restart(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "netns-persist-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "network": {
                    "mode": "isolated",
                    "egress": {"default": "allow"},
                    "ingress": {
                        "default": "deny",
                        "allowed_ips": ["127.0.0.1/32"],
                        "allowed_ports": [18083],
                    },
                },
            },
        )

        first_exec = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", _loopback_ingress_script(True), "18083"],
            "timeout_seconds": 5,
        })
        assert first_exec.status_code == 200
        first_data = first_exec.json()
        assert first_data["exit_code"] == 0, first_data

        stop_response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/stop")
        assert stop_response.status_code == 200

        start_response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/start")
        assert start_response.status_code == 200

        second_exec = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["python3", "-c", _loopback_ingress_script(True), "18083"],
            "timeout_seconds": 5,
        })
        assert second_exec.status_code == 200
        second_data = second_exec.json()
        assert second_data["exit_code"] == 0, second_data

        delete_response = client.delete(f"/api/v1/sandboxes/{sandbox['id']}")
        assert delete_response.status_code == 204

    # ------------------------------------------------------------------
    # ``/jiuwenbox`` runtime-script integrity
    #
    # jiuwenbox places two trusted scripts on a tmpfs at ``/jiuwenbox``
    # inside every sandbox:
    #
    #   * ``/jiuwenbox/landlock-launcher.py`` - applies the Landlock
    #     ruleset, then either ``compile``/``exec``s the daemon
    #     in-process or ``execvp``s a one-shot user command.
    #   * ``/jiuwenbox/sandbox-daemon.py``    - long-running daemon that
    #     fronts ``exec`` / ``write_file`` / ``read_file`` / ``list_dir``
    #     IPC requests with the policy uid/gid, mount layout, seccomp
    #     filter, and Landlock ruleset already applied.
    #
    # If user code inside the sandbox could read, modify, replace, or
    # unlink either script, the entire trust model collapses: a hostile
    # payload could rewrite the daemon and gain a foothold for every
    # *subsequent* exec the box-server dispatches into the sandbox.
    #
    # The launcher pre-reads both scripts into memory **before** Landlock
    # is installed, so user code (which always runs strictly after
    # Landlock is in force) is supposed to see ``/jiuwenbox`` as
    # completely off-limits. The two cases below pin that contract; the
    # complementary ``TestReservedSandboxPaths`` class below proves that
    # user policies cannot widen the Landlock allowlist to expose
    # ``/jiuwenbox`` from the outside.
    # ------------------------------------------------------------------

    _RESERVED_SCRIPT_DAEMON = "/jiuwenbox/sandbox-daemon.py"
    _RESERVED_SCRIPT_LAUNCHER = "/jiuwenbox/landlock-launcher.py"

    _RESERVED_INTEGRITY_POLICY = {
        "name": "reserved-script-integrity-policy",
        "filesystem_policy": {
            # /jiuwenbox is intentionally NOT listed below - the runtime
            # artifacts there must remain inaccessible to user code, and
            # PolicyEngine would reject the policy outright if it tried.
            "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
            "read_write": ["/tmp"],
        },
        "landlock": {
            "compatibility": "hard_requirement",
        },
        "network": {
            "mode": "host",
        },
    }

    @staticmethod
    def test_reserved_dir_scripts_cannot_be_tampered_with(
        create_sandbox_with_policy,
        client,
    ):
        """User code must hit ``PermissionError`` on every flavour of
        access against ``/jiuwenbox`` and the trusted scripts inside it.

        The script returns exit code 0 only when *every* attempted attack
        was rejected; any successful read/write/delete/symlink/listdir
        would surface as a non-zero exit code with a label in stderr, so
        the happy path here is the locked-down path.
        """
        sandbox = create_sandbox_with_policy(
            policy=TestPolicyEnforcement._RESERVED_INTEGRITY_POLICY,
        )

        daemon_path = TestPolicyEnforcement._RESERVED_SCRIPT_DAEMON
        launcher_path = TestPolicyEnforcement._RESERVED_SCRIPT_LAUNCHER

        attack_script = textwrap.dedent(
            f"""
            import errno
            import os
            import sys

            DAEMON = {daemon_path!r}
            LAUNCHER = {launcher_path!r}

            failures = []

            # Errnos that mean the attack was rejected. The first three
            # come straight from Landlock (EACCES on read/write/exec,
            # EPERM on operations like unlink, EROFS on tmpfs that we
            # remount read-only). The last two come from the filesystem
            # layer doing its own job *before* Landlock even gets to
            # rule:
            #   * EEXIST - ``os.symlink(target, link)`` refuses to clobber
            #     an existing file at ``link``. The daemon script is
            #     therefore not replaced, which is exactly the
            #     containment guarantee we are pinning here.
            #   * EXDEV - ``os.rename(src, dst)`` cannot move a file
            #     across different mounts. ``/jiuwenbox`` is its own
            #     tmpfs and ``/tmp`` is another one, so the
            #     rename-shadow attack cannot complete regardless of
            #     Landlock. Treating this as containment success
            #     documents the additional defence-in-depth that the
            #     runtime relies on.
            BLOCKED_ERRNOS = (
                errno.EACCES,
                errno.EPERM,
                errno.EROFS,
                errno.EEXIST,
                errno.EXDEV,
            )

            def expect_blocked(label, fn):
                try:
                    fn()
                except PermissionError:
                    return
                except FileNotFoundError:
                    # Landlock can mask the path so it appears not to
                    # exist; that is also a containment success.
                    return
                except FileExistsError:
                    # See BLOCKED_ERRNOS comment above re: EEXIST.
                    return
                except OSError as exc:
                    if exc.errno in BLOCKED_ERRNOS:
                        return
                    failures.append(
                        f"{{label}}: unexpected OSError errno={{exc.errno}} {{exc!r}}"
                    )
                    return
                failures.append(f"{{label}}: did not raise")

            # 1. Direct read of the trusted scripts must be denied.
            expect_blocked("read-daemon",   lambda: open(DAEMON, "rb").close())
            expect_blocked("read-launcher", lambda: open(LAUNCHER, "rb").close())

            # 2. Truncating / overwriting either script must be denied.
            expect_blocked("write-truncate-daemon",   lambda: open(DAEMON, "wb").close())
            expect_blocked("write-append-daemon",     lambda: open(DAEMON, "ab").close())
            expect_blocked("write-truncate-launcher", lambda: open(LAUNCHER, "wb").close())

            # 3. Unlinking the scripts must be denied.
            expect_blocked("unlink-daemon",   lambda: os.unlink(DAEMON))
            expect_blocked("unlink-launcher", lambda: os.unlink(LAUNCHER))

            # 4. Replacing them via symlink (atomic shadow attack) must
            #    be denied. We try both ``symlink`` over the existing
            #    path and ``rename`` of an attacker-controlled file.
            expect_blocked(
                "symlink-shadow-daemon",
                lambda: os.symlink("/tmp/evil", DAEMON),
            )
            attacker = "/tmp/jiuwenbox-attacker.py"
            try:
                with open(attacker, "wb") as fh:
                    fh.write(b"# planted by user code\\n")
            except OSError as exc:
                failures.append(
                    f"setup-attacker-failed: {{exc!r}}"
                )
            else:
                expect_blocked(
                    "rename-shadow-daemon",
                    lambda: os.rename(attacker, DAEMON),
                )

            # 5. Creating *new* files inside ``/jiuwenbox`` must also
            #    be denied so an attacker cannot drop a co-resident
            #    decoy that the runtime might later mistakenly load.
            expect_blocked(
                "create-new-file-in-reserved-dir",
                lambda: open("/jiuwenbox/evil.py", "wb").close(),
            )

            # 6. Even directory enumeration must be denied; otherwise an
            #    attacker could probe what exists before mounting another
            #    technique.
            expect_blocked("listdir-reserved", lambda: os.listdir("/jiuwenbox"))
            expect_blocked("scandir-reserved", lambda: list(os.scandir("/jiuwenbox")))

            # 7. Permission bits must not be mutable from user code.
            expect_blocked("chmod-daemon", lambda: os.chmod(DAEMON, 0o777))

            if failures:
                for fail in failures:
                    print(fail, file=sys.stderr)
                sys.exit(1)
            print("all-blocked")
            """
        ).strip()

        response = client.post(
            f"/api/v1/sandboxes/{sandbox['id']}/exec",
            json={
                "command": ["python3", "-c", attack_script],
                "timeout_seconds": 10,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip() == "all-blocked", data

    @staticmethod
    def test_sandbox_remains_functional_after_attempted_reserved_dir_tampering(
        create_sandbox_with_policy,
        client,
    ):
        """Attempting to tamper with ``/jiuwenbox`` scripts must not
        damage the IPC daemon.

        The daemon is loaded into memory before Landlock applies, so its
        on-disk artifact is consumed only at sandbox-creation time. This
        test guards against a regression where a future change makes
        the daemon reload from ``/jiuwenbox`` on later requests - which
        would mean an attacker who *did* break in could bend subsequent
        execs to their will. We pound the attack endpoint, then verify
        that two different IPC code paths (exec and read_file via
        download) still produce the expected results.
        """
        sandbox = create_sandbox_with_policy(
            policy=TestPolicyEnforcement._RESERVED_INTEGRITY_POLICY,
        )
        sandbox_id = sandbox["id"]

        daemon_path = TestPolicyEnforcement._RESERVED_SCRIPT_DAEMON
        launcher_path = TestPolicyEnforcement._RESERVED_SCRIPT_LAUNCHER

        attack_script = textwrap.dedent(
            f"""
            import os
            DAEMON = {daemon_path!r}
            LAUNCHER = {launcher_path!r}
            for path in (DAEMON, LAUNCHER):
                for opener in (
                    lambda p=path: open(p, 'rb').close(),
                    lambda p=path: open(p, 'wb').close(),
                    lambda p=path: os.unlink(p),
                    lambda p=path: os.symlink('/tmp/evil', p),
                    lambda p=path: os.chmod(p, 0o777),
                ):
                    try:
                        opener()
                    except OSError:
                        pass
            print('attempted')
            """
        ).strip()

        attack_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["python3", "-c", attack_script],
                "timeout_seconds": 10,
            },
        )
        assert attack_resp.status_code == 200, attack_resp.text
        # The script swallows every error on purpose; what matters is
        # that the daemon survives the volley of attempted mutations.
        assert attack_resp.json()["exit_code"] == 0, attack_resp.json()

        # IPC-exec must still work end-to-end after the attack.
        followup_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [
                    "python3",
                    "-c",
                    "print('post-attack-exec-ok')",
                ],
                "timeout_seconds": 10,
            },
        )
        assert followup_resp.status_code == 200, followup_resp.text
        followup = followup_resp.json()
        assert followup["exit_code"] == 0, followup
        assert "post-attack-exec-ok" in followup["stdout"]

        # IPC file-op fast paths must also still work. Round-trip a
        # payload through upload + download to confirm the daemon is
        # still serving non-exec requests too.
        target = "/tmp/post-attack-marker.txt"
        marker = b"post-attack-file-op-ok"
        upload_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload",
            params={"sandbox_path": target},
            files={"file": ("marker.txt", marker, "text/plain")},
        )
        assert upload_resp.status_code == 204, upload_resp.text

        download_resp = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/download",
            params={"sandbox_path": target},
        )
        assert download_resp.status_code == 200, download_resp.text
        assert download_resp.content == marker


# ----------------------------------------------------------------------
# Reserved-sandbox-path validation
#
# ``PolicyEngine`` reserves a small set of in-sandbox paths
# (``_RESERVED_SANDBOX_PATHS``, currently ``("/jiuwenbox",)``) for the
# trusted launcher and daemon scripts. Any user policy that names that
# subtree must be rejected at sandbox-creation time, otherwise:
#
#   * ``read_only`` / ``read_write`` / ``directories`` / ``files``
#     entries would punch ``/jiuwenbox`` into the Landlock allowlist
#     (see ``jiuwenbox/supervisor/landlock.py``), letting user code read
#     the launcher and daemon scripts and bypassing the
#     ``test_reserved_dir_scripts_cannot_be_tampered_with`` guarantee;
#   * ``bind_mounts`` with ``sandbox_path`` under ``/jiuwenbox`` would
#     either shadow our launcher mount or bind a user-controlled host
#     directory under the reserved name, which would also leak into the
#     Landlock allowlist;
#   * ``device`` mounts behave the same way as ``bind_mounts``, with
#     the additional risk of granting ``--dev-bind`` privileges inside
#     a path the runtime treats as trusted.
#
# The cases below assert that every flavour of policy-supplied path
# referencing the reserved subtree is rejected with a 400 response and a
# message that names the offending path.
# ----------------------------------------------------------------------


class TestReservedSandboxPaths:
    """Server must reject user policies that target ``/jiuwenbox``."""

    _RESERVED_DIR = "/jiuwenbox"
    _RESERVED_NESTED = "/jiuwenbox/landlock-launcher.py"
    _RESERVED_DEEP_NESTED = "/jiuwenbox/sub/dir/file.py"

    @staticmethod
    def _post_policy(client, policy: dict):
        """Submit a policy and return ``(status_code, body)`` pairs.

        ``create_sandbox_with_policy`` asserts ``phase == "ready"`` so it
        is unsuitable for negative tests; we hit the endpoint directly.
        """
        return client.post(
            "/api/v1/sandboxes",
            json={
                "policy_mode": "override",
                "policy": _with_runtime_support(policy),
            },
        )

    @staticmethod
    def _assert_rejected_with_reserved_message(response, expected_path: str):
        assert response.status_code == 400, response.text
        body = response.json()
        assert "error" in body, body
        message = body["error"]
        assert expected_path in message, message
        assert "reserved" in message.lower(), message

    @staticmethod
    @pytest.mark.parametrize(
        "field, sandbox_path",
        [
            ("read_only", _RESERVED_DIR),
            ("read_only", _RESERVED_NESTED),
            ("read_only", _RESERVED_DEEP_NESTED),
            ("read_write", _RESERVED_DIR),
            ("read_write", _RESERVED_NESTED),
            ("read_write", _RESERVED_DEEP_NESTED),
        ],
    )
    def test_read_lists_cannot_reference_reserved_subtree(
        client,
        field: str,
        sandbox_path: str,
    ):
        """``read_only`` / ``read_write`` are pushed straight into the
        Landlock allowlist by ``encode_landlock_payload``. Letting a user
        sneak ``/jiuwenbox`` in there would expose the launcher / daemon
        scripts the moment Landlock applies, so the policy engine has to
        bounce these inputs before any sandbox is created.
        """
        policy = {
            "name": f"reserved-{field}-policy",
            "filesystem_policy": {field: [sandbox_path]},
        }
        response = TestReservedSandboxPaths._post_policy(client, policy)
        TestReservedSandboxPaths._assert_rejected_with_reserved_message(
            response, sandbox_path,
        )

    @staticmethod
    @pytest.mark.parametrize(
        "sandbox_path",
        [_RESERVED_DIR, _RESERVED_NESTED, _RESERVED_DEEP_NESTED],
    )
    def test_directories_field_cannot_reference_reserved_subtree(
        client,
        sandbox_path: str,
    ):
        """``filesystem_policy.directories`` ultimately becomes a
        ``--dir`` mount + Landlock read_write entry; both behaviours
        would clobber our reserved tmpfs.
        """
        policy = {
            "name": "reserved-directories-policy",
            "filesystem_policy": {"directories": [sandbox_path]},
        }
        response = TestReservedSandboxPaths._post_policy(client, policy)
        TestReservedSandboxPaths._assert_rejected_with_reserved_message(
            response, sandbox_path,
        )

    @staticmethod
    @pytest.mark.parametrize(
        "sandbox_path",
        [_RESERVED_DIR, _RESERVED_NESTED, _RESERVED_DEEP_NESTED],
    )
    def test_files_field_cannot_reference_reserved_subtree(
        client,
        sandbox_path: str,
    ):
        """A single user-controlled file inside ``/jiuwenbox`` would
        still leak that path into the Landlock allowlist via
        ``encode_landlock_payload``; reject the whole subtree, not just
        the canonical script names.
        """
        policy = {
            "name": "reserved-files-policy",
            "filesystem_policy": {"files": [sandbox_path]},
        }
        response = TestReservedSandboxPaths._post_policy(client, policy)
        TestReservedSandboxPaths._assert_rejected_with_reserved_message(
            response, sandbox_path,
        )

    @staticmethod
    @pytest.mark.parametrize("mode", ["ro", "rw"])
    @pytest.mark.parametrize(
        "sandbox_path",
        [_RESERVED_DIR, _RESERVED_NESTED, _RESERVED_DEEP_NESTED],
    )
    def test_bind_mounts_cannot_target_reserved_subtree(
        client,
        sandbox_path: str,
        mode: str,
    ):
        """``bind_mounts.sandbox_path`` is the most dangerous knob: it
        mounts a host-controlled directory under the reserved subtree
        and tells Landlock to allow it. ``host_path`` is irrelevant for
        the policy reservation (it lives in the operator's filesystem)
        but we need to supply *something* that exists on host so the
        request reaches validation.
        """
        policy = {
            "name": "reserved-bind-mounts-policy",
            "filesystem_policy": {
                "bind_mounts": [
                    {
                        "host_path": "/tmp",
                        "sandbox_path": sandbox_path,
                        "mode": mode,
                    },
                ],
            },
        }
        response = TestReservedSandboxPaths._post_policy(client, policy)
        TestReservedSandboxPaths._assert_rejected_with_reserved_message(
            response, sandbox_path,
        )

    @staticmethod
    @pytest.mark.parametrize(
        "sandbox_path",
        [_RESERVED_DIR, _RESERVED_NESTED, _RESERVED_DEEP_NESTED],
    )
    def test_device_mounts_cannot_target_reserved_subtree(
        client,
        sandbox_path: str,
    ):
        """``device`` mounts go through ``--dev-bind`` and the Landlock
        allowlist; if a user can pin ``/jiuwenbox/foo`` here, they
        smuggle the reserved subtree back into Landlock the same way
        ``bind_mounts`` would.
        """
        policy = {
            "name": "reserved-device-policy",
            "filesystem_policy": {
                "device": [
                    {
                        "host_path": "/dev/null",
                        "sandbox_path": sandbox_path,
                    },
                ],
            },
        }
        response = TestReservedSandboxPaths._post_policy(client, policy)
        TestReservedSandboxPaths._assert_rejected_with_reserved_message(
            response, sandbox_path,
        )

    @staticmethod
    def test_unrelated_sandbox_paths_are_not_rejected(
        client, create_sandbox_with_policy,
    ):
        """Sanity check: paths that merely resemble the reserved name
        (substring matches, suffix collisions, ``/run`` left over from
        the previous design) must still be allowed. This guards against
        an over-broad ``startswith`` style implementation.
        """
        policy = {
            "name": "non-reserved-policy",
            "filesystem_policy": {
                "read_only": [
                    "/usr",
                    "/lib",
                    "/lib64",
                    "/etc",
                    "/opt",
                    # ``/jiuwenbox-public`` is *not* under
                    # ``/jiuwenbox`` because PurePosixPath compares full
                    # path components, not raw string prefixes.
                    "/jiuwenbox-public",
                    # Legacy directory we used to host the launcher in -
                    # plain ``/run`` must remain a normal user-policy
                    # path now that the reserved subtree has moved.
                    "/run",
                ],
                "read_write": ["/tmp"],
            },
        }
        sandbox = create_sandbox_with_policy(
            policy=policy,
        )
        assert sandbox["phase"] == "ready", sandbox

    @staticmethod
    def test_reserved_subtree_rejection_runs_before_sandbox_creation(
        client,
    ):
        """The launcher / daemon scripts must never be touched by an
        invalid policy. We assert the failure path returns 400 *and*
        does not surface a non-zero phase, which would imply the runtime
        partially started a sandbox before bouncing.
        """
        policy = {
            "name": "reserved-pre-creation-policy",
            "filesystem_policy": {
                "read_only": [TestReservedSandboxPaths._RESERVED_DIR],
            },
        }
        response = TestReservedSandboxPaths._post_policy(client, policy)
        TestReservedSandboxPaths._assert_rejected_with_reserved_message(
            response, TestReservedSandboxPaths._RESERVED_DIR,
        )
        # The /sandboxes endpoint only returns a created body on 201.
        # 400 responses must not contain phase information.
        assert "phase" not in response.json(), response.json()


class TestBwrapFilesystem:
    @staticmethod
    def test_read_rules_do_not_mount_host_paths():
        policy = SecurityPolicy.model_validate({
            "filesystem_policy": {
                "read_only": ["/host-read-only"],
                "read_write": ["/host-read-write"],
                "bind_mounts": [
                    {
                        "host_path": "/host-source-ro",
                        "sandbox_path": "/sandbox-target-ro",
                        "mode": "ro",
                    },
                    {
                        "host_path": "/host-source-rw",
                        "sandbox_path": "/sandbox-target-rw",
                        "mode": "rw",
                    },
                ],
            },
        })

        args = BwrapConfig.from_policy(policy, ["true"]).to_args()

        assert not _has_mount(args, "--ro-bind", "/host-read-only", "/host-read-only")
        assert not _has_mount(args, "--bind", "/host-read-write", "/host-read-write")
        assert _has_mount(args, "--ro-bind", "/host-source-ro", "/sandbox-target-ro")
        assert _has_mount(args, "--bind", "/host-source-rw", "/sandbox-target-rw")

    @staticmethod
    def test_nested_bind_targets_create_parent_directories():
        policy = SecurityPolicy.model_validate({
            "filesystem_policy": {
                "bind_mounts": [{
                    "host_path": "/etc/resolv.conf",
                    "sandbox_path": "/etc/resolv.conf",
                    "mode": "ro",
                }],
            },
        })

        args = BwrapConfig.from_policy(policy, ["true"]).to_args()

        assert _has_arg_pair(args, "--dir", "/etc")
        assert _has_mount(args, "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf")

    @staticmethod
    def test_device_mounts_use_dev_bind_and_create_parent_directories():
        policy = SecurityPolicy.model_validate({
            "filesystem_policy": {
                "device": [{
                    "host_path": "/dev/dri/renderD128",
                    "sandbox_path": "/dev/dri/renderD128",
                }],
            },
        })

        args = BwrapConfig.from_policy(policy, ["true"]).to_args()

        assert not _has_arg_pair(args, "--dir", "/dev")
        assert _has_arg_pair(args, "--dir", "/dev/dri")
        assert _has_mount(args, "--dev-bind", "/dev/dri/renderD128", "/dev/dri/renderD128")

    @staticmethod
    def test_read_only_parent_of_nested_bind_is_remounted_read_only():
        policy = SecurityPolicy.model_validate({
            "filesystem_policy": {
                "read_only": ["/etc"],
                "read_write": ["/tmp"],
                "bind_mounts": [{
                    "host_path": "/etc/resolv.conf",
                    "sandbox_path": "/etc/resolv.conf",
                    "mode": "ro",
                }],
            },
        })

        args = BwrapConfig.from_policy(policy, ["true"]).to_args()

        assert not _has_arg_pair(args, "--dir", "/etc")
        assert _has_arg_pair(args, "--tmpfs", "/etc")
        assert _has_mount(args, "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf")
        assert _has_arg_pair(args, "--remount-ro", "/etc")


class TestNetworkUplink:
    @staticmethod
    def test_isolated_uplink_auto_allocates_cgnat_pool_block(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(policy=_isolated_network_policy())
        block, sandbox_ip, _gateway = _uplink_info_from_sandbox(client, sandbox["id"])
        assert block.subnet_of(ipaddress.ip_network("100.64.0.0/10"))
        assert sandbox_ip == block.network_address + 2

    @staticmethod
    def test_isolated_uplink_user_pool_allocates_within_subnet(
        client,
        create_sandbox_with_policy,
    ):
        pool = "10.55.0.0/24"
        sandbox = create_sandbox_with_policy(
            policy=_isolated_network_policy(uplink_subnet=pool),
        )
        block, _sandbox_ip, _gateway = _uplink_info_from_sandbox(client, sandbox["id"])
        assert block.subnet_of(ipaddress.ip_network(pool, strict=False))

    @staticmethod
    def test_isolated_uplink_skips_route_occupied_block(client):
        pool = "10.55.0.0/24"
        policy = _with_runtime_support(_isolated_network_policy(uplink_subnet=pool))

        first_response = client.post("/api/v1/sandboxes", json={
            "policy_mode": "override",
            "policy": policy,
            "sandbox_id": f"uplk1_{uuid.uuid4().hex[:6]}",
        })
        assert first_response.status_code == 201, first_response.text
        first = first_response.json()
        assert first["phase"] == "ready", first
        first_block, _, _ = _uplink_info_from_sandbox(client, first["id"])

        second_response = client.post("/api/v1/sandboxes", json={
            "policy_mode": "override",
            "policy": policy,
            "sandbox_id": f"uplk2_{uuid.uuid4().hex[:6]}",
        })
        assert second_response.status_code == 201, second_response.text
        second = second_response.json()
        assert second["phase"] == "ready", second
        second_block, _, _ = _uplink_info_from_sandbox(client, second["id"])
        assert first_block != second_block

    @staticmethod
    def test_isolated_uplink_concurrent_sandboxes_use_distinct_blocks(client):
        policy = _with_runtime_support(
            _isolated_network_policy(uplink_subnet="10.55.0.0/24"),
        )

        def create_one(index: int):
            return client.post("/api/v1/sandboxes", json={
                "policy_mode": "override",
                "policy": policy,
                "sandbox_id": f"uplk{index:02d}_{uuid.uuid4().hex[:6]}",
            })

        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(create_one, range(4)))

        sandbox_ids: list[str] = []
        for response in responses:
            assert response.status_code == 201, response.text
            sandbox = response.json()
            assert sandbox["phase"] == "ready", sandbox
            sandbox_ids.append(sandbox["id"])

        blocks = [
            _uplink_info_from_sandbox(client, sandbox_id)[0]
            for sandbox_id in sandbox_ids
        ]
        assert len({str(block) for block in blocks}) == len(blocks)

    @staticmethod
    def test_isolated_uplink_reports_error_when_pool_has_no_free_block(client):
        # Policy allows pools up to /24; /30 is rejected at validation time.
        # 169.254.0.0/24 is a valid pool, but every /30 overlaps the reserved
        # link-local range and is filtered out by the allocator.
        pool = "169.254.0.0/24"
        response = client.post("/api/v1/sandboxes", json={
            "policy_mode": "override",
            "policy": _with_runtime_support(
                _isolated_network_policy(uplink_subnet=pool),
            ),
            "sandbox_id": f"uplkex_{uuid.uuid4().hex[:6]}",
        })
        assert response.status_code == 201, response.text
        sandbox = response.json()
        assert sandbox["phase"] == "error", sandbox


class TestNetworkIptables:
    @staticmethod
    def test_iptables_backend_falls_back_to_legacy(monkeypatch):
        select_iptables_binary = getattr(network_module, "_select_iptables_binary")
        select_iptables_binary.cache_clear()

        def fake_candidates(ip_version):
            assert ip_version == 4
            return [
                network_module.IPTABLES_BINARY,
                network_module.IPTABLES_LEGACY_BINARY,
            ]

        def fake_run(binary, args, *, check=True, namespace=None):
            if binary == network_module.IPTABLES_BINARY:
                return network_module.subprocess.CompletedProcess(
                    args=[binary, *args],
                    returncode=3,
                    stdout="",
                    stderr="iptables-nft failed",
                )
            return network_module.subprocess.CompletedProcess(
                args=[binary, *args],
                returncode=0,
                stdout="",
                stderr="",
            )

        monkeypatch.setattr(network_module, "_iptables_candidates", fake_candidates)
        monkeypatch.setattr(network_module, "_run_iptables_binary", fake_run)

        assert select_iptables_binary(4, "test-netns") == (
            network_module.IPTABLES_LEGACY_BINARY
        )

    @staticmethod
    def test_iptables_backend_error_includes_stderr(monkeypatch):
        select_iptables_binary = getattr(network_module, "_select_iptables_binary")
        select_iptables_binary.cache_clear()

        def fake_candidates(ip_version):
            assert ip_version == 4
            return [network_module.IPTABLES_BINARY]

        def fake_run(binary, args, *, check=True, namespace=None):
            return network_module.subprocess.CompletedProcess(
                args=[binary, *args],
                returncode=3,
                stdout="",
                stderr="kernel/userspace mismatch",
            )

        monkeypatch.setattr(network_module, "_iptables_candidates", fake_candidates)
        monkeypatch.setattr(network_module, "_run_iptables_binary", fake_run)

        with pytest.raises(network_module.NetworkSetupError) as exc_info:
            select_iptables_binary(4, "test-netns")

        assert "kernel/userspace mismatch" in str(exc_info.value)


class TestSandboxExec:
    @staticmethod
    def test_exec_requires_running_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        # Stop it first
        client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")

        resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": ["echo", "hello"],
        })
        assert resp.status_code == 409


def _exec_background(client, sandbox_id: str, command: list[str], **kwargs):
    body = {"command": command, **kwargs}
    response = client.post(
        f"/api/v1/sandboxes/{sandbox_id}/exec_background",
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait_background_job_finished(
    client,
    sandbox_id: str,
    job_id: str,
    *,
    timeout: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}")
        assert response.status_code == 200, response.text
        status = response.json()
        if not status.get("running"):
            return status
        time.sleep(0.1)
    raise AssertionError(
        f"background job {job_id!r} did not finish within {timeout}s; last={status}"
    )


class TestBackgroundJobs:
    @staticmethod
    def test_instant_task_reports_exit_code(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        assert create_resp.json()["phase"] == "ready"

        started = _exec_background(
            client,
            sandbox_id,
            ["python3", "--version"],
        )
        assert started["started"] is True
        assert isinstance(started.get("job_id"), str) and started["job_id"]

        status = _wait_background_job_finished(
            client, sandbox_id, started["job_id"],
        )
        assert status["exit_code"] == 0, status
        assert status["stdout"] == ""
        assert status["stderr"] == ""

    @staticmethod
    def test_long_running_job_with_custom_job_id(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        job_id = f"sleep-{uuid.uuid4().hex[:4]}"

        started = _exec_background(
            client,
            sandbox_id,
            ["python3", "-c", "import time; time.sleep(3600)"],
            job_id=job_id,
        )
        assert started["started"] is True
        assert started["job_id"] == job_id
        assert started["running"] is True

        status_resp = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}",
        )
        assert status_resp.status_code == 200, status_resp.text
        status = status_resp.json()
        assert status["running"] is True
        assert status["stdout"] == ""

        list_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}/background")
        assert list_resp.status_code == 200
        job_ids = [item["job_id"] for item in list_resp.json()["items"]]
        assert job_id in job_ids

        kill_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}/kill",
            json={},
        )
        assert kill_resp.status_code == 200
        assert kill_resp.json()["killed"] is True

        finished = _wait_background_job_finished(client, sandbox_id, job_id)
        assert finished["running"] is False

    @staticmethod
    def test_background_output_not_captured(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        started = _exec_background(
            client,
            sandbox_id,
            ["python3", "-c", "print('x' * 10000)"],
        )
        status = _wait_background_job_finished(
            client, sandbox_id, started["job_id"],
        )
        assert status["exit_code"] == 0
        assert status["stdout"] == ""
        assert status["stderr"] == ""

    @staticmethod
    def test_duplicate_job_id_returns_409(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        job_id = f"dup-{uuid.uuid4().hex[:4]}"

        first = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec_background",
            json={
                "command": ["python3", "-c", "import time; time.sleep(3600)"],
                "job_id": job_id,
            },
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec_background",
            json={
                "command": ["python3", "-c", "import time; time.sleep(3600)"],
                "job_id": job_id,
            },
        )
        assert second.status_code == 409, second.text
        assert job_id in second.json()["error"]

        client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}/kill",
            json={},
        )

    @staticmethod
    @pytest.mark.parametrize("invalid_id", ["ab", "ABC123", "my job", "a" * 41])
    def test_invalid_job_id_returns_400(client, invalid_id):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec_background",
            json={
                "command": ["python3", "--version"],
                "job_id": invalid_id,
            },
        )
        assert resp.status_code == 400, resp.text
        assert JOB_ID_FORMAT_MESSAGE in resp.json()["error"]

    @staticmethod
    def test_kill_already_exited_job(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        job_id = f"done-{uuid.uuid4().hex[:4]}"

        started = _exec_background(
            client,
            sandbox_id,
            ["python3", "--version"],
            job_id=job_id,
        )
        _wait_background_job_finished(client, sandbox_id, job_id)

        kill_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}/kill",
            json={},
        )
        assert kill_resp.status_code == 200
        payload = kill_resp.json()
        assert payload["killed"] is False
        assert payload["reason"] == "already_exited"
        assert payload["exit_code"] == 0

    @staticmethod
    def test_kill_unknown_job_returns_404(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/no-such-job/kill",
            json={},
        )
        assert resp.status_code == 404, resp.text

    @staticmethod
    def test_list_running_only(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]

        finished_job = f"fin-{uuid.uuid4().hex[:4]}"
        _exec_background(
            client,
            sandbox_id,
            ["python3", "--version"],
            job_id=finished_job,
        )
        _wait_background_job_finished(client, sandbox_id, finished_job)

        running_job = f"run-{uuid.uuid4().hex[:4]}"
        _exec_background(
            client,
            sandbox_id,
            ["python3", "-c", "import time; time.sleep(3600)"],
            job_id=running_job,
        )

        list_resp = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/background",
            params={"running_only": "true"},
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        job_ids = {item["job_id"] for item in items}
        assert running_job in job_ids
        assert finished_job not in job_ids
        assert all(item["running"] for item in items)

        client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/{running_job}/kill",
            json={},
        )

    @staticmethod
    def test_get_job_after_sandbox_destroy_returns_404(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        job_id = f"gone-{uuid.uuid4().hex[:4]}"

        started = _exec_background(
            client,
            sandbox_id,
            ["python3", "-c", "import time; time.sleep(3600)"],
            job_id=job_id,
        )
        assert started["started"] is True

        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code in (200, 202, 204)

        get_resp = client.get(
            f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}",
        )
        assert get_resp.status_code == 404, get_resp.text

    @staticmethod
    def test_bg_exec_visible_via_exec_ps(client):
        create_resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = create_resp.json()["id"]
        job_id = f"ps-{uuid.uuid4().hex[:4]}"

        started = _exec_background(
            client,
            sandbox_id,
            ["sleep", "3600"],
            job_id=job_id,
        )
        assert started["started"] is True

        ps_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["ps", "-eo", "pid,args"],
                "timeout_seconds": 10,
            },
        )
        assert ps_resp.status_code == 200, ps_resp.text
        assert ps_resp.json()["exit_code"] == 0
        assert "sleep" in ps_resp.json()["stdout"]

        client.post(
            f"/api/v1/sandboxes/{sandbox_id}/background/{job_id}/kill",
            json={},
        )


class TestSandboxListing:
    @staticmethod
    def test_list_returns_all_sandboxes(client):
        for i in range(3):
            client.post("/api/v1/sandboxes", json={})

        resp = client.get("/api/v1/sandboxes")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


def _run_python(client, sandbox_id: str, source: str, *, timeout: int = 10):
    """Run a snippet of Python in the sandbox and return the parsed exec result."""
    response = client.post(
        f"/api/v1/sandboxes/{sandbox_id}/exec",
        json={
            "command": ["python3", "-c", source],
            "timeout_seconds": timeout,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# bind_root_entries fixture layout. Each entry is (relative path, contents):
#   * subdirs are auto-created
#   * regular files get the listed bytes written verbatim
#   * dot-prefixed names exercise ``include_hidden`` semantics
# The mirror of this layout lives on the box-server's filesystem (not on the
# pytest host) because the box-server typically runs inside Docker and the
# test process runs outside; see ``_seed_bind_root_entries_host_tree`` for the
# rationale.
_BIND_ROOT_ENTRIES_LAYOUT: list[tuple[str, str]] = [
    ("file1.txt", "hello-file1"),
    ("nested_dir/inner.txt", "hello-inner"),
    (".hidden.txt", "secret"),
    ("skip_me/sentinel", "x"),
]


def _seed_bind_root_entries_host_tree(
    client,
    tracked_ids: list[str],
    *,
    fixture_label: str,
) -> str:
    """Seed a fixed bind_root_entries layout on the box-server's filesystem.

    The integration suite typically runs the box-server inside Docker while
    the pytest process runs on the host, so ``tmp_path`` is not visible to
    ``bind_root_entries`` (which resolves paths in the box-server's mount
    namespace). To work around that we spin up a short-lived "setup sandbox"
    with ``/tmp`` bind-mounted to ``/host-tmp rw`` and seed the fixture there
    via a single ``exec`` Python script. The fixture directory lives under
    the box-server's ``/tmp`` and is returned as an absolute host path that
    can be plugged directly into ``bind_root_entries.host_root``.
    """
    fixture_dir_name = f"jiuwenbox-bind-root-entries-{fixture_label}-{uuid.uuid4().hex[:8]}"
    sandbox_setup_root = f"/host-tmp/{fixture_dir_name}"
    server_host_root = f"/tmp/{fixture_dir_name}"

    setup_resp = client.post(
        "/api/v1/sandboxes",
        json={
            "policy_mode": "override",
            "policy": _with_runtime_support({
                "name": f"bind-root-entries-setup-{fixture_label}",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp", "/host-tmp"],
                    "bind_mounts": [{
                        "host_path": "/tmp",
                        "sandbox_path": "/host-tmp",
                        "mode": "rw",
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            }),
        },
    )
    assert setup_resp.status_code == 201, setup_resp.text
    setup_id = setup_resp.json()["id"]
    tracked_ids.append(setup_id)

    seed_script = textwrap.dedent(
        f"""
        import os
        import sys

        root = {sandbox_setup_root!r}
        layout = {_BIND_ROOT_ENTRIES_LAYOUT!r}

        os.makedirs(root, exist_ok=True)
        os.chmod(root, 0o755)
        for relpath, content in layout:
            target = os.path.join(root, relpath)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write(content)
            os.chmod(target, 0o644)
        sys.exit(0)
        """
    ).strip()

    seed_result = _run_python(client, setup_id, seed_script, timeout=15)
    assert seed_result["exit_code"] == 0, seed_result

    # Tear the setup sandbox down right away; we keep the seeded files on the
    # box-server's /tmp until the cleanup fixture removes them. Keeping the
    # setup sandbox alive for the duration of the test would mean holding the
    # rw bind on /tmp open, which a stricter policy could legitimately reject.
    delete_resp = client.delete(f"/api/v1/sandboxes/{setup_id}")
    assert delete_resp.status_code in (200, 202, 204), delete_resp.text
    try:
        tracked_ids.remove(setup_id)
    except ValueError:
        pass

    return server_host_root


def _cleanup_bind_root_entries_host_tree(
    client,
    tracked_ids: list[str],
    server_host_root: str,
) -> None:
    """Best-effort removal of files seeded into the box-server's /tmp."""
    if not server_host_root.startswith("/tmp/"):
        return
    cleanup_resp = client.post(
        "/api/v1/sandboxes",
        json={
            "policy_mode": "override",
            "policy": _with_runtime_support({
                "name": "bind-root-entries-cleanup",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp", "/host-tmp"],
                    "bind_mounts": [{
                        "host_path": "/tmp",
                        "sandbox_path": "/host-tmp",
                        "mode": "rw",
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            }),
        },
    )
    if cleanup_resp.status_code != 201:
        logger.warning(
            "bind_root_entries cleanup: failed to start cleanup sandbox (%s)",
            cleanup_resp.text,
        )
        return
    cleanup_id = cleanup_resp.json()["id"]
    tracked_ids.append(cleanup_id)
    try:
        in_sandbox = posixpath.join("/host-tmp", server_host_root[len("/tmp/"):])
        client.post(
            f"/api/v1/sandboxes/{cleanup_id}/exec",
            json={
                "command": ["rm", "-rf", in_sandbox],
                "timeout_seconds": 10,
            },
        )
    finally:
        client.delete(f"/api/v1/sandboxes/{cleanup_id}")
        try:
            tracked_ids.remove(cleanup_id)
        except ValueError:
            pass


@pytest.fixture
def bind_root_entries_host_tree(client, request):
    """Yield an absolute host_root path on the box-server's filesystem.

    Each test gets a uniquely-named directory under the box-server's ``/tmp``
    seeded with ``_BIND_ROOT_ENTRIES_LAYOUT``. Cleanup is best-effort: even
    if the test crashes we run a cleanup sandbox to ``rm -rf`` the seeded
    directory so the box-server's /tmp doesn't accumulate test debris across
    sessions.
    """
    label = request.node.name.replace("test_", "")[:16]
    tracked: list[str] = []
    host_root = _seed_bind_root_entries_host_tree(
        client, tracked, fixture_label=label,
    )
    try:
        yield host_root
    finally:
        _cleanup_bind_root_entries_host_tree(client, tracked, host_root)


class TestBindRootEntries:
    """End-to-end coverage for the ``filesystem_policy.bind_root_entries`` field.

    Each child entry (regular file or subdirectory) directly under ``host_root``
    is bind-mounted into ``sandbox_path/<child_name>`` with the configured
    uniform ``mode``. Hidden entries and excluded basenames are filtered out.
    These tests run against the default jiuwenbox server (which loads
    ``configs/default-policy.yaml``) but always supply their own
    ``filesystem_policy`` in the request, with ``_with_runtime_support`` layering
    the system bind_mounts and directories on top for a runnable rootfs.
    """

    @staticmethod
    def test_bind_root_entries_ro_default_lists_visible_children(
        client,
        create_sandbox_with_policy,
        bind_root_entries_host_tree,
    ):
        host_root = bind_root_entries_host_tree
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "bind-root-entries-ro-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                    "bind_root_entries": [{
                        "host_root": host_root,
                        "sandbox_path": "/mnt/data",
                        "mode": "ro",
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            },
        )

        listing = _run_python(
            client,
            sandbox["id"],
            "import os; print('\\n'.join(sorted(os.listdir('/mnt/data'))))",
        )
        assert listing["exit_code"] == 0, listing
        visible = listing["stdout"].splitlines()
        assert visible == ["file1.txt", "nested_dir", "skip_me"], visible
        assert ".hidden.txt" not in visible

        for path, expected in [
            ("/mnt/data/file1.txt", "hello-file1"),
            ("/mnt/data/nested_dir/inner.txt", "hello-inner"),
        ]:
            result = _run_python(
                client,
                sandbox["id"],
                f"print(open({path!r}).read())",
            )
            assert result["exit_code"] == 0, result
            assert result["stdout"].rstrip("\n") == expected, result

        write_attempt = _run_python(
            client,
            sandbox["id"],
            "open('/mnt/data/file1.txt', 'w').write('nope')",
        )
        assert write_attempt["exit_code"] != 0, write_attempt
        combined = (write_attempt["stdout"] + write_attempt["stderr"]).lower()
        assert any(
            marker in combined
            for marker in ("read-only", "readonly", "erofs", "permission denied")
        ), write_attempt

    @staticmethod
    def test_bind_root_entries_rw_allows_writes(
        client,
        create_sandbox_with_policy,
        bind_root_entries_host_tree,
    ):
        host_root = bind_root_entries_host_tree
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "bind-root-entries-rw-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp", "/mnt/data"],
                    "bind_root_entries": [{
                        "host_root": host_root,
                        "sandbox_path": "/mnt/data",
                        "mode": "rw",
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            },
        )

        overwrite = _run_python(
            client,
            sandbox["id"],
            "open('/mnt/data/file1.txt', 'w').write('rewritten')",
        )
        assert overwrite["exit_code"] == 0, overwrite
        read_back = _run_python(
            client,
            sandbox["id"],
            "print(open('/mnt/data/file1.txt').read())",
        )
        assert read_back["exit_code"] == 0, read_back
        assert read_back["stdout"].rstrip("\n") == "rewritten", read_back

        nested_write = _run_python(
            client,
            sandbox["id"],
            (
                "open('/mnt/data/nested_dir/new.txt', 'w').write('new-content'); "
                "print(open('/mnt/data/nested_dir/new.txt').read())"
            ),
        )
        assert nested_write["exit_code"] == 0, nested_write
        assert nested_write["stdout"].rstrip("\n") == "new-content", nested_write

        # Round-trip the writes through a separate verification sandbox so we
        # confirm the bind actually shares the host inode rather than landing
        # on a tmpfs unique to this sandbox.
        verify_sandbox = create_sandbox_with_policy(
            policy={
                "name": "bind-root-entries-rw-verify-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                    "bind_mounts": [{
                        "host_path": host_root,
                        "sandbox_path": "/mnt/host-view",
                        "mode": "ro",
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            },
        )
        verify_one = _run_python(
            client,
            verify_sandbox["id"],
            "print(open('/mnt/host-view/file1.txt').read())",
        )
        assert verify_one["exit_code"] == 0, verify_one
        assert verify_one["stdout"].rstrip("\n") == "rewritten", verify_one
        verify_two = _run_python(
            client,
            verify_sandbox["id"],
            "print(open('/mnt/host-view/nested_dir/new.txt').read())",
        )
        assert verify_two["exit_code"] == 0, verify_two
        assert verify_two["stdout"].rstrip("\n") == "new-content", verify_two

    @staticmethod
    def test_bind_root_entries_exclude_and_include_hidden(
        client,
        create_sandbox_with_policy,
        bind_root_entries_host_tree,
    ):
        host_root = bind_root_entries_host_tree
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "bind-root-entries-filter-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                    "bind_root_entries": [{
                        "host_root": host_root,
                        "sandbox_path": "/mnt/data",
                        "mode": "ro",
                        "include_hidden": True,
                        # ``*.txt`` should drop ``file1.txt`` and ``.hidden.txt``
                        # so we get a clean assertion that hidden inclusion and
                        # fnmatch exclusion compose as expected.
                        "exclude": ["skip_me", "*.txt"],
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            },
        )

        listing = _run_python(
            client,
            sandbox["id"],
            "import os; print('\\n'.join(sorted(os.listdir('/mnt/data'))))",
        )
        assert listing["exit_code"] == 0, listing
        visible = listing["stdout"].splitlines()
        # ``nested_dir`` is the only child not caught by either exclude pattern.
        assert visible == ["nested_dir"], visible

    @staticmethod
    def test_bind_root_entries_missing_host_root_is_noop(
        client,
        create_sandbox_with_policy,
    ):
        # Pick a box-server-side path that is guaranteed not to exist. We use
        # /tmp/<uuid> rather than tmp_path because tmp_path lives on the pytest
        # host while the box-server typically runs inside Docker - the two
        # processes see different /tmp filesystems and we want this assertion
        # to be independent of that topology.
        missing = f"/tmp/jiuwenbox-bind-root-entries-missing-{uuid.uuid4().hex}"
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "bind-root-entries-missing-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                    "bind_root_entries": [{
                        "host_root": missing,
                        "sandbox_path": "/mnt/data",
                        "mode": "ro",
                    }],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            },
        )
        assert sandbox["phase"] == "ready", sandbox

        probe = _run_python(
            client,
            sandbox["id"],
            (
                "import os; "
                "print('exists=', os.path.exists('/mnt/data')); "
                "print('contents=', sorted(os.listdir('/mnt/data')) "
                "  if os.path.isdir('/mnt/data') else [])"
            ),
        )
        assert probe["exit_code"] == 0, probe
        lines = probe["stdout"].splitlines()
        # Either the parent dir was never created (no children to mount) or it
        # exists but is empty. Both outcomes are acceptable; what matters is
        # that the sandbox came up cleanly and host_root absence didn't error.
        assert lines[0] in ("exists= False", "exists= True"), probe
        assert lines[1] == "contents= []", probe

    @staticmethod
    def test_policy_api_round_trips_bind_root_entries(
        client,
        create_sandbox_with_policy,
        bind_root_entries_host_tree,
    ):
        host_root = bind_root_entries_host_tree
        nested_host_root = f"{host_root}/nested_dir"
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "bind-root-entries-roundtrip-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                    "bind_root_entries": [
                        {
                            "host_root": host_root,
                            "sandbox_path": "/mnt/data",
                            "mode": "ro",
                        },
                        {
                            "host_root": nested_host_root,
                            "sandbox_path": "/mnt/nested",
                            "mode": "rw",
                            "include_hidden": True,
                            "exclude": ["skip_me"],
                        },
                    ],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
            },
        )

        resp = client.get(f"/api/v1/policies/{sandbox['id']}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        round_tripped = data["filesystem_policy"]["bind_root_entries"]
        assert round_tripped == [
            {
                "host_root": host_root,
                "sandbox_path": "/mnt/data",
                "mode": "ro",
                "include_hidden": False,
                "exclude": [],
            },
            {
                "host_root": nested_host_root,
                "sandbox_path": "/mnt/nested",
                "mode": "rw",
                "include_hidden": True,
                "exclude": ["skip_me"],
            },
        ], round_tripped

    @staticmethod
    def test_bind_mount_rejects_star_host_path(client):
        resp = client.post(
            "/api/v1/sandboxes",
            json={
                "policy": {
                    "name": "bind-mount-star-rejected",
                    "filesystem_policy": {
                        "bind_mounts": [{
                            "host_path": "*",
                            "sandbox_path": "/mnt/all",
                            "mode": "ro",
                        }],
                    },
                    "network": {"mode": "host"},
                },
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        message = body.get("error") or body.get("detail") or ""
        assert "bind_root_entries" in message, body

    @staticmethod
    def test_bind_root_entries_root_with_overriding_rw_bind_mount(
        client,
        create_sandbox_with_policy,
    ):
        """``bind_root_entries`` covers the whole host root ``/`` read-only,
        then ``bind_mounts`` re-mounts ``/app/build`` at the *same* path with
        read-write permissions.

        Layering order inside bwrap:

        1. ``--ro-bind`` for each immediate child of host ``/`` at sandbox
           ``/<child>`` (emitted by ``bind_root_entries``). The kernel
           pseudo filesystems (``proc``, ``dev``, ``sys``, ``run``) and the
           bwrap-managed scratch dirs (``tmp``, ``home``) are excluded so
           we don't shadow bwrap's own ``--proc`` / ``--dev`` mounts or the
           ``directories`` block injected by ``_with_runtime_support``.
        2. ``--bind /app/build /app/build`` (emitted by ``bind_mounts``)
           overlays the rw mount on top of the ro ``/app`` bind at the very
           same sandbox path.

        The net effect is: the entire rootfs (including ``/app``,
        ``/app/pyproject.toml``, ``/usr``, ``/etc``, ...) is ro, but
        ``/app/build`` is writable. We deliberately use the same
        ``host_path`` and ``sandbox_path`` for the rw overlay so the test
        mirrors a real "ro by default, selected rw" mount strategy rather
        than aliasing ``/app/build`` to an unrelated backing directory.
        """
        # /app exists in the box-server's image (Dockerfile WORKDIR=/app)
        # but /app/build does not. We need it to exist on the host so
        # ``bind_mounts`` can resolve it. Use a privileged setup sandbox
        # that rw-binds host /app into the sandbox and creates the dir
        # there. Run as root so the dir creation and chmod succeed; chmod
        # 0o0777 + 0o0666 ensures the subsequent unprivileged test sandbox
        # (uid 65534) can both read the seeded binary and write fresh
        # files.
        setup_resp = client.post(
            "/api/v1/sandboxes",
            json={
                "policy_mode": "override",
                "policy": _with_runtime_support({
                    "name": "bind-root-entries-app-build-setup",
                    "filesystem_policy": {
                        "bind_mounts": [{
                            "host_path": "/app",
                            "sandbox_path": "/app",
                            "mode": "rw",
                        }],
                    },
                    "process": {
                        "run_as_user": "root",
                        "run_as_group": "root",
                    },
                    "landlock": {"compatibility": "disabled"},
                    "network": {"mode": "host"},
                }),
            },
        )
        assert setup_resp.status_code == 201, setup_resp.text
        setup_id = setup_resp.json()["id"]
        try:
            seed_script = textwrap.dedent(
                """
                import os
                os.makedirs("/app/build", exist_ok=True)
                os.chmod("/app/build", 0o0777)
                with open("/app/build/binary", "w") as fh:
                    fh.write("v1-from-host")
                os.chmod("/app/build/binary", 0o0666)
                """
            ).strip()
            seed_result = _run_python(client, setup_id, seed_script, timeout=15)
            assert seed_result["exit_code"] == 0, seed_result
        finally:
            client.delete(f"/api/v1/sandboxes/{setup_id}")

        try:
            sandbox = create_sandbox_with_policy(
                policy={
                    "name": "bind-root-entries-app-build-rw-overlay-policy",
                    "filesystem_policy": {
                        # Mount every immediate child of host / at the
                        # corresponding sandbox path read-only. /app is one
                        # of those children and lands recursively at sandbox
                        # /app (including /app/build) as ro. Kernel pseudo
                        # filesystems and bwrap-managed scratch dirs are
                        # excluded so they don't clobber bwrap's own
                        # --proc/--dev/--tmpfs setup.
                        "bind_root_entries": [{
                            "host_root": "/",
                            "sandbox_path": "/",
                            "mode": "ro",
                            "exclude": [
                                "proc",
                                "dev",
                                "sys",
                                "run",
                                "tmp",
                                "home",
                            ],
                        }],
                        # Re-mount /app/build at the *same* sandbox path
                        # with rw permissions. bwrap layers this --bind on
                        # top of the earlier --ro-bind /app, so /app/build
                        # becomes writable while the rest of /app (e.g.
                        # /app/pyproject.toml) and the rest of the rootfs
                        # (e.g. /etc/passwd, /usr/bin/python3) stay
                        # read-only.
                        "bind_mounts": [{
                            "host_path": "/app/build",
                            "sandbox_path": "/app/build",
                            "mode": "rw",
                        }],
                    },
                    "landlock": {"compatibility": "disabled"},
                    "network": {"mode": "host"},
                },
            )
            assert sandbox["phase"] == "ready", sandbox

            # /app/pyproject.toml is one of the children that bind_root_entries
            # mounted ro. Reading it confirms the ro side of /app is wired up.
            read_pyproject = _run_python(
                client,
                sandbox["id"],
                "import os; print(os.path.isfile('/app/pyproject.toml'))",
            )
            assert read_pyproject["exit_code"] == 0, read_pyproject
            assert read_pyproject["stdout"].rstrip("\n") == "True", read_pyproject

            # Writing to a file inside /app but outside /app/build must fail
            # because that path is still ro from bind_root_entries.
            write_pyproject = _run_python(
                client,
                sandbox["id"],
                "open('/app/pyproject.toml', 'w').write('nope')",
            )
            assert write_pyproject["exit_code"] != 0, write_pyproject
            combined = (
                write_pyproject["stdout"] + write_pyproject["stderr"]
            ).lower()
            assert any(
                marker in combined
                for marker in ("read-only", "readonly", "erofs", "permission denied")
            ), write_pyproject

            # /app/build is the rw overlay. The pre-seeded binary file must
            # round-trip from the host through the rw bind, and the sandbox
            # must be able to add a fresh file there.
            read_existing = _run_python(
                client,
                sandbox["id"],
                "print(open('/app/build/binary').read())",
            )
            assert read_existing["exit_code"] == 0, read_existing
            assert read_existing["stdout"].rstrip("\n") == "v1-from-host", read_existing

            write_new = _run_python(
                client,
                sandbox["id"],
                (
                    "open('/app/build/new.txt', 'w').write('v2-from-sandbox'); "
                    "print(open('/app/build/new.txt').read())"
                ),
            )
            assert write_new["exit_code"] == 0, write_new
            assert write_new["stdout"].rstrip("\n") == "v2-from-sandbox", write_new

            # Independent verification: a fresh sandbox that only ro-binds
            # host /app/build observes the same file the test sandbox just
            # wrote, proving the rw bind shares the host inode rather than a
            # per-sandbox tmpfs.
            verify_sandbox = create_sandbox_with_policy(
                policy={
                    "name": "bind-root-entries-app-build-verify-policy",
                    "filesystem_policy": {
                        "bind_mounts": [{
                            "host_path": "/app/build",
                            "sandbox_path": "/mnt/build-view",
                            "mode": "ro",
                        }],
                    },
                    "landlock": {"compatibility": "disabled"},
                    "network": {"mode": "host"},
                },
            )
            verify = _run_python(
                client,
                verify_sandbox["id"],
                "print(open('/mnt/build-view/new.txt').read())",
            )
            assert verify["exit_code"] == 0, verify
            assert verify["stdout"].rstrip("\n") == "v2-from-sandbox", verify
        finally:
            # Best-effort cleanup: remove /app/build so the box-server's
            # /app doesn't accumulate state between test runs. Needs the
            # same privileged setup as the seed step.
            cleanup_resp = client.post(
                "/api/v1/sandboxes",
                json={
                    "policy_mode": "override",
                    "policy": _with_runtime_support({
                        "name": "bind-root-entries-app-build-cleanup",
                        "filesystem_policy": {
                            "bind_mounts": [{
                                "host_path": "/app",
                                "sandbox_path": "/app",
                                "mode": "rw",
                            }],
                        },
                        "process": {
                            "run_as_user": "root",
                            "run_as_group": "root",
                        },
                        "landlock": {"compatibility": "disabled"},
                        "network": {"mode": "host"},
                    }),
                },
            )
            if cleanup_resp.status_code == 201:
                cleanup_id = cleanup_resp.json()["id"]
                try:
                    client.post(
                        f"/api/v1/sandboxes/{cleanup_id}/exec",
                        json={
                            "command": ["rm", "-rf", "/app/build"],
                            "timeout_seconds": 10,
                        },
                    )
                finally:
                    client.delete(f"/api/v1/sandboxes/{cleanup_id}")


# ---------------------------------------------------------------------------
# Cgroup policy field
# ---------------------------------------------------------------------------


_CGROUP_PROBE_POLICY: dict[str, object] = {
    "name": "cgroup-probe",
    "filesystem_policy": {
        "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
        "read_write": ["/tmp"],
    },
    "landlock": {"compatibility": "disabled"},
    "network": {"mode": "host"},
    "cgroup": {"pids_max": 1024},
}

# Module-level cache for the cgroup-probe result. The first ``_cgroup_supported``
# fixture invocation pays the one-time cost of creating a sandbox with a real
# cgroup policy; subsequent invocations short-circuit on the cached verdict so
# we don't slow the rest of the suite down. ``None`` means "not probed yet";
# ``True`` / ``False`` is the cached verdict.
_cgroup_probe_cached: bool | None = None
_cgroup_probe_skip_reason: str | None = None


def _is_cgroup_unsupported_error(text: str) -> bool:
    """Return True when ``text`` looks like a cgroup-not-writable diagnostic.

    Both the runtime layer and the cgroup backend wrap the underlying
    ``CgroupSetupError`` into different strings (``RuntimeError`` from
    ``ProcessRuntime.create``, plain ``CgroupSetupError`` from the backend
    helper). Matching the lowercased message keeps the probe robust against
    future wording changes.
    """
    lowered = (text or "").lower()
    return "cgroup" in lowered and (
        "cgroupsetuperror" in lowered
        or "no writable cgroup backend" in lowered
        or "failed to apply cgroup limits" in lowered
        or "failed to write" in lowered
        or "failed to create" in lowered
        or "failed to enable controllers" in lowered
        or "permission denied" in lowered
    )


class TestCgroupPolicy:
    """End-to-end coverage for the ``cgroup`` policy field.

    Tests are split into two groups:

    - Group A is independent of cgroup availability and exercises Pydantic
      validation plus the "no cgroup field -> skip" early-exit. Every test
      in this group must pass even on hosts without a writable cgroup tree.
    - Group B exercises actual kernel enforcement (memory.max, pids.max,
      cpu.max) and depends on ``_cgroup_supported`` to skip when the host
      lacks a writable cgroup backend.
    """

    # ------------------------------------------------------------------
    # Group A: cgroup-independent
    # ------------------------------------------------------------------

    @staticmethod
    def _expect_no_limit_policy(extra_cgroup: dict | None) -> dict:
        """Build a policy dict whose ``cgroup`` part should be a no-op.

        ``extra_cgroup`` lets each test pick the exact equivalent form
        (omitted, ``{}``, all-null) we want to assert is treated the same
        way as the others.
        """
        policy: dict[str, object] = {
            "name": "cgroup-skip",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                "read_write": ["/tmp"],
            },
            "landlock": {"compatibility": "disabled"},
            "network": {"mode": "host"},
        }
        if extra_cgroup is not None:
            policy["cgroup"] = extra_cgroup
        return policy

    @staticmethod
    def _assert_skip_path_runs_cleanly(client, sandbox_id: str) -> None:
        """Allocate 64MB inside the sandbox to prove no cgroup limit is in
        play. We don't push to 128MB to avoid spurious OOM on CI machines
        whose docker containers are constrained by the *parent* cgroup.
        """
        result = _run_python(
            client,
            sandbox_id,
            (
                "import sys; "
                "buf = bytearray(64 * 1024 * 1024); "
                "print('len=', len(buf), 'last=', buf[-1])"
            ),
            timeout=15,
        )
        assert result["exit_code"] == 0, result
        assert "len= 67108864" in result["stdout"], result

    @staticmethod
    def test_no_cgroup_field_skips_setup_entirely(client, create_sandbox_with_policy):
        sandbox = create_sandbox_with_policy(
            policy=TestCgroupPolicy._expect_no_limit_policy(None),
        )
        TestCgroupPolicy._assert_skip_path_runs_cleanly(client, sandbox["id"])

    @staticmethod
    def test_empty_cgroup_field_skips_setup(client, create_sandbox_with_policy):
        sandbox = create_sandbox_with_policy(
            policy=TestCgroupPolicy._expect_no_limit_policy({}),
        )
        TestCgroupPolicy._assert_skip_path_runs_cleanly(client, sandbox["id"])

    @staticmethod
    def test_all_null_cgroup_field_skips_setup(client, create_sandbox_with_policy):
        sandbox = create_sandbox_with_policy(
            policy=TestCgroupPolicy._expect_no_limit_policy({
                "memory_max": None,
                "cpu_max": None,
                "pids_max": None,
            }),
        )
        TestCgroupPolicy._assert_skip_path_runs_cleanly(client, sandbox["id"])

    @staticmethod
    def test_cgroup_rejects_invalid_memory_max(client):
        resp = client.post(
            "/api/v1/sandboxes",
            json={
                "policy_mode": "override",
                "policy": _with_runtime_support({
                    "name": "cgroup-invalid-memory",
                    "filesystem_policy": {
                        "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                        "read_write": ["/tmp"],
                    },
                    "landlock": {"compatibility": "disabled"},
                    "network": {"mode": "host"},
                    "cgroup": {"memory_max": "not-a-size"},
                }),
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        message = body.get("error") or body.get("detail") or ""
        assert "memory_max" in message, body

    @staticmethod
    def test_cgroup_rejects_invalid_cpu_max(client):
        resp = client.post(
            "/api/v1/sandboxes",
            json={
                "policy_mode": "override",
                "policy": _with_runtime_support({
                    "name": "cgroup-invalid-cpu",
                    "filesystem_policy": {
                        "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                        "read_write": ["/tmp"],
                    },
                    "landlock": {"compatibility": "disabled"},
                    "network": {"mode": "host"},
                    "cgroup": {"cpu_max": "weird"},
                }),
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        message = body.get("error") or body.get("detail") or ""
        assert "cpu_max" in message, body

    @staticmethod
    def test_cgroup_rejects_invalid_pids_max(client):
        resp = client.post(
            "/api/v1/sandboxes",
            json={
                "policy_mode": "override",
                "policy": _with_runtime_support({
                    "name": "cgroup-invalid-pids",
                    "filesystem_policy": {
                        "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                        "read_write": ["/tmp"],
                    },
                    "landlock": {"compatibility": "disabled"},
                    "network": {"mode": "host"},
                    "cgroup": {"pids_max": -1},
                }),
            },
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        message = body.get("error") or body.get("detail") or ""
        assert "pids_max" in message, body

    # ------------------------------------------------------------------
    # Group B: requires a writable cgroup backend
    # ------------------------------------------------------------------

    @pytest.fixture
    def _cgroup_supported(self, client):
        """Probe the box-server for a writable cgroup backend by trying to
        create a sandbox with a minimal cgroup policy.

        The probe result is cached at module level (``_cgroup_probe_cached``)
        so only the first test in the group actually pays the
        sandbox-create cost; subsequent tests short-circuit on the cached
        verdict. We can't use ``scope="class"`` here because ``client``
        is function-scoped (pytest ``ScopeMismatch``), and we don't want
        to broaden the client fixture's scope just for this probe.
        """
        global _cgroup_probe_cached, _cgroup_probe_skip_reason
        if _cgroup_probe_cached is False:
            pytest.skip(_cgroup_probe_skip_reason or "cgroup not writable")
        if _cgroup_probe_cached is None:
            resp = client.post(
                "/api/v1/sandboxes",
                json={
                    "policy_mode": "override",
                    "policy": _with_runtime_support(_CGROUP_PROBE_POLICY),
                },
            )
            if resp.status_code == 201:
                sandbox_id = resp.json()["id"]
                client.delete(f"/api/v1/sandboxes/{sandbox_id}")
                _cgroup_probe_cached = True
            else:
                _cgroup_probe_cached = False
                if resp.status_code >= 500 and _is_cgroup_unsupported_error(resp.text):
                    _cgroup_probe_skip_reason = (
                        "cgroup not writable in this environment"
                    )
                else:
                    _cgroup_probe_skip_reason = (
                        f"cgroup probe sandbox failed to start "
                        f"(status={resp.status_code}): {resp.text}"
                    )
                pytest.skip(_cgroup_probe_skip_reason)
        return True

    @staticmethod
    def test_cgroup_policy_round_trips_via_api(
        client,
        _cgroup_supported,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "cgroup-roundtrip-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
                "cgroup": {
                    "memory_max": "256M",
                    "cpu_max": "0.5",
                    "pids_max": 64,
                },
            },
        )

        resp = client.get(f"/api/v1/policies/{sandbox['id']}")
        assert resp.status_code == 200, resp.text
        cgroup_data = resp.json().get("cgroup")
        assert cgroup_data is not None, resp.text
        assert cgroup_data["memory_max"] == 256 * 1024 * 1024, cgroup_data
        # ``cpu_max`` is a Python tuple internally; FastAPI serializes it
        # as a 2-element list.
        assert cgroup_data["cpu_max"] == [50000, 100000], cgroup_data
        assert cgroup_data["pids_max"] == 64, cgroup_data

    @staticmethod
    def test_cgroup_memory_max_kills_offending_process(
        client,
        _cgroup_supported,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "cgroup-memory-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
                "cgroup": {"memory_max": "32M"},
            },
        )
        # Allocate 128MB - 4x the 32M cap. Touching the buffer forces real
        # page allocation so the OOM killer (cgroup v2) or memory.failcnt
        # (cgroup v1) actually fires. Without the touch the kernel might
        # only reserve virtual address space.
        result = _run_python(
            client,
            sandbox["id"],
            (
                "buf = bytearray(128 * 1024 * 1024); "
                "buf[::4096] = b'x' * (len(buf) // 4096); "
                "print('survived')"
            ),
            timeout=30,
        )
        assert result["exit_code"] != 0, result
        assert "survived" not in result["stdout"], result
        combined = (result["stdout"] + result["stderr"]).lower()
        # The python process is either killed by SIGKILL (OOM kill) or
        # raises MemoryError before printing "survived"; both outcomes
        # confirm memory_max enforcement.
        assert (
            "memoryerror" in combined
            or "killed" in combined
            or "cannot allocate memory" in combined
            or result["exit_code"] < 0
            or result["exit_code"] == 137  # 128 + SIGKILL
            or result["exit_code"] == 9
        ), result

    @staticmethod
    def test_cgroup_pids_max_limits_fork_count(
        client,
        _cgroup_supported,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "cgroup-pids-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
                "cgroup": {"pids_max": 5},
            },
        )
        # Try to fork 32 children. With pids_max=5 the cgroup should reject
        # at least one fork with EAGAIN well before we get to 32. We catch
        # OSError so the script prints how many succeeded before bailing.
        script = textwrap.dedent(
            """
            import os
            import sys
            import time

            children = []
            try:
                for _ in range(32):
                    pid = os.fork()
                    if pid == 0:
                        time.sleep(2)
                        os._exit(0)
                    children.append(pid)
            except OSError as exc:
                print('blocked_after=', len(children), 'errno=', exc.errno)
            else:
                print('blocked_after=', len(children), 'errno= none')
            finally:
                for pid in children:
                    try:
                        os.kill(pid, 9)
                        os.waitpid(pid, 0)
                    except OSError:
                        pass
            sys.exit(0)
            """
        ).strip()
        result = _run_python(client, sandbox["id"], script, timeout=20)
        assert result["exit_code"] == 0, result
        stdout = result["stdout"]
        # The exact threshold depends on whether the parent process and
        # any helper threads count toward pids_max, but in every realistic
        # case at least a few forks must fail before we reach 32.
        assert "errno= 11" in stdout or "errno= 35" in stdout, result
        # Extract ``blocked_after=`` value and assert it's well below 32.
        marker = "blocked_after="
        idx = stdout.find(marker)
        assert idx >= 0, result
        blocked_value = stdout[idx + len(marker):].split()[0]
        assert int(blocked_value) < 32, result

    @staticmethod
    def test_cgroup_cpu_max_throttles_busy_loop(
        client,
        _cgroup_supported,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            policy={
                "name": "cgroup-cpu-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "landlock": {"compatibility": "disabled"},
                "network": {"mode": "host"},
                "cgroup": {"cpu_max": "0.1"},
            },
        )
        # Spin for ~2 seconds of wall-clock time and report how much CPU
        # time we actually got. With cpu_max=0.1 cores, process_time
        # should be ~10% of wall-clock or less; we assert <30% to leave a
        # generous margin for measurement noise on busy CI hosts.
        script = textwrap.dedent(
            """
            import time

            target_wall = 2.0
            start_wall = time.monotonic()
            start_cpu = time.process_time()
            while time.monotonic() - start_wall < target_wall:
                pass
            wall = time.monotonic() - start_wall
            cpu = time.process_time() - start_cpu
            print('wall=', wall)
            print('cpu=', cpu)
            print('ratio=', cpu / wall if wall > 0 else 0)
            """
        ).strip()
        result = _run_python(client, sandbox["id"], script, timeout=20)
        assert result["exit_code"] == 0, result
        ratio_line = next(
            (line for line in result["stdout"].splitlines() if line.startswith("ratio=")),
            "",
        )
        assert ratio_line, result
        try:
            ratio_value = float(ratio_line.split("=", 1)[1].strip())
        except ValueError:
            pytest.fail(f"unparseable ratio line: {ratio_line!r}; full: {result}")
        # Heuristic threshold. cpu_max=0.1 caps CPU usage at ~10% on a
        # single core; on multi-core busy CI hosts ratios slightly above
        # 0.1 are tolerable, but anything north of 0.3 indicates the
        # cgroup didn't actually throttle the process.
        assert ratio_value < 0.3, (ratio_value, result)
