# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Private launch identity for the 0.2.8 attached transport only.

Resident Workers retain their original launch and wire protocol. Unlike
develop, this branch has no identity cache or resident deployment election.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _deployment_path() -> str:
    path = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return os.path.normcase(str(path.resolve()))


def data_scope() -> str:
    from jiuwenswarm.common.utils import get_user_workspace_dir

    path = os.path.normcase(str(get_user_workspace_dir().resolve()))
    profile = os.environ.get("JIUWENSWARM_RUNTIME_PROFILE", "desktop").strip() or "desktop"
    return hashlib.sha256(f"{path}\0{profile}".encode()).hexdigest()


def code_identity() -> str:
    """Hash deployed Python code without importing the Agent Runtime."""
    package = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    digest.update(_deployment_path().encode())
    digest.update(os.environ.get("JIUWENSWARM_BUILD_ID", "").strip().encode())
    if getattr(sys, "frozen", False):
        paths = [Path(sys.executable).resolve()]
    else:
        paths = sorted(package.rglob("*.py"))
        if not paths:
            paths = sorted(package.rglob("*.pyc"))
    if not paths:
        raise RuntimeError("runtime_ipc=front_loopback stage=identity deployed code is unavailable")
    for path in paths:
        name = path.relative_to(package).as_posix() if path.is_relative_to(package) else path.name
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    for name in ("pyproject.toml", "uv.lock"):
        path = package.parent / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _is_launch_parent(parent_pid: int) -> bool:
    direct = os.getppid()
    if direct == parent_pid:
        return True
    if sys.platform != "win32":
        return False
    # Windows virtualenv launchers introduce one known executable hop.
    import psutil

    try:
        launcher = psutil.Process(direct)
        return (launcher.ppid() == parent_pid
                and os.path.normcase(str(Path(launcher.exe()).resolve()))
                == os.path.normcase(str(Path(sys.executable).resolve())))
    except (psutil.Error, OSError):
        return False


def launch_identity_payload(fingerprint: str, *, runtime_ipc: dict) -> bytes:
    payload = {"schema": 1, "parent_pid": os.getpid(), "package": _deployment_path(),
               "data_scope": data_scope(), "code_identity": fingerprint,
               "build_label": os.environ.get("JIUWENSWARM_BUILD_ID", "").strip(),
               "runtime_ipc": runtime_ipc}
    value = (json.dumps(payload) + "\n").encode()
    if len(value) > 4096:
        raise ValueError("runtime_ipc=front_loopback stage=launch-pipe payload too large")
    return value


def read_launch_payload() -> dict:
    """Validate the private parent pipe before any attached connection."""
    if sys.stdin is None:
        raise RuntimeError("runtime_ipc=front_loopback stage=launch-pipe stdin unavailable")
    value = sys.stdin.buffer.readline(4097)
    try:
        payload = json.loads(value)
        if len(value) > 4096 or payload.get("schema") != 1:
            raise ValueError("invalid launch schema")
        if type(payload.get("parent_pid")) is not int or not _is_launch_parent(payload["parent_pid"]):
            raise ValueError("invalid launch parent")
        if payload.get("package") != _deployment_path() or payload.get("data_scope") != data_scope():
            raise ValueError("invalid launch deployment")
        if payload.get("build_label") != os.environ.get("JIUWENSWARM_BUILD_ID", "").strip():
            raise ValueError("invalid launch build")
        if payload.get("code_identity") != code_identity():
            raise ValueError("deployed code changed after spawn")
        return payload
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("Invalid Worker parent identity") from exc
