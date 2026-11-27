"""In-sandbox Landlock launcher.

This module is executed inside bubblewrap after mounts/namespaces are in place.
It applies Landlock to itself, then execs the target command so restrictions are
inherited by the sandboxed program.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys

LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        logger.error(
            "Usage: landlock_launcher.py <payload> -- <command> [args...]",
        )
        return 2

    payload = _decode_payload(sys.argv[1])
    command = sys.argv[3:]
    try:
        apply_landlock(payload)
        os.execvp(command[0], command)
    except LandlockHardRequirementError:
        return 126
    except OSError as exc:
        logger.error("Failed to exec command %s: %s", command[0], exc)
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
