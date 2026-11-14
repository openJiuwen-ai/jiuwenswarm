"""Best-effort, correlated task diagnostics shared by Gateway and AgentServer."""

from datetime import datetime, timezone
import logging
import os

from jiuwenswarm.common.utils import append_jsonl_log, get_user_workspace_dir

logger = logging.getLogger(__name__)


_VERBOSE_EVENTS = {
    "checkpoint_sent", "checkpoint_acknowledged", "task_push_sent",
    "task_trace_progress_store_observed", "task_trace_queue_store_observed",
    "task_trace_queue_received", "task_trace_queue_ignored", "task_trace_progress_ignored",
}


def trace_task(event, **fields):
    """Record control metadata, never audio, images, credentials or tool results."""
    verbose = event in _VERBOSE_EVENTS or (
        event == "cancel_rpc_sent" and fields.get("probe")
    ) or (
        event == "cancel_rpc_response" and fields.get("probe") and fields.get("ok")
        and isinstance(fields.get("payload"), dict)
        and fields["payload"].get("managed_task_status") == "running"
        and fields["payload"].get("success") is not False
        and not fields["payload"].get("error")
    )
    if verbose and not logger.isEnabledFor(logging.DEBUG):
        return
    record = {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        path = get_user_workspace_dir() / "logs" / "duplex-task-trace.jsonl"
        append_jsonl_log(path, record)
    except Exception:
        # Diagnostics must never change cancellation or dispatch semantics.
        logger.warning("Could not write duplex task diagnostics", exc_info=True)


def task_state(task):
    """Select the authoritative lifecycle fields needed to compare queue updates."""
    keys = (
        "id", "session", "core_session_id", "request_id", "status", "revision",
        "sequence", "position", "execution_bound", "checkpoint_open",
        "execution_settled", "execution_cancelled", "output_closed",
    )
    return {key: task.get(key) for key in keys}


def receipt_state(payload):
    """Retain operation outcomes and queue identities without full task content."""
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    keys = (
        "state", "applied", "code", "error", "queue_version", "call_id",
        "job_id", "task_id", "status", "success", "search_session_id", "revision",
        "managed_task_status", "managed_task_settled",
    )
    result = {key: payload[key] for key in keys if key in payload}
    for key in ("operation", "tool_result", "search_job", "target"):
        if isinstance(payload.get(key), dict):
            result[key] = receipt_state(payload[key])
    if isinstance(payload.get("jobs"), list):
        job_keys = (
            "id", "job_id", "status", "revision", "sequence", "queue_position",
            "queue_version", "request_id",
        )
        result["jobs"] = []
        for job in payload["jobs"]:
            result["jobs"].append({key: job.get(key) for key in job_keys})
    return result
