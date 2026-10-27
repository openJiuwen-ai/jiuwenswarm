# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned Windows venv identity; never stored in sandbox-writable files."""

import hashlib
import json
import os
from pathlib import Path

_PIN_ROOT = r"Software\JiuwenSwarm\SandboxRuntimePins"


class RuntimePythonTrustError(RuntimeError):
    """A managed interpreter cannot be authorized from untrusted metadata."""


def _open_pin_key(venv_dir: Path):
    import winreg
    import win32api
    import win32con
    import win32security

    # Use the configured path, not a junction's current target, as the key.
    identity = os.path.normcase(str(venv_dir.absolute()))
    name = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            rf"{_PIN_ROOT}\{name}",
                            0, winreg.KEY_ALL_ACCESS)
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        sid = win32security.ConvertSidToStringSid(user)
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"D:P(A;;KA;;;{sid})(A;;KA;;;SY)(A;;KA;;;BA)", 1,
        )
        win32security.SetSecurityInfo(
            int(key), win32security.SE_REGISTRY_KEY,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, descriptor.GetSecurityDescriptorDacl(), None,
        )
    except Exception:
        key.Close()
        raise
    return key


def read_python_base_pin(venv_dir: Path) -> Path | None:
    import winreg

    try:
        with _open_pin_key(venv_dir) as key:
            try:
                raw, kind = winreg.QueryValueEx(key, "Identity")
            except FileNotFoundError:
                return None
        value = json.loads(raw) if kind == winreg.REG_SZ else {}
        base = Path(value["base_python"])
        if (value["venv_root"] != os.path.normcase(str(venv_dir.resolve()))
                or not base.is_absolute() or base.name.lower() not in {"python.exe", "pythonw.exe"}
                or base.resolve() != base):
            raise ValueError("invalid interpreter identity")
        return base
    except Exception as exc:
        raise RuntimePythonTrustError(f"Cannot read host Python pin for {venv_dir}: {exc}") from exc


def write_python_base_pin(venv_dir: Path, base: Path, *, replace: bool = False) -> None:
    """Record a verified base; only trusted host provisioning may replace an old pin."""
    import winreg

    existing = read_python_base_pin(venv_dir)
    if existing is not None and not replace:
        if existing != base:
            raise RuntimePythonTrustError(f"Host Python pin already differs for {venv_dir}")
        return
    if (not base.is_absolute() or not base.is_file()
            or base.name.lower() not in {"python.exe", "pythonw.exe"} or base.resolve() != base
            or base.is_relative_to(venv_dir.resolve())):
        raise RuntimePythonTrustError(f"Invalid host base Python for {venv_dir}")
    value = {"venv_root": os.path.normcase(str(venv_dir.resolve())), "base_python": str(base)}
    try:
        with _open_pin_key(venv_dir) as key:
            winreg.SetValueEx(key, "Identity", 0, winreg.REG_SZ, json.dumps(value))
    except Exception as exc:
        raise RuntimePythonTrustError(f"Cannot write host Python pin for {venv_dir}: {exc}") from exc
