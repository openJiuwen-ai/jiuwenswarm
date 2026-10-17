"""Real-model Web/TUI acceptance driver for JiuwenSwarm Persist Session.

The 200 natural tasks are loaded from the versioned workload module next to
this file. This runner drives each product Channel over WebSocket and never
calls an Adapter directly.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

from jiuwenswarm.agents.harness.common.rails.eternal_conversation.prompts import (
    prompt_hashes,
)
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_agent_sessions_dir


WORKLOAD = Path(__file__).with_name("eternal_conversation_200_workload.py")
SOURCE_MANIFEST = Path(__file__).with_name("ETERNAL_CONVERSATION_SOURCE.json")
FINAL_EVENTS = {"chat.final", "chat.error", "chat.ask_user_question"}
TRACKED_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
IGNORED_PARTS = {".git", ".pytest_cache", ".venv", "__pycache__"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_ask_user_answer(payload: dict[str, Any]) -> str:
    """Render structured ask-user evidence as text without losing option details."""
    questions = payload.get("questions")
    if not isinstance(questions, list):
        content = payload.get("content")
        return content if isinstance(content, str) else ""
    rendered: list[str] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        header = str(item.get("header") or "").strip()
        options = item.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                description = str(option.get("description") or "").strip()
                detail = "：".join(part for part in (label, description) if part)
                if detail:
                    rendered.append(detail)
        question = str(item.get("question") or "").strip()
        if question:
            rendered.append(f"{header}：{question}" if header else question)
    return "\n".join(rendered)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def rewrite_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def iter_jsonl(path: Path):
    """Stream JSONL evidence so formal runs never load multi-GB history at once."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # A concurrently appended final record may not yet be complete.
                    # Any corruption before EOF still fails closed.
                    if not line.endswith("\n"):
                        return
                    raise
    except FileNotFoundError:
        return


def append_interrupted_once(path: Path, row: dict[str, Any]) -> None:
    request_id = row.get("request_id")
    if request_id and any(existing.get("request_id") == request_id for existing in load_jsonl(path)):
        return
    append_jsonl(path, row)


def _scenario_namespace() -> dict[str, Any]:
    """Compile only the versioned workload's data and build helpers."""
    tree = ast.parse(WORKLOAD.read_text(encoding="utf-8"), filename=str(WORKLOAD))
    wanted_functions = {
        "build_tasks",
        "initialize_project",
        "project_manifest",
        "changed_project_paths",
        "question_evidence",
        "probe_memory_evidence",
    }
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Component":
            body.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            body.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted_functions:
            body.append(node)
    namespace: dict[str, Any] = {
        "dataclass": dataclass,
        "Path": Path,
        "Any": Any,
        "hashlib": hashlib,
        "re": re,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(WORKLOAD), "exec"), namespace)
    return namespace


def resolve_configured_model(requested: str | None) -> dict[str, str]:
    defaults = list((get_config().get("models") or {}).get("defaults") or [])
    for entry in defaults:
        mcc = dict((entry or {}).get("model_client_config") or {})
        model = str(mcc.get("model_name") or "").strip()
        endpoint = str(mcc.get("api_base") or "").strip()
        provider = str(mcc.get("client_provider") or "").strip()
        alias = str((entry or {}).get("alias") or "").strip()
        if requested and requested not in {model, alias}:
            continue
        if not model:
            continue
        if not str(mcc.get("api_key") or "").strip():
            raise RuntimeError(f"configured model {alias or model!r} has no API key")
        return {
            "model_name": alias or model,
            "resolved_model_name": model,
            "provider": provider,
            "endpoint": endpoint,
        }
    detail = f" {requested!r}" if requested else ""
    raise RuntimeError(f"formal acceptance requires a valid configured model{detail}")


def project_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.suffix.casefold() not in TRACKED_SUFFIXES:
            continue
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_project_checkpoint(path: Path, root: Path, task_number: int) -> None:
    files: dict[str, str] = {}
    for relative in project_manifest(root):
        files[relative] = base64.b64encode((root / relative).read_bytes()).decode("ascii")
    write_json(
        path,
        {
            "task_number": task_number,
            "captured_at": utc_now(),
            "files": files,
        },
    )


def ensure_project_checkpoint(path: Path, root: Path, task_number: int) -> bool:
    """Capture one immutable workspace baseline per natural task.

    A retry must never promote a failed attempt's partial edits into the next
    attempt's baseline.  Returning whether a new baseline was written keeps
    the helper directly testable without coupling tests to the websocket run.
    """
    checkpoint = load_json(path, {}) or {}
    if int(checkpoint.get("task_number") or 0) == task_number:
        return False
    write_project_checkpoint(path, root, task_number)
    return True


def restore_incomplete_checkpoint(
    path: Path,
    root: Path,
    completed_prefix: int,
    audit_path: Path,
) -> None:
    checkpoint = load_json(path, {}) or {}
    task_number = int(checkpoint.get("task_number") or 0)
    if task_number <= completed_prefix:
        return
    encoded_files = checkpoint.get("files")
    if not isinstance(encoded_files, dict):
        raise RuntimeError(f"invalid acceptance checkpoint: {path}")
    resolved_root = root.resolve()
    current = set(project_manifest(root))
    expected = {str(relative) for relative in encoded_files}
    for relative in current - expected:
        target = (root / relative).resolve()
        if resolved_root not in target.parents:
            raise RuntimeError(f"checkpoint path escapes acceptance workspace: {relative}")
        target.unlink()
    for relative, encoded in encoded_files.items():
        target = (root / str(relative)).resolve()
        if resolved_root not in target.parents:
            raise RuntimeError(f"checkpoint path escapes acceptance workspace: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(base64.b64decode(str(encoded), validate=True))
        temporary.replace(target)
    append_jsonl(
        audit_path,
        {
            "task_number": task_number,
            "completed_prefix": completed_prefix,
            "restored_at": utc_now(),
            "file_count": len(encoded_files),
        },
    )


def task_evidence(feature_root: Path, task_id: str) -> dict[str, Any]:
    task_digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    task_path = feature_root / "raw-history" / "tasks" / f"{task_digest}.jsonl"
    evidence_path = task_path if task_path.exists() else feature_root / "raw-history" / "events.jsonl"
    events = [
        row
        for row in iter_jsonl(evidence_path)
        if row.get("task_id") == task_id
    ]
    calls = [row for row in events if row.get("type") == "tool-call"]
    results: list[dict[str, Any]] = []
    for row in events:
        payload = row.get("payload") or {}
        if row.get("type") == "tool-result" and payload.get("status") == "succeeded":
            results.append(row)

    def tool_args(row: dict[str, Any]) -> dict[str, Any]:
        value = (row.get("payload") or {}).get("tool_args")
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    commands = [str(tool_args(row).get("command") or "") for row in calls]
    return {
        "raw_events": len(events),
        "tool_calls": len(calls),
        "successful_tool_results": len(results),
        "tool_names": sorted(
            {str((row.get("payload") or {}).get("tool_name") or "") for row in calls}
        ),
        "verification_commands": [command for command in commands if "pytest" in command.casefold()],
        "memory_searches": sum(
            1
            for row in calls
            if (row.get("payload") or {}).get("tool_name") == "search_long_term_memory"
        ),
    }


def transport_task_evidence(
    transport_path: Path,
    task_id: str,
    start_offset: int = 0,
) -> dict[str, Any]:
    """Build non-persist task evidence from its product WebSocket window."""
    window: list[dict[str, Any]] = []
    collecting = False

    def window_rows():
        try:
            with transport_path.open("r", encoding="utf-8") as handle:
                handle.seek(start_offset)
                for line in handle:
                    if line.strip():
                        yield json.loads(line)
        except FileNotFoundError:
            return

    for row in window_rows():
        if row.get("direction") == "out" and row.get("kind") == "natural-task":
            frame = row.get("frame") or {}
            matches = str(frame.get("id") or "") == task_id
            if collecting and not matches:
                break
            collecting = matches
            if collecting:
                window = []
            continue
        if collecting:
            window.append(row)

    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for row in window:
        if row.get("direction") != "in":
            continue
        frame = row.get("frame") or {}
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        event_type = str(payload.get("event_type") or frame.get("event") or "")
        if event_type == "chat.tool_call" and isinstance(payload.get("tool_call"), dict):
            calls.append(payload["tool_call"])
        elif event_type == "chat.tool_result":
            result = payload.get("result")
            if not (isinstance(result, str) and "success=False" in result):
                results.append(payload)

    def tool_args(call: dict[str, Any]) -> dict[str, Any]:
        value = call.get("arguments")
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    commands = [
        str(tool_args(call).get("command") or tool_args(call).get("cmd") or "")
        for call in calls
    ]
    return {
        "raw_events": sum(row.get("direction") == "in" for row in window),
        "tool_calls": len(calls),
        "successful_tool_results": len(results),
        "tool_names": sorted({str(call.get("name") or "") for call in calls}),
        "verification_commands": [command for command in commands if "pytest" in command.casefold()],
        "memory_searches": sum(
            1 for call in calls if call.get("name") == "search_long_term_memory"
        ),
    }


def transport_metrics(transport_path: Path) -> dict[str, Any]:
    metrics = {"records": 0, "model_calls": 0, "context_replacements": 0}
    for row in iter_jsonl(transport_path):
        metrics["records"] += 1
        frame = row.get("frame") or {}
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        event_type = str(payload.get("event_type") or frame.get("event") or "")
        if row.get("direction") == "out" and row.get("kind") == "natural-task":
            metrics["model_calls"] += 1
        if event_type in {"context.replaced", "chat.context_replaced"}:
            metrics["context_replacements"] += 1
    return metrics


def raw_history_metrics(feature_root: Path, covered_through: int = 0) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "records": 0,
        "last_cursor": 0,
        "last_hash": "",
        "model_calls": 0,
        "context_replacements": 0,
        "uncovered_finished_tasks": 0,
    }
    for row in iter_jsonl(feature_root / "raw-history" / "events.jsonl"):
        cursor = int(row.get("cursor") or 0)
        kind = row.get("type")
        metrics["records"] += 1
        metrics["last_cursor"] = cursor
        metrics["last_hash"] = str(row.get("hash") or "")
        if kind == "model-visible-envelope":
            metrics["model_calls"] += 1
        elif kind == "context-replaced":
            metrics["context_replacements"] += 1
        elif kind == "task-finished" and cursor > covered_through:
            metrics["uncovered_finished_tasks"] += 1
    return metrics


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        chunk = handle.read(8 * 1024 * 1024)
        while chunk:
            digest.update(chunk)
            chunk = handle.read(8 * 1024 * 1024)
    return digest.hexdigest()


def projection_summary(projection: dict[str, Any]) -> dict[str, Any]:
    """Select the revision fields used in acceptance evidence."""
    fields = ("memory_revision", "snapshot_revision", "covered_through")
    result: dict[str, Any] = {}
    for key in fields:
        result[key] = projection.get(key)
    return result


def emit_json(value: dict[str, Any]) -> None:
    """Write one undecorated JSON document through the logging subsystem."""
    logger = logging.getLogger("persist-session-acceptance.output")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(json.dumps(value, ensure_ascii=False, indent=2))
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()


def history_file_inventory(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    records = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            if line.strip():
                records += 1
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "records": records,
        "sha256": digest.hexdigest(),
    }


def raw_hash_chain_inventory(path: Path) -> dict[str, Any]:
    previous_hash: str | None = None
    session_id: str | None = None
    records = 0
    for row in iter_jsonl(path):
        records += 1
        cursor = int(row.get("cursor") or 0)
        if cursor != records:
            raise RuntimeError(f"Raw History cursor mismatch at record {records}")
        current_session = str(row.get("session_id") or "")
        if not current_session:
            raise RuntimeError(f"Raw History session missing at cursor {cursor}")
        if session_id is None:
            session_id = current_session
        elif current_session != session_id:
            raise RuntimeError(f"Raw History session changed at cursor {cursor}")
        if row.get("previous_hash") != previous_hash:
            raise RuntimeError(f"Raw History previous_hash mismatch at cursor {cursor}")
        claimed_hash = str(row.get("hash") or "")
        digest_value = dict(row)
        digest_value.pop("hash", None)
        digest_source = json.dumps(
            digest_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        actual_hash = hashlib.sha256(digest_source).hexdigest()
        if claimed_hash != actual_hash:
            raise RuntimeError(f"Raw History event hash mismatch at cursor {cursor}")
        previous_hash = claimed_hash
    return {
        "verified": True,
        "records": records,
        "session_id": session_id,
        "last_cursor": records,
        "last_hash": previous_hash or "",
    }


def derived_evidence_view_inventory(feature_root: Path) -> dict[str, Any]:
    raw_path = feature_root / "raw-history" / "events.jsonl"
    canonical: dict[int, tuple[str, str, str | None]] = {}
    for row in iter_jsonl(raw_path):
        cursor = int(row.get("cursor") or 0)
        canonical[cursor] = (
            str(row.get("hash") or ""),
            str(row.get("type") or ""),
            str(row.get("task_id")) if row.get("task_id") is not None else None,
        )

    search_path = feature_root / "raw-history" / "search.jsonl"
    search_records = 0
    if not search_path.is_file():
        raise RuntimeError("Raw History search view is missing")
    for row in iter_jsonl(search_path):
        search_records += 1
        cursor = int(row.get("cursor") or 0)
        expected = canonical.get(cursor)
        actual = (
            str(row.get("hash") or ""),
            str(row.get("type") or ""),
            str(row.get("task_id")) if row.get("task_id") is not None else None,
        )
        if expected != actual:
            raise RuntimeError(f"Raw History search view mismatch at cursor {cursor}")
    if search_records != len(canonical):
        raise RuntimeError("Raw History search view is not a complete canonical projection")

    indexed_records = 0
    task_files = 0
    for task_path in sorted((feature_root / "raw-history" / "tasks").glob("*.jsonl")):
        task_files += 1
        for row in iter_jsonl(task_path):
            indexed_records += 1
            task_id = str(row.get("task_id") or "")
            if hashlib.sha256(task_id.encode("utf-8")).hexdigest() != task_path.stem:
                raise RuntimeError(f"Raw History task index filename mismatch: {task_path}")
            cursor = int(row.get("cursor") or 0)
            expected = canonical.get(cursor)
            actual = (
                str(row.get("hash") or ""),
                str(row.get("type") or ""),
                task_id or None,
            )
            if expected != actual:
                raise RuntimeError(f"Raw History task index mismatch at cursor {cursor}")
    return {
        "verified": True,
        "search_records": search_records,
        "task_files": task_files,
        "task_index_records": indexed_records,
    }


def evidence_inventory(feature_root: Path) -> dict[str, Any]:
    """Verify complete histories, content-addressed blobs and lossless archives."""
    paths = {
        role: feature_root / "agent-history" / role / "conversation.jsonl"
        for role in ("foreground", "extractor", "builder")
    }
    paths["raw"] = feature_root / "raw-history" / "events.jsonl"
    paths["raw_search_view"] = feature_root / "raw-history" / "search.jsonl"
    paths["memory_cli_calls"] = feature_root / "audit" / "memory-cli-calls.jsonl"
    histories = {
        name: history_file_inventory(path)
        for name, path in paths.items()
        if path.exists()
    }

    blobs: list[dict[str, Any]] = []
    for blob in sorted(feature_root.glob("**/blobs/*.json")):
        value = json.loads(blob.read_text(encoding="utf-8"))
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        actual = hashlib.sha256(canonical).hexdigest()
        if actual != blob.stem:
            raise RuntimeError(f"content-addressed evidence blob mismatch: {blob}")
        blobs.append(
            {
                "path": str(blob),
                "canonical_bytes": len(canonical),
                "sha256": actual,
            }
        )

    archives: list[dict[str, Any]] = []
    extractor_path = paths["extractor"]
    if extractor_path.exists():
        for row in iter_jsonl(extractor_path):
            archive = row.get("archive")
            if not isinstance(archive, dict):
                continue
            archive_path = Path(str(archive.get("path") or ""))
            if not archive_path.is_file():
                raise RuntimeError(f"archived Agent history is missing: {archive_path}")
            actual_bytes = archive_path.stat().st_size
            actual_hash = sha256_file(archive_path)
            if actual_bytes != int(archive.get("bytes") or -1):
                raise RuntimeError(f"archived Agent history byte mismatch: {archive_path}")
            if actual_hash != str(archive.get("sha256") or ""):
                raise RuntimeError(f"archived Agent history hash mismatch: {archive_path}")
            archives.append(
                {
                    "path": str(archive_path),
                    "bytes": actual_bytes,
                    "sha256": actual_hash,
                    "verified": True,
                }
            )
    return {
        "histories": histories,
        "raw_hash_chain": raw_hash_chain_inventory(paths["raw"]),
        "derived_evidence_views": derived_evidence_view_inventory(feature_root),
        "content_addressed_blobs": {
            "count": len(blobs),
            "canonical_bytes": sum(int(item["canonical_bytes"]) for item in blobs),
            "verified": True,
            "items": blobs,
        },
        "archives": archives,
    }


async def run_final_pytest(workspace: Path, timeout: float) -> dict[str, Any]:
    def invoke() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    started = time.monotonic()
    try:
        result = await asyncio.to_thread(invoke)
        return {
            "command": [sys.executable, "-m", "pytest", "-q"],
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": [sys.executable, "-m", "pytest", "-q"],
            "returncode": None,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"final pytest exceeded {timeout}s",
        }


async def receive_turn(
    ws,
    request_id: str,
    timeout: float,
    transport_path: Path,
    *,
    session_id: str,
    canonical_mode: str,
    work_mode: str,
    workspace: Path,
    model_name: str,
    persist_session: bool,
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    chunks: list[str] = []
    final = ""
    saw_terminal = False
    active_request_id = request_id
    approvals = 0
    approval_in_flight: str | None = None
    pending_approvals: list[dict[str, Any]] = []

    async def send_next_approval() -> None:
        nonlocal active_request_id, approval_in_flight, approvals
        if approval_in_flight is not None or not pending_approvals:
            return
        payload = pending_approvals.pop(0)
        interrupt_request_id = str(payload.get("request_id") or "").strip()
        if not interrupt_request_id:
            raise RuntimeError("permission interaction omitted its request_id")
        approvals += 1
        if approvals > 20:
            raise RuntimeError("too many permission interactions in one natural task")
        active_request_id = f"approval-{uuid.uuid4().hex}"
        approval_in_flight = active_request_id
        approval = {
            "type": "req",
            "id": active_request_id,
            "method": "chat.send",
            "params": {
                "session_id": session_id,
                "content": "",
                "query": "",
                "mode": canonical_mode,
                "work_mode": work_mode,
                "project_dir": str(workspace),
                "cwd": str(workspace),
                "workspace_dir": str(workspace),
                "trusted_dirs": [str(workspace)],
                "model_name": model_name,
                "eternal_conversation_enabled": persist_session,
                "request_id": interrupt_request_id,
                "source": str(payload.get("source")),
                "answers": [
                    {"selected_options": ["session_allow"], "custom_input": ""}
                ],
            },
        }
        append_jsonl(
            transport_path,
            {"direction": "out", "kind": "permission-approval", "frame": approval},
        )
        await ws.send(json.dumps(approval, ensure_ascii=False))

    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - asyncio.get_running_loop().time()))
        frame = json.loads(raw)
        append_jsonl(transport_path, {"direction": "in", "frame": frame})
        if frame.get("type") == "res" and frame.get("id") == active_request_id and not frame.get("ok", True):
            raise RuntimeError(str(frame.get("error") or frame))
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        event_type = str(payload.get("event_type") or frame.get("event") or "")
        content = payload.get("content")
        if event_type == "chat.ask_user_question" and payload.get("source") in {
            "permission_interrupt",
            "confirm_interrupt",
        }:
            pending_approvals.append(payload)
            await send_next_approval()
            chunks.clear()
            final = ""
            saw_terminal = False
            continue
        approval_completed = (
            payload.get("request_id") == approval_in_flight
            and payload.get("is_complete") is True
        )
        if event_type == "chat.processing_status" and approval_in_flight is not None:
            if not approval_completed:
                continue
            approval_in_flight = None
            await send_next_approval()
            continue
        if event_type == "chat.delta" and isinstance(content, str):
            chunks.append(content)
        elif event_type == "chat.ask_user_question":
            saw_terminal = True
            structured = render_ask_user_answer(payload)
            final = "\n".join(part for part in ("".join(chunks).strip(), structured) if part)
        elif event_type in FINAL_EVENTS:
            saw_terminal = True
            if isinstance(content, str):
                final = content
                if event_type == "chat.final" and content.strip():
                    return content
        if frame.get("event") == "stream.end" and (
            frame.get("id") in {None, active_request_id}
            or frame.get("request_id") == active_request_id
        ):
            if saw_terminal or chunks:
                return final or "".join(chunks)
        if saw_terminal and event_type in {"chat.error", "chat.ask_user_question"}:
            return final or "".join(chunks)
    raise TimeoutError(f"turn {request_id} did not finish within {timeout}s")


async def await_tui_connection_ack(ws, timeout: float, transport_path: Path) -> None:
    """Mirror the real TUI startup barrier before issuing chat.send."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(
            ws.recv(),
            timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
        )
        frame = json.loads(raw)
        append_jsonl(transport_path, {"direction": "in", "frame": frame})
        if frame.get("type") == "event" and frame.get("event") == "connection.ack":
            return
    raise TimeoutError("TUI connection did not become ready before chat.send")


async def wait_memory_idle(feature_root: Path, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        harness = load_json(feature_root / "state" / "harness.json", {}) or {}
        projection = load_json(feature_root / "state" / "eternal-conversation.json", {}) or {}
        requested = int(harness.get("requested_cursor") or 0)
        covered = int(projection.get("covered_through") or 0)
        pending = 0
        database = feature_root / "memory" / "memory.sqlite3"
        if database.exists():
            with sqlite3.connect(database) as connection:
                pending = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM uts WHERE status='active' AND build_state='pending'"
                    ).fetchone()[0]
                )
        last = {"requested_cursor": requested, "covered_through": covered, "pending": pending, **projection}
        if requested > 0 and covered == requested and pending == 0:
            return last
        worker_errors = {
            key: harness[key]
            for key in ("extractor_error", "builder_error")
            if harness.get(key)
        }
        if worker_errors:
            raise RuntimeError(f"memory worker failed before idle: {worker_errors}")
        await asyncio.sleep(0.5)
    raise TimeoutError(f"memory workers did not become idle: {last}")


async def run_quadrant(args: argparse.Namespace, channel: str, mode: str) -> dict[str, Any]:
    scenario = _scenario_namespace()
    all_tasks = list(scenario["build_tasks"](args.workspace))[: args.task_limit]
    if args.formal and len(all_tasks) != 200:
        raise RuntimeError("formal quadrant must execute all 200 tasks")
    model = resolve_configured_model(args.model_name)
    run_kind = "ablation" if args.ablation_no_eternal else "eternal"
    run_root: Path | None = None
    rows: list[dict[str, Any]] = []
    if args.resume:
        candidates = sorted(
            args.evidence_root.glob(f"{channel}-{mode}-{run_kind}-{channel}-{mode}-*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            run_root = candidates[0]
            existing_proof = load_json(run_root / "proof.json", {}) or {}
            if (
                existing_proof.get("accepted") is True
                and int(existing_proof.get("tasks_required") or 0) == args.task_limit
            ):
                proof_path = run_root / "proof.json"
                return {
                    **existing_proof,
                    "proof_path": str(proof_path),
                    "proof_sha256": sha256_file(proof_path),
                }
            session_id = run_root.name.removeprefix(f"{channel}-{mode}-")
            previous = load_jsonl(run_root / "progress.jsonl")
            for row in previous:
                if (
                    int(row.get("number") or 0) == len(rows) + 1
                    and (row.get("passed") or args.continue_after_failure)
                ):
                    rows.append(row)
                else:
                    append_interrupted_once(run_root / "interrupted-tasks.jsonl", row)
            rewrite_jsonl(run_root / "progress.jsonl", rows)
            restore_incomplete_checkpoint(
                run_root / "active-checkpoint.json",
                args.workspace,
                len(rows),
                run_root / "checkpoint-restores.jsonl",
            )
        else:
            session_id = args.session_id or f"{run_kind}-{channel}-{mode}-{uuid.uuid4().hex}"
    else:
        session_id = args.session_id or f"{run_kind}-{channel}-{mode}-{uuid.uuid4().hex}"
    session_root = get_agent_sessions_dir() / session_id
    feature_root = session_root / "eternal-conversation"
    resume_path_missing = not args.workspace.exists() or not session_root.exists()
    required_feature_missing = (
        not args.ablation_no_eternal and not feature_root.exists()
    )
    if run_root is None:
        if feature_root.exists() or session_root.exists() and any(session_root.iterdir()):
            raise RuntimeError(f"acceptance requires a fresh empty Session: {session_root}")
        args.workspace.mkdir(parents=True, exist_ok=False)
        scenario["initialize_project"](args.workspace)
        run_root = args.evidence_root / f"{channel}-{mode}-{session_id}"
        run_root.mkdir(parents=True, exist_ok=False)
    elif resume_path_missing or required_feature_missing:
        raise RuntimeError("resume requires the original Session and project workspace")
    transport_path = run_root / "transport.jsonl"
    url = args.web_url if channel == "web" else args.tui_url
    canonical_mode = "agent" if mode == "work" else "code.normal"

    async def interrupt_timed_out_turn(ws, request_id: str) -> dict[str, Any]:
        """Cancel a timed-out foreground turn before restoring its workspace."""
        interrupt_id = f"{request_id}-timeout-interrupt"
        request = {
            "type": "req",
            "id": interrupt_id,
            "method": "chat.interrupt",
            "params": {
                "intent": "cancel",
                "session_id": session_id,
                "mode": canonical_mode,
                "trusted_dirs": [str(args.workspace)],
            },
        }
        append_jsonl(
            transport_path,
            {"direction": "out", "kind": "task-timeout-interrupt", "frame": request},
        )
        await ws.send(json.dumps(request, ensure_ascii=False))
        deadline = asyncio.get_running_loop().time() + 30.0
        while asyncio.get_running_loop().time() < deadline:
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
            )
            frame = json.loads(raw)
            append_jsonl(transport_path, {"direction": "in", "frame": frame})
            if frame.get("type") == "res" and frame.get("id") == interrupt_id:
                return {
                    "request_id": interrupt_id,
                    "accepted": bool(frame.get("ok")),
                    "payload": frame.get("payload") or {},
                }
        return {"request_id": interrupt_id, "accepted": False, "reason": "timeout"}

    async def execute_task(ws, task: dict[str, Any], attempt: int) -> dict[str, Any]:
        request_id = f"acceptance-{int(task['number']):03d}-{uuid.uuid4().hex[:8]}"
        before = project_manifest(args.workspace)
        transport_start_offset = transport_path.stat().st_size if transport_path.exists() else 0
        request = {
            "type": "req",
            "id": request_id,
            "method": "chat.send",
            "params": {
                "session_id": session_id,
                "content": task["prompt"],
                "query": task["prompt"],
                "mode": canonical_mode,
                "work_mode": mode,
                "project_dir": str(args.workspace),
                "cwd": str(args.workspace),
                "workspace_dir": str(args.workspace),
                "trusted_dirs": [str(args.workspace)],
                "model_name": model["model_name"],
                "eternal_conversation_enabled": not args.ablation_no_eternal,
            },
        }
        started = time.monotonic()
        append_jsonl(
            transport_path,
            {"direction": "out", "kind": "natural-task", "attempt": attempt, "frame": request},
        )
        await ws.send(json.dumps(request, ensure_ascii=False))
        try:
            answer = await receive_turn(
                ws,
                request_id,
                args.turn_timeout,
                transport_path,
                session_id=session_id,
                canonical_mode=canonical_mode,
                work_mode=mode,
                workspace=args.workspace,
                model_name=model["model_name"],
                persist_session=not args.ablation_no_eternal,
            )
        except TimeoutError as exc:
            interrupt = await interrupt_timed_out_turn(ws, request_id)
            after = project_manifest(args.workspace)
            changed = sorted(
                path for path in set(before) | set(after) if before.get(path) != after.get(path)
            )
            evidence = (
                transport_task_evidence(transport_path, request_id, transport_start_offset)
                if args.ablation_no_eternal
                else task_evidence(feature_root, request_id)
            )
            return {
                **task,
                "attempt": attempt,
                "request_id": request_id,
                "duration_seconds": round(time.monotonic() - started, 3),
                "answer": "",
                "changed_paths": changed,
                "evidence": evidence,
                "question_evidence": False,
                "marker_evidence": None,
                "timeout_interrupt": interrupt,
                "failures": [f"{type(exc).__name__}: {exc}"],
                "passed": False,
            }
        after = project_manifest(args.workspace)
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        evidence = (
            transport_task_evidence(transport_path, request_id, transport_start_offset)
            if args.ablation_no_eternal
            else task_evidence(feature_root, request_id)
        )
        is_probe = bool(task.get("conflict_probe"))
        marker = task.get("probe_marker")
        question = bool(re.search(r"[?？][\s*_`'\"）)\]]*$", answer.strip()))
        marker_found = bool(marker and str(marker).casefold() in answer.casefold()) if is_probe else None
        failures: list[str] = []
        if not answer.strip():
            failures.append("empty foreground answer")
        if evidence["successful_tool_results"] < 2:
            failures.append("fewer than two successful real tool calls")
        if is_probe:
            if changed:
                failures.append("blind conflict probe changed project files")
            if not question:
                failures.append("blind conflict probe did not end with one question")
            if not marker_found:
                failures.append("blind conflict probe did not recover hidden marker")
            if not args.ablation_no_eternal and evidence["memory_searches"] < 1:
                failures.append("blind conflict probe did not call memory search")
        else:
            if not changed:
                failures.append("natural development task made no persistent change")
            if not evidence["verification_commands"]:
                failures.append("natural development task ran no pytest verification")
        return {
            **task,
            "attempt": attempt,
            "request_id": request_id,
            "duration_seconds": round(time.monotonic() - started, 3),
            "answer": answer,
            "changed_paths": changed,
            "evidence": evidence,
            "question_evidence": question,
            "marker_evidence": marker_found,
            "failures": failures,
            "passed": not failures,
        }

    async with websockets.connect(url, max_size=None, ping_timeout=60) as ws:
        if channel == "tui":
            await await_tui_connection_ack(ws, args.turn_timeout, transport_path)
        stop_quadrant = False
        for task in all_tasks[len(rows):]:
            for attempt in range(1, args.max_task_attempts + 1):
                ensure_project_checkpoint(
                    run_root / "active-checkpoint.json",
                    args.workspace,
                    int(task["number"]),
                )
                row = await execute_task(ws, task, attempt)
                if row["passed"]:
                    if args.ablation_no_eternal:
                        row["memory_barrier_waited"] = False
                        row["uncovered_finished_tasks"] = None
                        rows.append(row)
                        append_jsonl(run_root / "progress.jsonl", row)
                        write_json(
                            run_root / "heartbeat.json",
                            {
                                "completed": len(rows),
                                "passed": sum(bool(item.get("passed")) for item in rows),
                                "updated_at": utc_now(),
                            },
                        )
                        break
                    projection_before_barrier = load_json(
                        feature_root / "state" / "eternal-conversation.json", {}
                    ) or {}
                    metrics = raw_history_metrics(
                        feature_root,
                        int(projection_before_barrier.get("covered_through") or 0),
                    )
                    row["memory_barrier_waited"] = False
                    row["uncovered_finished_tasks"] = metrics[
                        "uncovered_finished_tasks"
                    ]
                    periodic_barrier = (
                        int(task["number"]) % args.max_uncovered_tasks == 0
                    )
                    if (
                        metrics["uncovered_finished_tasks"] >= args.max_uncovered_tasks
                        or periodic_barrier
                    ):
                        row["memory_barrier_waited"] = True
                        row["memory_barrier_reason"] = (
                            "periodic"
                            if periodic_barrier
                            else "uncovered-task-threshold"
                        )
                        try:
                            projection_after_barrier = await wait_memory_idle(
                                feature_root, args.background_timeout
                            )
                        except Exception as exc:
                            # The foreground task is already durably complete.
                            # Record it before aborting so --resume never replays
                            # a natural task against a diverged Session/project.
                            row["memory_barrier_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                            rows.append(row)
                            append_jsonl(run_root / "progress.jsonl", row)
                            write_json(
                                run_root / "heartbeat.json",
                                {
                                    "completed": len(rows),
                                    "passed": sum(bool(item.get("passed")) for item in rows),
                                    "memory_barrier_error": row["memory_barrier_error"],
                                    "updated_at": utc_now(),
                                },
                            )
                            raise
                        row["projection_after_barrier"] = projection_summary(
                            projection_after_barrier
                        )
                    rows.append(row)
                    append_jsonl(run_root / "progress.jsonl", row)
                    write_json(
                        run_root / "heartbeat.json",
                        {
                            "completed": len(rows),
                            "passed": sum(bool(item.get("passed")) for item in rows),
                            "updated_at": utc_now(),
                        },
                    )
                    break
                restore_incomplete_checkpoint(
                    run_root / "active-checkpoint.json",
                    args.workspace,
                    len(rows),
                    run_root / "checkpoint-restores.jsonl",
                )
                row["workspace_restored_to_task_baseline"] = True
                if attempt < args.max_task_attempts and not args.ablation_no_eternal:
                    try:
                        retry_projection = await wait_memory_idle(
                            feature_root, args.background_timeout
                        )
                    except Exception as exc:
                        row["retry_memory_barrier_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        append_interrupted_once(
                            run_root / "interrupted-tasks.jsonl", row
                        )
                        raise
                    row["retry_projection"] = projection_summary(retry_projection)
                append_interrupted_once(run_root / "interrupted-tasks.jsonl", row)
                if attempt == args.max_task_attempts:
                    rows.append(row)
                    append_jsonl(run_root / "progress.jsonl", row)
                    write_json(
                        run_root / "heartbeat.json",
                        {
                            "completed": len(rows),
                            "passed": sum(bool(item.get("passed")) for item in rows),
                            "updated_at": utc_now(),
                        },
                    )
                    stop_quadrant = not args.continue_after_failure
            if stop_quadrant:
                break
    projection = (
        {}
        if args.ablation_no_eternal
        else await wait_memory_idle(feature_root, args.background_timeout)
    )
    final_pytest = await run_final_pytest(args.workspace, args.final_pytest_timeout)
    write_json(run_root / "final-pytest.json", final_pytest)
    if args.ablation_no_eternal:
        histories = {"foreground": str(session_root / "history.jsonl")}
        raw_metrics = transport_metrics(transport_path)
    else:
        histories = {
            role: str(feature_root / "agent-history" / role / "conversation.jsonl")
            for role in ("foreground", "extractor", "builder")
        }
        histories["raw"] = str(feature_root / "raw-history" / "events.jsonl")
        raw_metrics = raw_history_metrics(feature_root)
    blind_probe_rows = [row for row in rows if row.get("conflict_probe")]
    blind_probes_using_memory_search = sum(
        row["passed"] and int(row["evidence"]["memory_searches"]) > 0
        for row in blind_probe_rows
    )
    metadata = load_json(session_root / "metadata.json", {}) or {}
    if args.ablation_no_eternal:
        eternal_artifacts = sorted(
            str(path.relative_to(session_root))
            for path in feature_root.rglob("*")
            if path.is_file()
        ) if feature_root.exists() else []
        foreground_history = session_root / "history.jsonl"
        inventory = {
            "transport": history_file_inventory(transport_path),
            "foreground_history": (
                history_file_inventory(foreground_history)
                if foreground_history.exists()
                else None
            ),
            "eternal_artifacts": eternal_artifacts,
        }
        formal_gates = (
            not args.formal
            or (
                metadata.get("persist_session") is False
                and not eternal_artifacts
                and foreground_history.exists()
                and len(blind_probe_rows) == 5
            )
        )
    else:
        inventory = evidence_inventory(feature_root)
        formal_gates = (
            not args.formal
            or (
            len(blind_probe_rows) == 5
            and blind_probes_using_memory_search == 5
            and int(raw_metrics["context_replacements"]) > 0
            and int(projection.get("snapshot_revision") or 0) >= 2
            and int(projection.get("covered_through") or 0) > 0
            and int(projection.get("pending") or 0) == 0
            and inventory["raw_hash_chain"]["verified"] is True
            and int(inventory["raw_hash_chain"]["records"]) == int(raw_metrics["records"])
            and inventory["derived_evidence_views"]["verified"] is True
            )
        )
    accepted = (
        len(rows) == len(all_tasks)
        and all(row["passed"] for row in rows)
        and final_pytest.get("returncode") == 0
        and formal_gates
    )
    proof = {
        "accepted": accepted,
        "ablation_no_eternal": args.ablation_no_eternal,
        "channel": channel,
        "mode": mode,
        "session_id": session_id,
        "tasks_required": len(all_tasks),
        "tasks_completed": len(rows),
        "tasks_passed": sum(row["passed"] for row in rows),
        "failed_attempts": len(load_jsonl(run_root / "interrupted-tasks.jsonl")),
        "memory_barrier_failures": sum(
            bool(row.get("memory_barrier_error")) for row in rows
        ),
        "blind_probes_passed": sum(bool(row.get("conflict_probe")) and row["passed"] for row in rows),
        "blind_probes_using_memory_search": blind_probes_using_memory_search,
        "memory_searches": sum(int(row["evidence"]["memory_searches"]) for row in rows),
        "projection": {key: projection.get(key) for key in ("memory_revision", "snapshot_revision", "covered_through")},
        "raw_history": raw_metrics,
        "model": model,
        "real_foreground_model_calls": raw_metrics["model_calls"],
        "final_pytest": {
            "returncode": final_pytest.get("returncode"),
            "duration_seconds": final_pytest.get("duration_seconds"),
            "evidence_path": str(run_root / "final-pytest.json"),
        },
        "acceptance_runtime": {
            "permissions_enabled": bool(
                (get_config().get("permissions") or {}).get("enabled", False)
            ),
            "mock_model_fallback": False,
        },
        "prompt_hashes": prompt_hashes(),
        "skill_source_hash": hashlib.sha256(
            (
                Path(__file__).parents[2]
                / "jiuwenswarm/resources/agent/workspace/skills/dynamic-memory-cli/SKILL.md"
            ).read_bytes()
        ).hexdigest(),
        "acceptance_source": load_json(SOURCE_MANIFEST),
        "histories": histories,
        "evidence_inventory": inventory,
        "feature_root": str(feature_root),
        "project_root": str(args.workspace),
        "persist_session": metadata.get("persist_session"),
        "first_failure": next(
            (
                {
                    "number": row.get("number"),
                    "phase": row.get("phase"),
                    "component": row.get("component"),
                    "failures": row.get("failures"),
                    "request_id": row.get("request_id"),
                }
                for row in rows
                if not row.get("passed")
            ),
            None,
        ),
    }
    proof_path = run_root / "proof.json"
    write_json(proof_path, proof)
    return {
        **proof,
        "proof_path": str(proof_path),
        "proof_sha256": sha256_file(proof_path),
    }


async def run(args: argparse.Namespace) -> int:
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    if args.matrix:
        quadrants = (("web", "work"), ("web", "code"), ("tui", "work"), ("tui", "code"))
        semaphore = asyncio.Semaphore(max(1, args.matrix_concurrency))

        async def run_matrix_quadrant(channel: str, mode: str) -> dict[str, Any]:
            quadrant = argparse.Namespace(**vars(args))
            quadrant.workspace = args.workspace / f"{channel}-{mode}"
            quadrant.session_id = None
            async with semaphore:
                return await run_quadrant(quadrant, channel, mode)

        if args.parallel_matrix:
            proofs = list(
                await asyncio.gather(
                    *(run_matrix_quadrant(channel, mode) for channel, mode in quadrants)
                )
            )
        else:
            proofs = []
            for channel, mode in quadrants:
                proofs.append(await run_matrix_quadrant(channel, mode))
        total_tasks = sum(int(proof["tasks_completed"]) for proof in proofs)
        total_blind_probes = sum(int(proof["blind_probes_passed"]) for proof in proofs)
        matrix = {
            "accepted": (
                all(proof["accepted"] for proof in proofs)
                and total_tasks == 800
                and total_blind_probes == 20
            ),
            "total_tasks": total_tasks,
            "total_failed_attempts": sum(int(proof["failed_attempts"]) for proof in proofs),
            "total_blind_probes_passed": total_blind_probes,
            "child_proofs": [
                {
                    "channel": proof["channel"],
                    "mode": proof["mode"],
                    "session_id": proof["session_id"],
                    "path": proof["proof_path"],
                    "sha256": proof["proof_sha256"],
                }
                for proof in proofs
            ],
            "quadrants": proofs,
        }
        write_json(args.evidence_root / "matrix-proof.json", matrix)
        emit_json(matrix)
        return 0 if matrix["accepted"] else 1
    proof = await run_quadrant(args, args.channel, args.mode)
    emit_json(proof)
    return 0 if proof["accepted"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-url", default="ws://127.0.0.1:20001/ws")
    parser.add_argument("--tui-url", default="ws://127.0.0.1:20001/tui")
    parser.add_argument("--channel", choices=("web", "tui"), default="web")
    parser.add_argument("--mode", choices=("work", "code"), default="work")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument(
        "--parallel-matrix",
        action="store_true",
        help=(
            "run independent matrix quadrants concurrently; task order within each Session "
            "remains sequential"
        ),
    )
    parser.add_argument("--matrix-concurrency", type=int, default=4)
    parser.add_argument("--max-task-attempts", type=int, default=3)
    parser.add_argument(
        "--continue-after-failure",
        action="store_true",
        help="record an exhausted failed task and continue the remaining workload",
    )
    parser.add_argument("--formal", action="store_true")
    parser.add_argument(
        "--ablation-no-eternal",
        action="store_true",
        help="run the full workload with Persist Session disabled and score from transport evidence",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--task-limit", type=int, default=200)
    parser.add_argument("--turn-timeout", type=float, default=30 * 60)
    parser.add_argument("--background-timeout", type=float, default=30 * 60)
    parser.add_argument(
        "--max-uncovered-tasks",
        type=int,
        default=4,
        help="wait for Extractor/Builder after this many completed-but-uncovered natural tasks",
    )
    parser.add_argument("--final-pytest-timeout", type=float, default=30 * 60)
    parser.add_argument("--model-name")
    parser.add_argument("--session-id")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
