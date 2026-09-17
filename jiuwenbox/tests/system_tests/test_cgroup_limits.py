"""cgroup resource limits tests."""

from __future__ import annotations

import textwrap

import pytest


def _get_base_policy(*, cgroup: dict | None = None):
    policy = {
        "filesystem_policy": {
            "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
            "read_write": ["/tmp"],
            "bind_mounts": [
                {"host_path": "/bin", "sandbox_path": "/bin", "mode": "ro"},
                {"host_path": "/usr", "sandbox_path": "/usr", "mode": "ro"},
                {"host_path": "/lib", "sandbox_path": "/lib", "mode": "ro"},
                {"host_path": "/lib64", "sandbox_path": "/lib64", "mode": "ro"},
                {"host_path": "/opt/python3.11", "sandbox_path": "/opt/python3.11", "mode": "ro"},
            ],
        },
        "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
        "namespace": {"user": True, "pid": True, "ipc": True, "cgroup": True, "uts": True},
        "network": {"mode": "isolated"},
        "environment": {"PATH": "/opt/python3.11/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    if cgroup is not None:
        policy["cgroup"] = cgroup
    return policy


_CGROUP_PROBE_POLICY = _get_base_policy(cgroup={"pids_max": 1024})
_cgroup_probe_cached: bool | None = None
_cgroup_probe_skip_reason: str | None = None


@pytest.fixture
def cgroup_supported(client):
    """Probe whether the server can apply per-sandbox cgroup limits."""
    global _cgroup_probe_cached, _cgroup_probe_skip_reason
    if _cgroup_probe_cached is False:
        pytest.skip(_cgroup_probe_skip_reason or "cgroup not writable")
    if _cgroup_probe_cached is None:
        resp = client.post("/api/v1/sandboxes", json={"policy": _CGROUP_PROBE_POLICY})
        if resp.status_code == 201:
            sandbox_id = resp.json()["id"]
            client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            _cgroup_probe_cached = True
        else:
            _cgroup_probe_cached = False
            _cgroup_probe_skip_reason = (
                f"cgroup probe sandbox failed (status={resp.status_code}): {resp.text}"
            )
            pytest.skip(_cgroup_probe_skip_reason)
    return True


def _cpu_ratio_script() -> str:
    return textwrap.dedent(
        """
        import time

        target_wall = 2.0
        start_wall = time.monotonic()
        start_cpu = time.process_time()
        while time.monotonic() - start_wall < target_wall:
            pass
        wall = time.monotonic() - start_wall
        cpu = time.process_time() - start_cpu
        print('ratio=', cpu / wall if wall > 0 else 0)
        """
    ).strip()


def _parse_cpu_ratio(stdout: str) -> float:
    ratio_line = next(
        (line for line in stdout.splitlines() if line.startswith("ratio=")),
        "",
    )
    assert ratio_line, f"Missing ratio= line in stdout: {stdout!r}"
    return float(ratio_line.split("=", 1)[1].strip())


@pytest.mark.system
class TestCgroupCPULimits:
    """CPU cgroup limits tests."""

    @staticmethod
    def test_cg_001_cpu_limit_half_core(
        client, wait_for_sandbox_ready, exec_command, cgroup_supported
    ):
        policy = _get_base_policy(cgroup={"cpu_max": "0.5"})
        policy["name"] = "cpu-limit-test"

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            ["python3", "-c", _cpu_ratio_script()],
            timeout_seconds=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0, data
        assert _parse_cpu_ratio(data.get("stdout") or "") < 0.75

    @staticmethod
    def test_cg_002_cpu_limit_low(
        client, wait_for_sandbox_ready, exec_command, cgroup_supported
    ):
        policy = _get_base_policy(cgroup={"cpu_max": "0.1"})
        policy["name"] = "cpu-limit-low"

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            ["python3", "-c", _cpu_ratio_script()],
            timeout_seconds=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0, data
        assert _parse_cpu_ratio(data.get("stdout") or "") < 0.3

    @staticmethod
    def test_cg_003_cpu_limit_full(
        client, wait_for_sandbox_ready, exec_command, cgroup_supported
    ):
        policy = _get_base_policy(cgroup={"cpu_max": "4"})
        policy["name"] = "cpu-limit-full"

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            ["python3", "-c", _cpu_ratio_script()],
            timeout_seconds=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0, data
        assert _parse_cpu_ratio(data.get("stdout") or "") > 0.3


@pytest.mark.system
class TestCgroupMemoryLimits:
    """Memory cgroup limits tests."""

    @staticmethod
    def test_cg_004_memory_limit_1g(
        client, wait_for_sandbox_ready, exec_command, cgroup_supported
    ):
        policy = _get_base_policy(cgroup={"memory_max": "1G"})
        policy["name"] = "memory-limit-1g"

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            ["python3", "-c", "data = b'x' * 500 * 1024 * 1024; print(len(data))"],
            timeout_seconds=30,
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 0

    @staticmethod
    def test_cg_005_memory_limit_128m(
        client, wait_for_sandbox_ready, exec_command, cgroup_supported
    ):
        policy = _get_base_policy(cgroup={"memory_max": "128M"})
        policy["name"] = "memory-limit-128m"

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            [
                "python3",
                "-c",
                (
                    "buf = bytearray(256 * 1024 * 1024); "
                    "buf[::4096] = b'x' * (len(buf) // 4096); "
                    "print('survived')"
                ),
            ],
            timeout_seconds=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] != 0, data
        combined = ((data.get("stdout") or "") + (data.get("stderr") or "")).lower()
        assert "survived" not in combined, data

    @staticmethod
    def test_cg_006_memory_limit_full(
        client, wait_for_sandbox_ready, exec_command, cgroup_supported
    ):
        policy = _get_base_policy(cgroup={"memory_max": "4G"})
        policy["name"] = "memory-limit-full"

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            ["python3", "-c", "data = b'x' * 2 * 1024 * 1024 * 1024; print(len(data))"],
            timeout_seconds=60,
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 0
