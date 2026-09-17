# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Process-based runtime adapter (bare-metal mode).

Spawns bubblewrap once per sandbox lifecycle to start the in-sandbox daemon.
All ``exec`` and ``exec_background`` requests are served via daemon IPC so
user commands inherit the same namespace/mount/seccomp/Landlock envelope
without spawning bwrap per call.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import errno
import grp
import json
import logging
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import NetworkMode, NetworkPolicy, SecurityPolicy
from jiuwenbox.models.sandbox import (
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobResult,
)
from jiuwenbox.server.runtime.base import (
    RuntimeAdapter,
    RuntimeBackgroundExecRequest,
    RuntimeExecRequest,
    RuntimeFileOpResult,
)
from jiuwenbox.server.workspace import SANDBOX_WORKSPACE
from jiuwenbox.supervisor import cgroup as cgroup_module
from jiuwenbox.supervisor import network as network_module
from jiuwenbox.supervisor.bwrap import BwrapConfig
from jiuwenbox.supervisor.daemon_ipc import (
    LISTENER_FD_ENV,
    MAX_FILE_BYTES,
    MAX_HEADER_BYTES,
    REQUEST_TYPE_BG_KILL,
    REQUEST_TYPE_BG_STATUS,
    REQUEST_TYPE_EXEC,
    REQUEST_TYPE_EXEC_BACKGROUND,
    REQUEST_TYPE_LIST_DIR,
    REQUEST_TYPE_READ_FILE,
    REQUEST_TYPE_SHUTDOWN,
    REQUEST_TYPE_WRITE_FILE,
    SANDBOX_CONTROL_SOCKET_NAME,
    SANDBOX_DAEMON_COMMAND,
    SANDBOX_DAEMON_SANDBOX_PATH,
    SANDBOX_LAUNCHER_PATH,
    SANDBOX_RESERVED_DIR,
    encode_request,
    recv_frame,
    send_frame,
)
from jiuwenbox.supervisor.landlock import encode_landlock_payload
from jiuwenbox.supervisor.seccomp import build_seccomp_filter

configure_logging()
logger = logging.getLogger(__name__)
SERVER_PROTECT_PORTS_ENV = "JIUWENBOX_SERVER_PROTECT_PORTS"
# Host-level firewall protection: for ``network.mode: host`` sandboxes we
# attempt to install an iptables OUTPUT block by-sandbox-uid against the
# box-server's own HTTP port, so untrusted code running inside the sandbox
# cannot ``curl http://127.0.0.1:8321/...`` and pivot through the management
# API. The default tracks the listener configured via ``JIUWENBOX_LISTEN``
# (set by ``launcher.py``); operators who run the server on a non-default
# port no longer have to remember to also bump this list. When
# ``JIUWENBOX_LISTEN`` is UDS or unparseable we fall back to the
# historical default of port 8321 so existing setups behave unchanged. The list can
# still be overridden via ``JIUWENBOX_SERVER_PROTECT_PORTS=...``
# (comma-separated integers) or disabled entirely by exporting empty.
#
# The install path needs root for ``iptables -L OUTPUT`` and ``-I OUTPUT``;
# when jiuwenbox runs unprivileged (e.g. the code-agent setup from
# ``configs/code-agent-policy.yaml``) those calls return EPERM. In that case
# we log a warning and **continue** sandbox creation without installing the
# rule rather than aborting - the user picked an unprivileged deployment and
# explicitly accepted that the box-server has no host-level protection.
LISTEN_URI_ENV = "JIUWENBOX_LISTEN"
DEFAULT_SERVER_PROTECT_PORTS: tuple[int, ...] = (8321,)


def _derive_protect_ports_from_listen() -> tuple[int, ...]:
    """Extract a TCP port from ``$JIUWENBOX_LISTEN`` for self-protection.

    Recognized shapes (matching ``launcher.parse_listen``):
        - ``http://host:port`` -> ``(port,)``
        - ``unix:///abs/path`` -> ``()`` (no TCP port to protect)

    Anything else (env unset, malformed) returns the historical default of
    ``(8321,)`` so existing operator workflows that never set
    ``JIUWENBOX_LISTEN`` (e.g. test harnesses that call ``ProcessRuntime``
    directly) keep their previous behavior.
    """
    uri = os.environ.get(LISTEN_URI_ENV, "")
    if not uri:
        return DEFAULT_SERVER_PROTECT_PORTS
    if uri.startswith("unix://"):
        return ()
    if uri.startswith("http://"):
        # ``http://0.0.0.0:8321`` -> ``("0.0.0.0", "8321")``. ``rsplit``
        # guards against IPv6 hosts like ``http://[::]:8321`` where ``:``
        # appears multiple times.
        host_port = uri[len("http://"):]
        try:
            _, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        except (ValueError, IndexError):
            return DEFAULT_SERVER_PROTECT_PORTS
        if 1 <= port <= 65535:
            return (port,)
    return DEFAULT_SERVER_PROTECT_PORTS

_SUPERVISOR_DIR = Path(__file__).resolve().parents[2] / "supervisor"
LANDLOCK_LAUNCHER_SOURCE = _SUPERVISOR_DIR / "landlock_launcher.py"
SANDBOX_DAEMON_SOURCE = _SUPERVISOR_DIR / "sandbox_daemon.py"
# Read the launcher and daemon source once at module load so we do not pay
# the I/O cost on every sandbox creation; bytes are immutable so sharing is
# safe across sandboxes.
_LANDLOCK_LAUNCHER_BYTES = LANDLOCK_LAUNCHER_SOURCE.read_bytes()
_SANDBOX_DAEMON_BYTES = SANDBOX_DAEMON_SOURCE.read_bytes()
PYTHON_EXECUTABLE = "python3"

# Per-sandbox control socket: box-server ``bind()``s a Unix socket on its
# own host filesystem inside a per-sandbox control directory, then passes
# the listener fd into bubblewrap via ``subprocess.Popen(pass_fds=...)``.
# Bubblewrap forks twice (monitor → intermediate → user command) but the
# user command path never closes arbitrary inherited fds, and Python's
# ``pass_fds`` clears CLOEXEC, so the listener fd survives all the way to
# the daemon. The daemon recovers the fd number from ``LISTENER_FD_ENV``
# and ``accept()``s on it. Because the socket file never appears under any
# sandbox-visible path, user code spawned by the daemon cannot reach the
# listener (and the daemon's ``subprocess.Popen`` calls run with
# ``close_fds=True`` so the inherited listener fd is not exposed to
# children either).
DAEMON_CONNECT_TIMEOUT_SECONDS = 2.0
DAEMON_SHUTDOWN_TIMEOUT_SECONDS = 3.0
DAEMON_STARTUP_GRACE_SECONDS = 0.3
DAEMON_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
# Upper bound on how much of the daemon's spawn-time stdout/stderr we
# include in the ``RuntimeError`` raised when the supervisor exits before
# becoming ready. 16 KiB comfortably fits a Python traceback plus a few
# lines of bwrap/seccomp/landlock diagnostics, while keeping the resulting
# exception message bounded for callers that surface it over HTTP.
DAEMON_STARTUP_LOG_MAX_BYTES = 16 * 1024
# File ops (upload/download/list) are CPU-cheap on the daemon side - it
# is just an open/read/write or scandir call. Cap the IPC roundtrip at a
# short upper bound so a wedged daemon does not stall HTTP requests.
DAEMON_FILE_OP_TIMEOUT_SECONDS = 30.0
# When the caller omits an exec timeout the daemon still waits for the
# child to finish before responding. Cap the IPC read at the common
# agent-core default (300s) plus the same +5s cushion used for explicit
# timeouts so a wedged daemon cannot hang HTTP requests forever.
DEFAULT_EXEC_IPC_READ_TIMEOUT_SECONDS = 305.0

# OS-level errors that mean the daemon is actually gone or its control
# socket is permanently broken. When we see one of these, ``_daemon_socket_ready``
# is flipped to ``False`` so subsequent calls take the slow legacy
# ``bash`` / ``python3`` fallback path instead of repeatedly re-trying a
# dead socket.
FATAL_DAEMON_ERRNOS: frozenset[int] = frozenset(
    (
        errno.ECONNREFUSED,    # nothing listening → daemon crashed
        errno.ECONNRESET,      # peer (daemon) closed mid-stream
        errno.ENOENT,          # listener path vanished
        errno.EPIPE,           # writing to a closed pipe/socket
        errno.ETIMEDOUT,       # daemon never responded → hung
        errno.EBADF,           # our fd was already closed → dead session
    ),
)

# OS-level errors that are *recoverable* - they describe a transient
# resource shortage on this specific call (host out of fds, fork queue
# full, signal interrupt, ...) but say nothing about whether the daemon
# itself is healthy. Reporting them to the caller as ``transport_failure``
# without flipping ``_daemon_socket_ready`` lets the next request try the
# fast path again. The earlier version of this fix lumped them into the
# fatal set, which caused a single ``EAGAIN`` / ``EBUSY`` / ``EINTR`` to
# permanently demote a sandbox to the bash+base64 path - sandbox-count=8
# regressed from ~217 ms back to ~919 ms because every later call paid the
# python cold-start tax.
RECOVERABLE_DAEMON_ERRNOS: frozenset[int] = frozenset(
    (
        errno.EAGAIN,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOMEM,
        errno.ENOBUFS,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EBUSY,
        errno.EINTR,
        # Some platforms expose ``EWOULDBLOCK`` as a distinct value from
        # ``EAGAIN``; on Linux they are equal so the frozenset deduplicates.
        errno.EWOULDBLOCK,
    ),
)


@dataclasses.dataclass(frozen=True)
class _DaemonExecCall:
    """Inputs to one IPC ``exec`` request.

    Bundled into a single value object so the per-call worker thread
    only takes one positional argument (G.FNM.03 keeps the signature at
    or below five arguments).
    """

    socket_path: Path
    command: list[str]
    env: dict[str, str] | None
    workdir: str | None
    stdin_bytes: bytes | None
    timeout: float | None


@dataclasses.dataclass(frozen=True)
class _DaemonListDirCall:
    """Inputs to one IPC ``list_dir`` request (see ``_DaemonExecCall``)."""

    socket_path: Path
    sandbox_path: str
    recursive: bool
    max_depth: int | None
    include_files: bool
    include_dirs: bool


@dataclasses.dataclass(frozen=True)
class _DaemonBgExecCall:
    """Inputs to one IPC ``exec_background`` request."""

    socket_path: Path
    job_id: str
    command: list[str]
    env: dict[str, str] | None
    workdir: str | None
    stdin_bytes: bytes | None


@dataclasses.dataclass(frozen=True)
class _DaemonBgStatusCall:
    """Inputs to one IPC ``bg_status`` request."""

    socket_path: Path
    job_id: str


@dataclasses.dataclass(frozen=True)
class _DaemonBgKillCall:
    """Inputs to one IPC ``bg_kill`` request."""

    socket_path: Path
    job_id: str
    signum: int


# Admission control for ``exec``. ``exec`` is the one operation that
# spawns a fresh ``python3`` (or other) child inside the sandbox, which
# is overwhelmingly CPU-bound (interpreter cold start + imports + user
# script). Allowing more concurrent ``exec`` calls than the box has
# usable CPUs causes classic throughput collapse: TLB churn, L3-cache
# eviction across competing python interpreters, and per-fork mmap_sem
# contention in the kernel. Empirically each unit of oversubscription
# beyond 1.0 multiplies per-call latency by roughly 2-3x because of
# this collapse, *not* by the linear factor a fully scheduler-friendly
# workload would predict.
#
# The ``JIUWENBOX_EXEC_CONCURRENCY`` env var lets operators tune the
# limit (e.g. set it lower than CPU count to leave headroom for the
# server, or set it to a very large number to disable throttling). When
# unset we use the cgroup-aware ``os.process_cpu_count()`` if available
# (Python 3.13+) and fall back to ``os.cpu_count()`` so containers with
# CPU quotas get the right value automatically.
EXEC_CONCURRENCY_ENV = "JIUWENBOX_EXEC_CONCURRENCY"

# ---------------------------------------------------------------------------
# Zombie reaper plumbing.
#
# Background: each sandbox lifecycle spawns one bwrap monitor process as a
# direct child of box-server. With ``--unshare-user`` (default in jiuwenbox)
# bwrap additionally clones a userns helper via ``CLONE_PARENT`` so the
# helper's parent is *also* box-server, not bwrap. When that helper finishes
# its short setup and exits, it becomes a zombie of box-server until somebody
# calls ``waitpid`` on it.
#
# Background jobs no longer spawn per-command bwrap processes; they are forked
# inside the sandbox daemon's PID namespace via daemon IPC.
#
# The fix is three-pronged:
# - ``prctl(PR_SET_CHILD_SUBREAPER, 1)`` so even when box-server isn't PID 1
#   of its namespace, any descendant orphan reparents to us instead of
#   escaping to the real init (which would silently mask the leak).
# - SIGCHLD-driven reaper installed via ``asyncio.loop.add_signal_handler``
#   for the fast (sub-millisecond) wake path. This is the preferred channel
#   on stock asyncio; uvloop, however, *refuses* SIGCHLD handlers
#   unconditionally (loop.pyx raises ``RuntimeError`` because uvloop reserves
#   it for its own libuv child watcher). Since uvicorn auto-selects uvloop
#   when the package is installed (the docker image does ship uvloop), we
#   cannot rely on this channel in production.
# - Periodic ``asyncio.Task`` fallback that polls every
#   ``ZOMBIE_REAPER_INTERVAL_ENV`` seconds (default 2.0). 2 s keeps the peak
#   ``<defunct>`` count tiny under realistic loads (bwrap spawn rate is
#   bounded by the exec admission semaphore) while costing essentially zero
#   CPU on an idle server.
#
# At startup we *try* the SIGCHLD path; if it raises, we silently degrade to
# the periodic task. ``register_zombie_reaper`` returns ``True`` as long as
# *either* path is active, so the lifespan hook doesn't need to know which.
PR_SET_CHILD_SUBREAPER = 36  # Linux prctl(2)
_subreaper_enabled: bool = False
ZOMBIE_REAPER_INTERVAL_ENV = "JIUWENBOX_ZOMBIE_REAPER_INTERVAL"
ZOMBIE_REAPER_DEFAULT_INTERVAL_SECONDS = 2.0


@dataclass
class BackgroundJob:
    job_id: str
    sandbox_id: str
    command: list[str]
    pid: int | None
    workdir: str | None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class BackgroundJobNotFoundError(Exception):
    """Raised when a background job id is unknown for a sandbox."""


def _resolve_zombie_reaper_interval() -> float:
    """Return the periodic-reaper interval in seconds.

    Operators can tune via ``JIUWENBOX_ZOMBIE_REAPER_INTERVAL``. Invalid /
    non-positive values fall back to the default and emit a warning rather
    than crashing the server -- a misconfigured env var must never block
    box-server startup.
    """
    raw = os.environ.get(ZOMBIE_REAPER_INTERVAL_ENV)
    if not raw:
        return ZOMBIE_REAPER_DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using default %.1fs",
            ZOMBIE_REAPER_INTERVAL_ENV,
            raw,
            ZOMBIE_REAPER_DEFAULT_INTERVAL_SECONDS,
        )
        return ZOMBIE_REAPER_DEFAULT_INTERVAL_SECONDS
    if value <= 0:
        logger.warning(
            "%s=%r must be positive; using default %.1fs",
            ZOMBIE_REAPER_INTERVAL_ENV,
            raw,
            ZOMBIE_REAPER_DEFAULT_INTERVAL_SECONDS,
        )
        return ZOMBIE_REAPER_DEFAULT_INTERVAL_SECONDS
    return value


def enable_child_subreaper() -> bool:
    """Make the current process a subreaper for its descendant tree.

    Idempotent and best-effort; logs (but does not raise) when prctl is
    unavailable (non-Linux) or denied. Returns ``True`` when the flag is
    active after the call. The flag persists for the lifetime of the
    process, so it is safe to call again on a hot reload.
    """
    global _subreaper_enabled
    if _subreaper_enabled:
        return True
    if not sys.platform.startswith("linux"):
        logger.debug("PR_SET_CHILD_SUBREAPER unavailable on %s", sys.platform)
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        ret = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except OSError as exc:
        logger.warning("PR_SET_CHILD_SUBREAPER prctl failed: %s", exc)
        return False
    except Exception:  # noqa: BLE001 - ctypes is permissive about errors
        logger.warning("PR_SET_CHILD_SUBREAPER prctl raised", exc_info=True)
        return False
    if ret != 0:
        logger.warning(
            "PR_SET_CHILD_SUBREAPER prctl returned %d (errno=%d); box-server "
            "will not act as a subreaper, orphan bwrap helpers may escape "
            "to PID 1",
            ret,
            ctypes.get_errno(),
        )
        return False
    _subreaper_enabled = True
    logger.info(
        "PR_SET_CHILD_SUBREAPER enabled; orphan descendants will reparent "
        "to box-server (pid=%d) and be reaped by the SIGCHLD handler",
        os.getpid(),
    )
    return True


def _read_cgroup_cpu_quota() -> int | None:
    """Return the cgroup-imposed CPU count, or ``None`` if uncapped.

    On Python 3.11 (which the box-server image ships with) ``os.cpu_count()``
    on Linux returns the *host* CPU count, NOT the container's
    ``--cpus``/cgroup quota. That means a 4-CPU container running on a
    16-core host saw ``os.cpu_count() == 16``, which silently undid the
    exec admission semaphore: the limit became 16, all 8 sandboxes ran
    concurrently, throughput collapsed under 4x oversubscription, and
    sandbox-count=8 latency stayed pinned at ~900 ms instead of ~200 ms.
    Reading cgroup directly gives the actual scheduling budget.
    """
    # cgroup v2: ``/sys/fs/cgroup/cpu.max`` -> ``"<quota> <period>"`` or
    # ``"max <period>"`` if uncapped.
    try:
        with open("/sys/fs/cgroup/cpu.max", "r") as fh:
            raw = fh.read().strip()
    except OSError:
        raw = ""
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota = int(parts[0])
                period = int(parts[1])
            except ValueError:
                quota = period = 0
            if quota > 0 and period > 0:
                cpus = max(1, round(quota / period))
                return cpus
        if parts and parts[0] == "max":
            return None
    # cgroup v1 fallback.
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as fh:
            quota = int(fh.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r") as fh:
            period = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, round(quota / period))


def _detect_default_exec_concurrency() -> int:
    """Detect how many parallel CPU-bound execs the host can actually run.

    Preference order (most to least authoritative for our purposes):
      1. cgroup CPU quota - exactly matches Docker's ``--cpus``.
      2. ``os.process_cpu_count()`` - Python 3.13+ cgroup-aware helper.
      3. ``os.sched_getaffinity(0)`` - Linux affinity mask, useful when
         the user pinned us via ``taskset``.
      4. ``os.cpu_count()`` - finally, the host's reported CPU count.
    """
    cgroup_cpus = _read_cgroup_cpu_quota()
    if cgroup_cpus is not None and cgroup_cpus >= 1:
        return cgroup_cpus

    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        try:
            value = process_cpu_count()
        except (OSError, ValueError):
            value = None
        if value and value >= 1:
            return value

    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if callable(sched_getaffinity):
        try:
            mask = sched_getaffinity(0)
        except OSError:
            mask = None
        if mask:
            return len(mask)

    cpu_count = os.cpu_count()
    if cpu_count and cpu_count >= 1:
        return cpu_count
    return 1


def _resolve_exec_concurrency() -> int:
    raw = os.environ.get(EXEC_CONCURRENCY_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring non-integer %s=%r; falling back to detected CPU count",
                EXEC_CONCURRENCY_ENV,
                raw,
            )
        else:
            if value >= 1:
                return value
            logger.warning(
                "Ignoring %s=%r (must be >= 1); falling back to detected CPU count",
                EXEC_CONCURRENCY_ENV,
                raw,
            )
    return _detect_default_exec_concurrency()


def _safe_close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        logger.debug("Failed to close fd %d", fd, exc_info=True)


def _summarize_command(command: list[str], max_length: int = 180) -> str:
    text = json.dumps(command, ensure_ascii=False)
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}... ({len(text)} chars)"


class ProcessRuntime(RuntimeAdapter):
    """Runtime that spawns supervisor as a local process.

    Sandbox stdout/stderr (both the daemon and any background ``exec``)
    is dropped at the kernel level via ``subprocess.DEVNULL``: the
    historical ``runtime.log`` files were removed in favour of the
    single structured ``audit.log`` written by :class:`AuditLogger`,
    which already carries truncated stdout/stderr per command. Anyone
    needing the raw byte stream should attach a debugger or run the
    container with ``-it``; we no longer persist it to disk.
    """

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._policy_paths: dict[str, Path] = {}
        self._runtime_policies: dict[str, SecurityPolicy] = {}
        self._policy_binds: dict[str, list[dict[str, str]]] = {}
        self._network_modes: dict[str, NetworkMode] = {}
        self._netns_names: dict[str, str] = {}
        self._uplink_handles: dict[str, network_module.UplinkHandle] = {}
        self._directory_roots: dict[str, Path] = {}
        self._file_roots: dict[str, Path] = {}
        self._launcher_dirs: dict[str, Path] = {}
        self._control_dirs: dict[str, Path] = {}
        self._daemon_socket_ready: dict[str, bool] = {}
        self._seccomp_bpf: dict[str, bytes] = {}
        self._landlock_payloads: dict[str, str] = {}
        self._background_processes: dict[str, dict[str, BackgroundJob]] = {}
        self._host_firewall_refcounts: dict[tuple[int, int], int] = {}
        self._sandbox_host_firewall_rules: dict[str, list[tuple[int, int]]] = {}
        self._cgroup_handles: dict[str, cgroup_module.CgroupHandle] = {}
        # Admission-control semaphore for ``exec``. Lazy-initialized in
        # ``_ensure_exec_semaphore`` because ``asyncio.Semaphore`` binds to
        # the running loop on first use, and ``ProcessRuntime`` is built
        # outside the asyncio loop in some startup paths (CLI, tests).
        self._exec_concurrency_limit: int = _resolve_exec_concurrency()
        self._exec_semaphore: asyncio.Semaphore | None = None
        # Zombie reaper plumbing. Plumbed in via ``register_zombie_reaper``
        # once an event loop is available (lifespan startup); we cannot
        # install a signal handler / create a Task before the loop exists.
        # ``_sigchld_loop`` is the loop the SIGCHLD handler is bound to
        # (None when the SIGCHLD fast path is unavailable, e.g. under
        # uvloop). ``_reaper_task`` is the periodic asyncio.Task that
        # always runs as the universal fallback. They are not mutually
        # exclusive on purpose: even when SIGCHLD works, the periodic task
        # serves as a backstop in case a signal is missed during a busy
        # loop iteration.
        self._sigchld_loop: asyncio.AbstractEventLoop | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._reaper_loop: asyncio.AbstractEventLoop | None = None
        self._zombie_reaper_interval: float = _resolve_zombie_reaper_interval()
        logger.info(
            "ProcessRuntime exec concurrency limit = %d "
            "(override via %s; cgroup_cpus=%s, os.cpu_count=%s, "
            "sched_affinity=%s)",
            self._exec_concurrency_limit,
            EXEC_CONCURRENCY_ENV,
            _read_cgroup_cpu_quota(),
            os.cpu_count(),
            len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        )

    @staticmethod
    def _load_policy(policy_path: Path) -> SecurityPolicy:
        with open(policy_path) as f:
            data = yaml.safe_load(f)
        return SecurityPolicy.model_validate(data)

    def _ensure_launcher_dir(self, sandbox_id: str) -> Path:
        """Create a per-sandbox host directory holding the launcher scripts.

        The directory is reused across all ``exec`` calls for the sandbox so we
        do not pay the ``tempfile.TemporaryDirectory`` + file-write cost on the
        hot path. The directory is removed in :meth:`cleanup`.
        """
        existing = self._launcher_dirs.get(sandbox_id)
        if existing is not None and existing.exists():
            return existing

        sandbox_root = self._sandbox_root()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        launcher_dir = Path(tempfile.mkdtemp(
            prefix=f"{sandbox_id}-launcher-",
            dir=sandbox_root,
        ))
        launcher_dst = launcher_dir / "landlock-launcher.py"
        daemon_dst = launcher_dir / "sandbox-daemon.py"
        launcher_dst.write_bytes(_LANDLOCK_LAUNCHER_BYTES)
        daemon_dst.write_bytes(_SANDBOX_DAEMON_BYTES)
        os.chmod(launcher_dst, 0o644)
        os.chmod(daemon_dst, 0o644)
        self._launcher_dirs[sandbox_id] = launcher_dir
        return launcher_dir

    def _ensure_control_dir(self, sandbox_id: str) -> Path:
        """Return the per-sandbox host directory holding the control socket.

        The directory is restricted to mode 0700 owned by the box-server
        process; the listener socket is created here, but the directory is
        **never** bind-mounted into the sandbox - box-server keeps exclusive
        filesystem access to the IPC endpoint.
        """
        existing = self._control_dirs.get(sandbox_id)
        if existing is not None and existing.exists():
            return existing

        sandbox_root = self._sandbox_root()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        control_dir = Path(tempfile.mkdtemp(
            prefix=f"{sandbox_id}-control-",
            dir=sandbox_root,
        ))
        try:
            os.chmod(control_dir, 0o700)
        except OSError:
            logger.debug(
                "Failed to chmod control dir %s; relying on default mode",
                control_dir,
                exc_info=True,
            )
        self._control_dirs[sandbox_id] = control_dir
        return control_dir

    def _background_jobs_for_sandbox(self, sandbox_id: str) -> dict[str, BackgroundJob]:
        return self._background_processes.setdefault(sandbox_id, {})

    def _apply_bg_status_response(
        self,
        job: BackgroundJob,
        response: dict[str, Any],
    ) -> None:
        if not response.get("ok"):
            return
        pid = response.get("pid")
        if isinstance(pid, int):
            job.pid = pid
        running = response.get("running")
        if not isinstance(running, bool):
            return
        if running:
            job.exit_code = None
            job.finished_at = None
            return
        exit_code = response.get("exit_code")
        if exit_code is None or isinstance(exit_code, int):
            job.exit_code = exit_code
        if job.finished_at is None:
            job.finished_at = datetime.now(timezone.utc)

    def _sync_background_job_blocking(
        self,
        sandbox_id: str,
        job: BackgroundJob,
    ) -> None:
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return
        try:
            response = self._bg_status_via_daemon_blocking(
                _DaemonBgStatusCall(socket_path=socket_path, job_id=job.job_id),
            )
        except (OSError, ValueError):
            # ``OSError`` already covers ``ConnectionError`` and ``socket.timeout`` (G.ERR.09).
            return
        self._apply_bg_status_response(job, response)

    def _job_status(self, job: BackgroundJob) -> BackgroundJobStatus:
        running = job.exit_code is None
        return BackgroundJobStatus(
            job_id=job.job_id,
            sandbox_id=job.sandbox_id,
            command=list(job.command),
            pid=job.pid,
            running=running,
            exit_code=job.exit_code,
            started_at=job.started_at,
            finished_at=job.finished_at,
            stdout=job.stdout,
            stderr=job.stderr,
            workdir=job.workdir,
        )

    def _job_summary(self, job: BackgroundJob) -> BackgroundJobSummary:
        running = job.exit_code is None
        return BackgroundJobSummary(
            job_id=job.job_id,
            pid=job.pid,
            command=list(job.command),
            running=running,
            exit_code=job.exit_code,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    def _get_background_job_record(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> BackgroundJob:
        job = self._background_jobs_for_sandbox(sandbox_id).get(job_id)
        if job is None:
            raise BackgroundJobNotFoundError(
                f"Background job '{job_id}' not found in sandbox '{sandbox_id}'",
            )
        return job

    def _control_socket_host_path(self, sandbox_id: str) -> Path | None:
        control_dir = self._control_dirs.get(sandbox_id)
        if control_dir is None:
            return None
        return control_dir / SANDBOX_CONTROL_SOCKET_NAME

    def _create_daemon_listener(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> socket.socket:
        """Create the per-sandbox host listener socket the daemon will adopt.

        The fd is later handed to bubblewrap via
        ``subprocess.Popen(pass_fds=[fd])``. Bubblewrap's user command
        path (PID 2 inside the new pid namespace) never closes arbitrary
        inherited fds, so the listener flows naturally through bwrap →
        launcher → daemon. We mark the fd non-CLOEXEC so it survives every
        ``execve`` along that chain.
        """
        control_dir = self._ensure_control_dir(sandbox_id)
        socket_path = control_dir / SANDBOX_CONTROL_SOCKET_NAME
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
            try:
                # ``0o600`` keeps the listener reachable only by the
                # box-server uid (and root); daemon connects come from
                # outside the sandbox so the sandbox uid does not need
                # access to this path.
                os.chmod(socket_path, 0o600)
            except OSError:
                logger.debug(
                    "Failed to chmod listener socket %s; relying on umask",
                    socket_path,
                    exc_info=True,
                )
            # Restrict ownership tightly: the listener fd is what the daemon
            # uses, so file ownership only protects against in-host meddling.
            try:
                os.chown(socket_path, os.geteuid(), os.getegid())
            except OSError:
                pass
            listener.listen(64)
            # ``pass_fds`` already clears CLOEXEC on the listed fd before
            # exec'ing bwrap; calling ``set_inheritable`` makes the intent
            # explicit and survives even if the caller forgets to thread
            # the fd through ``pass_fds``.
            os.set_inheritable(listener.fileno(), True)
        except Exception:
            try:
                listener.close()
            except OSError:
                pass
            raise
        # Track per-sandbox so the policy-aware uid (used for chown of
        # other policy-managed paths) is consistent with what created the
        # socket; we do not actually need ``policy`` further here.
        _ = policy
        return listener

    def _ensure_seccomp_bpf(self, sandbox_id: str, policy: SecurityPolicy) -> bytes:
        bpf = self._seccomp_bpf.get(sandbox_id)
        if bpf is not None:
            return bpf
        bpf = build_seccomp_filter(policy.syscall)
        self._seccomp_bpf[sandbox_id] = bpf
        return bpf

    def _ensure_landlock_payload(self, sandbox_id: str, policy: SecurityPolicy) -> str:
        payload = self._landlock_payloads.get(sandbox_id)
        if payload is not None:
            return payload
        payload = encode_landlock_payload(policy)
        self._landlock_payloads[sandbox_id] = payload
        return payload

    @staticmethod
    def _open_seccomp_fd_from_bytes(bpf: bytes) -> int:
        """Create an anonymous memfd preloaded with ``bpf`` for bwrap.

        Works from cached BPF bytes so the BPF program does not have to be
        re-assembled for every exec.
        """
        if not hasattr(os, "memfd_create"):
            raise RuntimeError("os.memfd_create is required for seccomp filters")
        fd = os.memfd_create("jiuwenbox-seccomp", getattr(os, "MFD_CLOEXEC", 0x0001))
        try:
            offset = 0
            while offset < len(bpf):
                offset += os.write(fd, bpf[offset:])
            os.lseek(fd, 0, os.SEEK_SET)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _build_sandbox_bwrap_args(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
        command: list[str],
        *,
        workdir: str | None,
        sandbox_env: dict[str, str] | None,
        netns_attached: bool,
        seccomp_fd: int | None,
        listener_fd: int | None = None,
    ) -> list[str]:
        """Build a ready-to-spawn bwrap argv vector for ``command``.

        Caches populated during :meth:`create` (policy binds, launcher
        scripts, seccomp BPF, landlock payload) keep the per-call cost low.
        """
        config = BwrapConfig.from_policy(policy, list(command))
        process_uid = config.uid
        process_gid = config.gid
        if sandbox_env:
            config.env.update(sandbox_env)
        if workdir:
            config.workdir = workdir

        # When the runtime has joined the bwrap process to a pre-configured
        # named netns via ``ip netns exec``, bwrap must not unshare it again
        # otherwise the carefully prepared firewall rules become invisible.
        if policy.network.mode == NetworkMode.ISOLATED and netns_attached:
            config.unshare_net = False

        for entry in self._policy_binds.get(sandbox_id, []):
            config.rw_binds.append((entry["host_path"], entry["sandbox_path"]))

        launcher_dir = self._launcher_dirs.get(sandbox_id)
        landlock_enabled = policy.landlock.compatibility != "disabled"

        if launcher_dir is not None:
            daemon_path = launcher_dir / "sandbox-daemon.py"
            config.ro_binds.append(
                (str(daemon_path), SANDBOX_DAEMON_SANDBOX_PATH),
            )

        if listener_fd is not None:
            # The listener fd is delivered into the sandbox purely through
            # natural fd inheritance: box-server creates the listener with
            # CLOEXEC cleared, hands the fd to bubblewrap via
            # ``subprocess.Popen(pass_fds=[fd])``, and bubblewrap's user
            # command path (PID 2 of the new pid namespace) never calls
            # ``fdwalk`` to close arbitrary inherited descriptors, so the fd
            # survives the bwrap → launcher → daemon ``execve`` chain. The
            # daemon adopts it via ``LISTENER_FD_ENV`` and never reaches
            # into the filesystem for the IPC endpoint, which means
            # Landlock can stay locked down.
            config.env[LISTENER_FD_ENV] = str(listener_fd)

        if launcher_dir is not None and landlock_enabled:
            launcher_path = launcher_dir / "landlock-launcher.py"
            # ``SANDBOX_RESERVED_DIR`` is created as a fresh tmpfs by bwrap
            # so we can ``--ro-bind`` the trusted scripts on top of it.
            # ``PolicyEngine`` rejects any user policy that references this
            # subtree, which prevents the launcher / daemon mount from
            # colliding with a user-supplied ``bind_mount`` or being
            # accidentally exposed to user code via the Landlock allowlist.
            config.add_dir_mount(SANDBOX_RESERVED_DIR)
            config.ro_binds.append((str(launcher_path), SANDBOX_LAUNCHER_PATH))
            payload = self._ensure_landlock_payload(sandbox_id, policy)
            # ``-S`` skips ``import site`` so the launcher does not pay for
            # building the global ``sys.path`` table; the launcher is
            # stdlib-only.
            #
            # For the per-sandbox daemon we use the launcher's
            # ``--daemon`` mode: the launcher pre-reads the daemon
            # script, applies Landlock, and then runs the daemon code in
            # the same Python process via ``compile``/``exec``. There is
            # no second ``execve`` after Landlock is locked in, which
            # means the daemon (and every ``fork+exec`` it does for IPC
            # requests) inherits Landlock without the daemon script's
            # on-disk path needing to remain reachable. That lets us
            # keep ``/run`` outside the Landlock allowlist so user code
            # cannot read the launcher / daemon scripts at runtime.
            config.command = [
                PYTHON_EXECUTABLE,
                "-S",
                SANDBOX_LAUNCHER_PATH,
                payload,
                "--daemon",
                SANDBOX_DAEMON_SANDBOX_PATH,
            ]

        config.seccomp_fd = seccomp_fd
        if self._needs_privdrop_for_process_identity(
            process_uid,
            use_user_namespace=policy.namespace.user,
        ):
            if process_uid is None or process_gid is None:
                raise RuntimeError(
                    "run_as_user/run_as_group could not be resolved for privilege drop",
                )
            config.command = self._wrap_command_with_privdrop(
                config.command,
                process_uid,
                process_gid,
            )
            logger.info(
                "Dropping sandbox command to host uid %d gid %d via Python",
                process_uid,
                process_gid,
            )
        return config.to_args()

    def _get_netns_name(self, sandbox_id: str) -> str:
        return self._netns_names.setdefault(
            sandbox_id,
            network_module.netns_name_for_sandbox(sandbox_id),
        )

    def _ensure_named_netns(self, sandbox_id: str, policy: SecurityPolicy) -> str | None:
        if policy.network.mode != NetworkMode.ISOLATED:
            return None

        old_uplink = self._uplink_handles.pop(sandbox_id, None)
        if old_uplink is not None:
            try:
                network_module.teardown_network_uplink(old_uplink)
            except Exception:
                logger.warning(
                    "Failed to teardown previous uplink for sandbox %s before rebuild",
                    sandbox_id,
                    exc_info=True,
                )

        namespace = self._get_netns_name(sandbox_id)
        if network_module.namespace_exists(namespace):
            network_module.delete_named_namespace(namespace)

        network_module.create_named_namespace(namespace)
        uplink_handle: network_module.UplinkHandle | None = None
        try:
            uplink_handle = network_module.setup_network_uplink(
                namespace,
                sandbox_id,
                policy.network.uplink,
                management_ports=self._server_protect_ports(),
            )
            network_module.setup_network_isolation(policy.network, namespace=namespace)
            self._uplink_handles[sandbox_id] = uplink_handle
        except Exception:
            if uplink_handle is not None:
                network_module.teardown_network_uplink(uplink_handle)
            try:
                network_module.delete_named_namespace(namespace)
            except Exception:
                logger.warning(
                    "Failed to rollback network namespace %s after setup error",
                    namespace,
                    exc_info=True,
                )
            raise

        return namespace

    @staticmethod
    def _directory_spec(directory: object) -> tuple[str, str | None]:
        if isinstance(directory, str):
            return directory, None
        return getattr(directory, "path"), getattr(directory, "permissions", None)

    @staticmethod
    def _file_spec(file: object) -> tuple[str, str | None]:
        if isinstance(file, str):
            return file, None
        return getattr(file, "path"), getattr(file, "permissions", None)

    @staticmethod
    def _host_entry_name(sandbox_path: str) -> str:
        encoded = base64.urlsafe_b64encode(sandbox_path.encode()).decode()
        return encoded.rstrip("=")

    @staticmethod
    def _sandbox_root() -> Path:
        return SANDBOX_WORKSPACE

    @staticmethod
    def _resolve_backing_identity(policy: SecurityPolicy) -> tuple[int, int]:
        if policy.namespace.user:
            # For unprivileged user namespaces, the server uid is mapped to the
            # sandbox uid, so keeping the backing directory owned by the current
            # process is the writable choice. When the server runs as root, bwrap
            # can drop to the requested sandbox uid; root-owned 0755 directories
            # would then reject writes such as uploads under /home.
            if os.geteuid() != 0:
                return os.getuid(), os.getgid()

        try:
            uid = pwd.getpwnam(policy.process.run_as_user).pw_uid
        except KeyError:
            uid = 65534
        try:
            gid = grp.getgrnam(policy.process.run_as_group).gr_gid
        except KeyError:
            gid = 65534
        return uid, gid

    @staticmethod
    def _resolve_process_uid(policy: SecurityPolicy) -> int:
        try:
            return pwd.getpwnam(policy.process.run_as_user).pw_uid
        except KeyError:
            return 65534

    @classmethod
    def _host_firewall_uids(cls, policy: SecurityPolicy) -> list[int]:
        uids = [cls._resolve_process_uid(policy)]
        current_uid = os.geteuid()
        if policy.namespace.user and current_uid == 0 and current_uid not in uids:
            uids.append(current_uid)
        return uids

    @staticmethod
    def _server_protect_ports() -> list[int]:
        raw_ports = os.environ.get(SERVER_PROTECT_PORTS_ENV)
        if raw_ports is None:
            # No explicit override: track whatever port uvicorn actually
            # bound. ``launcher.main`` always writes the resolved listen
            # URI back to ``$JIUWENBOX_LISTEN`` (see launcher.py:234) so
            # this stays consistent even when the operator passed
            # ``--listen http://0.0.0.0:18321``.
            return list(_derive_protect_ports_from_listen())
        if not raw_ports.strip():
            return []

        ports: list[int] = []
        for raw_port in raw_ports.split(","):
            raw_port = raw_port.strip()
            if not raw_port:
                continue
            try:
                port = int(raw_port)
            except ValueError:
                logger.warning(
                    "Ignoring invalid %s entry: %s",
                    SERVER_PROTECT_PORTS_ENV,
                    raw_port,
                )
                continue
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
            else:
                logger.warning(
                    "Ignoring out-of-range %s entry: %s",
                    SERVER_PROTECT_PORTS_ENV,
                    raw_port,
                )
        return ports

    @staticmethod
    def _host_firewall_insert_args(uid: int, port: int) -> list[str]:
        return [
            "-I", "OUTPUT", "1",
            "-p", "tcp",
            "-m", "owner", "--uid-owner", str(uid),
            "--dport", str(port),
            "-j", "REJECT",
        ]

    @staticmethod
    def _host_firewall_delete_args(uid: int, port: int) -> list[str]:
        return [
            "-D", "OUTPUT",
            "-p", "tcp",
            "-m", "owner", "--uid-owner", str(uid),
            "--dport", str(port),
            "-j", "REJECT",
        ]

    def _install_host_firewall_rule(self, uid: int, port: int) -> tuple[int, int]:
        key = (uid, port)
        current_count = self._host_firewall_refcounts.get(key, 0)
        if current_count == 0:
            network_module.run_iptables(
                self._host_firewall_insert_args(uid, port),
                ip_version=4,
            )
            logger.info(
                "Blocked sandbox uid %d from connecting to box-server port %d",
                uid,
                port,
            )
        self._host_firewall_refcounts[key] = current_count + 1
        return key

    def _remove_host_firewall_rule(self, uid: int, port: int) -> None:
        key = (uid, port)
        current_count = self._host_firewall_refcounts.get(key, 0)
        if current_count <= 1:
            self._host_firewall_refcounts.pop(key, None)
            try:
                network_module.run_iptables(
                    self._host_firewall_delete_args(uid, port),
                    ip_version=4,
                )
            except Exception:
                logger.warning(
                    "Failed to remove sandbox uid %d box-server port %d block rule",
                    uid,
                    port,
                    exc_info=True,
                )
            return
        self._host_firewall_refcounts[key] = current_count - 1

    def _install_sandbox_host_firewall_rules(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> None:
        if policy.network.mode != NetworkMode.HOST:
            return

        if not network_module.policy_has_network_rules(policy.network):
            # The operator declared ``mode: host`` and did not list any
            # egress/ingress entries, so they have explicitly opted out of
            # all network restrictions. Skip the implicit iptables-based
            # box-server self-protection in that case: it lets jiuwenbox
            # run on hosts that do not ship ``iptables`` / ``iptables-nft``
            # at all, matching the behavior promised by the policy.
            logger.debug(
                "Skipping host firewall install for sandbox %s "
                "(policy declares no egress/ingress rules)",
                sandbox_id,
            )
            return

        ports = self._server_protect_ports()
        if not ports:
            # ``JIUWENBOX_SERVER_PROTECT_PORTS=""`` lets operators turn the
            # default off explicitly. Debug-only breadcrumb (info-level would
            # spam production logs under heavy sandbox churn).
            logger.debug(
                "Skipping host firewall install for sandbox %s "
                "(%s set to empty; mode=host has no host-level uid blocks)",
                sandbox_id,
                SERVER_PROTECT_PORTS_ENV,
            )
            return

        installed: list[tuple[int, int]] = []
        try:
            for uid in self._host_firewall_uids(policy):
                for port in ports:
                    installed.append(self._install_host_firewall_rule(uid, port))
        except network_module.NetworkSetupError as exc:
            # Most common cause: jiuwenbox runs unprivileged so ``iptables``
            # returns ``Permission denied (you must be root)``. Roll back any
            # partial install but DO NOT raise - the deployment explicitly
            # chose to run without root, and the alternative (failing sandbox
            # creation) would make even fully-local-only policies unusable.
            for installed_uid, installed_port in reversed(installed):
                self._remove_host_firewall_rule(installed_uid, installed_port)
            logger.warning(
                "Skipping host firewall install for sandbox %s; the "
                "box-server's port(s) %s remain reachable from the sandbox "
                "host netns (iptables unavailable: %s). Re-run jiuwenbox "
                "with sufficient privileges or set %s='' to silence this "
                "warning.",
                sandbox_id,
                ports,
                exc,
                SERVER_PROTECT_PORTS_ENV,
            )
            return
        except Exception:
            for installed_uid, installed_port in reversed(installed):
                self._remove_host_firewall_rule(installed_uid, installed_port)
            raise
        self._sandbox_host_firewall_rules[sandbox_id] = installed

    def _remove_sandbox_host_firewall_rules(self, sandbox_id: str) -> None:
        for uid, port in reversed(self._sandbox_host_firewall_rules.pop(sandbox_id, [])):
            self._remove_host_firewall_rule(uid, port)

    @staticmethod
    def _apply_path_ownership(path: Path, uid: int, gid: int) -> bool:
        try:
            os.chown(path, uid, gid)
            return True
        except PermissionError:
            logger.warning(
                "Failed to chown policy path %s to %d:%d; keeping current owner",
                path,
                uid,
                gid,
            )
            return False

    @staticmethod
    def _apply_path_permissions(path: Path, permissions: str | None) -> None:
        if permissions is None:
            return
        os.chmod(path, int(permissions, 8))

    @staticmethod
    def _needs_userns_write_fallback(
        policy: SecurityPolicy,
        uid: int,
        permissions: str | None,
    ) -> bool:
        if os.geteuid() != 0 or not policy.namespace.user or uid == 0:
            return False
        if permissions is None:
            return True
        mode = int(permissions, 8)
        return (
            bool(mode & 0o200)
            and (mode & 0o005) == 0o005
            and not bool(mode & 0o002)
        )

    @staticmethod
    def _needs_userns_owner_access_fallback(
        policy: SecurityPolicy,
        uid: int,
    ) -> bool:
        return os.geteuid() == 0 and policy.namespace.user and uid != 0

    @staticmethod
    def _apply_userns_write_fallback(path: Path) -> None:
        mode = path.stat().st_mode & 0o777
        fallback_mode = mode | 0o003
        if fallback_mode == mode:
            return
        logger.warning(
            "Relaxing policy path %s permissions from %s to %s because "
            "root-run user namespaces cannot map the sandbox uid onto the "
            "bind-mounted backing path owner",
            path,
            oct(mode),
            oct(fallback_mode),
        )
        os.chmod(path, fallback_mode)

    @staticmethod
    def _apply_userns_file_access_fallback(path: Path) -> None:
        mode = path.stat().st_mode & 0o777
        owner_bits = (mode & 0o700) >> 6
        fallback_mode = mode | owner_bits
        if fallback_mode == mode:
            return
        logger.warning(
            "Relaxing policy file %s permissions from %s to %s because "
            "root-run user namespaces cannot preserve owner-only file access",
            path,
            oct(mode),
            oct(fallback_mode),
        )
        os.chmod(path, fallback_mode)

    @staticmethod
    def _ensure_writable_when_chown_unavailable(path: Path, owner_applied: bool) -> None:
        if owner_applied:
            return

        if path.stat().st_uid == os.getuid():
            return

        mode = path.stat().st_mode & 0o777
        if mode & 0o005 and not mode & 0o002:
            fallback_mode = mode | 0o002
            logger.warning(
                "Relaxing policy path %s permissions from %s to %s because chown failed",
                path,
                oct(mode),
                oct(fallback_mode),
            )
            os.chmod(path, fallback_mode)

    @staticmethod
    def _needs_privdrop_for_process_identity(
        uid: int | None,
        *,
        use_user_namespace: bool,
    ) -> bool:
        """Whether to drop to the host uid before running the sandbox command.

        When ``namespace.user`` is enabled, bubblewrap already applies
        ``--uid`` / ``--gid`` together with ``--unshare-user``.

        Privilege drop is only needed when the server runs as root *and* the
        sandbox reuses the host user namespace (``namespace.user: false``).
        """
        if use_user_namespace:
            return False
        return os.geteuid() == 0 and uid is not None and uid != 0

    @staticmethod
    def _wrap_command_with_privdrop(
        command: list[str],
        uid: int,
        gid: int,
    ) -> list[str]:
        """Wrap ``command`` in a short Python helper that drops uid/gid.

        Uses stdlib ``os.setuid`` / ``os.setgid`` so jiuwenbox does not depend
        on the external ``setpriv(1)`` binary from util-linux.
        """
        encoded_command = json.dumps(command)
        script = "; ".join([
            "import json, os",
            f"uid, gid = {uid}, {gid}",
            f"cmd = json.loads({encoded_command!r})",
            "exec('try:\\n os.setgroups([])\\nexcept OSError:\\n pass', globals())",
            "os.setgid(gid)",
            "os.setuid(uid)",
            "os.execvp(cmd[0], cmd)",
        ])
        return [PYTHON_EXECUTABLE, "-S", "-c", script]

    def _ensure_policy_directories(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> list[dict[str, str]]:
        directories = policy.filesystem_policy.directories
        if not directories:
            return []

        sandbox_root = self._sandbox_root()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        directory_root = self._directory_roots.get(sandbox_id)
        if directory_root is None:
            directory_root = Path(tempfile.mkdtemp(
                prefix=f"{sandbox_id}-dirs-",
                dir=sandbox_root,
            ))
            self._directory_roots[sandbox_id] = directory_root
        else:
            directory_root.mkdir(parents=True, exist_ok=True)

        uid, gid = self._resolve_backing_identity(policy)
        binds: list[dict[str, str]] = []
        for directory in directories:
            sandbox_path, permissions = self._directory_spec(directory)
            host_path = directory_root / self._host_entry_name(sandbox_path)
            host_path.mkdir(parents=True, exist_ok=True)
            owner_applied = self._apply_path_ownership(host_path, uid, gid)
            self._apply_path_permissions(host_path, permissions)
            if self._needs_userns_write_fallback(policy, uid, permissions):
                self._apply_userns_write_fallback(host_path)
            self._ensure_writable_when_chown_unavailable(host_path, owner_applied)
            binds.append({
                "host_path": str(host_path),
                "sandbox_path": sandbox_path,
            })
        return binds

    def _ensure_policy_files(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> list[dict[str, str]]:
        files = policy.filesystem_policy.files
        if not files:
            return []

        sandbox_root = self._sandbox_root()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        file_root = self._file_roots.get(sandbox_id)
        if file_root is None:
            file_root = Path(tempfile.mkdtemp(
                prefix=f"{sandbox_id}-files-",
                dir=sandbox_root,
            ))
            self._file_roots[sandbox_id] = file_root
        else:
            file_root.mkdir(parents=True, exist_ok=True)

        uid, gid = self._resolve_backing_identity(policy)
        binds: list[dict[str, str]] = []
        for file in files:
            sandbox_path, permissions = self._file_spec(file)
            host_path = file_root / self._host_entry_name(sandbox_path)
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.touch(exist_ok=True)
            owner_applied = self._apply_path_ownership(host_path, uid, gid)
            self._apply_path_permissions(host_path, permissions)
            if self._needs_userns_owner_access_fallback(policy, uid):
                self._apply_userns_file_access_fallback(host_path)
            if self._needs_userns_write_fallback(policy, uid, permissions):
                self._apply_userns_write_fallback(host_path)
            self._ensure_writable_when_chown_unavailable(host_path, owner_applied)
            binds.append({
                "host_path": str(host_path),
                "sandbox_path": sandbox_path,
            })
        return binds

    @staticmethod
    def _wrap_command_in_namespace(command: list[str], namespace: str | None) -> list[str]:
        if not namespace:
            return command
        return [network_module.IP_BINARY, "netns", "exec", namespace, *command]

    def _policy_for_sandbox(self, sandbox_id: str, policy_path: Path) -> SecurityPolicy:
        policy = self._runtime_policies.get(sandbox_id)
        if policy is None:
            policy = self._load_policy(policy_path)
            self._runtime_policies[sandbox_id] = policy
            self._network_modes[sandbox_id] = policy.network.mode
        return policy

    def _policy_binds_for_sandbox(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> list[dict[str, str]]:
        policy_binds = self._policy_binds.get(sandbox_id)
        if policy_binds is None:
            directory_binds = self._ensure_policy_directories(sandbox_id, policy)
            file_binds = self._ensure_policy_files(sandbox_id, policy)
            policy_binds = [*directory_binds, *file_binds]
            self._policy_binds[sandbox_id] = policy_binds
        return policy_binds

    def _reap_background_processes(self, sandbox_id: str) -> None:
        for job in self._background_jobs_for_sandbox(sandbox_id).values():
            self._sync_background_job_blocking(sandbox_id, job)

    # ------------------------------------------------------------------
    # SIGCHLD-driven zombie reaper.
    #
    # The reaper has two responsibilities:
    #   1. Poll every tracked ``Popen`` (sandbox daemon monitor) so its exit
    #      status is recorded promptly.
    #   2. Drain any residual zombie via ``os.waitpid(-1, WNOHANG)``.
    #      are bwrap-internal helpers (most importantly the ``--unshare-user``
    #      userns helper that is cloned with ``CLONE_PARENT`` and is therefore
    #      a direct child of box-server, *not* a child of the bwrap monitor)
    #      plus anything else that landed on us via ``PR_SET_CHILD_SUBREAPER``.
    #      We then look up the pid in our tracked-Popen map and, if found,
    #      back-fill ``proc.returncode`` so a later ``Popen.poll()``/``wait()``
    #      returns the actual exit status instead of taking the ``ECHILD``
    #      branch (which silently sets ``returncode = 0``).
    # ------------------------------------------------------------------
    def _iter_tracked_popens(self) -> list[subprocess.Popen]:
        """Snapshot of sandbox lifecycle ``Popen`` objects tracked by this runtime."""
        return list(self._processes.values())

    def _reap_zombies(self) -> None:
        """Reap every zombie child (tracked + orphan).

        Safe to call from the asyncio event-loop thread (which is where
        ``loop.add_signal_handler`` delivers SIGCHLD callbacks); not safe
        from a signal context outside asyncio because it allocates and
        logs, which Python signal handlers must avoid.
        """
        tracked_popens = self._iter_tracked_popens()
        tracked_pids: dict[int, subprocess.Popen] = {}
        for proc in tracked_popens:
            pid = proc.pid
            if pid is not None:
                tracked_pids[pid] = proc
            if proc.returncode is None:
                try:
                    proc.poll()
                except OSError:
                    logger.debug(
                        "Popen.poll() raised during reap", exc_info=True,
                    )

        reaped_tracked: list[tuple[int, int]] = []
        reaped_orphans: list[int] = []
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            except OSError:
                logger.debug("waitpid(-1) raised during reap", exc_info=True)
                break
            if pid == 0:
                break
            proc = tracked_pids.get(pid)
            if proc is not None:
                # Race: ``Popen.poll()`` above did not catch this child
                # (it was still running then; it exited between the poll
                # and our ``waitpid(-1)``). Back-fill the returncode so
                # downstream ``stop()``/``is_running()`` see the real exit
                # status.
                if proc.returncode is None:
                    try:
                        proc.returncode = os.waitstatus_to_exitcode(status)
                    except ValueError:
                        # Stopped/continued status; ignore so a later
                        # ``Popen.wait()`` can resync properly.
                        logger.debug(
                            "waitstatus_to_exitcode rejected status %r for pid %d",
                            status,
                            pid,
                        )
                reaped_tracked.append((pid, status))
            else:
                reaped_orphans.append(pid)

        if reaped_orphans or reaped_tracked:
            logger.debug(
                "zombie reaper: tracked=%d orphans=%d",
                len(reaped_tracked),
                len(reaped_orphans),
            )

    def register_zombie_reaper(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> bool:
        """Install the zombie reaper on the given event loop.

        Tries the SIGCHLD ``add_signal_handler`` fast path first; on
        failure (most notably under uvloop, which uvicorn auto-selects
        when the package is installed) silently falls back to the
        periodic-Task reaper. Always starts the periodic Task as a
        backstop so a missed signal cannot strand zombies indefinitely.

        Returns ``True`` if *any* reaping channel is active. Idempotent
        on the same loop; replaces an existing registration when called
        with a different loop (only realistic in unit tests).
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning(
                    "register_zombie_reaper called without a running loop;"
                    " zombie reaper not installed",
                )
                return False
        if self._reaper_loop is not None and self._reaper_loop is not loop:
            # Different loop -> tear down whatever we had on the old one
            # before re-installing here. Practically only fires in tests
            # that recreate event loops between cases.
            self.unregister_zombie_reaper(self._reaper_loop)

        sigchld_ok = self._install_sigchld_handler(loop)
        task_ok = self._install_periodic_reaper(loop)

        if not sigchld_ok and not task_ok:
            return False

        # Drain anything that piled up before the reaper was wired in.
        # Safe to call even when only one channel was installed.
        self._reap_zombies()
        logger.info(
            "zombie reaper active on loop %s (pid=%d, sigchld=%s, "
            "periodic=%.1fs)",
            id(loop),
            os.getpid(),
            "on" if sigchld_ok else "off",
            self._zombie_reaper_interval if task_ok else 0.0,
        )
        return True

    def unregister_zombie_reaper(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Tear down everything ``register_zombie_reaper`` set up.

        Runs one final synchronous reap before returning so the caller
        (lifespan shutdown) does not have to schedule another loop tick
        for the periodic Task's ``CancelledError`` final-pass to fire --
        by the time we are unregistered the event loop is typically
        winding down and the cancelled coroutine may not get another
        slot. A sync sweep here makes shutdown cleanup deterministic.
        """
        target = loop or self._reaper_loop or self._sigchld_loop
        if target is None:
            return
        if self._sigchld_loop is target:
            self._uninstall_sigchld_handler(target)
            self._sigchld_loop = None
        if self._reaper_loop is target:
            self._cancel_periodic_reaper()
            self._reaper_loop = None
        try:
            self._reap_zombies()
        except Exception:  # noqa: BLE001
            logger.debug(
                "final _reap_zombies pass raised during unregister",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # SIGCHLD fast path.
    # ------------------------------------------------------------------
    def _install_sigchld_handler(self, loop: asyncio.AbstractEventLoop) -> bool:
        if self._sigchld_loop is loop:
            return True
        try:
            loop.add_signal_handler(signal.SIGCHLD, self._reap_zombies)
        except (ValueError, RuntimeError) as exc:
            # ``RuntimeError`` covers uvloop's "SIGCHLD reserved" path
            # *and* ``NotImplementedError`` (the latter is a RuntimeError
            # subclass, raised by non-Unix selector loops). ``ValueError``
            # covers the "signal handlers must be set from the main
            # thread of the main interpreter" case. All of them are
            # benign here -- the periodic task picks up the slack.
            logger.info(
                "SIGCHLD fast-path reaper unavailable (%s); relying on "
                "periodic poll only",
                exc,
            )
            return False
        self._sigchld_loop = loop
        return True

    def _uninstall_sigchld_handler(
        self, loop: asyncio.AbstractEventLoop,
    ) -> None:
        try:
            loop.remove_signal_handler(signal.SIGCHLD)
        except (ValueError, RuntimeError) as exc:
            # All benign at unregister time:
            # - ``ValueError`` -> handler was never installed for that
            #   sig on this loop (already-cleaned state);
            # - ``RuntimeError`` (incl. ``NotImplementedError``) ->
            #   non-Unix loop or already-closed loop during shutdown.
            # Either way, nothing left to clean up; log at DEBUG so the
            # shutdown path stays quiet on the success case.
            logger.debug("remove_signal_handler(SIGCHLD) raised: %s", exc)

    # ------------------------------------------------------------------
    # Periodic asyncio.Task fallback / backstop.
    # ------------------------------------------------------------------
    async def _periodic_reaper_loop(self, interval: float) -> None:
        """Run ``_reap_zombies`` every ``interval`` seconds until cancelled.

        A single iteration's exception is logged and swallowed: the
        reaper is best-effort plumbing and a transient OSError (e.g.
        ``EINTR`` racing with shutdown) must not silently kill the loop
        and resurrect the leak we are trying to plug.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    self._reap_zombies()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "periodic zombie reaper iteration raised",
                    )
        except asyncio.CancelledError:
            # Drain one last time on shutdown so anything that died
            # between the previous tick and now is still cleaned up
            # before the event loop closes.
            try:
                self._reap_zombies()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "periodic zombie reaper final pass raised",
                )
            raise

    def _install_periodic_reaper(
        self, loop: asyncio.AbstractEventLoop,
    ) -> bool:
        if self._reaper_task is not None and not self._reaper_task.done():
            if self._reaper_loop is loop:
                return True
            # Different loop; cancel old, create new below.
            self._cancel_periodic_reaper()
        try:
            self._reaper_task = loop.create_task(
                self._periodic_reaper_loop(self._zombie_reaper_interval),
                name="jiuwenbox-zombie-reaper",
            )
        except RuntimeError as exc:
            logger.warning(
                "could not start periodic zombie reaper task: %s", exc,
            )
            self._reaper_task = None
            return False
        self._reaper_loop = loop
        return True

    def _cancel_periodic_reaper(self) -> None:
        task = self._reaper_task
        if task is None:
            return
        self._reaper_task = None
        if task.done():
            return
        task.cancel()

    async def _stop_background_processes(self, sandbox_id: str, timeout: float = 5.0) -> None:
        jobs = self._background_processes.pop(sandbox_id, {})
        if not jobs:
            return

        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return

        loop = asyncio.get_running_loop()
        running_jobs = [
            job for job in jobs.values() if job.exit_code is None
        ]
        for job in running_jobs:
            try:
                await loop.run_in_executor(
                    None,
                    self._bg_kill_via_daemon_blocking,
                    _DaemonBgKillCall(
                        socket_path=socket_path,
                        job_id=job.job_id,
                        signum=signal.SIGTERM,
                    ),
                )
            except (OSError, ValueError):
                # ``OSError`` already covers ``ConnectionError`` and ``socket.timeout`` (G.ERR.09).
                continue

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            still_running = False
            for job in running_jobs:
                await loop.run_in_executor(
                    None,
                    self._sync_background_job_blocking,
                    sandbox_id,
                    job,
                )
                if job.exit_code is None:
                    still_running = True
            if not still_running:
                return
            await asyncio.sleep(0.1)

        for job in running_jobs:
            if job.exit_code is not None:
                continue
            try:
                await loop.run_in_executor(
                    None,
                    self._bg_kill_via_daemon_blocking,
                    _DaemonBgKillCall(
                        socket_path=socket_path,
                        job_id=job.job_id,
                        signum=signal.SIGKILL,
                    ),
                )
            except (OSError, ValueError):
                # ``OSError`` already covers ``ConnectionError`` and ``socket.timeout`` (G.ERR.09).
                continue
            await loop.run_in_executor(
                None,
                self._sync_background_job_blocking,
                sandbox_id,
                job,
            )

    async def get_sandbox_ip_address(self, sandbox_id: str) -> str | None:
        """Return the sandbox IPv4 confirmed by the current network setup."""
        mode = self._network_modes.get(sandbox_id)
        if mode == NetworkMode.ISOLATED:
            handle = self._uplink_handles.get(sandbox_id)
            if handle is None:
                return None
            return handle.sandbox_ip or None
        if mode == NetworkMode.HOST:
            return await asyncio.to_thread(network_module.resolve_host_egress_ipv4)
        return None

    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        env: dict[str, str] | None = None,
    ) -> int:
        existing = self._processes.get(sandbox_id)
        if existing is not None:
            if existing.poll() is None:
                raise RuntimeError(f"Sandbox {sandbox_id} already has a running process")
            self._processes.pop(sandbox_id, None)

        policy = self._load_policy(policy_path)
        self._runtime_policies[sandbox_id] = policy
        self._network_modes[sandbox_id] = policy.network.mode
        self._policy_paths[sandbox_id] = Path(policy_path)

        netns_name = self._ensure_named_netns(sandbox_id, policy)
        self._policy_binds_for_sandbox(sandbox_id, policy)
        self._install_sandbox_host_firewall_rules(sandbox_id, policy)

        # Pre-build expensive per-sandbox artifacts so subsequent ``exec``
        # calls only have to allocate a fresh seccomp memfd and assemble the
        # bwrap argv list.
        self._ensure_launcher_dir(sandbox_id)
        self._ensure_landlock_payload(sandbox_id, policy)
        self._daemon_socket_ready[sandbox_id] = False

        try:
            listener = self._create_daemon_listener(sandbox_id, policy)
        except OSError as exc:
            self._cleanup_sandbox_artifacts(sandbox_id, netns_name)
            raise RuntimeError(
                f"Failed to bind daemon control listener for sandbox "
                f"{sandbox_id}: {exc}",
            ) from exc
        listener_fd = listener.fileno()

        seccomp_fd: int | None = None
        try:
            seccomp_fd = self._open_seccomp_fd_from_bytes(
                self._ensure_seccomp_bpf(sandbox_id, policy),
            )
        except Exception:
            logger.warning(
                "Failed to build seccomp filter for sandbox %s; continuing without seccomp",
                sandbox_id,
                exc_info=True,
            )

        bwrap_args = self._build_sandbox_bwrap_args(
            sandbox_id,
            policy,
            list(SANDBOX_DAEMON_COMMAND),
            workdir=None,
            sandbox_env=env,
            netns_attached=netns_name is not None,
            seccomp_fd=seccomp_fd,
            listener_fd=listener_fd,
        )
        daemon_cmd = self._wrap_command_in_namespace(bwrap_args, netns_name)

        process_env = {**os.environ, **(env or {})}
        # ``LISTENER_FD_ENV`` is injected into the sandboxed process via
        # ``BwrapConfig.env`` -> ``bwrap --setenv``; bwrap itself never
        # consumes it, so we no longer set it on the bwrap parent env.

        logger.info("Spawning sandbox daemon for %s", sandbox_id)
        logger.debug("Sandbox daemon bwrap command for %s: %s", sandbox_id, daemon_cmd)

        pass_fd_list: list[int] = [listener_fd]
        if seccomp_fd is not None:
            pass_fd_list.append(seccomp_fd)
        pass_fds = tuple(pass_fd_list)

        # Apply cgroup resource limits *before* spawning bwrap, then migrate
        # the freshly-forked child into the per-sandbox cgroup via
        # ``preexec_fn`` (which runs in the child after ``fork`` but before
        # ``execve``). This is the only race-free way: ``bwrap`` clones
        # immediately on startup to set up its requested namespaces, and any
        # post-spawn ``cgroup.procs`` write from the parent only catches the
        # bwrap host PID -- descendants forked between Popen and the parent
        # attach call remain in the *old* cgroup (cgroup v2 inherits at
        # fork time, not retroactively). Migrating from the child side
        # closes the window entirely.
        #
        # Empty cgroup policies (``policy.cgroup.is_empty()``) are a no-op:
        # ``ProcessRuntime`` does not even probe for a writable cgroup
        # backend, which keeps existing behavior unchanged on hosts without
        # cgroup support.
        cgroup_handle: cgroup_module.CgroupHandle | None = None
        cgroup_procs_paths: list[str] = []
        if not policy.cgroup.is_empty():
            try:
                cgroup_handle = cgroup_module.setup(sandbox_id, policy.cgroup)
            except cgroup_module.CgroupSetupError as exc:
                logger.error(
                    "Failed to apply cgroup limits for sandbox %s: %s",
                    sandbox_id,
                    exc,
                )
                if seccomp_fd is not None:
                    _safe_close_fd(seccomp_fd)
                try:
                    listener.close()
                except OSError:
                    pass
                self._cleanup_sandbox_artifacts(sandbox_id, netns_name)
                raise RuntimeError(
                    f"Failed to apply cgroup limits for sandbox "
                    f"{sandbox_id}: {exc}"
                ) from exc
            cgroup_procs_paths = [
                str(group_dir / "cgroup.procs") for group_dir in cgroup_handle.paths
            ]

        def _preexec_attach_to_cgroup() -> None:
            # Runs in the child after fork, before execve. Writing our own
            # PID into each cgroup.procs migrates this process (and so
            # bwrap, and every descendant bwrap spawns) into the per-sandbox
            # cgroup before any user-controlled code starts. Any OSError
            # here propagates as a nonzero child exit, which the daemon
            # readiness probe surfaces to the caller -- silently leaving
            # the sandbox unconstrained would defeat the policy.
            child_pid = os.getpid()
            for procs_path in cgroup_procs_paths:
                with open(procs_path, "w", encoding="utf-8") as fh:
                    fh.write(str(child_pid))

        preexec = _preexec_attach_to_cgroup if cgroup_procs_paths else None

        # Capture daemon stdout/stderr into an anonymous tempfile so that
        # if the supervisor dies before becoming ready we can include the
        # actual error (Python traceback, bwrap/seccomp/landlock message,
        # missing binary, ...) in the ``RuntimeError`` surfaced upstream.
        # The previous implementation routed both streams to ``DEVNULL``,
        # which left operators with only ``exited with code 1`` to go on.
        # The file is unlinked at create time so it cannot leak onto disk
        # under any host path; the parent always closes its handle after
        # startup verification, and the daemon's own fd is reaped by the
        # kernel when the supervisor exits.
        startup_log_file = tempfile.TemporaryFile(
            prefix=f"jiuwenbox-{sandbox_id}-startup-",
            suffix=".log",
        )
        try:
            proc = subprocess.Popen(
                daemon_cmd,
                stdout=startup_log_file,
                stderr=subprocess.STDOUT,
                env=process_env,
                pass_fds=pass_fds,
                start_new_session=True,
                preexec_fn=preexec,
            )
        except Exception:
            startup_log_file.close()
            if seccomp_fd is not None:
                _safe_close_fd(seccomp_fd)
            try:
                listener.close()
            except OSError:
                pass
            if cgroup_handle is not None:
                # The cgroup was created but never populated; tear it down
                # so we don't leak an empty per-sandbox group on the host.
                try:
                    cgroup_module.teardown(cgroup_handle)
                except Exception:
                    logger.warning(
                        "cgroup teardown failed after Popen error for %s",
                        sandbox_id,
                        exc_info=True,
                    )
            self._cleanup_sandbox_artifacts(sandbox_id, netns_name)
            raise

        if seccomp_fd is not None:
            _safe_close_fd(seccomp_fd)
        # The daemon now owns the listener fd in its process; box-server's
        # copy is no longer useful and must not block accept-loop teardown.
        try:
            listener.close()
        except OSError:
            pass

        self._processes[sandbox_id] = proc
        if cgroup_handle is not None:
            self._cgroup_handles[sandbox_id] = cgroup_handle

        try:
            await self._wait_daemon_ready(
                sandbox_id, proc, netns_name, startup_log_file,
            )
        finally:
            # Parent no longer needs the fd; the daemon keeps writing into
            # its own (kernel-anonymous) copy until it exits.
            try:
                startup_log_file.close()
            except OSError:
                pass
        logger.info("Sandbox daemon started for %s (pid=%d)", sandbox_id, proc.pid)
        return proc.pid

    async def _wait_daemon_ready(
        self,
        sandbox_id: str,
        proc: subprocess.Popen,
        netns_name: str | None,
        startup_log_file: Any | None = None,
    ) -> None:
        """Verify the daemon is alive and mark its IPC channel ready.

        Box-server already created the listener and ``listen()``ed before
        spawning bubblewrap, so the kernel will queue connection attempts
        immediately - there is no socket file we still have to wait for. We
        sleep briefly to ensure the daemon process has had a chance to
        ``accept()`` (so the very first request does not block waiting for
        a worker thread to spin up) and confirm the bwrap parent has not
        already exited with an error.

        ``startup_log_file`` is the tempfile that captured the daemon's
        stdout/stderr; on failure :meth:`_verify_daemon_alive` reads from
        it to enrich the resulting ``RuntimeError`` with the underlying
        diagnostic (Python traceback, bwrap message, ...). It is optional
        so existing call sites and tests that build the runtime by hand
        can still drive the verification step without one.
        """
        await asyncio.sleep(DAEMON_STARTUP_GRACE_SECONDS)
        self._verify_daemon_alive(sandbox_id, proc, netns_name, startup_log_file)
        self._daemon_socket_ready[sandbox_id] = True

    def _verify_daemon_alive(
        self,
        sandbox_id: str,
        proc: subprocess.Popen,
        netns_name: str | None,
        startup_log_file: Any | None = None,
    ) -> None:
        if proc.poll() is None:
            return
        self._processes.pop(sandbox_id, None)
        self._cleanup_sandbox_artifacts(sandbox_id, netns_name)
        log_excerpt = self._read_daemon_startup_log(startup_log_file)
        message = (
            f"Sandbox daemon exited during startup with code {proc.returncode}."
        )
        if log_excerpt:
            message = (
                f"{message} Captured daemon stdout/stderr (truncated to "
                f"{DAEMON_STARTUP_LOG_MAX_BYTES} bytes):\n{log_excerpt}"
            )
        else:
            # The previous code path advertised "stdout/stderr persistence
            # is disabled"; we now always try to capture, so an empty log
            # really does mean the daemon died silently (e.g. via a fast
            # ``exit(1)`` before printing anything).
            message = (
                f"{message} No stdout/stderr was captured before the "
                f"daemon exited."
            )
        raise RuntimeError(message)

    @staticmethod
    def _read_daemon_startup_log(startup_log_file: Any | None) -> str:
        """Return a bounded UTF-8 excerpt of the daemon's startup log.

        The file is the anonymous tempfile installed by ``_spawn_supervisor``
        as the daemon's stdout/stderr. By the time we get here the daemon
        has already exited (``_verify_daemon_alive`` only calls in after
        ``proc.poll()`` returned a non-None code), so ``read`` will not
        block waiting for additional bytes. All errors are swallowed and
        turned into a synthetic placeholder line: failing to enrich the
        diagnostic must never mask the original ``RuntimeError``.
        """
        if startup_log_file is None:
            return ""
        try:
            startup_log_file.seek(0)
            data = startup_log_file.read(DAEMON_STARTUP_LOG_MAX_BYTES)
        except (OSError, ValueError) as exc:
            return f"<failed to read daemon startup log: {exc}>"
        if not data:
            return ""
        text = data.decode("utf-8", errors="replace").rstrip()
        return text

    def _cleanup_sandbox_artifacts(
        self,
        sandbox_id: str,
        netns_name: str | None,
    ) -> None:
        """Drop every cache that was populated during a failed ``create``."""
        directory_root = self._directory_roots.pop(sandbox_id, None)
        if directory_root is not None:
            shutil.rmtree(directory_root, ignore_errors=True)
        file_root = self._file_roots.pop(sandbox_id, None)
        if file_root is not None:
            shutil.rmtree(file_root, ignore_errors=True)
        launcher_dir = self._launcher_dirs.pop(sandbox_id, None)
        if launcher_dir is not None:
            shutil.rmtree(launcher_dir, ignore_errors=True)
        control_dir = self._control_dirs.pop(sandbox_id, None)
        if control_dir is not None:
            shutil.rmtree(control_dir, ignore_errors=True)
        self._daemon_socket_ready.pop(sandbox_id, None)
        uplink_handle = self._uplink_handles.pop(sandbox_id, None)
        if uplink_handle is not None:
            network_module.teardown_network_uplink(uplink_handle)
        if netns_name and network_module.namespace_exists(netns_name):
            network_module.delete_named_namespace(netns_name)
        self._network_modes.pop(sandbox_id, None)
        self._runtime_policies.pop(sandbox_id, None)
        self._policy_binds.pop(sandbox_id, None)
        self._seccomp_bpf.pop(sandbox_id, None)
        self._landlock_payloads.pop(sandbox_id, None)
        self._policy_paths.pop(sandbox_id, None)
        self._remove_sandbox_host_firewall_rules(sandbox_id)
        # Drop any cgroup handle that survived a partially-completed create
        # (e.g. setup succeeded but daemon readiness then failed). Calling
        # teardown here keeps the failure path symmetric with stop() so we
        # never leak per-sandbox cgroup dirs in /sys/fs/cgroup.
        self._teardown_cgroup(sandbox_id)

    async def stop(self, sandbox_id: str, timeout: float = 10.0) -> None:
        await self._stop_background_processes(sandbox_id)
        proc = self._processes.get(sandbox_id)
        if proc is None:
            self._remove_sandbox_host_firewall_rules(sandbox_id)
            self._teardown_cgroup(sandbox_id)
            return
        if proc.poll() is not None:
            self._processes.pop(sandbox_id, None)
            self._remove_sandbox_host_firewall_rules(sandbox_id)
            self._teardown_cgroup(sandbox_id)
            return

        logger.info("Stopping sandbox %s (pid=%d)", sandbox_id, proc.pid)
        # Politely ask the daemon to drain in-flight IPC requests first;
        # ``SIGTERM`` from the host then trips the kernel's normal default
        # action (the daemon installs no signal handlers, so PID-1 namespace
        # init protection prevents inside-sandbox processes from hijacking
        # this path).
        await self._send_daemon_shutdown(sandbox_id)
        # Briefly give the daemon a chance to exit on its own; if it does we
        # avoid the SIGTERM/SIGKILL escalation entirely.
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, proc.wait, DAEMON_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                self._processes.pop(sandbox_id, None)
                self._daemon_socket_ready.pop(sandbox_id, None)
                self._remove_sandbox_host_firewall_rules(sandbox_id)
                self._teardown_cgroup(sandbox_id)
                return

            try:
                await loop.run_in_executor(None, proc.wait, timeout)
            except subprocess.TimeoutExpired:
                logger.warning("SIGTERM timeout for %s, killing", sandbox_id)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    self._processes.pop(sandbox_id, None)
                    self._daemon_socket_ready.pop(sandbox_id, None)
                    self._remove_sandbox_host_firewall_rules(sandbox_id)
                    self._teardown_cgroup(sandbox_id)
                    return
                await loop.run_in_executor(None, proc.wait, 5.0)

        self._processes.pop(sandbox_id, None)
        self._daemon_socket_ready.pop(sandbox_id, None)
        self._remove_sandbox_host_firewall_rules(sandbox_id)
        self._teardown_cgroup(sandbox_id)

    def _teardown_cgroup(self, sandbox_id: str) -> None:
        """Drop the per-sandbox cgroup (if any) created during ``create``.

        No-op when ``create`` never set up a cgroup (e.g. the policy left
        ``cgroup`` empty, or setup failed and was already rolled back).
        Cgroup teardown is best-effort and must not block sandbox
        deletion - any retained directory only wastes a few inodes in
        ``/sys/fs/cgroup`` and gets noticed via the warning log.
        """
        handle = self._cgroup_handles.pop(sandbox_id, None)
        if handle is None:
            return
        try:
            cgroup_module.teardown(handle)
        except Exception:
            logger.warning(
                "cgroup teardown raised for sandbox %s; ignoring",
                sandbox_id,
                exc_info=True,
            )

    async def is_running(self, sandbox_id: str) -> bool:
        proc = self._processes.get(sandbox_id)
        if proc is None:
            return False
        return proc.poll() is None

    async def update_network_policy(
        self,
        sandbox_id: str,
        network_policy: NetworkPolicy,
    ) -> None:
        """Hot-replace egress/ingress iptables rules for a running sandbox.

        Requires an existing named network namespace (``network.mode: isolated``).
        Updates the in-memory runtime policy cache so subsequent restarts that
        reload from disk stay consistent with the manager's written YAML.
        """
        if network_policy.mode != NetworkMode.ISOLATED:
            raise RuntimeError(
                f"Cannot hot-update network rules for sandbox '{sandbox_id}': "
                f"network.mode is '{network_policy.mode.value}' (requires isolated)"
            )

        namespace = self._netns_names.get(
            sandbox_id,
            network_module.netns_name_for_sandbox(sandbox_id),
        )
        if not network_module.namespace_exists(namespace):
            raise RuntimeError(
                f"Cannot hot-update network rules for sandbox '{sandbox_id}': "
                f"network namespace '{namespace}' does not exist"
            )

        cached = self._runtime_policies.get(sandbox_id)
        if cached is not None:
            self._runtime_policies[sandbox_id] = cached.model_copy(
                update={"network": network_policy},
            )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            network_module.replace_network_isolation,
            network_policy,
            namespace,
        )

    def get_exit_diagnostics(self, sandbox_id: str) -> str:
        """Return diagnostics for a sandbox whose lifecycle process is not running.

        Per-sandbox runtime stdout/stderr is no longer captured to disk
        (the legacy ``runtime.log`` was removed; audit.log carries the
        per-command stdout/stderr instead). Callers should treat this
        as a short "process gone" signal and rely on ``audit.log`` plus
        the standard Python logger for context.
        """
        proc = self._processes.get(sandbox_id)
        returncode = None if proc is None else proc.poll()
        return (
            f"Sandbox lifecycle process is not running; returncode={returncode}"
        )

    def _daemon_ipc_available(self, sandbox_id: str) -> bool:
        if not self._daemon_socket_ready.get(sandbox_id, False):
            return False
        proc = self._processes.get(sandbox_id)
        if proc is None or proc.poll() is not None:
            return False
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None:
            return False
        try:
            return socket_path.exists()
        except OSError:
            return False

    @staticmethod
    def _connect_daemon_socket(socket_path: Path) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(DAEMON_CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(str(socket_path))
        except OSError:
            sock.close()
            raise
        return sock

    @staticmethod
    def _send_request_blob(
        sock: socket.socket,
        header_blob: bytes,
        stdin_bytes: bytes | None,
    ) -> None:
        send_frame(sock, header_blob)
        if stdin_bytes:
            sock.sendall(stdin_bytes)

    @staticmethod
    def _read_response_blob(sock: socket.socket) -> dict[str, Any]:
        blob = recv_frame(sock, DAEMON_MAX_RESPONSE_BYTES)
        return json.loads(blob.decode("utf-8"))

    def _exec_via_daemon_blocking(self, call: _DaemonExecCall) -> ExecResult:
        """Run one ``exec`` over the IPC channel and return an ``ExecResult``.

        Executed via a worker thread so the asyncio event loop does not block
        on the synchronous Unix-socket IO. The daemon ``communicate()``s the
        child entirely before responding, so we only have to handle the
        request/response pair here.
        """
        request_payload: dict[str, Any] = {
            "command": list(call.command),
            "stdin_size": len(call.stdin_bytes or b""),
        }
        if call.env:
            request_payload["env"] = dict(call.env)
        if call.workdir:
            request_payload["workdir"] = call.workdir
        if call.timeout is not None:
            request_payload["timeout"] = call.timeout
        header_blob = encode_request(
            request_type=REQUEST_TYPE_EXEC,
            payload=request_payload,
        )
        if len(header_blob) > MAX_HEADER_BYTES:
            return ExecResult(
                exit_code=1,
                stderr=(
                    f"daemon request header too large "
                    f"({len(header_blob)} > {MAX_HEADER_BYTES})"
                ),
            )

        sock = self._connect_daemon_socket(call.socket_path)
        try:
            # The daemon waits for the user command to finish before
            # responding, so the receive timeout has to outlive the request
            # timeout. When no exec timeout is configured, use a bounded
            # default instead of waiting forever on the socket.
            if call.timeout is not None:
                sock.settimeout(call.timeout + 5.0)
            else:
                sock.settimeout(DEFAULT_EXEC_IPC_READ_TIMEOUT_SECONDS)
            self._send_request_blob(sock, header_blob, call.stdin_bytes)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            response = self._read_response_blob(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        return ExecResult(
            exit_code=int(response.get("exit_code", 1)),
            stdout=str(response.get("stdout", "")),
            stderr=str(response.get("stderr", "")),
        )

    def _exec_background_via_daemon_blocking(
        self,
        call: _DaemonBgExecCall,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "job_id": call.job_id,
            "command": list(call.command),
            "stdin_size": len(call.stdin_bytes or b""),
        }
        if call.env:
            request_payload["env"] = dict(call.env)
        if call.workdir:
            request_payload["workdir"] = call.workdir
        header_blob = encode_request(
            request_type=REQUEST_TYPE_EXEC_BACKGROUND,
            payload=request_payload,
        )
        if len(header_blob) > MAX_HEADER_BYTES:
            return {
                "ok": False,
                "started": False,
                "stderr": (
                    f"daemon request header too large "
                    f"({len(header_blob)} > {MAX_HEADER_BYTES})"
                ),
            }

        sock = self._connect_daemon_socket(call.socket_path)
        try:
            sock.settimeout(30.0)
            self._send_request_blob(sock, header_blob, call.stdin_bytes)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return self._read_response_blob(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _bg_status_via_daemon_blocking(
        self,
        call: _DaemonBgStatusCall,
    ) -> dict[str, Any]:
        header_blob = encode_request(
            request_type=REQUEST_TYPE_BG_STATUS,
            payload={"job_id": call.job_id},
        )
        sock = self._connect_daemon_socket(call.socket_path)
        try:
            sock.settimeout(30.0)
            self._send_request_blob(sock, header_blob, None)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return self._read_response_blob(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _bg_kill_via_daemon_blocking(
        self,
        call: _DaemonBgKillCall,
    ) -> dict[str, Any]:
        header_blob = encode_request(
            request_type=REQUEST_TYPE_BG_KILL,
            payload={"job_id": call.job_id, "signum": call.signum},
        )
        sock = self._connect_daemon_socket(call.socket_path)
        try:
            sock.settimeout(30.0)
            self._send_request_blob(sock, header_blob, None)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return self._read_response_blob(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    async def _exec_via_daemon(
        self,
        sandbox_id: str,
        request: RuntimeExecRequest,
    ) -> ExecResult:
        """Run an ``exec`` over the daemon IPC channel.

        Transport-level failures (connection refused, daemon crashed,
        timeout, framing/JSON corruption) surface as a synthetic
        ``ExecResult`` with a non-zero exit code so callers see the same
        value-shape regardless of whether the IPC roundtrip succeeded.
        Whenever transport fails the daemon is also flagged unhealthy so
        the next call short-circuits without attempting another connect.
        """
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=(
                    f"sandbox {sandbox_id!r} daemon IPC channel unavailable; "
                    "the daemon is not running or its control socket is gone"
                ),
            )

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                self._exec_via_daemon_blocking,
                _DaemonExecCall(
                    socket_path=socket_path,
                    command=list(request.command),
                    env=dict(request.env) if request.env else None,
                    workdir=request.workdir,
                    stdin_bytes=request.stdin_data,
                    timeout=request.timeout,
                ),
            )
        except (ConnectionError, ValueError) as exc:
            # ``ValueError`` already covers ``json.JSONDecodeError`` (G.ERR.09).
            self._daemon_socket_ready[sandbox_id] = False
            logger.warning(
                "Daemon IPC transport failure for sandbox %s: %s",
                sandbox_id,
                exc,
            )
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr=f"daemon IPC transport failure: {exc}",
            )
        except socket.timeout as exc:
            self._daemon_socket_ready[sandbox_id] = False
            logger.warning(
                "Daemon IPC timeout for sandbox %s: %s",
                sandbox_id,
                exc,
            )
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"daemon IPC timeout: {exc}",
            )
        except OSError as exc:
            if exc.errno in FATAL_DAEMON_ERRNOS:
                # Daemon is genuinely gone - flip the flag so future
                # callers stop trying this socket and fall back.
                self._daemon_socket_ready[sandbox_id] = False
                logger.warning(
                    "Daemon IPC unavailable for sandbox %s (fatal errno=%s): %s",
                    sandbox_id,
                    exc.errno,
                    exc,
                )
                return ExecResult(
                    exit_code=1,
                    stdout="",
                    stderr=f"daemon IPC unavailable: {exc}",
                )
            if exc.errno in RECOVERABLE_DAEMON_ERRNOS:
                # This is just transient host-side pressure (EMFILE,
                # EAGAIN, ENOMEM, ...). Don't poison the daemon - the
                # next request should be able to use the fast path.
                logger.warning(
                    "Daemon IPC transient failure for sandbox %s (errno=%s): %s",
                    sandbox_id,
                    exc.errno,
                    exc,
                )
                return ExecResult(
                    exit_code=1,
                    stdout="",
                    stderr=f"daemon IPC transient failure: {exc}",
                )
            raise

    def _file_op_unavailable(self, sandbox_id: str) -> RuntimeFileOpResult:
        return RuntimeFileOpResult(
            ok=False,
            error="daemon_unavailable",
            detail=(
                f"sandbox {sandbox_id!r} daemon IPC channel unavailable; "
                "the daemon is not running or its control socket is gone"
            ),
        )

    def _file_op_transport_failure(
        self,
        sandbox_id: str,
        exc: BaseException,
        *,
        fatal: bool = True,
    ) -> RuntimeFileOpResult:
        """Build a transport-failure result.

        If ``fatal`` is true the sandbox's daemon is flagged unhealthy so
        subsequent callers fall back to the legacy ``bash`` / ``python3``
        path. Recoverable resource-pressure errors (EMFILE, EAGAIN, ...)
        should pass ``fatal=False`` so the next request can still take
        the IPC fast path - otherwise a single transient blip permanently
        demotes the sandbox to the slow exec fallback and turns subsequent
        calls into ~hundreds of ms each.
        """
        if fatal:
            self._daemon_socket_ready[sandbox_id] = False
        logger.warning(
            "Daemon IPC %s failure during file-op for sandbox %s: %s",
            "transport" if fatal else "transient",
            sandbox_id,
            exc,
        )
        return RuntimeFileOpResult(
            ok=False,
            error="transport_failure",
            detail=str(exc),
        )

    def _write_file_via_daemon_blocking(
        self,
        socket_path: Path,
        sandbox_path: str,
        content: bytes,
        mkdir_parents: bool,
        mode: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": sandbox_path,
            "content_size": len(content),
            "mkdir_parents": mkdir_parents,
        }
        if mode is not None:
            payload["mode"] = mode
        header_blob = encode_request(
            request_type=REQUEST_TYPE_WRITE_FILE,
            payload=payload,
        )
        sock = self._connect_daemon_socket(socket_path)
        try:
            sock.settimeout(DAEMON_FILE_OP_TIMEOUT_SECONDS)
            self._send_request_blob(sock, header_blob, content)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return self._read_response_blob(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _read_file_via_daemon_blocking(
        self,
        socket_path: Path,
        sandbox_path: str,
    ) -> tuple[dict[str, Any], bytes]:
        header_blob = encode_request(
            request_type=REQUEST_TYPE_READ_FILE,
            payload={"path": sandbox_path},
        )
        sock = self._connect_daemon_socket(socket_path)
        try:
            sock.settimeout(DAEMON_FILE_OP_TIMEOUT_SECONDS)
            self._send_request_blob(sock, header_blob, None)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            response = self._read_response_blob(sock)
            content = b""
            if response.get("ok"):
                size = int(response.get("content_size") or 0)
                if size > 0:
                    content = recv_frame(sock, MAX_FILE_BYTES)
                    if len(content) != size:
                        raise ConnectionError(
                            f"daemon returned {len(content)} bytes but advertised {size}",
                        )
            return response, content
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _list_dir_via_daemon_blocking(
        self, call: _DaemonListDirCall,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": call.sandbox_path,
            "recursive": call.recursive,
            "include_files": call.include_files,
            "include_dirs": call.include_dirs,
        }
        if call.max_depth is not None:
            payload["max_depth"] = call.max_depth
        header_blob = encode_request(
            request_type=REQUEST_TYPE_LIST_DIR,
            payload=payload,
        )
        sock = self._connect_daemon_socket(call.socket_path)
        try:
            sock.settimeout(DAEMON_FILE_OP_TIMEOUT_SECONDS)
            self._send_request_blob(sock, header_blob, None)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return self._read_response_blob(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _file_op_result_from_response(response: dict[str, Any]) -> RuntimeFileOpResult:
        if response.get("ok"):
            return RuntimeFileOpResult(ok=True)
        return RuntimeFileOpResult(
            ok=False,
            error=str(response.get("error") or "io_error"),
            errno=int(response["errno"]) if isinstance(response.get("errno"), int) else None,
            detail=str(response.get("stderr") or response.get("detail") or ""),
        )

    async def write_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
        *,
        mkdir_parents: bool = True,
        mode: int | None = None,
    ) -> RuntimeFileOpResult:
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return self._file_op_unavailable(sandbox_id)
        if len(content) > MAX_FILE_BYTES:
            return RuntimeFileOpResult(
                ok=False,
                error="too_large",
                detail=(
                    f"content size {len(content)} exceeds limit {MAX_FILE_BYTES}"
                ),
            )

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                self._write_file_via_daemon_blocking,
                socket_path,
                sandbox_path,
                content,
                mkdir_parents,
                mode,
            )
        except (ConnectionError, ValueError) as exc:
            # ``ValueError`` already covers ``json.JSONDecodeError`` (G.ERR.09).
            return self._file_op_transport_failure(sandbox_id, exc)
        except socket.timeout as exc:
            return self._file_op_transport_failure(sandbox_id, exc)
        except OSError as exc:
            if exc.errno in FATAL_DAEMON_ERRNOS:
                return self._file_op_transport_failure(sandbox_id, exc)
            if exc.errno in RECOVERABLE_DAEMON_ERRNOS:
                return self._file_op_transport_failure(sandbox_id, exc, fatal=False)
            raise
        return self._file_op_result_from_response(response)

    async def read_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> RuntimeFileOpResult:
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return self._file_op_unavailable(sandbox_id)

        loop = asyncio.get_running_loop()
        try:
            response, content = await loop.run_in_executor(
                None,
                self._read_file_via_daemon_blocking,
                socket_path,
                sandbox_path,
            )
        except (ConnectionError, ValueError) as exc:
            # ``ValueError`` already covers ``json.JSONDecodeError`` (G.ERR.09).
            return self._file_op_transport_failure(sandbox_id, exc)
        except socket.timeout as exc:
            return self._file_op_transport_failure(sandbox_id, exc)
        except OSError as exc:
            if exc.errno in FATAL_DAEMON_ERRNOS:
                return self._file_op_transport_failure(sandbox_id, exc)
            if exc.errno in RECOVERABLE_DAEMON_ERRNOS:
                return self._file_op_transport_failure(sandbox_id, exc, fatal=False)
            raise

        if response.get("ok"):
            return RuntimeFileOpResult(ok=True, content=content)
        return self._file_op_result_from_response(response)

    async def list_dir(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> RuntimeFileOpResult:
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return self._file_op_unavailable(sandbox_id)

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                self._list_dir_via_daemon_blocking,
                _DaemonListDirCall(
                    socket_path=socket_path,
                    sandbox_path=sandbox_path,
                    recursive=recursive,
                    max_depth=max_depth,
                    include_files=include_files,
                    include_dirs=include_dirs,
                ),
            )
        except (ConnectionError, ValueError) as exc:
            # ``ValueError`` already covers ``json.JSONDecodeError`` (G.ERR.09).
            return self._file_op_transport_failure(sandbox_id, exc)
        except socket.timeout as exc:
            return self._file_op_transport_failure(sandbox_id, exc)
        except OSError as exc:
            if exc.errno in FATAL_DAEMON_ERRNOS:
                return self._file_op_transport_failure(sandbox_id, exc)
            if exc.errno in RECOVERABLE_DAEMON_ERRNOS:
                return self._file_op_transport_failure(sandbox_id, exc, fatal=False)
            raise

        if response.get("ok"):
            items = response.get("items")
            if not isinstance(items, list):
                items = []
            return RuntimeFileOpResult(ok=True, items=items)
        return self._file_op_result_from_response(response)

    async def _send_daemon_shutdown(self, sandbox_id: str) -> None:
        """Politely ask the daemon to drain and exit before sending SIGTERM."""
        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_socket_ready.get(sandbox_id, False):
            return
        if not socket_path.exists():
            return

        def _ask_shutdown(path: Path) -> None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(DAEMON_SHUTDOWN_TIMEOUT_SECONDS)
            try:
                sock.connect(str(path))
                send_frame(
                    sock,
                    encode_request(request_type=REQUEST_TYPE_SHUTDOWN),
                )
                try:
                    recv_frame(sock, MAX_HEADER_BYTES)
                except OSError:
                    # ``OSError`` already covers ``ConnectionError`` (G.ERR.09).
                    pass
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _ask_shutdown, socket_path),
                timeout=DAEMON_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.debug(
                "Failed to send daemon shutdown for %s: %s",
                sandbox_id,
                exc,
            )

    def _ensure_exec_semaphore(self) -> asyncio.Semaphore:
        """Lazily build the per-runtime ``exec`` admission semaphore.

        Cannot be created in ``__init__`` because ``ProcessRuntime`` may
        be instantiated before any event loop is running (e.g. CLI tools
        or pytest fixtures), and ``asyncio.Semaphore`` will then bind to
        the wrong loop. By creating it the first time ``exec`` actually
        awaits inside the server's loop, we guarantee it is bound to the
        loop that will end up signalling it.
        """
        sem = self._exec_semaphore
        if sem is None:
            sem = asyncio.Semaphore(self._exec_concurrency_limit)
            self._exec_semaphore = sem
        return sem

    async def exec(
        self,
        sandbox_id: str,
        request: RuntimeExecRequest,
    ) -> ExecResult:
        """Execute a command in the sandbox via the daemon IPC channel.

        Each sandbox owns a long-running in-sandbox daemon (PID 1 of its
        own PID namespace) that bubblewrap set up once at sandbox creation
        with the full namespace/mount/seccomp/Landlock envelope. ``exec``
        therefore boils down to a single Unix-socket roundtrip; bubblewrap
        is **not** spawned per call. The same security envelope is
        inherited by every command the daemon ``fork+exec``s.

        Concurrency is gated by ``_exec_semaphore`` to keep the number of
        in-flight CPU-heavy commands below ``JIUWENBOX_EXEC_CONCURRENCY``
        (defaulted to the box's usable CPU count). Without this cap,
        running more concurrent ``exec`` calls than there are CPUs causes
        super-linear latency growth - the typical "throughput collapse"
        pattern of oversubscribed CPU-bound workloads. File-ops fast
        paths (``write_file``/``read_file``/``list_dir``) are *not*
        throttled because they are I/O-bound and barely consume CPU.
        """
        logger.info(
            "Executing command in sandbox %s via daemon IPC: %s",
            sandbox_id,
            _summarize_command(list(request.command)),
        )
        semaphore = self._ensure_exec_semaphore()
        async with semaphore:
            return await self._exec_via_daemon(sandbox_id, request)

    async def exec_background(
        self,
        sandbox_id: str,
        request: RuntimeBackgroundExecRequest,
    ) -> BackgroundExecResult:
        self._reap_background_processes(sandbox_id)

        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return BackgroundExecResult(
                started=False,
                command=list(request.command),
                error_message=(
                    f"sandbox {sandbox_id!r} daemon IPC channel unavailable; "
                    "the daemon is not running or its control socket is gone"
                ),
            )

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None,
                self._exec_background_via_daemon_blocking,
                _DaemonBgExecCall(
                    socket_path=socket_path,
                    job_id=request.job_id,
                    command=list(request.command),
                    env=dict(request.env) if request.env else None,
                    workdir=request.workdir,
                    stdin_bytes=request.stdin_data,
                ),
            )
        except (ConnectionError, ValueError) as exc:
            self._daemon_socket_ready[sandbox_id] = False
            return BackgroundExecResult(
                started=False,
                command=list(request.command),
                error_message=f"daemon IPC transport failure: {exc}",
            )
        except socket.timeout as exc:
            self._daemon_socket_ready[sandbox_id] = False
            return BackgroundExecResult(
                started=False,
                command=list(request.command),
                error_message=f"daemon IPC timeout: {exc}",
            )
        except OSError as exc:
            if exc.errno in FATAL_DAEMON_ERRNOS:
                self._daemon_socket_ready[sandbox_id] = False
            return BackgroundExecResult(
                started=False,
                command=list(request.command),
                error_message=f"daemon IPC unavailable: {exc}",
            )

        if not response.get("ok") or not response.get("started", False):
            error = response.get("stderr") or response.get("error") or "unknown error"
            return BackgroundExecResult(
                started=False,
                command=list(request.command),
                error_message=str(error),
            )

        pid = response.get("pid")
        pid_int = pid if isinstance(pid, int) else None
        running = response.get("running")
        running_bool = running if isinstance(running, bool) else True
        exit_code = response.get("exit_code")
        exit_code_int = exit_code if isinstance(exit_code, int) else None

        job = BackgroundJob(
            job_id=request.job_id,
            sandbox_id=sandbox_id,
            command=list(request.command),
            pid=pid_int,
            workdir=request.workdir,
            exit_code=exit_code_int if not running_bool else None,
        )
        if not running_bool and job.finished_at is None:
            job.finished_at = datetime.now(timezone.utc)

        jobs = self._background_jobs_for_sandbox(sandbox_id)
        jobs[request.job_id] = job

        logger.info(
            "Started background command in sandbox %s (job=%s pid=%s): %s",
            sandbox_id,
            request.job_id,
            pid_int,
            _summarize_command(list(request.command)),
        )
        return BackgroundExecResult(
            started=True,
            job_id=request.job_id,
            pid=pid_int,
            command=list(request.command),
            running=running_bool,
            exit_code=exit_code_int,
        )

    async def get_background_job(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> BackgroundJobStatus:
        job = self._get_background_job_record(sandbox_id, job_id)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._sync_background_job_blocking,
            sandbox_id,
            job,
        )
        return self._job_status(job)

    async def list_background_jobs(
        self,
        sandbox_id: str,
        *,
        running_only: bool = False,
    ) -> list[BackgroundJobSummary]:
        jobs = list(self._background_jobs_for_sandbox(sandbox_id).values())
        loop = asyncio.get_running_loop()
        for job in jobs:
            await loop.run_in_executor(
                None,
                self._sync_background_job_blocking,
                sandbox_id,
                job,
            )
        summaries = [self._job_summary(job) for job in jobs]
        if running_only:
            summaries = [item for item in summaries if item.running]
        summaries.sort(key=lambda item: item.started_at, reverse=True)
        return summaries

    async def kill_background_job(
        self,
        sandbox_id: str,
        job_id: str,
        signum: int = 15,
    ) -> KillBackgroundJobResult:
        job = self._get_background_job_record(sandbox_id, job_id)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._sync_background_job_blocking,
            sandbox_id,
            job,
        )
        if job.exit_code is not None:
            return KillBackgroundJobResult(
                job_id=job_id,
                killed=False,
                reason="already_exited",
                exit_code=job.exit_code,
            )

        socket_path = self._control_socket_host_path(sandbox_id)
        if socket_path is None or not self._daemon_ipc_available(sandbox_id):
            return KillBackgroundJobResult(
                job_id=job_id,
                killed=False,
                reason="permission_denied",
                exit_code=job.exit_code,
            )

        try:
            response = await loop.run_in_executor(
                None,
                self._bg_kill_via_daemon_blocking,
                _DaemonBgKillCall(
                    socket_path=socket_path,
                    job_id=job_id,
                    signum=signum,
                ),
            )
        except (OSError, ValueError):
            # ``OSError`` already covers ``ConnectionError`` and ``socket.timeout`` (G.ERR.09).
            return KillBackgroundJobResult(
                job_id=job_id,
                killed=False,
                reason="permission_denied",
                exit_code=job.exit_code,
            )

        reason = str(response.get("reason", "permission_denied"))
        killed = bool(response.get("killed", False))
        exit_code = response.get("exit_code")
        exit_code_int = exit_code if isinstance(exit_code, int) else job.exit_code
        await loop.run_in_executor(
            None,
            self._sync_background_job_blocking,
            sandbox_id,
            job,
        )
        return KillBackgroundJobResult(
            job_id=job_id,
            killed=killed,
            reason=reason,
            exit_code=exit_code_int,
        )

    async def cleanup(self, sandbox_id: str) -> None:
        await self.stop(sandbox_id)
        self._processes.pop(sandbox_id, None)
        policy_path = self._policy_paths.pop(sandbox_id, None)
        network_mode = self._network_modes.pop(sandbox_id, None)
        self._runtime_policies.pop(sandbox_id, None)
        self._policy_binds.pop(sandbox_id, None)
        self._seccomp_bpf.pop(sandbox_id, None)
        self._landlock_payloads.pop(sandbox_id, None)
        if network_mode is None and policy_path is not None and policy_path.exists():
            try:
                network_mode = self._load_policy(policy_path).network.mode
            except Exception:
                logger.warning(
                    "Failed to reload policy for sandbox %s during namespace cleanup",
                    sandbox_id,
                    exc_info=True,
                )

        if network_mode == NetworkMode.ISOLATED:
            namespace = self._netns_names.pop(
                sandbox_id,
                network_module.netns_name_for_sandbox(sandbox_id),
            )
            uplink_handle = self._uplink_handles.pop(sandbox_id, None)
            if uplink_handle is not None:
                network_module.teardown_network_uplink(uplink_handle)
            if network_module.namespace_exists(namespace):
                network_module.delete_named_namespace(namespace)
        else:
            self._netns_names.pop(sandbox_id, None)
            self._uplink_handles.pop(sandbox_id, None)
        self._remove_sandbox_host_firewall_rules(sandbox_id)

        directory_root = self._directory_roots.pop(sandbox_id, None)
        if directory_root is not None and directory_root.exists():
            shutil.rmtree(directory_root, ignore_errors=True)
        file_root = self._file_roots.pop(sandbox_id, None)
        if file_root is not None and file_root.exists():
            shutil.rmtree(file_root, ignore_errors=True)
        launcher_dir = self._launcher_dirs.pop(sandbox_id, None)
        if launcher_dir is not None and launcher_dir.exists():
            shutil.rmtree(launcher_dir, ignore_errors=True)
        control_dir = self._control_dirs.pop(sandbox_id, None)
        if control_dir is not None and control_dir.exists():
            shutil.rmtree(control_dir, ignore_errors=True)
        self._background_processes.pop(sandbox_id, None)
        self._daemon_socket_ready.pop(sandbox_id, None)
        # Runtime log files used to live here too; they were removed in
        # favour of the single ``audit.log`` written by ``AuditLogger``.
        # Nothing extra to clean up on this side any more.
