# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``exec:`` credential references for model api_key / custom_headers entries.

The ``exec:<command>`` value form resolves the model entry's secret by
running a secret-manager read CLI (1Password ``op``, Vault, pass, …) and
taking its stdout as the value. The contract:

* single call, no side effects, stdout is the secret;
* execution happens in exactly three places — process prewarm (once per
  reference), the save path (a low-frequency user action that awaits the
  result), and a single-worker background queue (serial, consuming misses
  from the ``resolve_env_vars`` dispatch). The ``resolve_env_vars`` dispatch
  branch itself never runs a subprocess: it only looks the reference up in
  ``_RESOLVED_SECRETS``, enqueues a miss, and returns ``""`` — the event
  loop never executes commands on this path;
* resolution failure degrades to ``""`` plus a warning; entries carry a
  status (``resolved`` / ``failed``) with a 5-minute retry backoff on
  ``failed`` — after the backoff window the queue may retry and self-heal
  (e.g. the user fixed the secret manager);
* subprocess stdout/stderr never reach the log; the command string is
  config.yaml text (not secret) and may appear in warnings;
* argv list via ``shlex.split`` + ``shell=False`` — no shell interpretation.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
from queue import Queue
from typing import Any

logger = logging.getLogger(__name__)

EXEC_PREFIX = "exec:"
_EXEC_TIMEOUT_SECONDS = 30
_BACKOFF_SECONDS = 300
# A pending claim older than 2x the subprocess timeout has a dead owner
# (nothing else holds a claim across a subprocess run longer than that).
_STALE_PENDING_SECONDS = 2 * _EXEC_TIMEOUT_SECONDS

# Process-level result store. Key: the full reference string; value: the
# secret plaintext plus its status ("pending" while a track is executing,
# then "resolved" or "failed"; "failed" entries carry a backoff timestamp —
# queue tasks for them are skipped until the backoff window passes, then
# retried once). The pending placeholder is what keeps the save track /
# prewarm pool / background queue from running the same command twice
# concurrently. Plaintext lives in process memory only — never logged,
# never persisted.
_RESOLVED_SECRETS: dict[str, dict[str, Any]] = {}
_RESOLVED_LOCK = threading.Lock()

# Background serial queue: one worker thread consumes resolve misses. Serial
# consumption is what makes concurrent execution structurally impossible —
# no in-flight set or state machine needed.投递侧由 resolve_env_vars 的分流
# 分支在 miss 时调用（非阻塞）；保存轨不走队列（await 直接拿结果）。
_RESOLVE_QUEUE: "Queue[str]" = Queue(maxsize=64)
_QUEUE_WORKER: threading.Thread | None = None
_QUEUE_WORKER_LOCK = threading.Lock()

# 投递侧去重：同引用在队列/退避期间不重复投递（防止高频 miss 顶掉其他引用的任务）。
_ENQUEUED: set[str] = set()

# Warning de-dup window: the same (reference, reason) failing at
# get_config() frequency (several times per second) must not flood logs.
_WARN_DEDUP_SECONDS = 60
_WARN_LAST_AT: dict[tuple[str, str], float] = {}
_WARN_DEDUP_LOCK = threading.Lock()

_QUEUE_MAXSIZE = 64


def is_reference(value: Any) -> bool:
    """Return True when the value is an ``exec:`` reference string."""
    return isinstance(value, str) and value.startswith(EXEC_PREFIX)


def _record(ref: str, value: str, status: str) -> None:
    with _RESOLVED_LOCK:
        _RESOLVED_SECRETS[ref] = {"value": value, "status": status, "ts": time.monotonic()}


def _claim_for_execution(ref: str, *, bypass_backoff: bool = False) -> bool:
    """Atomically place a pending placeholder; False when already claimed.

    The save track, the prewarm pool, and the queue worker all funnel through
    this claim before running a subprocess: a pending entry means another
    track owns the execution; a resolved entry means there is nothing to do;
    a failed entry inside its backoff window means the retry window is closed
    (bypass_backoff carries explicit user-retry intent — probe/save — past it,
    re-claiming the entry for an immediate run). A pending entry older than
    2x the subprocess timeout is stale — its owner died (crash/kill between
    claim and release) — and may be preempted; a live owner finishing later
    simply overwrites the store with its own result.
    """
    with _RESOLVED_LOCK:
        entry = _RESOLVED_SECRETS.get(ref)
        if entry is not None and entry.get("status") == "resolved":
            return False
        if entry is not None and entry.get("status") == "pending":
            stale = time.monotonic() - float(entry.get("ts", 0.0)) >= _STALE_PENDING_SECONDS
            if not stale:
                return False
            logger.warning("[credentials] stale pending claim on %r preempted", ref)
        if entry is not None and not bypass_backoff and _backoff_active(entry):
            return False
        _RESOLVED_SECRETS[ref] = {"value": "", "status": "pending", "ts": time.monotonic()}
        return True


def _release_claim(ref: str, value: str, status: str) -> None:
    """Replace the pending placeholder with a terminal status."""
    _record(ref, value, status)


def _backoff_active(entry: dict[str, Any]) -> bool:
    return (
        entry.get("status") == "failed"
        and time.monotonic() - float(entry.get("ts", 0.0)) < _BACKOFF_SECONDS
    )


def _warn_degraded(ref: str, field_path: str, reason: str) -> None:
    """Warn with the three-element message, de-duplicated per time window."""
    with _WARN_DEDUP_LOCK:
        key = (ref, reason)
        now = time.monotonic()
        if now - _WARN_LAST_AT.get(key, 0.0) < _WARN_DEDUP_SECONDS:
            return
        _WARN_LAST_AT[key] = now
    logger.warning(
        "[credentials] %s: reference %r failed to resolve (%s); "
        "falling back to empty until the background resolve succeeds",
        field_path or "<unknown field>",
        ref,
        reason,
    )


def _exec_secret_command(ref: str) -> tuple[str | None, str]:
    """Run the command; return ``(stdout_value, failure_reason)``.

    stdout/stderr content never leaves this function — only the value
    itself (returned) and a failure category string (for warnings).
    """
    command = ref[len(EXEC_PREFIX):].strip()
    if not command:
        return None, "empty command"
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, f"unparsable command ({type(exc).__name__})"
    if not argv:
        return None, "empty command"
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_EXEC_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout({_EXEC_TIMEOUT_SECONDS}s)"
    except OSError as exc:
        # FileNotFoundError (command not on PATH) and peers.
        return None, f"spawn failed ({type(exc).__name__})"
    if completed.returncode != 0:
        return None, f"exit={completed.returncode}"
    stdout_value = completed.stdout.strip()
    if not stdout_value:
        return None, "empty stdout"
    return stdout_value, ""


def _resolve_now(ref: str, *, field_path: str, bypass_backoff: bool = False) -> None:
    """Execute once (after claiming) and update the store; never raises.

    The claim funnels every executing track (save/probe/prewarm/queue)
    through one gate: when another track already holds the pending
    placeholder or a live backoff window, this call is a no-op — the owner's
    result will be in the store when it finishes. bypass_backoff carries the
    explicit user-retry intent of the probe/save track past a failed entry's
    backoff window.
    """
    if not _claim_for_execution(ref, bypass_backoff=bypass_backoff):
        return
    try:
        value, reason = _exec_secret_command(ref)
    except Exception:  # noqa: BLE001 — a broken command must not poison the store
        logger.warning("[credentials] unexpected error resolving %r", ref, exc_info=True)
        _release_claim(ref, "", "failed")
        _warn_degraded(ref, field_path, "unexpected error")
        return
    if value is not None:
        _release_claim(ref, value, "resolved")
        return
    _release_claim(ref, "", "failed")
    _warn_degraded(ref, field_path, reason)


def resolve_credential_ref_now(ref: str, *, field_path: str) -> tuple[bool, str]:
    """Save-track resolve: run the command now and report the outcome.

    Returns ``(ok, value)``: ``ok`` is False only when the reference ended in
    a terminal failure, so the save response can carry the failure instead
    of surfacing it later as a model-call 401. A ``pending`` entry means
    another track is executing the same command right now — that is not a
    failure (its result lands in the store within the subprocess timeout),
    so the caller gets ``(True, "")`` and no error is attached. Never raises.
    """
    with _RESOLVED_LOCK:
        entry = _RESOLVED_SECRETS.get(ref)
        if entry is not None and entry.get("status") == "resolved":
            return True, entry["value"]
    _resolve_now(ref, field_path=field_path, bypass_backoff=True)
    with _RESOLVED_LOCK:
        entry = _RESOLVED_SECRETS.get(ref)
        if entry is not None and entry.get("status") == "resolved":
            return True, entry["value"]
        if entry is not None and entry.get("status") == "pending":
            return True, ""  # another track is resolving; not a failure
        return False, ""


def _enqueue_worker_once() -> None:
    """Start the single worker thread lazily; idempotent."""
    global _QUEUE_WORKER
    with _QUEUE_WORKER_LOCK:
        if _QUEUE_WORKER is not None and _QUEUE_WORKER.is_alive():
            return
        _QUEUE_WORKER = threading.Thread(
            target=_consume_queue, name="exec-secret-resolve", daemon=True
        )
        _QUEUE_WORKER.start()


def _consume_queue() -> None:
    """Serial worker: one command at a time for the whole process.

    Seriality is what removes concurrent duplicate execution structurally
    (no in-flight set needed): misses may be enqueued by many threads, but
    only this thread ever runs a command. Consumption-side de-dup: a task
    whose reference is already resolved is skipped.
    """
    while True:
        try:
            ref, field_path = _RESOLVE_QUEUE.get()
        except Exception:  # noqa: BLE001 — a malformed item must not kill the only worker
            logger.warning("[credentials] queue worker received a malformed item", exc_info=True)
            continue
        try:
            with _RESOLVED_LOCK:
                entry = _RESOLVED_SECRETS.get(ref)
                if entry is not None and entry.get("status") == "resolved":
                    _ENQUEUED.discard(ref)
                    continue  # 保存轨/重复任务先到，已解决
                if entry is not None and _backoff_active(entry):
                    # 退避窗口内不重试；释放去重标记让窗口后的下一次 miss 能重新入队
                    _ENQUEUED.discard(ref)
                    continue
            _resolve_now(ref, field_path=field_path)
            _ENQUEUED.discard(ref)
        except Exception:  # noqa: BLE001 — worker must never die
            logger.warning("[credentials] queue worker error resolving %r", ref, exc_info=True)
            _ENQUEUED.discard(ref)
        finally:
            _RESOLVE_QUEUE.task_done()


def _enqueue_resolve(ref: str, *, field_path: str) -> None:
    """Non-blocking enqueue; drops when full or already queued. Never raises."""
    _enqueue_worker_once()
    with _RESOLVED_LOCK:
        if ref in _ENQUEUED:
            return
        if _RESOLVE_QUEUE.qsize() >= _QUEUE_MAXSIZE:
            return  # drop-newest when saturated; next miss re-enqueues
        _RESOLVE_QUEUE.put((ref, field_path))
        _ENQUEUED.add(ref)


def resolve_credential_ref(ref: str, *, field_path: str, probe: bool = False) -> str:
    """Look up / resolve one ``exec:`` reference; never raises.

    Called from two places with different intents:
    - ``resolve_env_vars`` dispatch (runtime path): returns the stored value,
      or ``""`` for a miss after enqueueing the background resolve;
    - probe (``config.validate_model`` and the save track): user-initiated
      action — bypasses the ``failed`` backoff and runs the command
      immediately; a success promotes the entry to ``resolved`` (probing
      through implies runtime usable).
    """
    with _RESOLVED_LOCK:
        entry = _RESOLVED_SECRETS.get(ref)
        if entry is not None and entry.get("status") == "resolved":
            return entry["value"]
        if entry is not None and _backoff_active(entry) and not probe:
            return entry["value"]  # 退避窗口内：返回空串（预热/按需失败已告警）

    if probe:
        _resolve_now(ref, field_path=field_path, bypass_backoff=True)
        with _RESOLVED_LOCK:
            return _RESOLVED_SECRETS.get(ref, {}).get("value", "")

    # Runtime miss: enqueue for the background worker, return "" immediately
    # (the event loop never runs commands on this path).
    _warn_once_for_new_reference(ref, field_path)
    _enqueue_resolve(ref, field_path=field_path)
    return ""


def _warn_once_for_new_reference(ref: str, field_path: str) -> None:
    # A runtime miss on a brand-new reference (user edited the command) would
    # otherwise be silent until the queue consumer logs it; surface it once.
    _warn_degraded(ref, field_path, "queued for background resolve")


def prewarm_from_config(config_raw: dict[str, Any]) -> None:
    """Prewarm: resolve every registry-matching exec: reference once.

    Called from each consuming process's startup sequence (Worker lifecycle,
    process_cli entry). Idempotent per reference: references already in the
    store are skipped (including previously failed ones — a failed prewarm
    leaves the entry failed; restart to retry). Bounded worker pool: each
    command is capped by the 30s subprocess timeout, so the pool bounds both
    concurrency and total wall-clock (references exceeding capacity are left
    for the background queue to resolve on first miss).

    幂等按引用逐条判定：不是"单例非空即跳过"——按需解析与预热写同一张表，
    逐条判定才能让"启动时未覆盖、运行期已按需解析"的引用不被重复执行。
    """
    from concurrent.futures import ThreadPoolExecutor

    from jiuwenswarm.common.credentials.registry import is_credential_field

    refs: list[tuple[str, str]] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if value.startswith(EXEC_PREFIX) and is_credential_field(path):
                refs.append((value, path))
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for item in value:  # list items keep the parent path (no index segment)
                _walk(item, path)

    _walk(config_raw, "")
    if not refs:
        return

    with _RESOLVED_LOCK:
        todo = [(ref, fp) for ref, fp in refs if ref not in _RESOLVED_SECRETS]
    if not todo:
        return

    with ThreadPoolExecutor(
        max_workers=min(8, len(todo)), thread_name_prefix="exec-secret-prewarm"
    ) as pool:
        for ref, fp in todo:
            pool.submit(_resolve_now, ref, field_path=fp)
