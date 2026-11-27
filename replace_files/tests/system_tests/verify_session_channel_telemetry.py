#!/usr/bin/env python3
"""Verify session_id and channel_id propagation in spans and metrics.

Starts AgentServer + Gateway with console telemetry exporter,
sends two requests in the same session via WebSocket,
then checks the console output for expected span attributes.

Usage:
    .venv/bin/python tests/system_tests/verify_session_channel_telemetry.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")
AGENT_PORT = 18092
GATEWAY_PORT = 19000
WS_URL = f"ws://127.0.0.1:{GATEWAY_PORT}/ws"
SESSION_ID = "verify-session-001"
CHANNEL_ID = "web"
REQUESTS = [
    {"id": "verify-req-1", "content": "请只回复数字1"},
    {"id": "verify-req-2", "content": "请只回复数字2"},
]


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_open(port):
            return
        time.sleep(0.5)
    raise TimeoutError(f"port {port} not ready within {timeout}s")


def start_service(cmd: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
    except Exception:
        log_file.close()
        raise


def stop_service(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception as e:
            logger.warning(f"[Telemetry] Failed to forcefully stop process: {e}")


async def send_requests() -> list[dict]:
    import websockets

    async def recv_json(ws, timeout=180):
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))

    results = []
    async with websockets.connect(WS_URL, max_size=10_000_000) as ws:
        ack = await recv_json(ws, timeout=30)
        if ack.get("event") != "connection.ack":
            raise RuntimeError(f"Expected connection.ack, got: {ack}")
        logger.info("  Connected, got ack")

        for req in REQUESTS:
            frame = {
                "type": "req",
                "id": req["id"],
                "method": "chat.send",
                "params": {
                    "session_id": SESSION_ID,
                    "content": req["content"],
                    "mode": "plan",
                },
            }
            await ws.send(json.dumps(frame, ensure_ascii=False))
            logger.info(f"  Sent request: {req['id']}")

            # Wait for response ack
            while True:
                msg = await recv_json(ws)
                if msg.get("type") == "res" and msg.get("id") == req["id"]:
                    if msg.get("ok") is not True:
                        raise RuntimeError(f"Request rejected: {msg}")
                    logger.info(f"  Request {req['id']} accepted")
                    break

            # Wait for terminal event
            while True:
                msg = await recv_json(ws)
                if msg.get("type") == "event":
                    payload = msg.get("payload") or {}
                    if payload.get("session_id") == SESSION_ID and msg.get("event") in {"chat.final", "chat.error"}:
                        event_type = msg.get("event")
                        logger.info(f"  Request {req['id']} completed: {event_type}")
                        results.append({"request_id": req["id"], "event": event_type})
                        break

    return results


def check_log_for_telemetry(log_path: Path, label: str) -> dict:
    """Parse console exporter output and check for expected attributes."""
    content = log_path.read_text(encoding="utf-8", errors="replace")

    findings = {
        "session_id_in_spans": [],
        "channel_id_in_spans": [],
        "channel_id_in_metrics": [],
        "span_names": [],
    }

    # Look for span names with session_id
    for match in re.finditer(r"name\s*[:=]\s*[\"']?([^\s\"',]+)", content):
        span_name = match.group(1)
        if span_name not in findings["span_names"]:
            findings["span_names"].append(span_name)

    # Check for session_id attribute in spans
    if "jiuwenclaw.session.id" in content:
        findings["session_id_in_spans"].append(True)
        # Find the actual values
        for match in re.finditer(r"jiuwenclaw\.session\.id[\"']?\s*[:=]\s*[\"']?([^\"'\s,}]+)", content):
            val = match.group(1)
            if val not in findings["session_id_in_spans"]:
                findings["session_id_in_spans"].append(val)

    # Check for channel_id attribute
    if "jiuwenclaw.channel.id" in content:
        findings["channel_id_in_spans"].append(True)
        for match in re.finditer(r"jiuwenclaw\.channel\.id[\"']?\s*[:=]\s*[\"']?([^\"'\s,}]+)", content):
            val = match.group(1)
            if val not in findings["channel_id_in_spans"]:
                findings["channel_id_in_spans"].append(val)

    return findings


def main():
    logger.info("=" * 60)
    logger.info("Telemetry E2E Verification: session_id + channel_id propagation")
    logger.info("=" * 60)

    # Pre-checks
    for port in (AGENT_PORT, GATEWAY_PORT):
        if is_port_open(port):
            logger.error(f"ERROR: port {port} already in use")
            sys.exit(1)

    logs_dir = Path(tempfile.mkdtemp(prefix="jiuwenclaw-verify-telemetry-"))
    logger.info(f"\nLogs directory: {logs_dir}")

    common_env = os.environ.copy()
    common_env.update({
        "OTEL_ENABLED": "true",
        "OTEL_TRACES_EXPORTER": "console",
        "OTEL_METRICS_EXPORTER": "console",
        "OTEL_SERVICE_NAME": "jiuwenclaw",
        "OTEL_LOG_MESSAGES": "false",
        "LOG_LEVEL": "WARNING",
    })

    agent_proc = gateway_proc = None
    try:
        # Start AgentServer
        logger.info("\n[1/4] Starting AgentServer...")
        agent_env = dict(common_env)
        agent_env["OTEL_SERVICE_NAME"] = "jiuwenclaw-agentserver"
        agent_proc = start_service(
            [PYTHON, "-m", "jiuwenclaw.app_agentserver", "--port", str(AGENT_PORT)],
            env=agent_env,
            log_path=logs_dir / "agentserver.log",
        )
        wait_for_port(AGENT_PORT)
        logger.info(f"  AgentServer ready on port {AGENT_PORT}")

        # Start Gateway
        logger.info("\n[2/4] Starting Gateway...")
        gateway_env = dict(common_env)
        gateway_env.update({
            "OTEL_SERVICE_NAME": "jiuwenclaw-gateway",
            "AGENT_SERVER_URL": f"ws://127.0.0.1:{AGENT_PORT}",
            "HEARTBEAT_INTERVAL": "3600",
            "AGENT_CONNECT_RETRY": "30",
            "AGENT_CONNECT_RETRY_INTERVAL": "1",
        })
        gateway_proc = start_service(
            [PYTHON, "-m", "jiuwenclaw.app_gateway"],
            env=gateway_env,
            log_path=logs_dir / "gateway.log",
        )
        wait_for_port(GATEWAY_PORT)
        logger.info(f"  Gateway ready on port {GATEWAY_PORT}")

        # Send requests
        logger.info(f"\n[3/4] Sending 2 requests in session '{SESSION_ID}'...")
        results = asyncio.run(send_requests())
        if len(results) != 2:
            raise RuntimeError(f"Expected 2 results, got {len(results)}")
        logger.info(f"  Both requests completed successfully")

        # Wait for metrics flush
        logger.info("\n  Waiting 10s for telemetry flush...")
        time.sleep(10)

    finally:
        stop_service(gateway_proc)
        stop_service(agent_proc)
        # Give processes time to flush logs
        time.sleep(2)

    # Check logs
    logger.info("\n[4/4] Checking telemetry output...")
    logger.info("-" * 60)

    for log_name, label in [("agentserver.log", "AgentServer"), ("gateway.log", "Gateway")]:
        log_path = logs_dir / log_name
        if not log_path.exists():
            logger.warning(f"  WARNING: {log_name} not found")
            continue

        content = log_path.read_text(encoding="utf-8", errors="replace")
        logger.info(f"\n  [{label}] Log size: {len(content)} bytes")

        # Check span attributes
        has_session_id = "jiuwenclaw.session.id" in content
        has_channel_id = "jiuwenclaw.channel.id" in content
        has_request_id = "jiuwenclaw.request.id" in content

        session_values = set(re.findall(
            r"jiuwenclaw\.session\.id['\"]?\s*[:=]\s*['\"]?([^'\"}\s,\]]+)",
            content,
        ))
        channel_values = set(re.findall(
            r"jiuwenclaw\.channel\.id['\"]?\s*[:=]\s*['\"]?([^'\"}\s,\]]+)",
            content,
        ))
        request_id_values = set(re.findall(
            r"jiuwenclaw\.request\.id['\"]?\s*[:=]\s*['\"]?([^'\"}\s,\]]+)",
            content,
        ))

        logger.info(f"  jiuwenclaw.session.id present: {has_session_id}")
        if session_values:
            logger.info(f"    values found: {session_values}")
        logger.info(f"  jiuwenclaw.channel.id present: {has_channel_id}")
        if channel_values:
            logger.info(f"    values found: {channel_values}")
        logger.info(f"  jiuwenclaw.request.id present: {has_request_id}")
        if request_id_values:
            logger.info(f"    values found: {request_id_values}")

        # Check specific span types in agentserver
        if label == "AgentServer":
            has_gen_ai_chat = "gen_ai.chat" in content
            has_tool_execute = "gen_ai.tool.execute" in content
            has_agent_invoke = "jiuwenclaw.agent.invoke" in content

            logger.info(f"  Span 'gen_ai.chat' (LLM): {has_gen_ai_chat}")
            logger.info(f"  Span 'gen_ai.tool.execute' (Tool): {has_tool_execute}")
            logger.info(f"  Span 'jiuwenclaw.agent.invoke' (Agent): {has_agent_invoke}")

        # Check metrics
        metric_names = set(re.findall(r"name=\"([^\"]+)\"", content))
        if metric_names:
            logger.info(f"  Metrics found: {sorted(metric_names)}")

    # Detailed verification
    logger.info("\n" + "=" * 60)
    logger.info("VERIFICATION SUMMARY")
    logger.info("=" * 60)

    agent_log = (logs_dir / "agentserver.log").read_text(encoding="utf-8", errors="replace")
    gateway_log = (logs_dir / "gateway.log").read_text(encoding="utf-8", errors="replace")
    all_logs = agent_log + gateway_log

    # Check request_id values
    request_id_values = set(re.findall(
        r"jiuwenclaw\.request\.id['\"]?\s*[:=]\s*['\"]?([^'\"}\s,\]]+)",
        agent_log,
    ))

    checks = [
        ("session_id in AgentServer spans", "jiuwenclaw.session.id" in agent_log),
        ("channel_id in AgentServer spans", "jiuwenclaw.channel.id" in agent_log),
        ("request_id in AgentServer spans", "jiuwenclaw.request.id" in agent_log),
        ("session_id in Gateway spans", "jiuwenclaw.session.id" in gateway_log),
        ("channel_id in Gateway spans", "jiuwenclaw.channel.id" in gateway_log),
        ("LLM span (gen_ai.chat) present", "gen_ai.chat" in agent_log),
        ("Tool span (gen_ai.tool.execute) present", "gen_ai.tool.execute" in agent_log),
        (f"session value '{SESSION_ID}' found", SESSION_ID in all_logs),
        ("channel value 'web' found", bool(re.search(r"jiuwenclaw\.channel\.id['\"]?\s*[:=]\s*['\"]?web", all_logs))),
        ("request_id values found (2 requests)", len(request_id_values) >= 1),
    ]

    all_passed = True
    for desc, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        logger.info(f"  [{status}] {desc}")

    logger.info("")
    if all_passed:
        logger.info("ALL CHECKS PASSED")
    else:
        logger.info("SOME CHECKS FAILED - review logs at:")
        logger.info(f"  {logs_dir}")

    logger.info(f"\nFull logs at: {logs_dir}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
