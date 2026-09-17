"""Test utility functions for system reliability and performance tests."""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import List, Optional

from tests.system_tests.test_config import EnvironmentDetector, TestConfig

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Performance metrics collected during tests."""

    latencies: List[float]
    qps: float = 0.0
    p99: float = 0.0
    p999: float = 0.0
    avg_latency: float = 0.0


class ResourceDegradationManager:
    """Manage resource degradation for testing."""

    def __init__(self):
        self._processes = []
        self._network_degraded = False

    def degrade_cpu(self, cores: int = TestConfig.CPU_CORES_SEVERE, duration: int = 30):
        """Degrade CPU by running stress-ng."""
        if not EnvironmentDetector.has_stress_ng():
            raise RuntimeError("stress-ng not available, cannot degrade CPU")

        proc = subprocess.Popen(
            ["/usr/bin/stress-ng", f"--cpu={cores}", f"--timeout={duration}s"],
        )
        self._processes.append(proc)
        time.sleep(2)
        return proc

    def degrade_memory(self, vm_bytes: str = TestConfig.MEMORY_SEVERE, duration: int = 30):
        """Degrade memory by running stress-ng."""
        if not EnvironmentDetector.has_stress_ng():
            raise RuntimeError("stress-ng not available, cannot degrade memory")

        proc = subprocess.Popen(
            ["/usr/bin/stress-ng", f"--vm-bytes={vm_bytes}", "--vm=4", f"--timeout={duration}s"],
        )
        self._processes.append(proc)
        time.sleep(2)
        return proc

    def degrade_network(self, loss: int = 10, delay: int = 100, interface: str = TestConfig.NETWORK_INTERFACE):
        """Degrade network using tc."""
        if not EnvironmentDetector.has_tc():
            raise RuntimeError("tc not available, cannot degrade network")

        subprocess.run(
            ["/sbin/tc", "qdisc", "add", "dev", interface, "root",
             "netem", "loss", "random", f"{loss}%", "delay", f"{delay}ms"],
            check=True,
        )
        self._network_degraded = True
        return interface

    def restore_network(self, interface: str = TestConfig.NETWORK_INTERFACE):
        """Restore network quality."""
        if self._network_degraded and EnvironmentDetector.has_tc():
            try:
                subprocess.run(
                    ["/sbin/tc", "qdisc", "del", "dev", interface, "root"],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning("Failed to restore network on %s: %s", interface, exc)
            self._network_degraded = False

    def degrade_disk(self, path: str = "/tmp", size: str = "5G", duration: int = 30):
        """Degrade disk by filling it."""
        count = size[:-1]
        proc = subprocess.Popen(
            [
                "/usr/bin/timeout", f"{duration}s",
                "/bin/dd", "if=/dev/urandom",
                f"of={path}/stress_file", "bs=1G", f"count={count}",
            ],
        )
        self._processes.append(proc)
        time.sleep(2)
        return proc

    @staticmethod
    def restore_disk(path: str = "/tmp"):
        """Clean up disk stress file."""
        subprocess.run(["/bin/rm", "-f", f"{path}/stress_file"], check=True)

    def cleanup(self):
        """Clean up all degradation processes."""
        self.restore_network()
        for proc in self._processes:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception as kill_exc:
                    logger.warning("Failed to kill process: %s", kill_exc)
            except Exception as exc:
                logger.debug("Failed to terminate process: %s", exc)
        self._processes.clear()


class ChaosInjector:
    """Inject chaos into the system for resilience testing."""

    def __init__(self, client):
        self._client = client

    def _resolve_daemon_pid(self, sandbox_id: str) -> str:
        """Discover the sandbox daemon PID inside the sandbox."""
        resp = self._client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["pgrep", "-f", "sandbox-daemon"],
                "timeout_seconds": 10,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to discover daemon PID: {resp.status_code} {resp.text}"
            )
        raw = (resp.json().get("stdout") or "").strip()
        pids = raw.splitlines()
        if not pids or not pids[0].isdigit():
            raise RuntimeError(
                f"Could not find sandbox daemon PID in sandbox {sandbox_id}: {raw!r}"
            )
        return pids[0]

    def kill_sandbox_daemon(self, sandbox_id: str):
        """Kill the sandbox daemon process."""
        pid = self._resolve_daemon_pid(sandbox_id)
        resp = self._client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["kill", "-9", pid], "timeout_seconds": 5},
        )
        return resp

    def stop_sandbox_daemon(self, sandbox_id: str):
        """Stop the sandbox daemon process (SIGSTOP)."""
        pid = self._resolve_daemon_pid(sandbox_id)
        resp = self._client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["kill", "-STOP", pid], "timeout_seconds": 5},
        )
        return resp

    def restart_sandbox_daemon(self, sandbox_id: str):
        """Restart the sandbox daemon process (SIGCONT)."""
        pid = self._resolve_daemon_pid(sandbox_id)
        resp = self._client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["kill", "-CONT", pid], "timeout_seconds": 5},
        )
        return resp

    @staticmethod
    def disconnect_network():
        """Disconnect network by bringing down interface."""
        if not EnvironmentDetector.has_iproute2():
            raise RuntimeError("iproute2 not available, cannot disconnect network")

        try:
            subprocess.run(
                ["/sbin/ip", "link", "set", TestConfig.NETWORK_INTERFACE, "down"],
                check=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to disconnect network: %s", exc)
            return False

    @staticmethod
    def reconnect_network():
        """Reconnect network by bringing up interface."""
        if EnvironmentDetector.has_iproute2():
            subprocess.run(
                ["/sbin/ip", "link", "set", TestConfig.NETWORK_INTERFACE, "up"],
                check=True,
            )

    def trigger_oom(self, sandbox_id: str, memory_gb: int = 22):
        """Trigger OOM inside sandbox."""
        resp = self._client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["/usr/bin/stress-ng", f"--vm-bytes={memory_gb}G", "--vm=1", "--timeout=60s"],
                "timeout_seconds": 120,
            },
        )
        return resp


class StateMachineValidator:
    """Validate sandbox state transitions."""

    VALID_TRANSITIONS = {
        "provisioning": ["ready", "error"],
        "ready": ["stopped", "error", "deleting"],
        "stopped": ["ready", "deleting"],
        "error": ["ready", "deleting"],
        "deleting": [],
    }

    @classmethod
    def is_valid_transition(cls, from_state: str, to_state: str) -> bool:
        """Check if a state transition is valid."""
        from_state_lower = from_state.lower()
        to_state_lower = to_state.lower()
        return to_state_lower in cls.VALID_TRANSITIONS.get(from_state_lower, [])

    @classmethod
    def validate_transition(cls, from_state: str, to_state: str):
        """Validate a state transition and raise if invalid."""
        if not cls.is_valid_transition(from_state, to_state):
            raise ValueError(
                f"Invalid state transition: {from_state} -> {to_state}. "
                f"Valid transitions from {from_state}: {cls.VALID_TRANSITIONS.get(from_state.lower(), [])}"
            )


class PerformanceCollector:
    """Collect and analyze performance metrics."""

    def __init__(self):
        self._latencies = []
        self._start_time = None
        self._end_time = None

    def start(self):
        """Start timing."""
        self._start_time = time.time()

    def stop(self):
        """Stop timing."""
        self._end_time = time.time()

    def record_latency(self, latency: float):
        """Record a latency measurement."""
        self._latencies.append(latency)

    def get_metrics(self) -> PerformanceMetrics:
        """Calculate and return performance metrics."""
        if not self._latencies:
            return PerformanceMetrics(latencies=[])

        latencies = sorted(self._latencies)
        total_time = self._end_time - self._start_time if self._start_time and self._end_time else 1
        qps = len(latencies) / total_time if total_time > 0 else 0

        n = len(latencies)
        p99_idx = int(n * 0.99) - 1
        p999_idx = int(n * 0.999) - 1

        return PerformanceMetrics(
            latencies=latencies,
            qps=qps,
            p99=latencies[p99_idx] if p99_idx >= 0 else latencies[-1],
            p999=latencies[p999_idx] if p999_idx >= 0 else latencies[-1],
            avg_latency=sum(latencies) / n,
        )


class RollingWindowStats:
    """Calculate statistics over a rolling window."""

    def __init__(self, window_size: int = 100):
        self._window = deque(maxlen=window_size)

    def add(self, value: float):
        """Add a value to the window."""
        self._window.append(value)

    def avg(self) -> float:
        """Calculate average."""
        if not self._window:
            return 0.0
        return sum(self._window) / len(self._window)

    def p99(self) -> float:
        """Calculate P99."""
        if not self._window:
            return 0.0
        sorted_vals = sorted(self._window)
        idx = int(len(sorted_vals) * 0.99) - 1
        return sorted_vals[idx] if idx >= 0 else sorted_vals[-1]

    def p999(self) -> float:
        """Calculate P999."""
        if not self._window:
            return 0.0
        sorted_vals = sorted(self._window)
        idx = int(len(sorted_vals) * 0.999) - 1
        return sorted_vals[idx] if idx >= 0 else sorted_vals[-1]

    def count(self) -> int:
        """Get count of values in window."""
        return len(self._window)
