"""Integration tests for box-server API endpoints."""

import copy
import logging
import textwrap

import httpx
import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.supervisor.bwrap import BwrapConfig

LONG_RUNNING_COMMAND = ["/usr/bin/python3", "-c", "import time; time.sleep(36000)"]
SYSTEM_BIND_MOUNTS = [
    {"host_path": "/bin", "sandbox_path": "/bin", "mode": "ro"},
    {"host_path": "/sbin", "sandbox_path": "/sbin", "mode": "ro"},
    {"host_path": "/usr", "sandbox_path": "/usr", "mode": "ro"},
    {"host_path": "/lib", "sandbox_path": "/lib", "mode": "ro"},
    {"host_path": "/lib64", "sandbox_path": "/lib64", "mode": "ro"},
    {"host_path": "/etc/resolv.conf", "sandbox_path": "/etc/resolv.conf", "mode": "ro"},
    {"host_path": "/etc/hosts", "sandbox_path": "/etc/hosts", "mode": "ro"},
    {"host_path": "/etc/nsswitch.conf", "sandbox_path": "/etc/nsswitch.conf", "mode": "ro"},
    {"host_path": "/etc/host.conf", "sandbox_path": "/etc/host.conf", "mode": "ro"},
    {"host_path": "/etc/ssl/certs", "sandbox_path": "/etc/ssl/certs", "mode": "ro"},
    {"host_path": "/etc/ssl/openssl.cnf", "sandbox_path": "/etc/ssl/openssl.cnf", "mode": "ro"},
    {"host_path": "/opt", "sandbox_path": "/opt", "mode": "ro"},
]
TMP_DIRECTORY = {"path": "/tmp", "permissions": "1777"}
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


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint if "://" in endpoint else f"http://{endpoint}"


def _sandbox_health_url(server_endpoint: str) -> str:
    return f"{_normalize_endpoint(server_endpoint).rstrip('/')}/health"


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
    if (
        "/tmp" in filesystem_policy.get("read_write", [])
        and not _has_directory(directories, "/tmp")
        and not _has_bind_mount(bind_mounts, "/tmp")
    ):
        directories.append(TMP_DIRECTORY.copy())

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
    with httpx.Client(base_url=_normalize_endpoint(server_endpoint), timeout=30.0) as external:
        tracking = SandboxTrackingClient(external)
        try:
            yield tracking
        finally:
            tracking.cleanup_sandboxes()


@pytest.fixture
def create_sandbox_with_policy(client):
    def factory(
        *,
        name_prefix: str,
        policy: dict,
        policy_mode: str = "override",
        command: list[str] | None = None,
    ) -> dict:
        response = client.post("/api/v1/sandboxes", json={
            "command": command or LONG_RUNNING_COMMAND,
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
        resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo", "hello"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "name" not in data
        assert data["phase"] in ("provisioning", "ready", "error")

    @staticmethod
    def test_list_sandboxes_after_create(client):
        create_resp = client.post("/api/v1/sandboxes", json={"command": ["/usr/bin/echo"]})
        sandbox_id = create_resp.json()["id"]
        resp = client.get("/api/v1/sandboxes")
        assert resp.status_code == 200
        data = resp.json()
        assert any(item["id"] == sandbox_id for item in data)
        assert len(data) == 1

    @staticmethod
    def test_get_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo"],
        })
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
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo"],
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert resp.status_code == 204

        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert resp.status_code == 404


class TestSandboxLifecycle:
    @staticmethod
    def test_start_stopped_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/sleep", "3600"],
        })
        sandbox_id = create_resp.json()["id"]

        stop_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert stop_resp.status_code == 200
        assert stop_resp.json()["phase"] == "stopped"

        start_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/start")
        assert start_resp.status_code == 200
        assert start_resp.json()["phase"] == "ready", start_resp.json()

    @staticmethod
    def test_stop_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/sleep", "3600"],
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "stopped"

    @staticmethod
    def test_restart_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/sleep", "3600"],
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/restart")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "ready"

    @staticmethod
    def test_get_logs(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo"],
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}/logs")
        assert resp.status_code == 200


class TestPolicyAPI:
    @staticmethod
    def test_get_sandbox_policy(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo"],
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 1
        assert data["name"] == "server-default"
        assert "resources" not in data
        assert data["filesystem_policy"]["directories"] == [{'path': '/home', 'permissions': '0755'},
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
        assert data["network"]["mode"] == "host"
        assert data["network"]["egress"]["allowed_domains"] == ["baidu.com"]
        assert data["network"]["egress"]["allowed_ips"] == ["127.0.0.1/32", "::1/128"]
        assert data["network"]["egress"]["blocked_ips"] == ["169.254.169.254/32"]
        assert data["network"]["egress"]["blocked_ports"] == [22]
        assert data["network"]["egress"]["default"] == "allow"
        assert data["network"]["egress"]["blocked_domains"] == ["ip.me"]
        assert data["network"]["egress"]["allowed_ports"] == [443, 80]
        assert data["network"]["ingress"]["default"] == "deny"
        assert data["network"]["ingress"]["allowed_domains"] == ["localhost"]
        assert data["network"]["ingress"]["allowed_ips"] == ["127.0.0.1/32", "::1/128"]
        assert data["network"]["ingress"]["blocked_ips"] == []
        assert data["network"]["ingress"]["allowed_ports"] == [8080]
        assert data["network"]["ingress"]["blocked_ports"] == [22]
        assert "profile" not in data["syscall"]
        assert "mount" in data["syscall"]["blocked"]
        assert "kexec_file_load" in data["syscall"]["blocked"]

    @staticmethod
    def test_append_policy_merges_with_server_default(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo"],
            "policy_mode": "append",
            "policy": {
                "name": "appended-policy",
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
                    "blocked": ["getpid"],
                },
            },
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "appended-policy"
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
        assert data["filesystem_policy"]["directories"] == [{'path': '/home', 'permissions': '0755'},
                                                            {'path': '/tmp', 'permissions': '1777'},
                                                            {"path": "/tmp/appended-dir", "permissions": "0700"}]
        assert data["filesystem_policy"]["bind_mounts"] == SYSTEM_BIND_MOUNTS + [{
            "host_path": "/tmp",
            "sandbox_path": "/tmp",
            "mode": "rw",
        }]
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
        assert "getpid" in data["syscall"]["blocked"]
        assert "mount" in data["syscall"]["blocked"]

    @staticmethod
    def test_override_policy_replaces_server_default(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo"],
            "policy_mode": "override",
            "policy": {
                "name": "override-policy",
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
                    "blocked": ["getppid"],
                },
            },
        })
        sandbox_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/policies/{sandbox_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "override-policy"
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
        assert data["syscall"]["blocked"] == ["getppid"]

    @staticmethod
    def test_get_nonexistent_policy(client):
        resp = client.get("/api/v1/policies/nonexistent")
        assert resp.status_code == 404

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_bind_mount(client):
        resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo", "hello"],
            "policy": {
                "name": "bad-sandbox-mount-policy",
                "filesystem_policy": {
                    "bind_mounts": [{
                        "host_path": "/sandbox/manual",
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
        assert "/sandbox" in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_path(client):
        resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo", "hello"],
            "policy": {
                "name": "bad-sandbox-path-policy",
                "filesystem_policy": {
                    "read_write": ["/sandbox/manual"],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert "/sandbox" in resp.json()["error"]

    @staticmethod
    def test_create_sandbox_rejects_direct_sandbox_directory(client):
        resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/echo", "hello"],
            "policy": {
                "name": "bad-sandbox-dir-policy",
                "filesystem_policy": {
                    "directories": [{
                        "path": "/sandbox/manual",
                        "permissions": "0700",
                    }],
                },
                "network": {
                    "mode": "host",
                },
            },
        })

        assert resp.status_code == 400
        assert "/sandbox" in resp.json()["error"]


class TestPolicyEnforcement:
    @staticmethod
    def test_filesystem_read_write_rule_allows_upload_and_download(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            name_prefix="fs-rw",
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
            name_prefix="fs-ro",
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
            name_prefix="fs-dir",
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
                "/usr/bin/python3",
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
    def test_filesystem_directories_rule_creates_directory_under_home(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            name_prefix="fs-home-dir",
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
                "/usr/bin/python3",
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
            name_prefix="exec-options",
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
            "command": ["/usr/bin/python3", "-c", script],
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
    def test_exec_runs_javascript_code(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            name_prefix="exec-js",
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
            "command": ["/usr/bin/node", "-e", js_code],
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
            name_prefix="download-missing",
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
            name_prefix="download-dir",
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
            name_prefix="list-files",
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
                "/usr/bin/python3",
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
            name_prefix="search-files",
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
                "/usr/bin/python3",
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
            name_prefix="process-root",
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
            "command": ["/usr/bin/id", "-u"],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        uid_data = response.json()
        assert uid_data["exit_code"] == 0, uid_data
        assert uid_data["stdout"].strip() == "0"

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["/usr/bin/id", "-g"],
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
        sandbox = create_sandbox_with_policy(
            name_prefix="syscall-block",
            command=["/usr/bin/sleep", "3600"],
            policy={
                "name": "syscall-block-policy",
                "filesystem_policy": {
                    "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                    "read_write": ["/tmp"],
                },
                "syscall": {
                    "blocked": ["getpid"],
                },
                "network": {
                    "mode": "host",
                },
            },
        )

        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": [
                "/usr/bin/python3",
                "-c",
                textwrap.dedent(
                    """
                    import ctypes
                    import errno
                    import platform
                    import sys

                    syscall_numbers = {
                        "x86_64": 39,
                        "AMD64": 39,
                        "aarch64": 172,
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
            name_prefix="pid-ns",
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
            "command": ["/usr/bin/python3", "-c", "import os; print(os.getpid())"],
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
            name_prefix="cap-drop",
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
            "command": ["/usr/bin/python3", "-c", _capability_check_script(13)],
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
            name_prefix="cap-add",
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
            "command": ["/usr/bin/python3", "-c", _capability_check_script(13)],
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
            "command": ["/usr/bin/echo", "landlock-ok"],
            "policy": {
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
            },
        })
        assert create_resp.status_code == 201
        data = create_resp.json()
        if data["phase"] == "ready":
            assert data["phase"] == "ready", data
        else:
            assert data["phase"] == "error", data
            assert "landlock" in data["error_message"].lower()

    @staticmethod
    def test_landlock_rules_allow_policy_paths_and_deny_other_mounted_paths(
        client,
        create_sandbox_with_policy,
    ):
        sandbox = create_sandbox_with_policy(
            name_prefix="landlock-rules",
            policy={
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
            },
        )

        script = textwrap.dedent(
            """
            from pathlib import Path
            import sys

            allowed = Path("/tmp/landlock-allowed.txt")
            allowed.write_text("landlock-allowed")
            assert allowed.read_text() == "landlock-allowed"

            try:
                Path("/run/jiuwenbox-landlock-launcher.py").read_text()
            except PermissionError:
                print("landlock-denied")
                sys.exit(7)

            print("unexpected-success")
            """
        ).strip()
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["/usr/bin/python3", "-c", script],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 7, data
        assert "landlock-denied" in data["stdout"]
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_network_mode_isolated_blocks_http_requests(
        client,
        create_sandbox_with_policy,
        server_endpoint,
    ):
        sandbox = create_sandbox_with_policy(
            name_prefix="net-isolated",
            policy={
                "name": "net-isolated-policy",
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

        script = (
            "import sys, urllib.request; "
            "urllib.request.urlopen(sys.argv[1], timeout=2).read(); "
            "print('unexpected-success')"
        )
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["/usr/bin/python3", "-c", script, _sandbox_health_url(server_endpoint)],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] != 0
        assert "unexpected-success" not in data["stdout"]

    @staticmethod
    def test_network_mode_host_allows_http_requests(
        client,
        create_sandbox_with_policy,
        server_endpoint,
    ):
        sandbox = create_sandbox_with_policy(
            name_prefix="net-host",
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

        script = (
            "import sys, urllib.request; "
            "print(urllib.request.urlopen(sys.argv[1], timeout=2).read().decode())"
        )
        response = client.post(f"/api/v1/sandboxes/{sandbox['id']}/exec", json={
            "command": ["/usr/bin/python3", "-c", script, _sandbox_health_url(server_endpoint)],
            "timeout_seconds": 5,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0
        assert '"status":"ok"' in data["stdout"].replace(" ", "")

    @staticmethod
    def test_default_policy_allows_network_https(client):
        response = client.post("/api/v1/sandboxes", json={
            "command": LONG_RUNNING_COMMAND,
        })
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
            "command": ["/usr/bin/python3", "-c", script, "https://www.huawei.com/"],
            "timeout_seconds": 15,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["exit_code"] == 0, data
        assert "huawei.com" in data["stdout"].lower()

    @staticmethod
    def test_default_policy_does_not_expose_sensitive_etc_files(client):
        response = client.post("/api/v1/sandboxes", json={
            "command": LONG_RUNNING_COMMAND,
        })
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
            "command": ["/usr/bin/python3", "-c", script],
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
            name_prefix="ingress-allow",
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
            "command": ["/usr/bin/python3", "-c", _loopback_ingress_script(True), "18081"],
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
            name_prefix="ingress-block",
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
            "command": ["/usr/bin/python3", "-c", _loopback_ingress_script(False), "18082"],
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
            name_prefix="netns-persist",
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
            "command": ["/usr/bin/python3", "-c", _loopback_ingress_script(True), "18083"],
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
            "command": ["/usr/bin/python3", "-c", _loopback_ingress_script(True), "18083"],
            "timeout_seconds": 5,
        })
        assert second_exec.status_code == 200
        second_data = second_exec.json()
        assert second_data["exit_code"] == 0, second_data

        delete_response = client.delete(f"/api/v1/sandboxes/{sandbox['id']}")
        assert delete_response.status_code == 204


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

        args = BwrapConfig.from_policy(policy, ["/usr/bin/true"]).to_args()

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

        args = BwrapConfig.from_policy(policy, ["/usr/bin/true"]).to_args()

        assert _has_arg_pair(args, "--dir", "/etc")
        assert _has_mount(args, "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf")


class TestSandboxExec:
    @staticmethod
    def test_exec_requires_running_sandbox(client):
        create_resp = client.post("/api/v1/sandboxes", json={
            "command": ["/usr/bin/sleep", "3600"],
        })
        sandbox_id = create_resp.json()["id"]

        # Stop it first
        client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")

        resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json={
            "command": ["/usr/bin/echo", "hello"],
        })
        assert resp.status_code == 409


class TestSandboxListing:
    @staticmethod
    def test_list_returns_all_sandboxes(client):
        for i in range(3):
            client.post("/api/v1/sandboxes", json={
                "command": ["/usr/bin/echo"],
            })

        resp = client.get("/api/v1/sandboxes")
        assert resp.status_code == 200
        assert len(resp.json()) == 3
