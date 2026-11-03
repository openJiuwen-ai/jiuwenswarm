"""Best-effort, correlated task diagnostics shared by Gateway and AgentServer."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def trace_task(event, **fields):
    """Record control metadata, never audio, images, credentials or tool results."""
    record = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        path = Path.home() / ".jiuwenswarm" / "logs" / "duplex-task-trace.jsonl"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except Exception:
        # Diagnostics must never change cancellation or dispatch semantics.
        logger.warning("Could not write duplex task diagnostics", exc_info=True)


def task_state(task):
    """Select the authoritative lifecycle fields needed to compare queue updates."""
    return {key: task.get(key) for key in (
        "id", "session", "core_session_id", "request_id", "status", "revision",
        "sequence", "position", "execution_bound", "checkpoint_open",
        "execution_settled", "execution_cancelled", "output_closed",
    )}


def receipt_state(payload):
    """Retain operation outcomes and queue identities without full task content."""
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    result = {key: payload[key] for key in (
        "state", "applied", "code", "error", "queue_version", "call_id",
        "job_id", "task_id", "status", "success", "search_session_id", "revision",
    ) if key in payload}
    for key in ("operation", "tool_result", "search_job", "target"):
        if isinstance(payload.get(key), dict):
            result[key] = receipt_state(payload[key])
    if isinstance(payload.get("jobs"), list):
        result["jobs"] = [{key: job.get(key) for key in (
            "id", "job_id", "status", "revision", "sequence", "queue_position",
            "queue_version", "request_id",
        )} for job in payload["jobs"]]
    return result
