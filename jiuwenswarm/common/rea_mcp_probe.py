"""Synchronous REA MCP inventory probe for setup and coverage diagnostics."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


PROTOCOL_VERSION = "2025-11-25"
FUNCTIONAL_PROBE_TIMEOUT_SECONDS = 10.0
FUNCTIONAL_TOOL_PROBES: dict[str, dict[str, Any]] = {
    "list_frida_devices": {},
    "list_cutter_sessions": {},
    "inspect_adb_client": {},
    "inspect_apktool_client": {},
    "inspect_with_objdump": {"operation": "architectures"},
    "execute_rizin_command": {},
}


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


def mcp_record_environment(record: dict[str, Any]) -> dict[str, str]:
    """Build the same restricted environment used by the registered REA MCP."""

    from jiuwenswarm.common.rea_setup import _record_process_env, resolve_node_runtime

    runtime = resolve_node_runtime(str(record.get("command") or "").strip() or None)
    record_env = record.get("env")
    return _record_process_env(
        runtime,
        record_env if isinstance(record_env, dict) else None,
    )


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
    availability_names = set()
    for group in availability.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for name in group.get("names") or []:
            if str(name):
                availability_names.add(str(name))
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


def binary_session_from_call_result(call_result: dict[str, Any]) -> dict[str, Any]:
    """Extract ``binary_session`` from a raw MCP ``tools/call`` result."""

    if not isinstance(call_result, dict):
        raise RuntimeError("binary_session returned no structured result")
    structured = call_result.get("structuredContent")
    binary_session = structured.get("result") if isinstance(structured, dict) else None
    if isinstance(binary_session, dict):
        return binary_session
    content = call_result.get("content")
    if isinstance(content, list):
        for item in content:
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            candidate = parsed.get("result") if isinstance(parsed, dict) else None
            if isinstance(candidate, dict):
                return candidate
    raise RuntimeError("binary_session returned no structured result")


def _tool_names(tools: Iterable[dict[str, Any]]) -> set[str]:
    return {
        str(tool.get("name") or tool.get("id") or "")
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or tool.get("id") or "")
    }


def _probe_error_text(call_result: dict[str, Any]) -> str:
    content = call_result.get("content")
    if isinstance(content, list):
        texts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if texts:
            return "; ".join(texts)[-2000:]
    structured = call_result.get("structuredContent")
    if structured:
        return json.dumps(structured, ensure_ascii=False)[-2000:]
    return "REA tool returned an error"


def _probe_payload(call_result: dict[str, Any]) -> dict[str, Any]:
    structured = call_result.get("structuredContent")
    if isinstance(structured, dict):
        result = structured.get("result")
        if isinstance(result, dict):
            return result
        return structured
    content = call_result.get("content")
    if isinstance(content, list):
        for item in content:
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                result = parsed.get("result")
                return result if isinstance(result, dict) else parsed
    return {}


def _semantic_probe_failure(
    name: str,
    payload: dict[str, Any],
) -> str | None:
    if name == "list_frida_devices":
        devices = payload.get("devices")
        if isinstance(devices, list) and not devices:
            return "Frida returned no local or remote devices"
    if name == "list_cutter_sessions":
        sessions = payload.get("sessions")
        if isinstance(sessions, list) and not sessions:
            discovery = str(payload.get("discovery_status") or "no_live_bridge_found")
            return f"Cutter has no live bridge session ({discovery})"
    return None


def normalize_functional_probe_result(
    name: str,
    call_result: Any = None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Normalize a safe tools/call probe into capability evidence."""

    if error:
        return {
            "ok": False,
            "reason": "functional_probe_failed",
            "error": error,
        }
    result = call_result if isinstance(call_result, dict) else {}
    if bool(result.get("isError")):
        return {
            "ok": False,
            "reason": "functional_probe_failed",
            "error": _probe_error_text(result),
        }
    semantic_failure = _semantic_probe_failure(name, _probe_payload(result))
    if semantic_failure:
        return {
            "ok": False,
            "reason": "functional_probe_failed",
            "error": semantic_failure,
        }
    return {
        "ok": True,
        "reason": "available",
        "error": None,
    }


async def run_functional_tool_probes(
    call_tool: Callable[[str, dict[str, Any]], Awaitable[Any]],
    tools: list[dict[str, Any]],
    *,
    timeout: float = FUNCTIONAL_PROBE_TIMEOUT_SECONDS,
    argument_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Call target-free provider diagnostics and retain per-tool evidence."""

    available = _tool_names(tools)

    async def run_probe(
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        call_arguments = dict(arguments)
        call_arguments.update((argument_overrides or {}).get(name) or {})
        if name == "execute_rizin_command":
            call_arguments = {
                "path": sys.executable,
                "command": "iI",
                **call_arguments,
            }
        try:
            call_result = await asyncio.wait_for(
                call_tool(name, call_arguments),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                name,
                normalize_functional_probe_result(
                    name,
                    error=str(exc) or type(exc).__name__,
                ),
            )
        return name, normalize_functional_probe_result(name, call_result)

    probes = [
        (name, arguments)
        for name, arguments in FUNCTIONAL_TOOL_PROBES.items()
        if name in available
    ]
    completed = await asyncio.gather(
        *(run_probe(name, arguments) for name, arguments in probes)
    )
    return dict(completed)


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
    env = mcp_record_environment(record)
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
        binary_session = binary_session_from_call_result(result)
        tools = listed.get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list returned no tools")
        functional_probes: dict[str, dict[str, Any]] = {}
        available_tools = _tool_names(tools)
        next_request_id = 4
        for name, arguments in FUNCTIONAL_TOOL_PROBES.items():
            if name not in available_tools:
                continue
            call_arguments = dict(arguments)
            if name == "execute_rizin_command":
                call_arguments = {
                    "path": sys.executable,
                    "command": "iI",
                    **call_arguments,
                }
            request_id = next_request_id
            next_request_id += 1
            _send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": call_arguments},
                },
            )
            probe_deadline = min(
                deadline,
                time.monotonic() + FUNCTIONAL_PROBE_TIMEOUT_SECONDS,
            )
            try:
                probed = _receive(messages, probe_deadline, request_id)
            except TimeoutError as exc:
                functional_probes[name] = normalize_functional_probe_result(
                    name,
                    error=str(exc),
                )
                continue
            error = probed.get("error")
            if error:
                functional_probes[name] = normalize_functional_probe_result(
                    name,
                    error=str(error),
                )
            else:
                functional_probes[name] = normalize_functional_probe_result(
                    name,
                    probed.get("result"),
                )
        if functional_probes:
            binary_session["functional_probes"] = functional_probes
        validation = validate_tool_inventory(tools, binary_session)
        return {
            "tools": tools,
            "binary_session": binary_session,
            "server_info": initialized.get("result", {}).get("serverInfo"),
            "functional_probe_results": functional_probes,
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
