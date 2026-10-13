# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Long-running in-sandbox daemon that handles ``exec`` requests.

The previous implementation was a placeholder that just blocked on
``signal.pause`` so the sandbox lifecycle stayed alive. To make exec
calls cheap, the daemon now hosts a Unix-socket IPC server inside the
sandbox: box-server connects to the socket and sends ``exec`` requests
that the daemon services by ``fork+exec``-ing the user command. Because
the daemon already has bubblewrap's namespaces, mounts, seccomp, and
Landlock applied, every spawned child inherits the same isolation - the
expensive ``bwrap`` setup happens once per sandbox lifecycle instead of
once per exec.

How the daemon is started
-------------------------
Inside the sandbox the Landlock launcher (see ``landlock_launcher.py``)
reads this script's source from ``/jiuwenbox`` *before* applying
Landlock and then runs the daemon code with ``compile``/``exec`` in the
launcher's own Python process. There is **no** second ``execve`` after
Landlock is locked in, which is what allows the ``/jiuwenbox`` directory
to be omitted from the Landlock allowlist: nothing in the sandbox needs
to read the daemon script from disk after Landlock applies. From the
kernel's point of view the daemon and the launcher are the same Linux
process (PID 1 of the sandbox PID namespace). The directory is reserved
by ``PolicyEngine`` so user policies cannot reference it; see
``_RESERVED_SANDBOX_PATHS`` in ``jiuwenbox/server/policy_engine.py``.

The control socket itself lives on the **host** filesystem; box-server
``bind()``s and ``listen()``s before spawning bubblewrap, then passes
the listener file descriptor into the sandbox via
``subprocess.Popen(pass_fds=...)``. Bubblewrap's user command path
never closes arbitrary inherited fds (only its own monitor/PID-1 paths
do), so the listener fd flows naturally through the bwrap → launcher
chain. The daemon recovers the fd from ``JIUWENBOX_CONTROL_LISTENER_FD``
and ``accept()``s against it. This keeps the IPC channel entirely
outside any sandbox-visible path, so user code spawned by the daemon
cannot reach (or delete) the listener.

Important security notes:

* Sandboxed payloads share the daemon's UID (typically ``nobody``).  The
  kernel's PID-1 signal protections are not sufficient on every platform,
  so seccomp additionally blocks ``kill``/``tkill``/``tgkill`` targeting
  infrastructure PIDs (1/2, process group, or broadcast).  Each user exec
  also runs in its own session so ``kill(0)`` cannot reach the daemon.

* Box-server shuts the daemon down via the ``shutdown`` IPC command
  (graceful) or ``SIGKILL`` from outside the namespace (forced).

* Children are spawned via :func:`subprocess.Popen` so the kernel does
  the standard ``fork``/``execve`` pair. The seccomp BPF filter and
  Landlock ruleset that bwrap and the launcher installed before running
  the daemon are inherited by every child - they cannot be relaxed or
  stripped from inside the sandbox. ``close_fds=True`` (the Python
  default) ensures the listener fd is **not** inherited by user code.

The module deliberately stays standard-library-only so it can be
launched with ``python3 -S`` (no ``import site``) for fastest cold
start, and so the launcher's ``compile``/``exec`` step does not need to
reach outside the standard library to load the daemon.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# These constants are duplicated from ``daemon_ipc`` so the in-sandbox
# daemon does not need to import the package; the script is mounted at
# ``/jiuwenbox/sandbox-daemon.py`` and executed directly. Any change must
# be mirrored in ``daemon_ipc.py``.
SANDBOX_RESERVED_DIR = "/jiuwenbox"
SANDBOX_DAEMON_SANDBOX_PATH = f"{SANDBOX_RESERVED_DIR}/sandbox-daemon.py"
SANDBOX_LAUNCHER_PATH = f"{SANDBOX_RESERVED_DIR}/landlock-launcher.py"
SANDBOX_DAEMON_COMMAND = ["python3", "-S", SANDBOX_DAEMON_SANDBOX_PATH]
LISTENER_FD_ENV = "JIUWENBOX_CONTROL_LISTENER_FD"
SANDBOX_IP_ENV = "SANDBOX_IP"

REQUEST_TYPE_EXEC = "exec"
REQUEST_TYPE_SHUTDOWN = "shutdown"
REQUEST_TYPE_WRITE_FILE = "write_file"
REQUEST_TYPE_READ_FILE = "read_file"
REQUEST_TYPE_LIST_DIR = "list_dir"
REQUEST_TYPE_EXEC_BACKGROUND = "exec_background"
REQUEST_TYPE_BG_STATUS = "bg_status"
REQUEST_TYPE_BG_KILL = "bg_kill"

PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 1 * 1024 * 1024
MAX_STDIN_BYTES = 64 * 1024 * 1024
MAX_STDOUT_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024

ACCEPT_TIMEOUT_SECONDS = 1.0
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger("jiuwenbox.sandbox_daemon")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"socket closed after {len(buf)}/{n} bytes",
            )
        buf.extend(chunk)
    return bytes(buf)


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > 0xFFFFFFFF:
        raise ValueError(f"frame size {len(payload)} exceeds 4 GiB")
    sock.sendall(struct.pack(">I", len(payload)))
    if payload:
        sock.sendall(payload)


def _recv_frame(sock: socket.socket, max_size: int) -> bytes:
    header = _recv_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size > max_size:
        raise ValueError(
            f"incoming frame size {size} exceeds limit {max_size}",
        )
    return _recv_exact(sock, size)


def _send_response(sock: socket.socket, response: dict[str, Any]) -> None:
    body = json.dumps(response, ensure_ascii=False).encode("utf-8")
    _send_frame(sock, body)


def _exec_response(
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    started: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "ok": started,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
    if error:
        response["error"] = error
    return response


def _stringify_command(command: list[Any]) -> list[str]:
    return [str(item) for item in command]


def _normalize_env(env: Any) -> dict[str, str] | None:
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError("env must be a JSON object")
    return {str(key): str(value) for key, value in env.items()}


def build_child_env(env_override: dict[str, str] | None) -> dict[str, str]:
    """Merge an exec environment while preserving runtime-owned metadata.

    Public so host-side tests can exercise reserved-key handling without
    poking private helpers. Keep in sync with ``apply_sandbox_ip_env`` in
    ``daemon_ipc.py`` (this script cannot import that module in-sandbox).
    """
    sandbox_ip = os.environ.get(SANDBOX_IP_ENV)
    merged_env = dict(os.environ)
    # Children must not see the listener fd or the env var pointing at it;
    # close_fds=True closes the fd, and removing the variable avoids exposing
    # an unusable internal descriptor number.
    merged_env.pop(LISTENER_FD_ENV, None)
    if env_override is not None:
        merged_env.update(env_override)
    # The sandbox address is a lifecycle snapshot owned by the runtime.
    # Always remove a caller value, including when address discovery failed.
    merged_env.pop(SANDBOX_IP_ENV, None)
    if sandbox_ip:
        merged_env[SANDBOX_IP_ENV] = sandbox_ip
    return merged_env


class DaemonState:
    """Shared mutable state guarded by ``lock``."""

    def __init__(self) -> None:
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self.lock = threading.Lock()
        self.completion = threading.Condition(self.lock)

    def begin_request(self) -> None:
        with self.lock:
            self.in_flight += 1

    def end_request(self) -> None:
        with self.lock:
            self.in_flight -= 1
            if self.in_flight <= 0:
                self.completion.notify_all()

    def wait_drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.lock:
            while self.in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.completion.wait(timeout=remaining)
            return True


def _handle_exec(conn: socket.socket, header: dict[str, Any], state: DaemonState) -> None:
    """Run a single user command and stream the result back to ``conn``."""
    try:
        command = header.get("command")
        if not isinstance(command, list) or not command:
            _send_response(
                conn,
                _exec_response(
                    exit_code=2,
                    stderr="exec request missing 'command'",
                    started=False,
                    error="bad_request",
                ),
            )
            return
        command = _stringify_command(command)

        env_override = _normalize_env(header.get("env"))
        workdir = header.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        timeout = header.get("timeout")
        if timeout is not None and not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a number")
        stdin_size = int(header.get("stdin_size") or 0)
        if stdin_size < 0 or stdin_size > MAX_STDIN_BYTES:
            raise ValueError(f"invalid stdin_size {stdin_size}")

        stdin_bytes = _recv_exact(conn, stdin_size) if stdin_size else b""

        # Python ForkServer fast path (default ON -- a transparent optimisation,
        # no env var required). Activated when BOTH
        # the server marked the request (``python_fastpath``) and the fast path
        # is enabled (it is, unless ``JIUWENBOX_PYTHON_FASTPATH=0`` opts out).
        # Falls back to the normal ``subprocess.Popen`` path below otherwise.
        if header.get("python_fastpath") and _fastpath_enabled():
            if _try_fastpath_exec(conn, command, header, stdin_bytes):
                return

        merged_env = build_child_env(env_override)

        proc_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE if stdin_size else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": merged_env,
            "close_fds": True,
            # Isolate each exec in its own session so kill(0) from user
            # code cannot reach the long-running daemon process group.
            "start_new_session": True,
        }
        if workdir:
            proc_kwargs["cwd"] = workdir

        try:
            proc = subprocess.Popen(command, **proc_kwargs)
        except OSError as exc:
            # ``OSError`` already covers ``FileNotFoundError`` and
            # ``PermissionError``; keep one branch (G.ERR.09).
            _send_response(
                conn,
                _exec_response(
                    exit_code=127,
                    stderr=f"failed to spawn command: {exc}",
                    started=False,
                    error="spawn_failed",
                ),
            )
            return

        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=stdin_bytes if stdin_size else None,
                timeout=timeout,
            )
            response = _exec_response(
                exit_code=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                stdout_bytes, stderr_bytes = b"", b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            stderr_text = (
                f"{stderr_text}\nCommand timed out"
                if stderr_text
                else "Command timed out"
            )
            response = _exec_response(
                exit_code=124,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_text,
                error="timeout",
            )

        _send_response(conn, response)
    except (ValueError, ConnectionError) as exc:
        try:
            _send_response(
                conn,
                _exec_response(
                    exit_code=2,
                    stderr=str(exc),
                    started=False,
                    error="bad_request",
                ),
            )
        except OSError:
            pass
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unhandled error while handling exec request")
        try:
            _send_response(
                conn,
                _exec_response(
                    exit_code=1,
                    stderr=f"daemon internal error: {exc}",
                    started=False,
                    error="internal",
                ),
            )
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Python ForkServer fast path (feature-flagged, default OFF; release candidate).
#
# When the fast path is enabled (default ON; opt out with
# ``JIUWENBOX_PYTHON_FASTPATH=0``), ``python3 -c <code>`` exec requests that the
# server marked with ``python_fastpath: true`` are routed to a small persistent
# in-sandbox ForkServer instead of spawning a fresh interpreter per call. The
# worker source is passed to the worker via ``python3 -c <source>`` so nothing
# needs to be read from disk after Landlock applies (mirroring how the launcher
# loads the daemon itself). The forked children inherit the exact same bwrap
# namespace / userns / cgroup / seccomp / Landlock / mount envelope as the
# daemon. This is a feature-flagged internal path -- it is not a formal
# external API and does not change the default ``/exec`` path. When the fast
# path is unavailable it falls back to the normal ``subprocess.Popen`` path.
FASTPATH_ENV = "JIUWENBOX_PYTHON_FASTPATH"
FASTPATH_WORKERS_ENV = "JIUWENBOX_PYTHON_FASTPATH_WORKERS"
FASTPATH_IDLE_TIMEOUT_ENV = "JIUWENBOX_PYTHON_FASTPATH_IDLE_TIMEOUT"
FASTPATH_DEFAULT_WORKERS = 2
FASTPATH_MARKER = "JIWENBOX_FORK_WORKER"
# Worker control fd is socketpair'd by the daemon; the fd number is passed
# as ``sys.argv[1]`` to the ``python3 -c`` worker.
# The worker->daemon response frame carries the child's stdout +
# stderr (JSON-encoded). This cap must not be a *tighter* bound than the
# normal ``subprocess.Popen`` /exec path's output contract, or FastPath would
# fail on outputs that Popen succeeds with -- breaking the "transparent
# optimisation" goal. The enforced contract on the daemon->box-server hop is
# ``DAEMON_MAX_RESPONSE_BYTES = 256 MiB`` (box-server ``recv_frame`` cap in
# ``server/runtime/process.py``); beyond that *both* paths fail identically at
# the box-server, so 256 MiB is the honest alignment point: FastPath succeeds
# exactly where Popen does. (``MAX_STDOUT_BYTES = 64 MiB`` in ``daemon_ipc.py``
# is documented but not enforced -- no truncation call -- so it is not the
# contract.) The frame is a *bounded* read (size prefix checked before
# allocation), never unbounded; worst case per in-flight FastPath request is
# ~256 MiB of daemon buffer, the same order as ``Popen.communicate``'s own
# accumulation, capped by the sandbox cgroup's memory limit.
_FASTPATH_MAX_FRAME = 256 * 1024 * 1024

# --- lifecycle / resilience / resource knobs ----------------------------
# Every knob is env-overridable but hard-clamped so a bad override can never
# grow the worker pool without bound or defeat the circuit breaker.
FASTPATH_MAX_WORKERS = 4            # per-sandbox hard ceiling
FASTPATH_DEFAULT_IDLE_TIMEOUT = 300.0  # recycle pool to 0 after this idle
FASTPATH_BREAKER_THRESHOLD = 3      # consecutive failures -> breaker open
FASTPATH_BREAKER_COOLDOWN = 30.0    # seconds before a half-open probe
FASTPATH_MAX_SPINS = 3              # worker-selection retries per request
FASTPATH_REAP_INTERVAL = 5.0        # idle-reaper wakeup interval
# Minimal observability: counters are mirrored (throttled) to a JSON
# snapshot inside the sandbox so host-side tooling / perf scripts can read
# them via the normal exec path without any new API surface. Purely
# diagnostic; never used for control flow.
FASTPATH_STATS_PATH = "/tmp/fastpath_stats.json"
FASTPATH_STATS_THROTTLE = 1.0       # min seconds between snapshot writes


def _fastpath_enabled() -> bool:
    """Whether the Python ForkServer fast path is active.

    The fast path is a **transparent optimisation** and is ON by
    default -- no environment variable is required to benefit from it.

    Semantics of ``JIUWENBOX_PYTHON_FASTPATH``:

    * unset  -> ON  (default-on; the user does nothing and still gets it)
    * ``"1"`` -> ON  (explicit enable, equivalent to the new default)
    * ``"0"`` -> OFF (explicit opt-out)
    * any other value -> OFF, fail-safe, with a warning so a typo does not
      silently leave the fast path on when the operator intended to disable
      it. Fail-safe here means "off" because that is the known-good path the
      operator can reason about; a silently-on typo would be the worse
      failure mode.

    The fast path only ever routes requests the server already marked as
    ``python_fastpath``; everything else falls through to ``subprocess.Popen``
    unchanged, so default-ON cannot change the contract for non-candidates.
    """
    raw = os.environ.get(FASTPATH_ENV)
    if raw is None:
        return True
    if raw == "1":
        return True
    if raw == "0":
        return False
    # Unrecognised value: fail safe (off) and surface it.
    logger.warning(
        "unrecognised %s=%r; expected '0' or '1'; defaulting fast path OFF",
        FASTPATH_ENV, raw,
    )
    return False


def _fastpath_worker_count() -> int:
    raw = os.environ.get(FASTPATH_WORKERS_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value >= 1:
            return min(value, FASTPATH_MAX_WORKERS)
    return FASTPATH_DEFAULT_WORKERS


def _fastpath_idle_timeout() -> float:
    raw = os.environ.get(FASTPATH_IDLE_TIMEOUT_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value >= 10.0:
            return value
    return FASTPATH_DEFAULT_IDLE_TIMEOUT


# ---------------------------------------------------------------------------
# Direct-script and EDPA-wrapper eligibility.
#
# Real EDPA traffic never reaches the daemon as ``python3 -c``. The upstream
# provider hard-codes ``["bash", "-lc", command]``, and 99.93% of those
# payloads are the single shape ``cd <dir> && python <file>.py [args]``. This
# block recognises exactly that shape (plus the direct ``python <file>.py``
# form) and nothing else.
#
# This is deliberately NOT a shell parser. It is a strict recogniser with an
# allowlisted character set: anything it does not positively understand -
# pipes, redirects, ``;``, command substitution, globs, variable expansion,
# escapes, env prefixes, extra commands after the interpreter - is rejected
# and the request takes the normal ``subprocess.Popen`` path. The governing
# rule is "rather miss a hit than convert one wrongly".
FASTPATH_SCRIPT_ENV = "JIUWENBOX_PYTHON_FASTPATH_SCRIPT"

# Unquoted words may only contain these characters. Every shell metacharacter
# is absent by construction, so a word that tokenises is a literal.
_FASTPATH_SAFE_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-/=+:,@%"
)
# Only these bare interpreter names are recognised. A path (``/usr/bin/python``)
# or a versioned name is not accepted: it would have to be identity-checked
# against the worker interpreter, which is out of scope.
_FASTPATH_INTERP_NAMES = frozenset(("python", "python3"))
# Env vars a login shell is allowed to introduce/change. Anything else means
# the sandbox image has profile scripts with real side effects, and the
# wrapper form is then not equivalent to running the script directly.
_FASTPATH_LOGIN_ENV_ALLOWED = frozenset(("PWD", "OLDPWD", "SHLVL", "_"))
# Module names already resident in the interpreter. A script directory that
# shadows one of these would import differently under a warm fork worker than
# under a fresh interpreter, so those requests fall back.
_FASTPATH_RESIDENT_MODULES = frozenset(sys.modules) | frozenset(
    ("json", "os", "select", "signal", "socket", "struct", "sys", "time",
     "traceback", "builtins", "base64", "encodings", "codecs", "io", "abc")
)

_FASTPATH_PROBE_LOCK = threading.Lock()
_FASTPATH_PROBE_CACHE: dict[str, Any] = {}
# Resolved once at import so the login-env probe uses an absolute path to the
# interpreter's bash rather than a bare ``"bash"`` lookup (G.EDV.05). Falls back
# to the conventional ``/bin/bash`` when ``shutil.which`` finds nothing.
_BASH_BIN = shutil.which("bash") or "/bin/bash"


def _fastpath_script_enabled() -> bool:
    """Script/wrapper eligibility is a separate opt-in on top of the flag.

    Defaults ON when the fast path itself is on, but can be disabled without
    turning off the (already released) ``python3 -c`` fast path.
    """
    return os.environ.get(FASTPATH_SCRIPT_ENV, "1") == "1"


def _fastpath_split_shell(payload: str) -> list[list[str]] | None:
    """Tokenise a tiny shell payload into ``&&``-separated word lists.

    Returns ``None`` - meaning "not understood, do not touch this" - for
    anything outside the recognised subset. Single quotes are literal;
    double quotes reject ``$``, backtick and backslash so no expansion or
    escape can hide inside them.
    """
    segments: list[list[str]] = []
    words: list[str] = []
    cur: list[str] = []
    has_cur = False
    i = 0
    n = len(payload)
    while i < n:
        ch = payload[i]
        if ch in " \t":
            if has_cur:
                words.append("".join(cur))
                cur, has_cur = [], False
            i += 1
            continue
        if ch == "'":
            end = payload.find("'", i + 1)
            if end == -1:
                return None
            cur.append(payload[i + 1:end])
            has_cur = True
            i = end + 1
            continue
        if ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and payload[j] != '"':
                if payload[j] in '$`\\':
                    return None
                buf.append(payload[j])
                j += 1
            if j >= n:
                return None
            cur.append("".join(buf))
            has_cur = True
            i = j + 1
            continue
        if ch == "&":
            # Exactly ``&&`` at top level is a segment separator; a single
            # ``&`` (background) or anything else is rejected.
            if i + 1 < n and payload[i + 1] == "&":
                if has_cur:
                    words.append("".join(cur))
                    cur, has_cur = [], False
                if not words:
                    return None
                segments.append(words)
                words = []
                i += 2
                continue
            return None
        if ch in _FASTPATH_SAFE_WORD_CHARS:
            cur.append(ch)
            has_cur = True
            i += 1
            continue
        # Any other byte (``| ; < > ( ) $ ` * ? ~ [ ] { } ! # \n \\``) means
        # the payload is outside the recognised subset.
        return None
    if has_cur:
        words.append("".join(cur))
    if words:
        segments.append(words)
    return segments or None


def _fastpath_which(name: str) -> str | None:
    """Resolve a bare interpreter name against PATH (no shutil import)."""
    path_env = os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _fastpath_interp_path(name: str) -> str | None:
    """Resolved path for ``name``, but only if it IS the worker interpreter.

    The fork worker runs the script in its own already-initialised
    interpreter. If ``python`` on PATH is a different build (python2, a venv,
    a wrapper script) the fast path would silently run the script under the
    wrong interpreter, so the request must fall back instead.
    """
    with _FASTPATH_PROBE_LOCK:
        if name in _FASTPATH_PROBE_CACHE:
            return _FASTPATH_PROBE_CACHE[name]
    resolved = _fastpath_which(name)
    verdict: str | None = None
    if resolved:
        try:
            same = os.path.realpath(resolved) == os.path.realpath(sys.executable)
        except OSError:
            same = False
        if same:
            verdict = resolved
        else:
            logger.info(
                "fastpath: %s resolves to %s which is not the worker "
                "interpreter (%s); script fastpath disabled for it",
                name, resolved, sys.executable,
            )
    with _FASTPATH_PROBE_LOCK:
        _FASTPATH_PROBE_CACHE[name] = verdict
    return verdict


def _fastpath_login_env_safe() -> bool:
    """Whether ``bash -lc`` is env-neutral in this image (probed once).

    The wrapper form runs the script through a *login* shell. If this
    sandbox image has ``/etc/profile`` or ``profile.d`` scripts that export
    variables, then ``bash -lc '... python x.py'`` is not equivalent to
    running the script directly, and the wrapper form must not be converted.
    """
    with _FASTPATH_PROBE_LOCK:
        cached = _FASTPATH_PROBE_CACHE.get("login_env_safe")
    if cached is not None:
        return bool(cached)

    safe = False
    try:
        probe = subprocess.run(
            [_BASH_BIN, "-lc",
             "exec python3 -c 'import json,os,sys;"
             "sys.stdout.write(json.dumps(dict(os.environ)))'"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if probe.returncode == 0:
            login_env = json.loads(probe.stdout.decode("utf-8", "replace"))
            base_env = dict(os.environ)
            # The listener fd var is the daemon's own plumbing. It must be
            # dropped from BOTH sides: the daemon always has it set, so the
            # probe shell inherits and re-exports it, and stripping it only
            # from ``base_env`` would make it a permanent phantom difference
            # that disables the wrapper fast path in every real deployment.
            base_env.pop(LISTENER_FD_ENV, None)
            login_env.pop(LISTENER_FD_ENV, None)
            diff = {
                key
                for key in set(login_env) | set(base_env)
                if login_env.get(key) != base_env.get(key)
            }
            unexpected = diff - _FASTPATH_LOGIN_ENV_ALLOWED
            safe = not unexpected
            if unexpected:
                logger.info(
                    "fastpath: login shell alters env %s; wrapper fastpath "
                    "disabled in this image", sorted(unexpected),
                )
    except Exception:  # probe must never break exec handling
        logger.warning("fastpath: login-env probe failed", exc_info=True)
        safe = False

    with _FASTPATH_PROBE_LOCK:
        _FASTPATH_PROBE_CACHE["login_env_safe"] = safe
    return safe


def _fastpath_shadow_conflict(script_dir: str) -> bool:
    """Whether ``script_dir`` shadows a module already loaded in the worker.

    ``python x.py`` puts the script's directory at ``sys.path[0]``, so a
    local ``json.py`` would win over the stdlib. In a warm fork worker the
    stdlib module is already in ``sys.modules`` and would win instead - a
    real semantic difference, so those requests fall back.
    """
    try:
        entries = os.listdir(script_dir)
    except OSError:
        return True  # cannot verify -> do not convert
    for entry in entries:
        if entry.endswith(".py"):
            stem = entry[:-3]
        elif os.path.isfile(os.path.join(script_dir, entry, "__init__.py")):
            stem = entry
        else:
            continue
        if stem in _FASTPATH_RESIDENT_MODULES:
            return True
    return False


def _fastpath_script_plan(
    words: list[str],
    cwd: str,
    interp_token: str,
) -> dict[str, Any] | None:
    """Build the execution plan for ``<python> <script>.py [args]``."""
    script_token = words[1]
    # No interpreter flags: the token right after the interpreter must be the
    # script itself. ``-c``/``-m``/``-u`` etc. are not this shape.
    if script_token.startswith("-") or not script_token.endswith(".py"):
        return None
    interp_path = _fastpath_interp_path(interp_token)
    if interp_path is None:
        return None
    script_path = script_token
    if not os.path.isabs(script_path):
        script_path = os.path.join(cwd, script_path)
    script_path = os.path.normpath(script_path)
    script_dir = os.path.dirname(script_path)
    if _fastpath_shadow_conflict(script_dir):
        return None
    return {
        "mode": "script",
        "path": script_path,
        "dir": script_dir,
        "argv": [script_token] + list(words[2:]),
        "cwd": cwd,
        "interp_name": interp_token,
        "interp_path": interp_path,
    }


def _fastpath_plan(
    command: list[str],
    header: dict[str, Any],
) -> dict[str, Any] | None:
    """Decide how (or whether) ``command`` can run on the fast path.

    Returns a plan dict for the worker, or ``None`` to fall back.
    """
    if not command:
        return None
    base_cwd = header.get("workdir") or os.getcwd()

    # --- shape 1: the ``python[3] -c CODE`` fast path ----------------------
    # The ForkServer worker is itself a warm ``python3 -c`` interpreter: it
    # runs the user's ``-c`` source by ``exec()``-ing it in its own already-
    # initialised process, so the interpreter name does not select a different
    # binary -- the worker IS the interpreter that runs the code. Two rules
    # follow from that, both measured against the real interpreter in-sandbox:
    #
    #   * ``python3 -c`` is equivalent by construction (the worker is the
    #     daemon's ``python3``). ``python -c`` is equivalent only when the
    #     bare ``python`` on PATH resolves to that same interpreter; if
    #     ``python`` were python2 / a venv / a wrapper, the fast path would
    #     silently run its code under python3, so it must fall back (the same
    #     identity check the script path uses). ``python3 -c`` keeps its
    #     released behaviour and is not identity-checked (no regression).
    #   * The worker's ``sys.flags`` and startup are fixed at worker spawn
    #     and cannot be changed per request. Any interpreter flag before
    #     ``-c`` (``-I`` / ``-S`` / ``-E`` / ``-u`` / ``-B`` ...) changes
    #     observable ``sys.flags`` or buffering/startup that the worker
    #     cannot reproduce, so a flagged ``-c`` must not take the code fast
    #     path. (Previously every flag was silently dropped and
    #     the request still hit -- e.g. ``python3 -I -c`` ran with
    #     ``sys.flags.isolated == 0``.)
    #
    # Code mode is therefore exactly the bare ``python[3] -c CODE``: ``-c``
    # must be the first token after the interpreter. A ``-c`` that appears
    # later belongs to a script's argv, not to the interpreter, and a flag
    # before it is not preservable -- both fall through to script mode,
    # which rejects flagged and non-.py shapes of its own accord (so they
    # end up on the normal ``subprocess.Popen`` path).
    if command[0] in ("python3", "python"):
        interp = command[0]
        if len(command) >= 2 and command[1] == "-c":
            if len(command) < 3:
                return None  # bare ``-c`` with no code -> let it error
            # Only the exact ``python[3] -c CODE`` shape (3 tokens)
            # is convertible. Any trailing arg would land in a fresh
            # interpreter's ``sys.argv`` but is currently dropped by the
            # worker's ``-c`` path, so refuse rather than run wrongly.
            if len(command) > 3:
                return None
            if interp == "python" and _fastpath_interp_path("python") is None:
                return None  # ``python`` is not the worker interpreter
            return {"mode": "code", "code": command[2]}
        if not _fastpath_script_enabled():
            return None
        # --- shape 2: direct ``python[3] <script>.py [args]`` --------------
        if len(command) >= 2:
            return _fastpath_script_plan(command, base_cwd, interp)
        return None

    # --- shape 3: the real EDPA wrapper -----------------------------------
    #   bash -lc 'cd <dir> && python <script>.py [args]'
    if command[0] != "bash" or len(command) != 3:
        return None
    if command[1] not in ("-lc", "-c"):
        return None
    if not _fastpath_script_enabled():
        return None
    segments = _fastpath_split_shell(command[2])
    if segments is None or len(segments) > 2:
        return None

    cwd = base_cwd
    if len(segments) == 2:
        head = segments[0]
        # The only prefix command understood is a plain ``cd <dir>``.
        if len(head) != 2 or head[0] != "cd":
            return None
        target = head[1]
        cwd = target if os.path.isabs(target) else os.path.join(base_cwd, target)
        cwd = os.path.normpath(cwd)
        if not os.path.isdir(cwd):
            return None
    tail = segments[-1]
    if len(tail) < 2 or tail[0] not in _FASTPATH_INTERP_NAMES:
        return None
    # A login shell must be env-neutral before the wrapper can be converted.
    if command[1] == "-lc" and not _fastpath_login_env_safe():
        return None
    plan = _fastpath_script_plan(tail, cwd, tail[0])
    if plan is None:
        return None
    # Reproduce the variables the shell itself would have exported.
    plan["env_extra"] = {
        "PWD": cwd,
        "OLDPWD": base_cwd,
        "SHLVL": "0",
        "_": plan["interp_path"],
    }
    return plan


# Source of the persistent in-sandbox ForkServer worker. Self-contained and
# stdlib-only. The worker is a single-threaded interpreter; each request
# ``fork()``s a child that runs the user's ``-c`` code, so the interpreter's
# already-loaded state (stdlib + site) is shared with the child via copy-on-
# write instead of paying a full interpreter cold start per exec.
FORKSERVER_WORKER_SOURCE = r'''
import builtins, json, os, select, signal, socket, struct, sys, time, traceback, types

MARKER = "JIWENBOX_FORK_WORKER"


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _recv_frame(sock, max_size):
    (size,) = struct.unpack(">I", _recv_exact(sock, 4))
    if size > max_size:
        raise ValueError("frame too large")
    return _recv_exact(sock, size)


def _send_frame(sock, payload):
    sock.sendall(struct.pack(">I", len(payload)))
    if payload:
        sock.sendall(payload)


def _send_response(sock, resp):
    _send_frame(sock, json.dumps(resp).encode())


def _flush_std():
    # ``os._exit`` skips Python's stdio flush, so a plain ``print`` in user
    # code would otherwise lose buffered output. Flush explicitly on every
    # child exit path to match ``subprocess`` semantics.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass


def _exec_script_in_child(plan):
    """Run ``python <script>.py [args]`` the way CPython itself would.

    Each step mirrors a property measured against the real interpreter on the
    target image (measured ground truth):

      * ``sys.argv[0]`` keeps the *token as written* ("p.py"), while
        ``__file__`` and traceback filenames are the *absolute* path -- these
        genuinely differ under CPython and scripts observe both.
      * ``sys.path[0]`` is the script's directory (absolute), which is what
        makes same-directory imports work.
      * ``__name__ == "__main__"``, ``__spec__ is None``, ``__package__ is
        None``, ``__loader__`` is a SourceFileLoader, ``__doc__`` is the
        script's own docstring -- all as a real ``__main__`` module.
      * The module object is installed in ``sys.modules["__main__"]`` before
        execution, so ``sys.modules['__main__'].__file__`` and pickling of
        ``__main__``-defined classes behave normally.
      * A missing/unreadable script reproduces CPython's own message and
        exit status 2, using the interpreter name as invoked.

    ``SystemExit`` and tracebacks propagate to ``_run_child``, which already
    implements the exit-code and traceback rules.
    """
    import importlib.machinery
    import io

    script_path = plan["path"]
    interp = plan.get("interp_name") or "python3"
    try:
        with io.open_code(script_path) as fh:
            raw = fh.read()
    except OSError as exc:
        # CPython: "python3: can't open file '/abs/x.py': [Errno 2] ..."
        sys.stderr.write(
            "%s: can't open file '%s': [Errno %d] %s\n"
            % (interp, script_path, exc.errno or 0,
               exc.strerror or "No such file or directory")
        )
        _flush_std()
        os._exit(2)

    if plan.get("env_extra"):
        os.environ.update(plan["env_extra"])

    sys.argv = list(plan["argv"])
    # ``sys.path[0]`` is the script's directory. Replace the worker's own
    # entry rather than inserting, so the child sees exactly one script dir.
    script_dir = plan["dir"]
    if sys.path and sys.path[0] in ("", os.getcwd(), script_dir):
        sys.path[0] = script_dir
    else:
        sys.path.insert(0, script_dir)

    main_mod = types.ModuleType("__main__")
    main_mod.__file__ = script_path
    main_mod.__loader__ = importlib.machinery.SourceFileLoader(
        "__main__", script_path)
    main_mod.__spec__ = None
    main_mod.__package__ = None
    main_mod.__builtins__ = builtins
    sys.modules["__main__"] = main_mod

    try:
        code_obj = compile(raw, script_path, "exec", dont_inherit=False)
    except SyntaxError as exc:
        # CPython prints the offending line + caret with no "Traceback"
        # header when a *script file* fails to compile, then exits 1.
        traceback.print_exception(type(exc), exc, None)
        _flush_std()
        os._exit(1)

    try:
        exec(code_obj, main_mod.__dict__)
    except SystemExit:
        # Exit-code semantics are shared with the ``-c`` path; let
        # ``_run_child`` apply them.
        raise
    except BaseException as exc:
        # Print the traceback as CPython would: starting at the script's own
        # ``<module>`` frame. ``exc.__traceback__`` begins with *this*
        # function's ``exec`` line, which a real ``python s.py`` never shows,
        # so drop that one frame before printing.
        tb = exc.__traceback__
        traceback.print_exception(
            type(exc), exc, tb.tb_next if tb is not None else None)
        _flush_std()
        os._exit(1)


def _run_child(code, stdin_bytes, workdir, env_overrides, timeout, control_fd,
               plan=None):
    """Fork a child that runs ``code`` (or a script); return (rc, out, err).

    Matches the daemon's sync-exec semantics: child runs in its own session
    (setsid), output is captured separately, and timeout yields exit 124.
    Signal deaths are reported as ``-signum`` (same as ``subprocess``).

    When ``plan`` is given it describes a ``python <script>.py`` execution
    instead of a ``-c`` payload; see ``_exec_script_in_child``.
    """
    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()
    r_in, w_in = os.pipe()
    pid = os.fork()
    if pid == 0:
        # --- child: run user code ---
        try:
            for fd in (r_out, r_err, w_in):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.close(control_fd)
            except OSError:
                pass
            os.setsid()
            try:
                os.dup2(w_out, 1)
                os.dup2(w_err, 2)
                os.dup2(r_in, 0)
            finally:
                for fd in (w_out, w_err, r_in):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if workdir:
                try:
                    os.chdir(workdir)
                except OSError:
                    pass
            if env_overrides:
                os.environ.update(env_overrides)
            if plan:
                _exec_script_in_child(plan)
            else:
                # Install a fresh ``__main__`` module the way
                # ``python3 -c`` does, so ``import __main__`` returns the module
                # whose namespace is the code's own globals -- previously it
                # returned the worker's own ``__main__``. This runs in the forked
                # child only (copy-on-write), and ``os._exit`` discards the
                # mutation, so the parent worker is unaffected. The filename is
                # ``<string>`` to match CPython's ``-c`` traceback filename.
                coded = compile(code, "<string>", "exec")
                main_mod = types.ModuleType("__main__")
                main_mod.__builtins__ = builtins
                sys.modules["__main__"] = main_mod
                # ``python3 -c CODE`` sets ``sys.argv`` to
                # ``['-c']``. The worker itself was launched as
                # ``python -c <worker_source> <control_fd>``, so without this
                # the forked child would inherit a ``sys.argv`` carrying the
                # internal control fd. Set it here (in the forked child only;
                # copy-on-write + os._exit discards it, so the parent worker is
                # unaffected) to match CPython ``-c`` semantics. The matcher
                # still rejects ``-c CODE arg1``, so there is no
                # argv tail to preserve.
                sys.argv = ["-c"]
                exec(coded, main_mod.__dict__)
            _flush_std()
            os._exit(0)
        except SystemExit as esc:
            code_val = getattr(esc, "code", None)
            if isinstance(code_val, str):
                # Match ``python3 -c``: a string exit code is printed to
                # stderr and the process exits 1.
                try:
                    sys.stderr.write(code_val + "\n")
                except Exception:
                    pass
                code_val = 1
            _flush_std()
            os._exit(code_val if isinstance(code_val, int) else (0 if code_val is None else 1))
        except BaseException:
            traceback.print_exc()
            _flush_std()
            os._exit(1)
    # --- parent (worker): feed stdin, collect stdout/stderr, enforce timeout ---
    for fd in (w_out, w_err, r_in):
        try:
            os.close(fd)
        except OSError:
            pass
    if stdin_bytes:
        try:
            os.write(w_in, stdin_bytes)
        except OSError:
            pass
    try:
        os.close(w_in)
    except OSError:
        pass

    out = b""
    err = b""
    deadline = time.monotonic() + timeout if timeout is not None else None
    timed_out = False
    stdout_fd = r_out
    stderr_fd = r_err
    # Reap the child exactly once. ``os.waitpid(pid, WNOHANG)`` may be called
    # many times across loop iterations (the child is often reaped before its
    # stdout/stderr pipes reach EOF), but a *second* successful ``waitpid`` on
    # the same pid raises ``ChildProcessError`` (ECHILD, errno 10). Before this
    # guard, that exception escaped ``_run_child`` and surfaced to the client
    # as ``worker error: [Errno 10] No child processes`` on ~0.1% of requests.
    # Once the child is reaped we save its wait status and never call
    # ``waitpid(pid)`` again -- subsequent iterations only drain the pipes.
    child_status = None   # saved wait status after the first successful reap
    child_done = False
    while True:
        fds = [fd for fd in (stdout_fd, stderr_fd) if fd != -1]
        if fds:
            readable, _, _ = select.select(fds, [], [], 0.05)
            for fd in readable:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if fd == stdout_fd:
                        stdout_fd = -1
                    else:
                        stderr_fd = -1
                elif fd == stdout_fd:
                    out += chunk
                else:
                    err += chunk
        if deadline is not None and time.monotonic() >= deadline and not timed_out:
            timed_out = True
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if not child_done:
            # First (and only) reap attempt. ``wpid == 0`` means the child is
            # still running; ``wpid == pid`` means it was just reaped. Either
            # way we never reach this branch again once ``child_done`` is set.
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                # Defensive: a direct fork child we have not reaped should not
                # disappear, but if it does, treat it as gone with an unknown
                # (zero) status rather than propagating ECHILD.
                wpid, status = pid, 0
            if wpid == pid:
                child_done = True
                child_status = status
        pipes_done = stdout_fd == -1 and stderr_fd == -1
        if child_done and pipes_done:
            if timed_out:
                # Shape stderr exactly like the normal subprocess path's
                # TimeoutExpired branch. box-server surfaces only
                # exit_code/stdout/stderr to the caller, so this marker is
                # the sole observable signal that 124 came from the deadline
                # rather than from a script that chose to exit(124) itself.
                marker = b"Command timed out"
                err = err + b"\n" + marker if err else marker
                return 124, out, err
            if child_status is None:
                return 0, out, err
            if os.WIFEXITED(child_status):
                return os.WEXITSTATUS(child_status), out, err
            return -os.WTERMSIG(child_status), out, err
        if not fds and not child_done:
            # No pipe activity and child still running; let the loop re-check
            # the deadline without busy-spinning.
            time.sleep(0.005)


def _worker_main(fd_str):
    fd = int(fd_str)
    sock = socket.socket(family=socket.AF_UNIX, type=socket.SOCK_STREAM, fileno=fd)
    while True:
        try:
            header = json.loads(_recv_frame(sock, 1 << 20))
        except Exception:
            break
        code = header.get("code")
        plan = header.get("plan")
        # Either a ``-c`` payload or a script plan must be present.
        if not isinstance(code, str) and not isinstance(plan, dict):
            _send_response(sock, {"error": "bad_request"})
            continue
        # The daemon always sends a stdin frame (4-byte length prefix +
        # payload, empty frame when no stdin); consume it so the stream
        # stays aligned across requests.
        stdin_bytes = _recv_frame(sock, 64 * 1024 * 1024)
        try:
            exit_code, out, err = _run_child(
                code,
                stdin_bytes,
                header.get("workdir"),
                header.get("env") or {},
                header.get("timeout"),
                fd,
                plan,
            )
            _send_response(sock, {
                "exit_code": exit_code,
                "stdout": out.decode("utf-8", errors="replace"),
                "stderr": err.decode("utf-8", errors="replace"),
            })
        except Exception as exc:  # defensive
            _send_response(sock, {"error": f"worker error: {exc}"})


if __name__ == "__main__":
    _worker_main(sys.argv[1])
'''


class FastPathStats:
    """Minimal thread-safe fast-path observability counters.

    Counters are kept in memory and mirrored (throttled) to a JSON snapshot
    at ``/tmp/fastpath_stats.json`` inside the sandbox so host-side tooling
    and the perf scripts can read them through the normal exec/files path -
    no new API surface. Purely diagnostic; never used for control flow.

    Counting口径 (daemon-side, single source of truth):

    * ``requests``  -- exec requests the daemon actually *judged*, i.e. that
      entered ``_try_fastpath_exec`` (server-marked candidates the admit cap
      let through). NOT total server candidates: server-side admit-cap drops
      never reach the daemon and are not represented here.
    * ``hits``      -- judged requests the fast path executed to a *normal*
      ``exit_code`` response (``"error" not in response``).
    * ``fallbacks`` -- judged requests that did not hit, broken down by
      ``fallback_reasons``: ``not_eligible`` / ``nonempty_stdin`` /
      ``breaker_open`` / ``capacity_busy`` / ``worker_unavailable`` /
      ``spawn_failed`` / ``worker_error`` / ``exec_uncertain``.
      ``exec_uncertain`` is a *post-dispatch* failure: the
      request was sent to a worker and the code may have run, so the daemon
      reports an explicit failure instead of replaying via ``subprocess.Popen``.
      All other reasons are *pre-dispatch* (safe Popen fallback).

    Invariant: ``requests == hits + fallbacks`` (every judged request lands in
    exactly one bucket). ``record_request`` is taken at the *entry* of
    ``_try_fastpath_exec`` (before the eligibility check) so ``not_eligible``
    is counted; ``record_hit`` is only taken on a normal worker response, so
    a ``worker_error`` response is a fallback, never also a hit.

    Throttle / eventual flush: ``write_snapshot`` writes at most once per
    ``FASTPATH_STATS_THROTTLE``; a skipped write sets ``_dirty`` so the next
    in-window record or the 5s idle-reaper wakeup (``flush``) force-writes
    the accumulated counters. A one-off fallback followed by silence is
    therefore never permanently lost.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "requests": 0,
            "hits": 0,
            "fallbacks": 0,
            "fallback_reasons": {},
            "cold_starts": 0,
            "worker_restarts": 0,
            "spawn_failures": 0,
            "active_workers": 0,
            "breaker_state": "closed",
            "breaker_failures": 0,
        }
        self._last_write = 0.0
        self._dirty = False

    def record_request(self) -> None:
        with self._lock:
            self._data["requests"] += 1

    def record_hit(self) -> None:
        with self._lock:
            self._data["hits"] += 1
        # A hit is the terminal state for this request; flush the snapshot
        # (throttled) so steady traffic is visible without per-record I/O.
        self.write_snapshot()

    def record_fallback(self, reason: str) -> None:
        with self._lock:
            self._data["fallbacks"] += 1
            reasons = self._data["fallback_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
        # Every fallback path must reach a flush attempt, otherwise a class
        # of fallback (e.g. ``not_eligible``) can stay invisible in the file
        # when no concurrent hit triggers a write. Throttled write.
        self.write_snapshot()

    def record_cold_start(self) -> None:
        with self._lock:
            self._data["cold_starts"] += 1

    def record_worker_restart(self, count: int = 1) -> None:
        with self._lock:
            self._data["worker_restarts"] += count

    def record_spawn_failure(self) -> None:
        with self._lock:
            self._data["spawn_failures"] += 1

    def set_active(self, n: int) -> None:
        with self._lock:
            self._data["active_workers"] = n

    def set_breaker(self, state: str, failures: int) -> None:
        with self._lock:
            self._data["breaker_state"] = state
            self._data["breaker_failures"] = failures

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def write_snapshot(self, force: bool = False) -> None:
        """Write the throttled JSON snapshot; never raises on failure.

        Within ``FASTPATH_STATS_THROTTLE`` of the last write the snapshot is
        *not* rewritten; instead ``_dirty`` is set so a later in-window record
        or ``flush`` (idle-reaper) picks up the accumulated counters. ``force``
        always writes and clears ``_dirty``.
        """
        now = time.monotonic()
        if not force and (now - self._last_write) < FASTPATH_STATS_THROTTLE:
            self._dirty = True
            return
        try:
            with open(FASTPATH_STATS_PATH, "wb") as fh:
                fh.write(json.dumps(self.snapshot()).encode("utf-8"))
            self._last_write = now
            self._dirty = False
        except Exception:  # pragma: no cover - diagnostics must not break exec
            pass

    def flush(self) -> None:
        """Force-write the snapshot if a throttled write was skipped.

        Called by the idle-reaper on every wakeup so a one-off fallback
        followed by silence does not stay invisible past the throttle window.
        Cheap when there is nothing pending (one lock + flag check).
        """
        with self._lock:
            dirty = self._dirty
        if dirty:
            self.write_snapshot(force=True)


class FastPathUnavailable(RuntimeError):
    """Raised by ``ForkServerPool.submit`` when the fast path is unusable.

    Carries a ``reason`` recorded for observability. Subclasses
    ``RuntimeError`` so existing callers catching ``RuntimeError`` keep
    working unchanged.

    Only raised for *pre-dispatch* failures -- the request never reached a
    worker -- so the caller may safely fall back to ``subprocess.Popen``
    without risk of replaying the command. Post-dispatch failures (the
    request may have run) raise ``FastPathExecUncertain`` instead, a sibling
    (NOT a subclass) so a broad ``except FastPathUnavailable`` cannot swallow
    a no-replay condition into a fallback.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"fork fastpath unavailable: {reason}")
        self.reason = reason


class FastPathExecUncertain(RuntimeError):
    """Raised by ``ForkServerPool.submit`` for a *post-dispatch* failure.

    Dispatch begins the moment ``_send_frame`` starts sending the request
    header to a worker. Any failure from that point on -- a half-sent frame,
    a worker that dies mid-round-trip, a ``_recv_frame`` error (including
    the >1 MiB ``frame too large`` path), a socket timeout, a
    ``json.loads`` error -- means the code may already have run in the
    forked child. Replaying via ``subprocess.Popen`` would risk duplicate
    side effects, so the caller MUST NOT fall back; it reports an explicit
    execution-uncertain failure instead.

    A sibling of ``FastPathUnavailable`` (both subclass ``RuntimeError``
    directly, neither is a subclass of the other) so an ``except
    FastPathUnavailable`` handler cannot accidentally catch this and turn a
    no-replay condition back into a Popen fallback.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"fork fastpath execution uncertain: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class _FastPathRequest:
    """One fast-path code payload or script plan, bundled for the worker.

    G.FNM.03 keeps ``ForkServerPool.submit`` at or below five arguments: the
    per-call inputs travel as a single value object, mirroring the
    ``_DaemonExecCall`` pattern in ``server/runtime/process.py``.
    """

    code: str | None
    stdin_bytes: bytes | None
    workdir: str | None
    env_overrides: dict[str, str] | None
    timeout: float | None
    plan: dict[str, Any] | None = None


class ForkServerPool:
    """Daemon-side pool of persistent in-sandbox ForkServer workers.

    Workers are spawned lazily on the first fast-path request (so an idle
    sandbox holds zero workers) and are direct children of the daemon, i.e.
    they live inside the same bwrap namespace / userns / pidns and inherit
    the same cgroup, seccomp, Landlock and mount envelope. Simple round-robin
    dispatch; no dynamic scaling.

    Concurrency: each worker has its own lock so two workers can service two
    requests concurrently. The pool lock only guards pool state (spawn/prune/
    selection), never a blocking worker round-trip.

    Resilience (all daemon-side, no new privileges):

    * **Auto-recovery**: killed/crashed workers are reaped and respawned on
      the next request; the daemon (PID 1) is never affected. Killing a
      worker only degrades *this sandbox's* fast path - the worker has no
      more privileges than any user child - so this is a same-sandbox
      availability concern, not a security-boundary breach.
    * **Circuit breaker**: after ``FASTPATH_BREAKER_THRESHOLD`` consecutive
      failures (worker death, spawn failure, protocol error) the breaker
      opens and fast-path requests fall straight back to ``/exec`` with no
      worker churn for ``FASTPATH_BREAKER_COOLDOWN`` seconds, then one
      half-open probe decides whether to re-close. This prevents rebuild
      storms.
    * **Idle recycle**: after ``FASTPATH_IDLE_TIMEOUT`` seconds of no fast
      path activity the pool recycles to zero workers (background reaper).
    * **Hard caps**: worker count is clamped to ``FASTPATH_MAX_WORKERS`` so
      a misconfigured override cannot grow the pool without bound.
    """

    def __init__(self) -> None:
        self._workers: list[tuple[subprocess.Popen, socket.socket, threading.Lock]] = []
        self._next = 0
        self._lock = threading.Lock()
        self._breaker_state = "closed"      # closed | open | half_open
        self._breaker_failures = 0
        self._cooldown_until = 0.0
        self._last_activity = time.monotonic()
        self._reaper_started = False
        self._stop_event = threading.Event()
        self.stats = FastPathStats()

    # -- lifecycle ---------------------------------------------------------
    def start_reaper(self) -> None:
        """Start the idle-recycle background thread (called once from main)."""
        if self._reaper_started:
            return
        self._reaper_started = True
        threading.Thread(
            target=self._reaper_loop,
            name="fastpath-idle-reaper",
            daemon=True,
        ).start()

    def _reaper_loop(self) -> None:
        while not self._stop_event.wait(FASTPATH_REAP_INTERVAL):
            # Flush any snapshot write skipped by the throttle so a one-off
            # fallback followed by silence does not stay invisible in the
            # stats file past the throttle window.
            self.stats.flush()
            idle_for = time.monotonic() - self._last_activity
            if idle_for < _fastpath_idle_timeout():
                continue
            self._recycle_idle(idle_for)

    def _recycle_idle(self, idle_for: float) -> None:
        with self._lock:
            if not self._workers:
                return
            # Only recycle when every live worker is idle: no request is
            # in flight and no request is blocked waiting on a worker lock.
            acquired: list[tuple[subprocess.Popen, socket.socket, threading.Lock]] = []
            all_idle = True
            for proc, sock, wlock in self._workers:
                if proc.poll() is not None:
                    continue
                if not wlock.acquire(blocking=False):
                    all_idle = False
                    break
                acquired.append((proc, sock, wlock))
            if not all_idle:
                for _p, _s, wl in acquired:
                    wl.release()
                return
            for proc, sock, _wl in self._workers:
                try:
                    sock.close()
                except OSError:
                    pass
                try:
                    proc.kill()
                except OSError:
                    pass
            for _p, _s, wl in acquired:
                wl.release()
            self._workers = []
            self.stats.set_active(0)
        self.stats.write_snapshot()
        logger.info(
            "fastpath pool idle-recycled to 0 workers after %.0fs idle",
            idle_for,
        )

    # -- breaker helpers (callers hold ``self._lock``) ---------------------
    def _bump_failure_locked(self) -> None:
        self._breaker_failures += 1
        if self._breaker_failures >= FASTPATH_BREAKER_THRESHOLD:
            self._breaker_state = "open"
            self._cooldown_until = time.monotonic() + FASTPATH_BREAKER_COOLDOWN
            self.stats.set_breaker("open", self._breaker_failures)
            logger.warning(
                "fastpath circuit breaker OPEN after %d consecutive failures; "
                "falling back to /exec for %.0fs",
                self._breaker_failures,
                FASTPATH_BREAKER_COOLDOWN,
            )

    def _record_success_locked(self) -> None:
        if self._breaker_failures:
            self._breaker_failures = 0
            self.stats.set_breaker(self._breaker_state, 0)
        if self._breaker_state == "half_open":
            self._breaker_state = "closed"
            self.stats.set_breaker("closed", 0)
            logger.info("fastpath circuit breaker CLOSED (probe succeeded)")

    def _allow_attempt(self) -> bool:
        with self._lock:
            if self._breaker_state != "open":
                return True
            if time.monotonic() >= self._cooldown_until:
                self._breaker_state = "half_open"
                self.stats.set_breaker("half_open", self._breaker_failures)
                logger.info("fastpath circuit breaker HALF-OPEN (allowing probe)")
                return True
            return False

    # -- worker management -------------------------------------------------
    @staticmethod
    def _spawn() -> tuple[subprocess.Popen, socket.socket]:
        parent, child = socket.socketpair()
        worker_env = dict(os.environ)
        worker_env.pop(LISTENER_FD_ENV, None)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                FORKSERVER_WORKER_SOURCE,
                str(child.fileno()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=worker_env,
            close_fds=True,
            pass_fds=[child.fileno()],
            start_new_session=True,
        )
        child.close()
        return proc, parent

    def _prune(self) -> None:
        alive: list[tuple[subprocess.Popen, socket.socket, threading.Lock]] = []
        for proc, sock, lock in self._workers:
            if proc.poll() is None:
                alive.append((proc, sock, lock))
            else:
                try:
                    sock.close()
                except OSError:
                    pass
        self._workers = alive

    def _ensure(self) -> bool:
        """Bring the pool up to target; ``True`` when live workers exist.

        Reaps dead/killed workers (counting restarts) and marks a cold start
        when a request turns an empty pool into a populated one. Must be
        called with ``self._lock`` held.
        """
        was_empty = not bool(self._workers)
        before = len(self._workers)
        self._prune()
        pruned = before - len(self._workers)
        if pruned:
            self.stats.record_worker_restart(pruned)
        target = min(_fastpath_worker_count(), FASTPATH_MAX_WORKERS)
        spawned = 0
        while len(self._workers) < target:
            try:
                proc, sock = self._spawn()
                self._workers.append((proc, sock, threading.Lock()))
                spawned += 1
            except OSError:
                logger.warning("fork fastpath worker spawn failed", exc_info=True)
                self.stats.record_spawn_failure()
                break
        if was_empty and spawned:
            self.stats.record_cold_start()
            logger.info("fastpath cold start: spawned %d worker(s)", spawned)
        self.stats.set_active(len(self._workers))
        return bool(self._workers)

    def _drop(self, proc: subprocess.Popen) -> None:
        # G.NAM.02: filter by the worker's process identity without unpacking
        # the tuple members into single-char names.
        self._workers = [entry for entry in self._workers if entry[0] is not proc]

    def submit(self, request: _FastPathRequest) -> dict[str, Any]:
        """Send one code payload or script plan to a worker and return its JSON response.

        Raises ``FastPathUnavailable`` (a ``RuntimeError``) when the fast
        path cannot be used, so the caller can fall back to the normal
        ``subprocess.Popen`` path.
        """
        self._last_activity = time.monotonic()
        # NOTE: request/hit accounting lives in ``_try_fastpath_exec`` so the
        # ``not_eligible`` fallback (which never reaches ``submit``) is counted
        # and ``requests == hits + fallbacks`` holds. Here we only advance the
        # breaker on a worker round-trip: a *responded* worker resets the
        # failure count even when the response itself carries an internal
        # ``error`` (that case is a ``worker_error`` fallback, not a hit).
        if not self._allow_attempt():
            raise FastPathUnavailable("breaker_open")

        code = request.code
        stdin_bytes = request.stdin_bytes
        workdir = request.workdir
        env_overrides = request.env_overrides
        timeout = request.timeout
        plan = request.plan

        header: dict[str, Any] = {
            "code": code,
            "stdin_size": len(stdin_bytes or b""),
            "timeout": timeout,
        }
        if plan is not None:
            header["plan"] = plan
            # A script plan carries its own cwd (post-``cd``); it must win
            # over the request-level workdir.
            workdir = plan.get("cwd") or workdir
        if workdir:
            header["workdir"] = workdir
        if env_overrides:
            header["env"] = env_overrides

        # Pick a free worker (round-robin, non-blocking acquire). When every
        # live worker is busy, raise ``capacity_busy`` immediately so the daemon
        # falls back to ``subprocess.Popen`` BEFORE any child runs -- no
        # blocking wait, no duplicate execution. ``capacity_busy`` is a
        # capacity signal, not a worker failure, so it must NOT bump the
        # breaker (only real failures -- dead/un-spawnable workers -- bump it).
        # Bounded spins still stop rebuild storms when workers keep dying
        # mid-round.
        spins = 0
        while True:
            spins += 1
            if spins > FASTPATH_MAX_SPINS:
                with self._lock:
                    self._bump_failure_locked()
                raise FastPathUnavailable("worker_unavailable")
            round_exhausted = False
            with self._lock:
                if not self._ensure():
                    self._bump_failure_locked()
                    raise FastPathUnavailable("spawn_failed")
                chosen: tuple[subprocess.Popen, socket.socket, threading.Lock] | None = None
                for _ in range(len(self._workers)):
                    proc, sock, wlock = self._workers[self._next % len(self._workers)]
                    self._next += 1
                    if proc.poll() is not None:
                        self._prune()
                        break  # a worker died; retry the round (chosen stays None)
                    if wlock.acquire(blocking=False):
                        chosen = (proc, sock, wlock)
                        break
                else:
                    # The round completed without breaking: every live worker
                    # was checked and none was free.
                    round_exhausted = True
                if chosen is None and not self._workers:
                    continue  # everything got pruned; re-ensure (respawn)
            if chosen is None:
                if round_exhausted:
                    # All live workers busy -> immediate fallback, no breaker bump.
                    raise FastPathUnavailable("capacity_busy")
                # A worker died mid-round; retry (prune already ran in the lock).
                continue
            if chosen[0].poll() is not None:
                with self._lock:
                    self._prune()
                chosen[2].release()
                continue
            break

        proc, sock, wlock = chosen
        # Once we begin sending the request header to the worker
        # the code may run in the forked child, so any subsequent failure must
        # NOT fall back to ``subprocess.Popen`` (that would replay side effects).
        # ``dispatched`` marks the no-replay boundary. It is set the instant we
        # commit to sending -- just before ``_send_frame`` -- so even a partial
        # send that raises mid-frame is treated as "may have reached the worker"
        # (we cannot prove the worker never read/executed). Only failures before
        # that point (``settimeout``) stay ``FastPathUnavailable`` for a safe
        # Popen fallback. Anything from ``_send_frame`` onward raises
        # ``FastPathExecUncertain`` (sibling, not subclass, of
        # ``FastPathUnavailable``).
        dispatched = False
        try:
            sock.settimeout((timeout or 305.0) + 5.0)
            dispatched = True  # dispatch begins here: about to send the header
            _send_frame(sock, json.dumps(header).encode("utf-8"))
            if stdin_bytes:
                sock.sendall(struct.pack(">I", len(stdin_bytes)))
                sock.sendall(stdin_bytes)
            else:
                sock.sendall(struct.pack(">I", 0))
            resp_bytes = _recv_frame(sock, _FASTPATH_MAX_FRAME)
            response = json.loads(resp_bytes.decode("utf-8"))
        except Exception as exc:  # worker became unusable
            logger.warning("fork fastpath worker failed: %s", exc)
            try:
                sock.close()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
            with self._lock:
                self._drop(proc)
                # A worker that dies mid-request is replaced on the next
                # request; count it as a restart so observability matches the
                # resurrected-worker-prune path below. A real worker failure
                # still bumps the breaker (capacity_busy does not).
                self.stats.record_worker_restart(1)
                self._bump_failure_locked()
            if dispatched:
                # Post-dispatch: the code may have run. Report an explicit
                # execution-uncertain failure; the caller must NOT Popen.
                raise FastPathExecUncertain("post_dispatch_failure") from exc
            # Pre-dispatch: the worker never received a complete header, so a
            # Popen fallback cannot double-execute.
            raise FastPathUnavailable("worker_unavailable") from exc
        else:
            with self._lock:
                self._record_success_locked()
            # ``record_hit`` is taken by ``_try_fastpath_exec`` only when the
            # response is a normal ``exit_code`` result; a response carrying
            # an internal ``error`` is recorded as a ``worker_error`` fallback
            # there instead, so one request is never both a hit and a fallback.
        finally:
            wlock.release()
        return response

    def shutdown(self) -> None:
        self._stop_event.set()
        for proc, sock, _lock in self._workers:
            try:
                sock.close()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
        self._workers = []
        self.stats.set_active(0)
        self.stats.write_snapshot(force=True)


_FORK_POOL = ForkServerPool()


def _try_fastpath_exec(
    conn: socket.socket,
    command: list[str],
    header: dict[str, Any],
    stdin_bytes: bytes,
) -> bool:
    """Route an eligible exec to the ForkServer.

    Eligible shapes: ``python3 [flags] -c CODE`` (as released),
    ``python|python3 <script>.py [args]``, and the real EDPA wrapper
    ``bash -lc 'cd <dir> && python <script>.py [args]'``. Anything else -
    including any shell construct the strict recogniser does not fully
    understand - returns ``False`` and takes the normal path.

    Returns ``True`` when a response was sent (fastpath success or timeout),
    ``False`` when the caller should fall back to ``subprocess.Popen``.
    Records fast-path hits / fallback reasons for observability.

    Accounting: every call counts one ``request`` (taken here, before the
    eligibility check, so ``not_eligible`` is counted). The request then lands
    in exactly one terminal bucket -- ``hits`` (normal ``exit_code`` response)
    or one ``fallbacks`` reason -- so ``requests == hits + fallbacks``.
    """
    _FORK_POOL.stats.record_request()
    # The worker writes stdin to the child synchronously before
    # draining stdout, which deadlocks when the child writes >64KB before
    # reading stdin. The worker I/O state machine does not handle this case,
    # so any request with a non-empty stdin takes the normal Popen path.
    # The real EDPA wrapper carries no stdin, so this does not affect current
    # gains.
    if stdin_bytes:
        _FORK_POOL.stats.record_fallback("nonempty_stdin")
        return False
    try:
        plan = _fastpath_plan(command, header)
    except Exception:  # a recogniser bug must never break exec handling
        logger.warning("fastpath plan failed; falling back", exc_info=True)
        plan = None
    if plan is None:
        _FORK_POOL.stats.record_fallback("not_eligible")
        return False

    if plan["mode"] == "code":
        source, script_plan = plan["code"], None
    else:
        source, script_plan = None, plan

    try:
        response = _FORK_POOL.submit(
            _FastPathRequest(
                code=source,
                stdin_bytes=stdin_bytes,
                workdir=header.get("workdir"),
                env_overrides=header.get("env"),
                timeout=header.get("timeout"),
                plan=script_plan,
            )
        )
    except FastPathUnavailable as exc:
        # Pre-dispatch: the request never reached a worker, so falling back to
        # ``subprocess.Popen`` cannot double-execute the command.
        _FORK_POOL.stats.record_fallback(exc.reason)
        return False
    except FastPathExecUncertain:
        # The request was dispatched -- the code may already have
        # run in the forked child. Replaying via ``subprocess.Popen`` would
        # risk duplicate side effects, so report an explicit execution-uncertain
        # failure and tell the caller a response was sent (it must NOT Popen).
        # ``FastPathExecUncertain`` is a sibling of ``FastPathUnavailable`` (not
        # a subclass), so this handler is reached independently of the one above.
        _FORK_POOL.stats.record_fallback("exec_uncertain")
        _send_response(
            conn,
            _exec_response(
                exit_code=1,
                stderr="fastpath execution status uncertain; command not re-run",
                started=False,
                error="fastpath_exec_uncertain",
            ),
        )
        return True
    if "error" in response:
        # The worker responded but hit an internal error (``_run_child``
        # defensive except) -- fastpath attempted and failed to run the code,
        # so this is a fallback, not a hit.
        _FORK_POOL.stats.record_fallback("worker_error")
        _send_response(
            conn,
            _exec_response(
                exit_code=1,
                stderr=response.get("stderr") or response["error"],
                started=False,
                error="fastpath_internal",
            ),
        )
        return True
    _FORK_POOL.stats.record_hit()
    _send_response(
        conn,
        _exec_response(
            exit_code=int(response.get("exit_code", 1)),
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
        ),
    )
    return True


def _fastpath_shutdown() -> None:
    _FORK_POOL.shutdown()


def _os_error_response(exc: OSError, fallback: str = "io_error") -> dict[str, Any]:
    """Build a JSON-friendly description of an ``OSError`` from a file op."""
    return {
        "v": PROTOCOL_VERSION,
        "ok": False,
        "error": fallback,
        "errno": exc.errno or 0,
        "stderr": exc.strerror or str(exc),
    }


def _handle_write_file(conn: socket.socket, header: dict[str, Any]) -> None:
    """Write a file on behalf of box-server, no child process involved.

    The daemon already runs inside the sandbox PID/mount/user namespaces
    with the policy uid/gid, Landlock ruleset, and seccomp filter applied,
    so doing this in-process is exactly equivalent to a sandboxed
    ``cat > path``: the same paths are reachable, the same uid owns the
    resulting file, and the same syscall filter is enforced. The win is
    that we avoid forking ``bash`` for every upload.
    """
    try:
        path = header.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("write_file request missing 'path'")
        content_size = int(header.get("content_size") or 0)
        if content_size < 0 or content_size > MAX_FILE_BYTES:
            raise ValueError(f"invalid content_size {content_size}")
        mkdir_parents = bool(header.get("mkdir_parents", True))
        mode = header.get("mode")
        if mode is not None and not isinstance(mode, int):
            raise ValueError("mode must be an integer")
    except ValueError as exc:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": str(exc),
            },
        )
        return

    try:
        content = _recv_exact(conn, content_size) if content_size else b""
    except ConnectionError as exc:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": f"truncated write_file payload: {exc}",
            },
        )
        return

    parent = os.path.dirname(path) or "/"
    if mkdir_parents and parent not in ("", "/"):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            _send_response(conn, _os_error_response(exc, "mkdir_failed"))
            return

    # Open with O_NOFOLLOW so a pre-existing symlink at ``path`` (which a
    # malicious user payload could have planted before the upload arrived)
    # cannot redirect the write to an attacker-chosen location. The link
    # would still be subject to Landlock, but refusing it outright keeps
    # the IPC fast path's behaviour aligned with what the previous
    # ``cat > $target`` pipeline produced.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode if mode is not None else 0o644)
    except OSError as exc:
        _send_response(conn, _os_error_response(exc, "open_failed"))
        return
    try:
        try:
            if content:
                view = memoryview(content)
                offset = 0
                while offset < len(view):
                    written = os.write(fd, view[offset:])
                    if written <= 0:
                        raise OSError(errno.EIO, "short write")
                    offset += written
        except OSError as exc:
            _send_response(conn, _os_error_response(exc, "write_failed"))
            return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    _send_response(conn, {"v": PROTOCOL_VERSION, "ok": True})


def _handle_read_file(conn: socket.socket, header: dict[str, Any]) -> None:
    """Read a file in-process and stream it back as a single frame.

    Matches the safety properties of the previous ``base64 -w 0 -- $path``
    helper but without a ``bash`` cold start. The header is sent first
    with ``content_size``; the body frame follows so binary content
    survives intact (no base64 round-trip, no ``replace`` decoding).
    """
    path = header.get("path")
    if not isinstance(path, str) or not path:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "read_file request missing 'path'",
            },
        )
        return

    try:
        if os.path.islink(path):
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "is_symlink",
                    "errno": errno.ELOOP,
                    "stderr": f"refusing to read symlink {path!r}",
                },
            )
            return
        try:
            stat = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            _send_response(conn, _os_error_response(exc, "not_found"))
            return
        except IsADirectoryError as exc:
            _send_response(conn, _os_error_response(exc, "is_directory"))
            return
        if os.path.isdir(path):
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "is_directory",
                    "errno": errno.EISDIR,
                    "stderr": f"{path!r} is a directory",
                },
            )
            return
        if stat.st_size > MAX_FILE_BYTES:
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "too_large",
                    "stderr": (
                        f"file size {stat.st_size} exceeds limit "
                        f"{MAX_FILE_BYTES}"
                    ),
                },
            )
            return
        try:
            # ``mode`` is required by G.FIO.01 even though the kernel
            # ignores it without ``O_CREAT``; a placeholder of ``0o600``
            # documents the (would-be) least-privilege permission.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            _send_response(conn, _os_error_response(exc, "open_failed"))
            return
        try:
            chunks: list[bytes] = []
            remaining = stat.st_size
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    except OSError as exc:
        _send_response(conn, _os_error_response(exc, "read_failed"))
        return

    _send_response(
        conn,
        {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "content_size": len(content),
        },
    )
    _send_frame(conn, content)


def _list_dir_entries(
    root: str,
    *,
    recursive: bool,
    max_depth: int | None,
    include_files: bool,
    include_dirs: bool,
) -> list[dict[str, Any]]:
    """Build the ``items`` payload for a list_dir response."""
    items: list[dict[str, Any]] = []
    pending: list[tuple[str, int]] = [(root, 0)]
    while pending:
        current, depth = pending.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel_depth = depth + 1
                if max_depth is not None and rel_depth > max_depth:
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and recursive:
                    pending.append((entry.path, rel_depth))
                if is_dir and not include_dirs:
                    continue
                if not is_dir and not include_files:
                    continue
                items.append({
                    "name": entry.name,
                    "path": entry.path,
                    "size": 0 if is_dir else stat.st_size,
                    "is_directory": is_dir,
                    "modified_time": datetime.datetime.fromtimestamp(
                        stat.st_mtime,
                    ).isoformat(),
                    "type": (
                        None
                        if is_dir
                        else (os.path.splitext(entry.name)[1] or None)
                    ),
                })
    items.sort(key=lambda item: item["path"])
    return items


def _handle_list_dir(conn: socket.socket, header: dict[str, Any]) -> None:
    """List a directory tree from the daemon process."""
    path = header.get("path")
    if not isinstance(path, str) or not path:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "list_dir request missing 'path'",
            },
        )
        return
    recursive = bool(header.get("recursive", False))
    raw_max_depth = header.get("max_depth")
    if raw_max_depth is None:
        max_depth: int | None = None
    elif isinstance(raw_max_depth, int) and raw_max_depth >= 0:
        max_depth = raw_max_depth
    else:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "max_depth must be a non-negative integer",
            },
        )
        return
    include_files = bool(header.get("include_files", True))
    include_dirs = bool(header.get("include_dirs", True))

    if not os.path.exists(path):
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "not_found",
                "errno": errno.ENOENT,
                "stderr": f"path {path!r} does not exist",
            },
        )
        return
    if not os.path.isdir(path):
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "not_a_directory",
                "errno": errno.ENOTDIR,
                "stderr": f"path {path!r} is not a directory",
            },
        )
        return

    try:
        items = _list_dir_entries(
            path,
            recursive=recursive,
            max_depth=max_depth,
            include_files=include_files,
            include_dirs=include_dirs,
        )
    except OSError as exc:
        _send_response(conn, _os_error_response(exc, "list_failed"))
        return

    _send_response(
        conn,
        {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "items": items,
        },
    )


@dataclass
class _BgJob:
    job_id: str
    command: list[str]
    proc: subprocess.Popen


_bg_jobs: dict[str, _BgJob | object] = {}
_bg_jobs_lock = threading.Lock()
_BG_JOB_RESERVED = object()


def _try_reserve_bg_job(job_id: str) -> bool:
    with _bg_jobs_lock:
        if job_id in _bg_jobs:
            return False
        _bg_jobs[job_id] = _BG_JOB_RESERVED
        return True


def _release_bg_job_reservation(job_id: str) -> None:
    with _bg_jobs_lock:
        if _bg_jobs.get(job_id) is _BG_JOB_RESERVED:
            del _bg_jobs[job_id]


def _commit_bg_job(job_id: str, job: _BgJob) -> None:
    with _bg_jobs_lock:
        _bg_jobs[job_id] = job


def _sync_bg_job(job: _BgJob) -> None:
    if job.proc.returncode is None:
        job.proc.poll()


def _bg_job_response(
    *,
    ok: bool,
    job_id: str,
    job: _BgJob | None = None,
    started: bool = True,
    error: str | None = None,
    stderr: str = "",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "ok": ok,
        "job_id": job_id,
        "started": started,
    }
    if error:
        response["error"] = error
    if stderr:
        response["stderr"] = stderr
    if job is not None:
        _sync_bg_job(job)
        response["pid"] = job.proc.pid
        response["running"] = job.proc.returncode is None
        response["exit_code"] = job.proc.returncode
    return response


def _handle_exec_background(conn: socket.socket, header: dict[str, Any]) -> None:
    try:
        job_id = header.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("exec_background request missing 'job_id'")
        job_id = job_id.strip()

        command = header.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("exec_background request missing 'command'")
        command = _stringify_command(command)

        env_override = _normalize_env(header.get("env"))
        workdir = header.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        stdin_size = int(header.get("stdin_size") or 0)
        if stdin_size < 0 or stdin_size > MAX_STDIN_BYTES:
            raise ValueError(f"invalid stdin_size {stdin_size}")

        stdin_bytes = _recv_exact(conn, stdin_size) if stdin_size else b""

        if not _try_reserve_bg_job(job_id):
            _send_response(
                conn,
                _bg_job_response(
                    ok=False,
                    job_id=job_id,
                    started=False,
                    error="job_exists",
                    stderr=f"background job {job_id!r} already exists",
                ),
            )
            return

        reserved = True
        try:
            merged_env = build_child_env(env_override)

            # Background jobs discard stdout/stderr; use ``exec`` when output
            # must be captured.
            proc_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE if stdin_size else subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": merged_env,
                "close_fds": True,
                "start_new_session": True,
            }
            if workdir:
                proc_kwargs["cwd"] = workdir

            try:
                proc = subprocess.Popen(command, **proc_kwargs)
                if stdin_size and proc.stdin is not None:
                    proc.stdin.write(stdin_bytes)
                    proc.stdin.close()
            except OSError as exc:
                _send_response(
                    conn,
                    _bg_job_response(
                        ok=False,
                        job_id=job_id,
                        started=False,
                        error="spawn_failed",
                        stderr=f"failed to spawn background command: {exc}",
                    ),
                )
                return

            job = _BgJob(
                job_id=job_id,
                command=command,
                proc=proc,
            )
            _commit_bg_job(job_id, job)
            reserved = False

            _send_response(conn, _bg_job_response(ok=True, job_id=job_id, job=job))
        finally:
            if reserved:
                _release_bg_job_reservation(job_id)
    except (ValueError, ConnectionError) as exc:
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id=str(header.get("job_id") or ""),
                started=False,
                error="bad_request",
                stderr=str(exc),
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Unhandled error while handling exec_background request")
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id=str(header.get("job_id") or ""),
                started=False,
                error="internal",
                stderr=f"daemon internal error: {exc}",
            ),
        )


def _handle_bg_status(conn: socket.socket, header: dict[str, Any]) -> None:
    job_id = header.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id="",
                started=False,
                error="bad_request",
                stderr="bg_status request missing 'job_id'",
            ),
        )
        return
    job_id = job_id.strip()
    with _bg_jobs_lock:
        job = _bg_jobs.get(job_id)
    if job is None or job is _BG_JOB_RESERVED:
        _send_response(
            conn,
            _bg_job_response(
                ok=False,
                job_id=job_id,
                started=False,
                error="not_found",
                stderr=f"background job {job_id!r} not found",
            ),
        )
        return
    _send_response(conn, _bg_job_response(ok=True, job_id=job_id, job=job))


def _handle_bg_kill(conn: socket.socket, header: dict[str, Any]) -> None:
    job_id = header.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "bg_kill request missing 'job_id'",
            },
        )
        return
    job_id = job_id.strip()
    signum = header.get("signum", 15)
    if not isinstance(signum, int):
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "error": "bad_request",
                "stderr": "signum must be an integer",
            },
        )
        return

    with _bg_jobs_lock:
        job = _bg_jobs.get(job_id)
    if job is None or job is _BG_JOB_RESERVED:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": False,
                "job_id": job_id,
                "killed": False,
                "reason": "not_found",
            },
        )
        return

    _sync_bg_job(job)
    if job.proc.returncode is not None:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "already_exited",
                "exit_code": job.proc.returncode,
            },
        )
        return

    try:
        job.proc.send_signal(signum)
    except ProcessLookupError:
        _sync_bg_job(job)
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "already_exited",
                "exit_code": job.proc.returncode,
            },
        )
        return
    except PermissionError:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "permission_denied",
                "exit_code": job.proc.returncode,
            },
        )
        return
    except OSError:
        _send_response(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "job_id": job_id,
                "killed": False,
                "reason": "permission_denied",
                "exit_code": job.proc.returncode,
            },
        )
        return

    _sync_bg_job(job)
    _send_response(
        conn,
        {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "job_id": job_id,
            "killed": True,
            "reason": "ok",
            "exit_code": job.proc.returncode,
        },
    )


def _handle_connection(conn: socket.socket, state: DaemonState) -> None:
    state.begin_request()
    try:
        try:
            header_bytes = _recv_frame(conn, MAX_HEADER_BYTES)
        except OSError:
            # ``OSError`` already covers ``ConnectionError`` (G.ERR.09).
            return
        try:
            header = json.loads(header_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "bad_request",
                    "stderr": f"invalid request header: {exc}",
                },
            )
            return

        request_type = header.get("type")
        if request_type == REQUEST_TYPE_EXEC:
            _handle_exec(conn, header, state)
        elif request_type == REQUEST_TYPE_SHUTDOWN:
            _send_response(
                conn,
                {"v": PROTOCOL_VERSION, "ok": True, "type": "shutdown_ack"},
            )
            state.shutdown_event.set()
        elif request_type == REQUEST_TYPE_WRITE_FILE:
            _handle_write_file(conn, header)
        elif request_type == REQUEST_TYPE_READ_FILE:
            _handle_read_file(conn, header)
        elif request_type == REQUEST_TYPE_LIST_DIR:
            _handle_list_dir(conn, header)
        elif request_type == REQUEST_TYPE_EXEC_BACKGROUND:
            _handle_exec_background(conn, header)
        elif request_type == REQUEST_TYPE_BG_STATUS:
            _handle_bg_status(conn, header)
        elif request_type == REQUEST_TYPE_BG_KILL:
            _handle_bg_kill(conn, header)
        else:
            _send_response(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": "unknown_request_type",
                    "stderr": f"unknown request type: {request_type!r}",
                },
            )
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
        state.end_request()


@contextlib.contextmanager
def _adopted_listener() -> Iterator[socket.socket]:
    """Recover the host-bound listener fd that box-server passed in.

    Box-server creates the Unix listening socket on its own filesystem and
    passes the resulting fd to bubblewrap via ``subprocess.Popen(pass_fds=...)``.
    Bubblewrap's user command path never closes arbitrary inherited fds, so
    by the time the daemon runs, the fd is already in our process and
    ready to ``accept()``. We wrap it in a Python socket object so the
    standard library accept loop works without re-binding (which Landlock
    would forbid, since the launcher applies the policy filesystem ruleset
    before exec'ing the daemon).

    Exposed as a context manager so resource acquisition (wrapping the
    pre-bound fd in a Python socket object) and release (closing that
    socket) live in the same lexical scope, satisfying the resource-pair
    requirement (G.PRM.03).
    """
    raw = os.environ.get(LISTENER_FD_ENV)
    if raw is None:
        raise RuntimeError(
            f"{LISTENER_FD_ENV} is not set; box-server must hand the daemon "
            "a pre-bound control listener fd",
        )
    try:
        fd = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{LISTENER_FD_ENV}={raw!r} is not an integer",
        ) from exc

    listener = socket.socket(
        family=socket.AF_UNIX,
        type=socket.SOCK_STREAM,
        fileno=fd,
    )
    try:
        listener.settimeout(ACCEPT_TIMEOUT_SECONDS)
        yield listener
    finally:
        try:
            listener.close()
        except OSError:
            pass


def _accept_loop(listener: socket.socket, state: DaemonState) -> None:
    while not state.shutdown_event.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError as exc:
            if exc.errno == errno.EBADF:
                return
            logger.warning("accept() failed: %s", exc)
            continue
        thread = threading.Thread(
            target=_handle_connection,
            args=(conn, state),
            name="jiuwenbox-daemon-worker",
            daemon=True,
        )
        thread.start()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = DaemonState()
    if _fastpath_enabled():
        # Idle-recycle background thread; runs only when the feature is on.
        _FORK_POOL.start_reaper()
    try:
        with _adopted_listener() as listener:
            logger.info(
                "sandbox daemon adopted listener fd; entering accept loop",
            )
            try:
                _accept_loop(listener, state)
            finally:
                # Give in-flight requests a brief window to complete cleanly
                # before the context manager closes the listener.
                state.wait_drain(SHUTDOWN_DRAIN_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    _fastpath_shutdown()
    logger.info("sandbox daemon exited cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
