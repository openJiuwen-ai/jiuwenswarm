"""Test configuration and environment detection utilities."""

from __future__ import annotations

import logging
import os
import platform
import shutil
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TestConfig:
    """Test configuration parameters."""

    # API Endpoint
    SERVER_ENDPOINT = os.environ.get("JIUWENBOX_SERVER", "https://localhost:8080")

    # Provisioning error test subnet (configurable via env var to avoid hardcoding)
    # Default uses a non-routable link-local subnet that will cause provisioning to fail
    PROVISIONING_ERROR_SUBNET = os.environ.get("PROVISIONING_ERROR_SUBNET", "invalid-subnet-for-testing")

    # Timeouts (seconds)
    DEFAULT_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "120"))
    SANDBOX_READY_TIMEOUT = int(os.environ.get("SANDBOX_READY_TIMEOUT", "30"))
    EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "60"))

    # Performance thresholds
    QPS_SINGLE_THRESHOLD = int(os.environ.get("QPS_SINGLE_THRESHOLD", "50"))
    QPS_MULTI_THRESHOLD = int(os.environ.get("QPS_MULTI_THRESHOLD", "100"))
    LATENCY_COMMAND_THRESHOLD = float(os.environ.get("LATENCY_COMMAND_THRESHOLD", "0.05"))
    LATENCY_STARTUP_THRESHOLD = float(os.environ.get("LATENCY_STARTUP_THRESHOLD", "0.1"))
    LATENCY_SERVICE_THRESHOLD = float(os.environ.get("LATENCY_SERVICE_THRESHOLD", "1.0"))
    P999_SPIKE_THRESHOLD = float(os.environ.get("P999_SPIKE_THRESHOLD", "0.5"))

    # Resource degradation parameters
    CPU_CORES_MILD = int(os.environ.get("CPU_CORES_MILD", "4"))
    CPU_CORES_MODERATE = int(os.environ.get("CPU_CORES_MODERATE", "8"))
    CPU_CORES_SEVERE = int(os.environ.get("CPU_CORES_SEVERE", "12"))
    MEMORY_MILD = os.environ.get("MEMORY_MILD", "8G")
    MEMORY_MODERATE = os.environ.get("MEMORY_MODERATE", "16G")
    MEMORY_SEVERE = os.environ.get("MEMORY_SEVERE", "20G")

    # Network interface
    NETWORK_INTERFACE = os.environ.get("NETWORK_INTERFACE", "eth0")

    # File transfer parameters
    FILE_UPLOAD_SPEED_THRESHOLD = float(os.environ.get("FILE_UPLOAD_SPEED_THRESHOLD", "10"))
    FILE_DOWNLOAD_SPEED_THRESHOLD = float(os.environ.get("FILE_DOWNLOAD_SPEED_THRESHOLD", "20"))
    FILE_TRANSFER_TIMEOUT = int(os.environ.get("FILE_TRANSFER_TIMEOUT", "300"))

    # Concurrent creation parameters
    CONCURRENT_CREATE_TIMEOUT_5 = int(os.environ.get("CONCURRENT_CREATE_TIMEOUT_5", "10"))
    CONCURRENT_CREATE_TIMEOUT_10 = int(os.environ.get("CONCURRENT_CREATE_TIMEOUT_10", "20"))
    CONCURRENT_CREATE_TIMEOUT_20 = int(os.environ.get("CONCURRENT_CREATE_TIMEOUT_20", "40"))

    # Long stability test parameters
    LONG_STABILITY_DURATION_30MIN = int(os.environ.get("LONG_STABILITY_DURATION_30MIN", "1800"))
    LONG_STABILITY_DURATION_1HOUR = int(os.environ.get("LONG_STABILITY_DURATION_1HOUR", "3600"))
    LONG_STABILITY_SANDBOX_COUNT = int(os.environ.get("LONG_STABILITY_SANDBOX_COUNT", "5"))
    LONG_STABILITY_REQUEST_INTERVAL_MS = int(os.environ.get("LONG_STABILITY_REQUEST_INTERVAL_MS", "100"))
    LONG_STABILITY_SUCCESS_RATE_THRESHOLD = float(os.environ.get("LONG_STABILITY_SUCCESS_RATE_THRESHOLD", "0.999"))
    LONG_STABILITY_AVG_LATENCY_THRESHOLD = float(os.environ.get("LONG_STABILITY_AVG_LATENCY_THRESHOLD", "0.1"))
    LONG_STABILITY_P99_LATENCY_THRESHOLD = float(os.environ.get("LONG_STABILITY_P99_LATENCY_THRESHOLD", "0.3"))
    LONG_STABILITY_CPU_THRESHOLD = float(os.environ.get("LONG_STABILITY_CPU_THRESHOLD", "80"))
    LONG_STABILITY_MEMORY_THRESHOLD = float(os.environ.get("LONG_STABILITY_MEMORY_THRESHOLD", "80"))


class EnvironmentDetector:
    """Detect test environment capabilities."""

    @staticmethod
    def is_linux() -> bool:
        """Check if running on Linux."""
        return platform.system().lower() == "linux"

    @staticmethod
    def is_windows() -> bool:
        """Check if running on Windows."""
        return platform.system().lower() == "windows"

    @staticmethod
    def is_macos() -> bool:
        """Check if running on macOS."""
        return platform.system().lower() == "darwin"

    @staticmethod
    def has_stress_ng() -> bool:
        """Check if stress-ng is available."""
        return shutil.which("stress-ng") is not None

    @staticmethod
    def has_tc() -> bool:
        """Check if tc (traffic control) is available."""
        return shutil.which("tc") is not None

    @staticmethod
    def has_iproute2() -> bool:
        """Check if iproute2 is available."""
        return shutil.which("ip") is not None

    @staticmethod
    def has_docker() -> bool:
        """Check if Docker is available."""
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            import subprocess
            result = subprocess.run([docker, "ps"], capture_output=True, text=True)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("docker check failed: %s", exc)
            return False

    @staticmethod
    def can_run_resource_tests() -> bool:
        """Check if resource degradation tests can run."""
        return EnvironmentDetector.is_linux() and EnvironmentDetector.has_stress_ng()

    @staticmethod
    def can_run_network_tests() -> bool:
        """Check if network degradation tests can run."""
        return EnvironmentDetector.is_linux() and EnvironmentDetector.has_tc()

    @staticmethod
    def can_run_chaos_tests() -> bool:
        """Check if chaos engineering tests can run."""
        return (
            EnvironmentDetector.is_linux()
            and EnvironmentDetector.has_stress_ng()
            and EnvironmentDetector.has_iproute2()
        )

    @staticmethod
    def get_capabilities() -> Dict[str, bool]:
        """Get all environment capabilities."""
        return {
            "linux": EnvironmentDetector.is_linux(),
            "windows": EnvironmentDetector.is_windows(),
            "macos": EnvironmentDetector.is_macos(),
            "stress_ng": EnvironmentDetector.has_stress_ng(),
            "tc": EnvironmentDetector.has_tc(),
            "iproute2": EnvironmentDetector.has_iproute2(),
            "docker": EnvironmentDetector.has_docker(),
            "resource_tests": EnvironmentDetector.can_run_resource_tests(),
            "network_tests": EnvironmentDetector.can_run_network_tests(),
            "chaos_tests": EnvironmentDetector.can_run_chaos_tests(),
        }

    @staticmethod
    def print_capabilities() -> None:
        """Print environment capabilities."""
        capabilities = EnvironmentDetector.get_capabilities()
        logger.info("=" * 50)
        logger.info("Environment Capabilities")
        logger.info("=" * 50)
        for key, value in capabilities.items():
            status = "available" if value else "unavailable"
            logger.info("  %s: %s", key, status)
        logger.info("=" * 50)
