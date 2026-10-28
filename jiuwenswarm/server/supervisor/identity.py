# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight deployment identity for Worker election and reattachment."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _deployment_path() -> str:
    path = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return os.path.normcase(str(path))


def deployment_scope() -> str:
    """A stable installation/environment owner, independent of its build ID."""
    environment = os.path.normcase(str(Path(sys.prefix).resolve()))
    return hashlib.sha256(f"{_deployment_path()}\0{environment}\0{data_scope()}".encode()).hexdigest()


def _identity_path() -> Path:
    if getattr(sys, "frozen", False):
        package = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "jiuwenswarm"
    else:
        package = Path(__file__).resolve().parents[2]
    return package / "_worker_build_identity.json"


def _source_identity(fingerprint: str, label: str) -> str:
    from jiuwenswarm.server.supervisor.dependency_identity import environment_identity

    environment = environment_identity()
    return hashlib.sha256(f"source-v2\0{fingerprint}\0{environment}\0{label}".encode()).hexdigest()


def _is_launch_parent(parent_pid: int) -> bool:
    direct = os.getppid()
    if direct == parent_pid:
        return True
    if sys.platform != "win32":
        return False
    # A Windows venv launcher creates the actual interpreter as its child.
    # Accept that single known executable hop, never an arbitrary ancestor.
    import psutil

    try:
        launcher = psutil.Process(direct)
        return (launcher.ppid() == parent_pid
                and os.path.normcase(str(Path(launcher.exe()).resolve()))
                == os.path.normcase(str(Path(sys.executable).resolve())))
    except (psutil.Error, OSError):
        return False


def launch_identity_payload(fingerprint: str) -> bytes:
    """Describe the parent's validated deployment on a private spawn pipe."""
    return (json.dumps({"schema": 1, "parent_pid": os.getpid(),
        "package": _deployment_path(),
        "data_scope": data_scope(), "deployment_scope": deployment_scope(),
        "build_label": os.environ.get("JIUWENSWARM_BUILD_ID", "").strip(),
        "code_identity": fingerprint}) + "\n").encode()


def _valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(letter in "0123456789abcdef" for letter in value)


def read_launch_identity() -> str:
    """Consume a one-use parent pipe; environment build labels cannot bypass validation."""
    if sys.stdin is not None:
        value = sys.stdin.buffer.readline(4097)
    elif sys.platform == "win32":
        # Windowed PyInstaller sets sys.stdin=None, but Popen's private input
        # handle still exists. Read it without depending on a console window.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.ReadFile.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(4097)
        received = wintypes.DWORD()
        if not kernel.ReadFile(kernel.GetStdHandle(-10 & 0xffffffff), buffer, len(buffer),
                               ctypes.byref(received), None):
            raise RuntimeError("Worker parent identity pipe is unavailable")
        value = buffer.raw[:received.value]
    else:
        raise RuntimeError("Worker parent identity pipe is unavailable")
    try:
        payload = json.loads(value)
        fingerprint = payload["code_identity"]
        if len(value) > 4096 or payload.get("schema") != 1:
            raise ValueError("identity pipe does not describe this child deployment")
        if not isinstance(payload.get("parent_pid"), int) or not _is_launch_parent(payload["parent_pid"]):
            raise ValueError("identity pipe does not describe this child deployment")
        if payload.get("package") != _deployment_path() or payload.get("data_scope") != data_scope():
            raise ValueError("identity pipe does not describe this child deployment")
        if payload.get("deployment_scope") != deployment_scope():
            raise ValueError("identity pipe does not describe this child deployment")
        if payload.get("build_label") != os.environ.get("JIUWENSWARM_BUILD_ID", "").strip():
            raise ValueError("identity pipe does not describe this child deployment")
        if not _valid_fingerprint(fingerprint):
            raise ValueError("identity pipe does not describe this child deployment")
        observed_identity = code_identity()
        if observed_identity != fingerprint:
            raise ValueError("Worker source changed after the parent validated it")
        return fingerprint
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("Invalid Worker parent identity") from exc


def data_scope() -> str:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    path = os.path.normcase(str(get_user_workspace_dir().resolve()))
    profile = os.environ.get("JIUWENSWARM_RUNTIME_PROFILE", "desktop").strip() or "desktop"
    return hashlib.sha256(f"{path}\0{profile}".encode()).hexdigest()


def code_identity() -> str:
    override = os.environ.get("JIUWENSWARM_BUILD_ID", "").strip()
    bundled = _identity_path()
    if bundled.exists() or getattr(sys, "frozen", False):
        from jiuwenswarm.common.worker_build_identity import read_build_identity

        payload = read_build_identity(bundled)
        fingerprint = payload["fingerprint"] if payload["schema"] == 1 else payload["backend_build_id"]
        if getattr(sys, "frozen", False):
            return hashlib.sha256(f"frozen-v2\0{fingerprint}\0{override}".encode()).hexdigest()
        # A wheel does not freeze its installed dependencies. The same wheel
        # can execute different SDK code after an environment update.
        return _source_identity(fingerprint, override)
    from jiuwenswarm.common.worker_build_identity import source_digest, dependency_digest
    root = Path(__file__).resolve().parents[3]
    fingerprint = hashlib.sha256((source_digest(root) + dependency_digest(root)).encode()).hexdigest()
    # An explicit build label distinguishes deployments, but cannot override
    # dirty source or lockfile validation in a source checkout.
    return _source_identity(fingerprint, override)
