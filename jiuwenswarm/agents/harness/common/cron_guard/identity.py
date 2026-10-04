# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted cron run identity, budget, and process registry (issue #5018, L0).

Identity model (design v2 §3.1):

* ``scheduled_run`` — the request carries a scheduler-signed ``cron.run_token``
  (HMAC-SHA256 over ``run_id|job_id|sid``).  Only the scheduler can mint it.
* ``interactive`` — everything else, including user follow-ups inside a
  cron-originated session.  cron_guard layers never apply.

The token secret comes from the env var named by
``execution_guard.cron_guard.trust.run_token_secret_env``.  When the secret is
not configured, tokens cannot be verified: the run is still treated as
``scheduled_run`` for *tightening-only* actions (deadline, sleep guard), but
state-changing actions (checkpoint quarantine) stay disabled.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from jiuwenswarm.common.utils import logger

# NOTE: config is imported lazily inside functions so tests can monkeypatch
# ``cron_guard.config.get_cron_guard_config`` and have it take effect here.

TOKEN_PURPOSE = "cron-run"


def _secret_from_env(cfg: dict[str, Any] | None = None) -> bytes | None:
    if cfg is None:
        from .config import get_cron_guard_config

        cfg = get_cron_guard_config()
    env_name = str(
        (cfg.get("trust") or {}).get("run_token_secret_env")
        or "JIUWENSWARM_CRON_RUN_SECRET"
    )
    value = os.environ.get(env_name, "").strip()
    return value.encode("utf-8") if value else None


def sign_run_token(run_id: str, job_id: str, sid: str, secret: bytes) -> str:
    """Mint the HMAC-SHA256 run token bound to (run_id, job_id, sid)."""
    payload = f"{TOKEN_PURPOSE}|{run_id}|{job_id}|{sid}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def maybe_sign_run_token(run_id: str, job_id: str, sid: str) -> str | None:
    """Sign a run token when a secret is configured; None otherwise (degraded)."""
    secret = _secret_from_env()
    if secret is None:
        return None
    return sign_run_token(run_id, job_id, sid, secret)


def verify_run_token(token: Any, run_id: str, job_id: str, sid: str) -> bool:
    """Constant-time verification of a run token.  Never raises."""
    try:
        secret = _secret_from_env()
        if secret is None or not isinstance(token, str) or not token:
            return False
        expected = sign_run_token(run_id, job_id, sid, secret)
        return hmac.compare_digest(expected, token)
    except Exception as exc:  # noqa: BLE001 — verification failure is fail-closed for trust, but never raises
        logger.warning("[cron_guard] token verification error: %s", exc)
        return False


@dataclass
class RunBudget:
    """Per-run shared budget counters (thread-safe; may be hit from thread pools)."""

    run_id: str
    max_iterations: Optional[int] = None
    tool_wait_budget_seconds: float = 300.0
    model_calls: int = 0
    shell_seconds: float = 0.0
    sleep_seconds: float = 0.0
    sleep_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_model_call(self) -> int:
        with self._lock:
            self.model_calls += 1
            return self.model_calls

    def add_sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleep_calls += 1
            self.sleep_seconds += max(0.0, float(seconds))

    def add_shell_seconds(self, seconds: float) -> None:
        with self._lock:
            self.shell_seconds += max(0.0, float(seconds))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model_calls": self.model_calls,
                "shell_seconds": round(self.shell_seconds, 3),
                "sleep_seconds": round(self.sleep_seconds, 3),
                "sleep_calls": self.sleep_calls,
            }

    def iterations_exceeded(self) -> bool:
        if self.max_iterations is None:
            return False
        with self._lock:
            return self.model_calls > int(self.max_iterations)

    def tool_wait_exceeded(self) -> bool:
        with self._lock:
            return self.shell_seconds > float(self.tool_wait_budget_seconds)


@dataclass
class CronRunContext:
    """Identity of the current cron scheduled run (design §3.1.1)."""

    run_id: str
    job_id: str
    sid: str
    trusted: bool  # token verified against a configured secret
    max_iterations: Optional[int] = None
    base_timeout_seconds: float = 3600.0
    deadline_soft: float = 0.0   # monotonic seconds
    deadline_hard: float = 0.0   # monotonic seconds
    budget: RunBudget = field(default_factory=lambda: RunBudget(run_id=""))
    started_monotonic: float = field(default_factory=time.monotonic)
    # Set by the L-WD soft timer; consumed by CronBudgetRail for soft finishing.
    soft_deadline_reached: bool = False
    force_finish_requested: bool = False

    @property
    def is_scheduled_run(self) -> bool:
        return True


#: ContextVar for the current cron run (asyncio task inheritance).
current_cron_run: contextvars.ContextVar[Optional[CronRunContext]] = contextvars.ContextVar(
    "current_cron_run", default=None
)


class RunRegistry:
    """Process-wide registry of in-flight cron runs (run_id → context).

    Also keeps a ``session_id → run_id`` index as the fallback lookup channel
    when the ContextVar is not visible (thread pools / executors).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, CronRunContext] = {}
        self._by_session: dict[str, str] = {}
        self._procs: dict[str, set[int]] = {}  # run_id -> registered pids

    def register(self, ctx: CronRunContext) -> None:
        with self._lock:
            self._runs[ctx.run_id] = ctx
            if ctx.sid:
                self._by_session[ctx.sid] = ctx.run_id

    def unregister(self, run_id: str) -> None:
        with self._lock:
            ctx = self._runs.pop(run_id, None)
            if ctx is not None and self._by_session.get(ctx.sid) == run_id:
                self._by_session.pop(ctx.sid, None)
            self._procs.pop(run_id, None)

    def get(self, run_id: str) -> Optional[CronRunContext]:
        with self._lock:
            return self._runs.get(run_id)

    def get_by_session(self, session_id: str | None) -> Optional[CronRunContext]:
        if not session_id:
            return None
        with self._lock:
            run_id = self._by_session.get(session_id)
            return self._runs.get(run_id) if run_id else None

    def register_proc(self, run_id: str, pid: int) -> None:
        with self._lock:
            self._procs.setdefault(run_id, set()).add(int(pid))

    def registered_pids(self, run_id: str) -> set[int]:
        with self._lock:
            return set(self._procs.get(run_id, set()))

    def reap(self, run_id: str, kill_grace_seconds: float = 3.0) -> bool:
        """Terminate all processes registered under *run_id*.

        SIGTERM the process group of each pid, wait ``kill_grace_seconds``,
        then SIGKILL survivors.  Returns True when nothing survived the TERM
        wave or no processes were registered.  Never raises.
        """
        try:
            pids = self.registered_pids(run_id)
            if not pids:
                return True
            import signal
            import subprocess

            try:
                my_pgid = os.getpgid(0)
            except OSError:  # pragma: no cover
                my_pgid = None
            for pid in pids:
                # Kill the child's process group only when it is NOT our own —
                # killpg on a child that shares our pgid would terminate the
                # whole AgentServer.  Children started with start_new_session
                # (or via nohup/setsid) get the full group kill; otherwise we
                # signal just the pid.
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    pgid = None
                if pgid is not None and my_pgid is not None and pgid != my_pgid:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                        continue
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            deadline = time.monotonic() + max(0.0, kill_grace_seconds)
            while time.monotonic() < deadline:
                if all(not _pid_alive(p) for p in pids):
                    return True
                time.sleep(0.1)
            for pid in pids:
                if not _pid_alive(pid):
                    continue
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    pgid = None
                if pgid is not None and my_pgid is not None and pgid != my_pgid:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                        continue
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            return all(not _pid_alive(p) for p in pids)
        except Exception as exc:  # noqa: BLE001 — reap must never raise
            logger.warning("[cron_guard] reap(%s) failed: %s", run_id, exc)
            return False

    def active_run_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs.keys())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    # os.kill(pid, 0) succeeds for zombies; treat reaped-but-unwaited children
    # as dead so the reaper does not spin on them.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat_line = fh.read().decode("utf-8", "replace")
        # state is the field after the (comm) — comm may contain spaces/parens.
        state = stat_line.rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


_registry: RunRegistry | None = None
_registry_lock = threading.Lock()


def get_run_registry() -> RunRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = RunRegistry()
        return _registry


def resolve_cron_run_context(
    request_metadata: dict[str, Any] | None,
    request_params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> Optional[CronRunContext]:
    """Build a :class:`CronRunContext` from an inbound request, or None.

    Called at the AgentServer request entry.  A request is a
    ``scheduled_run`` when its ``metadata.cron`` carries ``run_id`` and
    ``job_id``.  Token verification decides ``trusted`` only; an absent or
    forged token still yields a scheduled_run context so that
    tightening-only layers (deadline, sleep guard) apply — per design,
    a forged token can only make a request *more* constrained.
    """
    from .config import get_cron_guard_config

    cfg = get_cron_guard_config()
    if not cfg.get("enabled"):
        return None
    meta = request_metadata if isinstance(request_metadata, dict) else {}
    cron_meta = meta.get("cron")
    if not isinstance(cron_meta, dict):
        return None
    run_id = str(cron_meta.get("run_id") or "").strip()
    job_id = str(cron_meta.get("job_id") or "").strip()
    if not run_id or not job_id:
        return None
    sid = str(session_id or "")
    token = cron_meta.get("run_token")
    trusted = verify_run_token(token, run_id, job_id, sid)
    if token and not trusted:
        logger.warning(
            "[cron_guard] run_token failed verification (tightening-only, untrusted) run_id=%s",
            run_id,
        )

    base_timeout = float(cron_meta.get("timeout_seconds") or cfg.get("default_timeout_seconds") or 3600)
    max_iterations = cron_meta.get("max_iterations")
    if max_iterations is None:
        max_iterations = cfg.get("max_iterations")
    try:
        max_iterations = int(max_iterations) if max_iterations is not None else None
    except (TypeError, ValueError):
        max_iterations = int(DEFAULT_MAX_ITERATIONS_FALLBACK)

    from .config import clamp_deadlines

    hard_s, soft_s = clamp_deadlines(base_timeout, cfg)
    now = time.monotonic()
    budget = RunBudget(
        run_id=run_id,
        max_iterations=max_iterations,
        tool_wait_budget_seconds=float((cfg.get("wall_clock") or {}).get("tool_wait_budget_seconds", 300)),
    )
    return CronRunContext(
        run_id=run_id,
        job_id=job_id,
        sid=sid,
        trusted=trusted,
        max_iterations=max_iterations,
        base_timeout_seconds=base_timeout,
        deadline_soft=now + soft_s,
        deadline_hard=now + hard_s,
        budget=budget,
    )


DEFAULT_MAX_ITERATIONS_FALLBACK = 30


def get_current_or_registered_run(session_id: str | None = None) -> Optional[CronRunContext]:
    """Consumer-side lookup: ContextVar first, registry-by-session fallback."""
    ctx = current_cron_run.get()
    if ctx is not None:
        return ctx
    try:
        return get_run_registry().get_by_session(session_id)
    except Exception as exc:  # noqa: BLE001 — lookup failure = not a cron run (fail-open for interactive)
        logger.warning("[cron_guard] registry lookup failed: %s", exc)
        return None
