#!/usr/bin/env python3
"""HarmonyOS entry point for JiuwenSwarm services.

This script is the single orchestrator for all JiuwenSwarm services on HarmonyOS,
similar to the Windows desktop entry (jiuwenswarm_exe_entry.py).

It:
1. Sets up environment for HarmonyOS sandbox
2. Initializes workspace
3. Starts AgentServer subprocess (port 18092)
4. Starts Gateway subprocess (port 19000)
5. Starts Web frontend service subprocess (port 5173)
6. Waits for TCP ports to be ready
7. Prints HARMONY_READY:url signal to stdout
8. Monitors subprocesses
9. On SIGTERM, terminates children and exits

Output protocol (printed to stdout for ArkTS to parse):
  HARMONY_STARTING:<service>        — service is starting
  HARMONY_PORT_READY:<service>:<port> — TCP port is ready
  HARMONY_READY:http://localhost:<port> — all services ready, WebView URL
  HARMONY_SERVICE_EXIT:<service>:<pid>:exitcode=<code>  — a child service exited
  HARMONY_ERROR:<message>              — startup failure
"""

from __future__ import annotations

import os
import sys
import signal
import subprocess
import time
import socket
import argparse
from pathlib import Path

# ─── Constants ───
DEFAULT_AGENTSERVER_PORT = 18092
DEFAULT_GATEWAY_PORT = 19000
DEFAULT_FRONTEND_PORT = 5173
PORT_CHECK_TIMEOUT = 120  # seconds to wait for each port
PORT_CHECK_INTERVAL = 2   # seconds between checks
SERVICE_MONITOR_INTERVAL = 5  # seconds between health checks


def check_tcp_port(host: str, port: int) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect((host, port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def wait_for_tcp_port(host: str, port: int, timeout: int = PORT_CHECK_TIMEOUT,
                      interval: int = PORT_CHECK_INTERVAL, service_name: str = "") -> bool:
    """Wait for a TCP port to become available, with progress output."""
    start = time.time()
    while time.time() - start < timeout:
        if check_tcp_port(host, port):
            print(f"HARMONY_PORT_READY:{service_name}:{port}")
            sys.stdout.flush()
            return True
        time.sleep(interval)
    return False


def find_free_port(start_port: int) -> int:
    """Find a free TCP port starting from start_port."""
    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    return start_port  # fallback


# ─── Global state ───
children: list[subprocess.Popen] = []
service_names: dict[int, str] = {}  # pid → service name


def start_service(module_path: str, args: list[str], service_name: str) -> subprocess.Popen:
    """Start a JiuwenSwarm Python service as a subprocess."""
    cmd = [sys.executable, "-m", module_path] + args
    print(f"HARMONY_STARTING:{service_name}")
    sys.stdout.flush()

    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=None,
        stderr=None,
        env=child_env,
    )
    children.append(proc)
    service_names[proc.pid] = service_name
    return proc


def handle_sigterm(signum: int, frame) -> None:
    """SIGTERM handler: terminate all child processes gracefully."""
    print("HARMONY_SHUTDOWN:received SIGTERM")
    sys.stdout.flush()

    for child in children:
        try:
            child.terminate()
        except OSError:
            pass

    # Wait for children to exit (with timeout)
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
                child.wait(timeout=2)
            except OSError:
                pass

    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-harmony",
        description="JiuwenSwarm HarmonyOS service orchestrator",
    )
    parser.add_argument("--agentserver-port", type=int, default=DEFAULT_AGENTSERVER_PORT,
                        help=f"AgentServer TCP port (default: {DEFAULT_AGENTSERVER_PORT})")
    parser.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT,
                        help=f"Gateway TCP port (default: {DEFAULT_GATEWAY_PORT})")
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT,
                        help=f"Web frontend TCP port (default: {DEFAULT_FRONTEND_PORT})")
    parser.add_argument("--auto-port", action="store_true",
                        help="Automatically find free ports if defaults are occupied")
    parser.add_argument("--no-frontend", action="store_true",
                        help="Skip Web frontend service (fallback mode: rawfile frontend)")
    parser.add_argument("--dotenv", type=str, default=None,
                        help="Path to .env file for configuration")
    args = parser.parse_args()

    # ─── Environment setup ───
    home_dir = os.environ.get("JIUWENSWARM_HOME", os.environ.get("JWS_HOME", os.environ.get("HOME", "/storage/Users/currentUser")))
    os.environ["HOME"] = home_dir
    os.environ.setdefault("JIUWENSWARM_HOME", home_dir)

    # Ensure UTF-8 encoding
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    # ─── SSL CA certificates (HarmonyOS lacks system CA store) ───
    # HarmonyOS has no /etc/ssl/certs/ (verified empty on-device), so Python's
    # ssl module falls back to an empty CA bundle and rejects every chain's
    # root CA as "self-signed certificate in certificate chain". Force-load
    # certifi's cacert.pem so Lark/requests/httpx can verify feishu.cn etc.
    # Use setdefault so an explicit value from .env still wins.
    try:
        import certifi
        _cacert = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", _cacert)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", _cacert)
        os.environ.setdefault("CURL_CA_BUNDLE", _cacert)
        print(f"HARMONY_INFO:ssl_ca_cert:{_cacert}")
        sys.stdout.flush()
    except ImportError:
        print("HARMONY_WARN:certifi_not_found:ssl_ca_cert_not_set")
        sys.stdout.flush()

    # ─── dotenv parsing (before jiuwenswarm imports) ───
    if args.dotenv:
        dotenv_path = Path(args.dotenv)
        if dotenv_path.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(dotenv_path=dotenv_path, override=True)
            except ImportError:
                # Manual dotenv loading fallback
                with open(dotenv_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        os.environ[key] = value

    # ─── Workspace initialization ───
    print("HARMONY_STARTING:workspace")
    sys.stdout.flush()

    try:
        from jiuwenswarm.common.utils import get_user_workspace_dir, prepare_workspace
        workspace = get_user_workspace_dir()
        config_path = workspace / "config" / "config.yaml"
        if not config_path.exists():
            print("HARMONY_STARTING:workspace_init")
            sys.stdout.flush()
            prepare_workspace(overwrite=False)
    except Exception as e:
        print(f"HARMONY_ERROR:workspace_init_failed:{e}")
        sys.stdout.flush()
        # Don't exit — services may still work with defaults

    # ─── Port allocation ───
    agentserver_port = args.agentserver_port
    gateway_port = args.gateway_port
    frontend_port = args.frontend_port

    if args.auto_port:
        if not check_tcp_port("127.0.0.1", agentserver_port):
            agentserver_port = find_free_port(agentserver_port + 1)
            print(f"HARMONY_INFO:auto_port:agentserver:{agentserver_port}")
        if not check_tcp_port("127.0.0.1", gateway_port):
            gateway_port = find_free_port(gateway_port + 1)
            print(f"HARMONY_INFO:auto_port:gateway:{gateway_port}")
        if not check_tcp_port("127.0.0.1", frontend_port):
            frontend_port = find_free_port(frontend_port + 1)
            print(f"HARMONY_INFO:auto_port:frontend:{frontend_port}")

    # Set port environment variables for subprocesses
    os.environ["AGENT_SERVER_PORT"] = str(agentserver_port)
    os.environ.setdefault("JIUWENSWARM_START_MODE", "all")

    # ─── Start AgentServer (port 18092) ───
    agentserver_args = []
    if args.dotenv:
        agentserver_args.extend(["--dotenv", args.dotenv])

    agentserver_proc = start_service(
        "jiuwenswarm.server.app_agentserver",
        agentserver_args,
        "agentserver",
    )

    # ─── Wait for AgentServer port ───
    print("HARMONY_STARTING:agentserver_wait")
    sys.stdout.flush()
    time.sleep(3)  # Give AgentServer time to initialize

    # ─── Start Gateway (port 19000) ───
    gateway_args = []
    if args.dotenv:
        gateway_args.extend(["--dotenv", args.dotenv])

    gateway_proc = start_service(
        "jiuwenswarm.gateway.app_gateway",
        gateway_args,
        "gateway",
    )

    # ─── Wait for Gateway port ───
    gateway_ready = wait_for_tcp_port(
        "127.0.0.1", gateway_port,
        timeout=PORT_CHECK_TIMEOUT,
        interval=PORT_CHECK_INTERVAL,
        service_name="gateway",
    )

    if not gateway_ready:
        print(f"HARMONY_ERROR:gateway_port_timeout:{gateway_port}")
        sys.stdout.flush()
        if args.auto_port:
            gateway_port = find_free_port(gateway_port + 1)
            gateway_ready = wait_for_tcp_port(
                "127.0.0.1", gateway_port,
                timeout=30,
                service_name="gateway_alt",
            )
        if not gateway_ready:
            print("HARMONY_ERROR:startup_failed:gateway_not_ready")
            sys.stdout.flush()
            # Clean up started processes
            for child in children:
                if child.poll() is None:
                    child.terminate()
            sys.exit(1)

    # ─── Start Web frontend service (port 5173) ───
    if not args.no_frontend:
        frontend_args = []
        if args.dotenv:
            frontend_args.extend(["--dotenv", args.dotenv])

        frontend_proc = start_service(
            "jiuwenswarm.channels.web.app_web",
            frontend_args,
            "frontend",
        )

        # ─── Wait for frontend port ───
        frontend_ready = wait_for_tcp_port(
            "127.0.0.1", frontend_port,
            timeout=PORT_CHECK_TIMEOUT,
            interval=PORT_CHECK_INTERVAL,
            service_name="frontend",
        )

        if not frontend_ready:
            print(f"HARMONY_ERROR:frontend_port_timeout:{frontend_port}")
            sys.stdout.flush()
            if args.auto_port:
                frontend_port = find_free_port(frontend_port + 1)
                frontend_ready = wait_for_tcp_port(
                    "127.0.0.1", frontend_port,
                    timeout=30,
                    service_name="frontend_alt",
                )
            if not frontend_ready:
                print("HARMONY_ERROR:startup_failed:frontend_not_ready")
                sys.stdout.flush()
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                sys.exit(1)

        # ─── Signal readiness (frontend URL) ───
        service_url = f"http://localhost:{frontend_port}"
    else:
        # Fallback mode: frontend loaded from rawfile, no frontend service needed
        # Gateway serves API + WebSocket
        service_url = f"http://localhost:{gateway_port}"
        print("HARMONY_INFO:fallback_mode:no_frontend_service")

    print(f"HARMONY_READY:{service_url}")
    sys.stdout.flush()

    # ─── Register SIGTERM handler ───
    signal.signal(signal.SIGTERM, handle_sigterm)

    # ─── Monitor subprocesses ───
    while True:
        for child in children:
            ret = child.poll()
            if ret is not None:
                name = service_names.get(child.pid, "unknown")
                print(f"HARMONY_SERVICE_EXIT:{name}:{child.pid}:exitcode={ret}")
                sys.stdout.flush()

                # If a critical service exits, terminate everything
                if name in ("agentserver", "gateway", "frontend"):
                    print(f"HARMONY_ERROR:critical_service_exit:{name}")
                    sys.stdout.flush()
                    for other in children:
                        if other.poll() is None:
                            try:
                                other.terminate()
                            except OSError:
                                pass
                    sys.exit(1)

        time.sleep(SERVICE_MONITOR_INTERVAL)


if __name__ == "__main__":
    main()
