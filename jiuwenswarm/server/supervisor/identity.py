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
    observation = None
    if not getattr(sys, "frozen", False):
        from jiuwenswarm.server.supervisor.identity_cache import launch_source_observation

        observation = launch_source_observation()
    return (json.dumps({"schema": 1, "parent_pid": os.getpid(),
        "package": _deployment_path(),
        "data_scope": data_scope(), "build_label": os.environ.get("JIUWENSWARM_BUILD_ID", "").strip(),
        "code_identity": fingerprint, "source_observation": observation}) + "\n").encode()


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
        if payload.get("build_label") != os.environ.get("JIUWENSWARM_BUILD_ID", "").strip():
            raise ValueError("identity pipe does not describe this child deployment")
        if not _valid_fingerprint(fingerprint):
            raise ValueError("identity pipe does not describe this child deployment")
        observed_identity = None
        observation = payload.get("source_observation")
        if not getattr(sys, "frozen", False) and isinstance(observation, dict):
            from jiuwenswarm.server.supervisor.identity_cache import verify_launch_source

            source = verify_launch_source(Path(__file__).resolve().parents[2], observation)
            if source is not None:
                observed_identity = hashlib.sha256(
                    f"source-v1\0{source}\0{payload['build_label']}".encode()).hexdigest()
        # Frozen identity is an O(1) bundled check. Unobservable source/cache
        # falls back to full validation; a private pipe cannot hide an upgrade.
        if observed_identity is None:
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
    if getattr(sys, "frozen", False):
        bundled = (
            Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "jiuwenswarm" / "_worker_build_identity.json"
        )
        try:
            payload = json.loads(bundled.read_text(encoding="utf-8"))
            fingerprint = payload["fingerprint"]
            if payload.get("schema") != 1 or not _valid_fingerprint(fingerprint):
                raise ValueError("invalid controlled build identity")
        except (OSError, ValueError, KeyError, TypeError):
            fingerprint = ""
        if fingerprint:
            return hashlib.sha256(f"frozen-v1\0{fingerprint}\0{override}".encode()).hexdigest()
        # Older packages have no controlled build identity. Preserve the
        # executable-based validation rather than trusting an environment ID.
        executable = Path(sys.executable)
        stat = executable.stat()
        legacy = hashlib.sha256(f"{executable}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()
        return hashlib.sha256(f"{legacy}\0{override}".encode()).hexdigest() if override else legacy
    from jiuwenswarm.server.supervisor.identity_cache import source_fingerprint
    fingerprint = source_fingerprint(Path(__file__).resolve().parents[2])
    # An explicit build label distinguishes deployments, but cannot override
    # dirty source or lockfile validation in a source checkout.
    return hashlib.sha256(f"source-v1\0{fingerprint}\0{override}".encode()).hexdigest()
