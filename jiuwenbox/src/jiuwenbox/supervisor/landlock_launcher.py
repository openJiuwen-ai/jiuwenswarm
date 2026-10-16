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
from functools import lru_cache
import os
import stat
import sys
import types

LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

logger = logging.getLogger("jiuwenbox.landlock_launcher")

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


@lru_cache(maxsize=1)
def _libc():
    # Windows imports this module for validation/tests but never applies Landlock.
    library = ctypes.CDLL("libc.so.6", use_errno=True)
    library.syscall.restype = ctypes.c_long
    library.prctl.restype = ctypes.c_int
    return library


def prctl(*args):
    return _libc().prctl(*args)


class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class LandlockHardRequirementError(Exception):
    """Raised when hard-required Landlock setup cannot continue."""


def _syscall(nr: int, *args: int) -> int:
    return _libc().syscall(nr, *[ctypes.c_long(arg) for arg in args])


def _decode_payload(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value.encode()).decode())


def _detect_abi() -> int:
    return max(_syscall(LANDLOCK_CREATE_RULESET, 0, 0, 1), 0)


def _fail(message: str) -> None:
    logger.error("Landlock initialization failed: %s", message)
    raise LandlockHardRequirementError(message)


def _status(mode: str, state: str, abi: int, reason: str | None = None) -> dict:
    result = {"mode": mode, "state": state, "abi": abi, "reason": reason}
    if state != "enforced":
        logger.warning("Landlock %s: %s (ABI %s)", state, reason, abi)
    return result


def _reserved_path(path: str) -> bool:
    real = os.path.realpath(path)
    return real == "/jiuwenbox" or real.startswith("/jiuwenbox/")


def _add_rule(ruleset_fd: int, path: str, access: int) -> None:
    # A root grant would include the private launcher/daemon subtree. Expand
    # explicit broad-root policies into the current visible children instead;
    # root itself receives listing permission only. No new top-level creation.
    if os.path.realpath(path) == "/":
        _add_path_rule(ruleset_fd, "/", READ_DIR)
        with os.scandir("/") as entries:
            for entry in entries:
                if not _reserved_path(entry.path):
                    _add_path_rule(ruleset_fd, entry.path, access)
        return
    if _reserved_path(path):
        raise OSError("Landlock policy cannot grant access to /jiuwenbox")
    _add_path_rule(ruleset_fd, path, access)


def _add_path_rule(ruleset_fd: int, path: str, access: int) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        # Optional distro paths (e.g. /lib64 or /etc/pki) need not exist.
        # Omitting a missing object grants no rights; never use its parent.
        return
    try:
        # Recheck the opened inode, not just the supplied path: a symlink may
        # have changed between resolving its name and opening it.
        target = os.readlink(f"/proc/self/fd/{fd}")
        if _reserved_path(target):
            raise OSError("Landlock cannot grant an alias of /jiuwenbox")
        if os.path.realpath(target) == "/":
            access &= READ_DIR
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            access &= EXECUTE | READ_FILE | WRITE_FILE | TRUNCATE
        rule = LandlockPathBeneathAttr(access, fd)
        if (
            _syscall(
                LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.addressof(rule), 0
            )
            < 0
        ):
            raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
    finally:
        os.close(fd)


def apply_landlock(payload: dict) -> dict:
    mode = payload.get("compatibility", "hard_requirement")
    if mode not in {"disabled", "best_effort", "hard_requirement"}:
        _fail("Invalid Landlock compatibility mode")
    if mode == "disabled":
        return _status(mode, "disabled", 0, "Explicitly disabled by deployment policy")

    abi = _detect_abi()
    if abi <= 0:
        if mode == "best_effort":
            return _status(
                mode, "degraded", 0, "Landlock is unavailable; filesystem rules are not enforced"
            )
        _fail("Landlock is not supported or enabled by the kernel")
    if abi < 3 and mode == "hard_requirement":
        _fail(f"Landlock ABI >= 3 is required for complete filesystem protection; found ABI {abi}")
    read_only_access, read_write_access = _access_masks(abi)
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _fail("Failed to set no_new_privs")

    attr = LandlockRulesetAttr(read_write_access)
    ruleset_fd = _syscall(LANDLOCK_CREATE_RULESET, ctypes.addressof(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        _fail(f"landlock_create_ruleset failed (errno={ctypes.get_errno()})")
    try:
        for path in payload.get("directory_only", []):
            _add_path_rule(ruleset_fd, path, READ_DIR)
        for path in payload["read_only"]:
            _add_rule(ruleset_fd, path, read_only_access)
        for path in payload["read_write"]:
            _add_rule(ruleset_fd, path, read_write_access)
        if _syscall(LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            _fail(f"landlock_restrict_self failed (errno={ctypes.get_errno()})")
    except OSError as exc:
        _fail(str(exc))
    finally:
        os.close(ruleset_fd)
    if abi < 3:
        return _status(
            mode, "degraded", abi, "Older Landlock ABI lacks complete REFER/TRUNCATE protection"
        )
    return _status(mode, "enforced", abi)


def _run_daemon_in_process(daemon_path: str, payload: dict) -> int:
    """Apply Landlock and run the daemon script in this Python process.

    The daemon source is read **before** ``apply_landlock`` so the
    ``/jiuwenbox`` directory containing the daemon script can be locked
    away by Landlock immediately afterward. Running the daemon in-process
    (no second ``execve``) is what lets the daemon - and every user
    child it spawns - inherit Landlock without ever needing the on-disk
    daemon script to remain reachable. That keeps ``/jiuwenbox`` outside
    the Landlock allowlist, which matters for the policy-enforcement
    tests.

    ``__name__`` is deliberately **not** set to ``"__main__"`` so the
    ``if __name__ == "__main__": raise SystemExit(main())`` guard at the
    bottom of the daemon script does not fire. We call ``main()`` directly
    instead and use its int return value, which avoids ``except SystemExit``
    in this function (G.ERR.11).
    """
    try:
        with open(daemon_path, "rb") as fh:
            daemon_source = fh.read()
    except OSError as exc:
        logger.error("Failed to read daemon script %s: %s", daemon_path, exc)
        return 2

    try:
        landlock_status = apply_landlock(payload)
    except (LandlockHardRequirementError, OSError, ValueError) as exc:
        logger.error("Landlock setup failed; refusing to run daemon: %s", exc)
        return 126

    module_name = "jiuwenbox.supervisor.sandbox_daemon_inproc"
    module = types.ModuleType(module_name)
    module.__file__ = daemon_path
    sys.modules[module_name] = module
    daemon_globals = module.__dict__
    daemon_globals["__name__"] = module_name
    daemon_globals["__file__"] = daemon_path
    try:
        compiled = compile(daemon_source, daemon_path, "exec")
    except SyntaxError as exc:
        logger.error("Failed to compile daemon script %s: %s", daemon_path, exc)
        return 2
    exec(compiled, daemon_globals)
    # Trusted in-process result, never copied from command environment or IPC.
    daemon_globals["LANDLOCK_STATUS"] = landlock_status

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

    try:
        payload = _decode_payload(payload_b64)
    except ValueError as exc:
        # ``ValueError`` already covers ``json.JSONDecodeError`` (G.ERR.09).
        logger.error("Failed to decode landlock payload: %s", exc)
        return 2

    return _run_daemon_in_process(daemon_path, payload)


if __name__ == "__main__":
    raise SystemExit(main())
