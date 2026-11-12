"""Synchronous REA MCP inventory probe for setup and coverage diagnostics."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any


PROTOCOL_VERSION = "2025-11-25"


def _reader(stdout, messages: queue.Queue[dict[str, Any] | BaseException]) -> None:
    try:
        while True:
            line = stdout.readline()
            if not line:
                return
            text = line.strip()
            if not text:
                continue
            messages.put(json.loads(text))
    except BaseException as exc:  # noqa: BLE001
        messages.put(exc)


def _send(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("REA MCP stdin is unavailable")
    process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _receive(
    messages: queue.Queue[dict[str, Any] | BaseException],
    deadline: float,
    request_id: int,
) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for REA MCP response {request_id}")
        item = messages.get(timeout=remaining)
        if isinstance(item, BaseException):
            raise RuntimeError(str(item)) from item
        if item.get("id") == request_id:
            return item


def validate_tool_inventory(
    tools: list[dict[str, Any]],
    binary_session: dict[str, Any],
) -> dict[str, Any]:
    """Validate that ``tools/list`` equals the binary-session tool union."""

    listed_names = {
        str(tool.get("name") or tool.get("id") or "")
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or tool.get("id") or "")
    }
    availability = binary_session.get("tool_availability")
    availability = availability if isinstance(availability, dict) else {}
    availability_names = {
        str(name)
        for group in availability.get("groups") or []
        if isinstance(group, dict)
        for name in group.get("names") or []
        if str(name)
    }
    if listed_names != availability_names:
        missing = sorted(listed_names - availability_names)
        extra = sorted(availability_names - listed_names)
        raise RuntimeError(
            "REA tool inventory mismatch between tools/list and binary_session: "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    return {
        "tool_names": sorted(listed_names),
        "availability_tool_count": len(availability_names),
        "inventory_verified": True,
    }


def probe_rea_mcp_inventory(
    record: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Return ``tools/list`` and ``binary_session`` from a temporary REA MCP."""

    command = str(record.get("command") or "").strip()
    args = [str(item) for item in record.get("args") or []]
    if not command:
        raise RuntimeError("REA MCP command is missing")
    env = os.environ.copy()
    if isinstance(record.get("env"), dict):
        env.update({str(key): str(value) for key, value in record["env"].items() if value is not None})
    process = subprocess.Popen(  # noqa: S603
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
    reader = threading.Thread(
        target=_reader,
        args=(process.stdout, messages),
        name="rea-mcp-inventory-reader",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "workswarm-rea-setup", "version": "1.0.0"},
                },
            },
        )
        initialized = _receive(messages, deadline, 1)
        if initialized.get("error"):
            raise RuntimeError(str(initialized["error"]))
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        listed = _receive(messages, deadline, 2)
        if listed.get("error"):
            raise RuntimeError(str(listed["error"]))
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "binary_session", "arguments": {}},
            },
        )
        called = _receive(messages, deadline, 3)
        if called.get("error"):
            raise RuntimeError(str(called["error"]))
        result = called.get("result") if isinstance(called.get("result"), dict) else {}
        structured = result.get("structuredContent")
        binary_session = structured.get("result") if isinstance(structured, dict) else None
        if not isinstance(binary_session, dict):
            content = result.get("content")
            if isinstance(content, list) and content:
                text = content[0].get("text") if isinstance(content[0], dict) else None
                if isinstance(text, str):
                    parsed = json.loads(text)
                    binary_session = parsed.get("result") if isinstance(parsed, dict) else None
        if not isinstance(binary_session, dict):
            raise RuntimeError("binary_session returned no structured result")
        tools = listed.get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list returned no tools")
        validation = validate_tool_inventory(tools, binary_session)
        return {
            "tools": tools,
            "binary_session": binary_session,
            "server_info": initialized.get("result", {}).get("serverInfo"),
            **validation,
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
