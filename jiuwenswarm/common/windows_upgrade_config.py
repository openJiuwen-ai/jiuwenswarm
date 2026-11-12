# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recover only agent settings reset by a legacy administrator uninstaller."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path


def _decrypt_snapshot(encrypted: str) -> bytes:
    """Unprotect the installer's DPAPI snapshot in the same Windows account."""
    import base64
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    value = base64.b64decode(encrypted, validate=True)
    buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = DataBlob()
    # Bind typed stdcall functions directly; retain the Windows error for failures.
    # API contract: https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata
    unprotect = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
        use_last_error=True,
    )(("CryptUnprotectData", ctypes.WinDLL("crypt32")))
    free_result = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(
        ("LocalFree", ctypes.WinDLL("kernel32")),
    )
    cryptprotect_ui_forbidden = 0x1
    if not unprotect(ctypes.byref(source), None, None, None, None, cryptprotect_ui_forbidden, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free_result(result.data)


def restore_legacy_upgrade_config() -> None:
    """Restore two scoped fields under the existing config lock, then consume.

    A snapshot is created only when Setup invokes an old admin uninstaller.
    Verify the snapshot hash and the exact reset operation. Concurrent
    changes or any unexpected uninstall behavior leave recovery material intact.
    """
    import portalocker
    from jiuwenswarm.common.utils import get_user_workspace_dir

    runtime = get_user_workspace_dir() / "runtime"
    pending = runtime / "upgrade-config" / "pending.json"
    if not pending.is_file():
        return
    # Share the installer's migration exclusion before taking the config lock.
    with portalocker.Lock(str(runtime / "npm" / ".migration.lock"), mode="a+", timeout=1):
        _restore_snapshot(pending)


def _restore_snapshot(pending: Path) -> None:
    from ruamel.yaml import YAML
    from jiuwenswarm.common import config

    record = json.loads(pending.read_text(encoding="utf-8-sig"))
    if record.get("schema") != 1 or Path(record["config"]).resolve() != config.CONFIG_YAML_PATH.resolve():
        raise RuntimeError("Invalid legacy upgrade configuration snapshot")
    raw = _decrypt_snapshot(record["encrypted"])
    if hashlib.sha256(raw).hexdigest() != record["before_sha256"]:
        raise RuntimeError("Legacy upgrade configuration snapshot verification failed")
    original = YAML().load(io.StringIO(raw.decode("utf-8-sig"))) or {}
    expected = deepcopy(original)
    parent_path = config.EXTERNAL_CLI_AGENTS_CONFIG_PATH[:-1]
    keys = (config.EXTERNAL_CLI_AGENTS_CONFIG_PATH[-1], config.EXTERNAL_TRANSPORT_CONFIG_PATH[-1])
    expected_parent = expected
    original_parent = original
    for segment in parent_path:
        expected_parent = expected_parent.get(segment, {})
        original_parent = original_parent.get(segment, {})
    for key in keys:
        expected_parent.pop(key, None)

    def restore(current):
        # Idempotent if no reset occurred or a previous restore already wrote.
        if current == original:
            return None
        if current != expected:
            raise RuntimeError("Configuration changed during legacy upgrade; preserved snapshot requires recovery")
        parent = current
        for segment in parent_path:
            parent = parent[segment]
        for key in keys:
            if key in original_parent:
                parent[key] = deepcopy(original_parent[key])
        return current

    config.update_config(restore)
    pending.unlink()
