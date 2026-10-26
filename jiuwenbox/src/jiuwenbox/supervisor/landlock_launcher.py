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
import os
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
        apply_landlock(payload)
    except LandlockHardRequirementError:
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
