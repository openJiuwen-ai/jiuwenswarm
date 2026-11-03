# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""In-sandbox Landlock launcher.

This module is executed inside bubblewrap after mounts/namespaces are in place.
It pre-reads the in-sandbox daemon script, applies Landlock to itself, and
then runs the daemon code in the same Python process via ``compile`` + ``exec``.
No further ``execve`` happens between Landlock setup and the daemon, so the
daemon (and every child it spawns) inherits the Landlock ruleset directly
without ever needing the daemon script's on-disk path to remain readable.

This is what lets us keep ``/jiuwenbox`` *outside* the Landlock allowlist:
only the launcher (loaded before Landlock) and the daemon source (read
into memory before Landlock) need access to that subtree. After Landlock
applies, user code spawned by the daemon cannot read ``/jiuwenbox`` at
all, which is the property
``test_landlock_rules_allow_policy_paths_and_deny_other_mounted_paths``
and the runtime-script integrity tests rely on. The mirror invariant -
that user policies cannot punch ``/jiuwenbox`` back into the Landlock
allowlist via ``read_only`` / ``read_write`` / ``bind_mounts`` etc. -
is enforced by ``PolicyEngine._RESERVED_SANDBOX_PATHS``.

This module deliberately depends on the standard library only so the in-sandbox
Python interpreter does not have to load the rest of jiuwenbox before launching
the daemon. That keeps per-sandbox cold-start overhead low.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import marshal
import os
import sys
import time
import types

LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

logger = logging.getLogger("jiuwenbox.landlock_launcher")

# Kept in sync with ``server/runtime/process.py``. The launcher is copied
# into the sandbox and must stay standard-library-only, so it cannot import
# those names from the server package.
DROP_UID_ENV = "JIUWENBOX_DROP_UID"
DROP_GID_ENV = "JIUWENBOX_DROP_GID"
DAEMON_CODE_FD_ENV = "JIUWENBOX_DAEMON_CODE_FD"
PERF_CHILD_ENV = "JIUWENBOX_PERF_CHILD"
PERF_MARK_PREFIX = "JIUWENBOX_PERF"
# Set by the warm pool. The launcher loads the daemon, parks, and applies
# the real uid and Landlock only after the server writes a commit.
WARM_COMMIT_FD_ENV = "JIUWENBOX_WARM_COMMIT_FD"
WARM_PARKED_MARKER = "JIUWENBOX_WARM_PARKED"

READ_FILE = 1 << 2
READ_DIR = 1 << 3
EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13
TRUNCATE = 1 << 14

BASE_READ_ONLY_ACCESS = READ_FILE | READ_DIR | EXECUTE
BASE_READ_WRITE_ACCESS = (
    BASE_READ_ONLY_ACCESS
    | WRITE_FILE
    | REMOVE_DIR
    | REMOVE_FILE
    | MAKE_CHAR
    | MAKE_DIR
    | MAKE_REG
    | MAKE_SOCK
    | MAKE_FIFO
    | MAKE_BLOCK
    | MAKE_SYM
)
ABI2_ACCESS = REFER
ABI3_ACCESS = TRUNCATE


def _access_masks(abi: int) -> tuple[int, int]:
    read_only = BASE_READ_ONLY_ACCESS
    read_write = BASE_READ_WRITE_ACCESS
    if abi >= 2:
        read_write |= ABI2_ACCESS
    if abi >= 3:
        read_write |= ABI3_ACCESS
    return read_only, read_write


libc = ctypes.CDLL("libc.so.6", use_errno=True)
syscall = libc.syscall
syscall.restype = ctypes.c_long
prctl = libc.prctl
prctl.restype = ctypes.c_int


class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class LandlockHardRequirementError(Exception):
    """Raised when hard-required Landlock setup cannot continue."""


def _syscall(nr: int, *args: int) -> int:
    return syscall(nr, *[ctypes.c_long(arg) for arg in args])


def _decode_payload(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value.encode()).decode())


def _detect_abi() -> int:
    return max(_syscall(LANDLOCK_CREATE_RULESET, 0, 0, 1), 0)


def _fail_or_continue(payload: dict, message: str) -> bool:
    if payload["compatibility"] == "hard_requirement":
        logger.error("%s", message)
        raise LandlockHardRequirementError(message)
    return False


def _rule_anchor_path(path: str) -> str:
    """Landlock path-beneath rules must be anchored on a directory fd.

    For file mounts such as /etc/resolv.conf, fall back to the parent
    directory (e.g. /etc) so rule installation remains valid.
    """
    if os.path.isdir(path):
        return path

    normalized = path.rstrip("/") or "/"
    parent = os.path.dirname(normalized) or "/"
    return parent


def _add_rule(ruleset_fd: int, path: str, access: int) -> None:
    if not os.path.exists(path):
        return

    anchor_path = _rule_anchor_path(path)
    if not os.path.exists(anchor_path):
        return

    fd = os.open(anchor_path, os.O_PATH | os.O_CLOEXEC, 0o600)
    try:
        rule = LandlockPathBeneathAttr()
        rule.allowed_access = access
        rule.parent_fd = fd
        ret = _syscall(
            LANDLOCK_ADD_RULE,
            ruleset_fd,
            LANDLOCK_RULE_PATH_BENEATH,
            ctypes.addressof(rule),
            0,
        )
        if ret < 0:
            raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {anchor_path}")
    finally:
        os.close(fd)


def apply_landlock(payload: dict) -> None:
    if payload["compatibility"] == "disabled":
        return

    abi = _detect_abi()
    if abi <= 0:
        _fail_or_continue(payload, "Landlock is not supported")
        return
    read_only_access, read_write_access = _access_masks(abi)

    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _fail_or_continue(payload, "Failed to set no_new_privs")
        return

    attr = LandlockRulesetAttr()
    attr.handled_access_fs = read_write_access
    ruleset_fd = _syscall(
        LANDLOCK_CREATE_RULESET,
        ctypes.addressof(attr),
        ctypes.sizeof(attr),
        0,
    )
    if ruleset_fd < 0:
        _fail_or_continue(payload, "landlock_create_ruleset failed")
        return

    try:
        try:
            for path in payload["read_only"]:
                _add_rule(ruleset_fd, path, read_only_access)
            for path in payload["read_write"]:
                _add_rule(ruleset_fd, path, read_write_access)
        except OSError as exc:
            _fail_or_continue(payload, str(exc))
            return

        if _syscall(LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            _fail_or_continue(payload, "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)


def _perf_enabled() -> bool:
    return os.environ.get(PERF_CHILD_ENV) == "1"


def _write_stream(stream: object, text: str) -> None:
    """Write one protocol line. Logging would add a prefix the parent cannot parse."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode())
        buffer.flush()
        return
    write = getattr(stream, "write")
    write(text)
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def _mark(step: str, started: float) -> None:
    """Write one child-side duration. No-op unless the server asked for it."""
    if not _perf_enabled():
        return
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    _write_stream(sys.stderr, f"{PERF_MARK_PREFIX}\t{step}\t{elapsed_ms:.3f}\n")


def _drop_to(uid: int, gid: int) -> bool:
    """setgid then setuid. Failure is logged and reported to the caller."""
    try:
        os.setgroups([])
    except OSError:
        pass
    try:
        os.setgid(gid)
        os.setuid(uid)
    except OSError as exc:
        logger.error("Failed to drop privileges to uid %s gid %s: %s", uid, gid, exc)
        return False
    return True


def _drop_privileges() -> bool:
    """Drop to the host uid/gid before reading the daemon or locking Landlock.

    The server sets these variables only when it is root and the sandbox
    reuses the host user namespace. Doing the drop here avoids a second
    ``python -S -c`` process inside bwrap. A warm-pool process leaves the
    variables unset and drops later, from the commit message.
    """
    uid_raw = os.environ.pop(DROP_UID_ENV, None)
    gid_raw = os.environ.pop(DROP_GID_ENV, None)
    if uid_raw is None and gid_raw is None:
        return True
    if uid_raw is None or gid_raw is None:
        logger.error(
            "Privilege drop requires both %s and %s",
            DROP_UID_ENV,
            DROP_GID_ENV,
        )
        return False
    try:
        uid = int(uid_raw)
        gid = int(gid_raw)
    except ValueError:
        logger.error("Invalid privilege drop uid/gid: %r %r", uid_raw, gid_raw)
        return False
    return _drop_to(uid, gid)


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _load_daemon_code_from_fd() -> types.CodeType | None:
    raw_fd = os.environ.pop(DAEMON_CODE_FD_ENV, None)
    if raw_fd is None:
        return None
    try:
        fd = int(raw_fd)
    except ValueError:
        logger.error("Invalid %s=%r", DAEMON_CODE_FD_ENV, raw_fd)
        return None
    try:
        blob = _read_fd(fd)
    except OSError as exc:
        logger.error("Failed to read daemon bytecode fd %s: %s", fd, exc)
        return None
    finally:
        os.close(fd)
    try:
        code = marshal.loads(blob)
    except (ValueError, EOFError, TypeError) as exc:
        logger.error("Failed to unmarshal daemon bytecode: %s", exc)
        return None
    if not isinstance(code, types.CodeType):
        logger.error("Daemon bytecode fd did not contain a code object")
        return None
    return code


def _load_daemon_code_from_path(daemon_path: str) -> types.CodeType | None:
    try:
        with open(daemon_path, "rb") as fh:
            source = fh.read()
    except OSError as exc:
        logger.error("Failed to read daemon script %s: %s", daemon_path, exc)
        return None
    try:
        compiled = compile(source, daemon_path, "exec")
    except SyntaxError as exc:
        logger.error("Failed to compile daemon script %s: %s", daemon_path, exc)
        return None
    if not isinstance(compiled, types.CodeType):
        return None
    return compiled


def _load_daemon_code(daemon_path: str) -> types.CodeType | None:
    """Load the server-supplied bytecode, or compile the bind-mounted script."""
    code = _load_daemon_code_from_fd()
    if code is not None:
        return code
    return _load_daemon_code_from_path(daemon_path)


def _read_warm_commit() -> dict:
    """Read the checkout message and close the inherited pipe."""
    raw_fd = os.environ.pop(WARM_COMMIT_FD_ENV, None)
    if raw_fd is None:
        logger.error("Warm launcher is missing %s", WARM_COMMIT_FD_ENV)
        return {}
    try:
        fd = int(raw_fd)
    except ValueError:
        logger.error("Invalid %s=%r", WARM_COMMIT_FD_ENV, raw_fd)
        return {}
    try:
        blob = _read_fd(fd)
    except OSError as exc:
        logger.error("Failed to read warm commit: %s", exc)
        return {}
    finally:
        os.close(fd)
    try:
        message = json.loads(blob.decode())
    except (UnicodeError, json.JSONDecodeError) as exc:
        logger.error("Warm commit is not JSON: %s", exc)
        return {}
    if not isinstance(message, dict):
        logger.error("Warm commit must be a JSON object")
        return {}
    return message


def _run_warm_daemon(daemon_path: str) -> int:
    """Load the daemon, park, then apply the checkout identity and Landlock.

    The warm pool does not know ``filesystem_policy`` or ``run_as`` until a
    sandbox is claimed. Module import happens before the park so that work
    stays off the checkout path. ``main`` runs only after Landlock.
    """
    started = time.perf_counter()
    compiled = _load_daemon_code(daemon_path)
    _mark("daemon.load", started)
    if compiled is None:
        return 2

    module_name = "jiuwenbox.supervisor.sandbox_daemon_inproc"
    module = types.ModuleType(module_name)
    module.__file__ = daemon_path
    sys.modules[module_name] = module
    daemon_globals = module.__dict__
    daemon_globals["__name__"] = module_name
    daemon_globals["__file__"] = daemon_path
    started = time.perf_counter()
    exec(compiled, daemon_globals)
    _mark("daemon.enter_main", started)

    _write_stream(sys.stderr, WARM_PARKED_MARKER + "\n")
    commit = _read_warm_commit()
    if not commit:
        return 2

    uid = commit.get("uid")
    gid = commit.get("gid")
    started = time.perf_counter()
    if uid is not None or gid is not None:
        if not isinstance(uid, int) or not isinstance(gid, int):
            logger.error("Warm commit uid/gid must both be ints")
            return 2
        if not _drop_to(uid, gid):
            return 2
    _mark("daemon.privdrop", started)

    try:
        payload = _decode_payload(str(commit.get("landlock_b64") or ""))
    except (ValueError, TypeError) as exc:
        logger.error("Warm commit Landlock payload is invalid: %s", exc)
        return 2
    try:
        started = time.perf_counter()
        apply_landlock(payload)
        _mark("daemon.landlock", started)
    except LandlockHardRequirementError:
        return 126

    extra_env = commit.get("env") or {}
    if isinstance(extra_env, dict):
        for key, value in extra_env.items():
            os.environ[str(key)] = str(value)
    os.environ.pop(PERF_CHILD_ENV, None)

    daemon_main = daemon_globals.get("main")
    if not callable(daemon_main):
        logger.error(
            "Daemon script %s does not expose a callable ``main`` symbol",
            daemon_path,
        )
        return 2
    result = daemon_main()
    try:
        return int(result) if result is not None else 0
    except (TypeError, ValueError):
        return 1


def _run_daemon_in_process(daemon_path: str, payload: dict) -> int:
    """Apply Landlock and run the daemon script in this Python process.

    Privilege drop and the daemon bytecode are both taken before
    ``apply_landlock``, so ``/jiuwenbox`` can be locked away immediately
    afterward. Running the daemon in-process (no second ``execve``) is
    what lets the daemon - and every user child it spawns - inherit
    Landlock without ever needing the on-disk daemon script to remain
    reachable. That keeps ``/jiuwenbox`` outside the Landlock allowlist,
    which matters for the policy-enforcement tests.

    ``__name__`` is deliberately **not** set to ``"__main__"`` so the
    ``if __name__ == "__main__": raise SystemExit(main())`` guard at the
    bottom of the daemon script does not fire. We call ``main()`` directly
    instead and use its int return value, which avoids ``except SystemExit``
    in this function (G.ERR.11).
    """
    started = time.perf_counter()
    if not _drop_privileges():
        return 2
    _mark("daemon.privdrop", started)

    started = time.perf_counter()
    compiled = _load_daemon_code(daemon_path)
    _mark("daemon.load", started)
    if compiled is None:
        return 2

    try:
        started = time.perf_counter()
        apply_landlock(payload)
        _mark("daemon.landlock", started)
    except LandlockHardRequirementError:
        return 126

    module_name = "jiuwenbox.supervisor.sandbox_daemon_inproc"
    module = types.ModuleType(module_name)
    module.__file__ = daemon_path
    sys.modules[module_name] = module
    daemon_globals = module.__dict__
    daemon_globals["__name__"] = module_name
    daemon_globals["__file__"] = daemon_path
    started = time.perf_counter()
    exec(compiled, daemon_globals)
    _mark("daemon.enter_main", started)
    os.environ.pop(PERF_CHILD_ENV, None)

    daemon_main = daemon_globals.get("main")
    if not callable(daemon_main):
        logger.error(
            "Daemon script %s does not expose a callable ``main`` symbol",
            daemon_path,
        )
        return 2
    result = daemon_main()
    try:
        return int(result) if result is not None else 0
    except (TypeError, ValueError):
        return 1


def main() -> int:
    """Apply Landlock and run the in-sandbox daemon in this process.

    Layout:
      ``landlock_launcher.py PAYLOAD --daemon DAEMON_SCRIPT_PATH``
    """
    if len(sys.argv) != 4 or sys.argv[2] != "--daemon":
        logger.error(
            "Usage: landlock_launcher.py <payload> --daemon <daemon_script>",
        )
        return 2

    payload_b64 = sys.argv[1]
    daemon_path = sys.argv[3]

    if os.environ.get(WARM_COMMIT_FD_ENV):
        return _run_warm_daemon(daemon_path)

    try:
        payload = _decode_payload(payload_b64)
    except ValueError as exc:
        # ``ValueError`` already covers ``json.JSONDecodeError`` (G.ERR.09).
        logger.error("Failed to decode landlock payload: %s", exc)
        return 2

    return _run_daemon_in_process(daemon_path, payload)


if __name__ == "__main__":
    raise SystemExit(main())
