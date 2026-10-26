# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Landlock policy helpers shared by the supervisor and launcher."""

from __future__ import annotations

import base64
import ctypes
import json

from jiuwenbox.models.policy import SecurityPolicy

LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_RULE_PATH_BENEATH = 1


def _syscall(nr: int, *args: int) -> int:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fn = libc.syscall
    fn.restype = ctypes.c_long
    return fn(nr, *[ctypes.c_long(a) for a in args])


def detect_landlock_abi() -> int:
    """Return the supported Landlock ABI version, or 0 when unavailable."""
    try:
        result = _syscall(LANDLOCK_CREATE_RULESET, 0, 0, 1)
    except OSError:
        return 0
    return max(result, 0)


def encode_landlock_payload(policy: SecurityPolicy) -> str:
    """Build a compact payload consumed by the in-sandbox Landlock launcher."""
    read_only = list(policy.filesystem_policy.read_only)
    read_write = list(policy.filesystem_policy.read_write)
    for directory in policy.filesystem_policy.directories:
        if isinstance(directory, str):
            read_write.append(directory)
        else:
            read_write.append(directory.path)
    for file in policy.filesystem_policy.files:
        if isinstance(file, str):
            read_write.append(file)
        else:
            read_write.append(file.path)
    for mount in policy.filesystem_policy.bind_mounts:
        if mount.mode == "ro":
            read_only.append(mount.sandbox_path)
        else:
            read_write.append(mount.sandbox_path)
    for device in policy.filesystem_policy.device:
        read_write.append(device.sandbox_path)
    # These pseudo-filesystems are created by bwrap and are needed by common runtimes.
    read_only.append("/proc")
    read_write.append("/dev")
    # ``/jiuwenbox`` is intentionally NOT added here. The runtime mounts
    # the Landlock launcher and the in-sandbox daemon script under that
    # reserved subtree, and the Python interpreter only needs to read
    # them *before* Landlock is active:
    #   * the launcher script is loaded by ``python3`` before any user
    #     code in this process runs, so ``/jiuwenbox`` is still openable
    #     at that point;
    #   * the daemon script is read into memory by the launcher in the
    #     same window and then ``compile``/``exec``ed in-process - no
    #     further on-disk access is required after Landlock applies.
    # Keeping ``/jiuwenbox`` outside the allowlist guarantees that user
    # code spawned by the daemon cannot read the launcher or daemon
    # scripts (and therefore cannot inspect or replace them), which is
    # what ``test_landlock_rules_allow_policy_paths_and_deny_other_mounted_paths``
    # and the ``/jiuwenbox`` runtime-script integrity tests pin.
    # ``PolicyEngine._RESERVED_SANDBOX_PATHS`` is the policy-validation
    # mirror of this invariant: any user policy that tries to add
    # ``/jiuwenbox`` (or anything under it) to ``read_only`` /
    # ``read_write`` / ``directories`` / ``files`` / ``bind_mounts`` /
    # ``device`` is rejected at sandbox creation time so the allowlist
    # below cannot be widened from the outside.

    payload = {
        "compatibility": policy.landlock.compatibility,
        "read_only": _dedupe(read_only),
        "read_write": _dedupe(read_write),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
