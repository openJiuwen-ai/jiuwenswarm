# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Isolation-venv provisioning for the Windows sandbox (jiuwenbox) env injection.

Live surface: only ``resolve_base_python``
and ``ensure_runtime_venv`` are wired — ``agent_ws_server._build_sandbox_env``
uses them to inject JIUWENBOX_BUNDLED_PYTHON / JIUWENCLAW_BASE_PYTHON /
JIUWENBOX_VENV_DIR for box-server. The pip command-rewrite / install helpers
(``rewrite_shell_command``/``install_packages`` and friends) were ported from
jiuwenclaw without their wiring (shell_pip_patch / pip_isolation_rail) and
have been removed as dead code.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.server.runtime.python_base_pin import (
    RuntimePythonTrustError, read_python_base_pin, write_python_base_pin,
)

logger = logging.getLogger(__name__)

_DEFAULT_VENV_DIRNAME = "isolation_venv"
_venv_lock = threading.Lock()
_venv_ready: Path | None = None


def _hidden_kwargs() -> dict:
    """Hide console windows for python.exe / pip on Windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def get_runtime_venv_dir() -> Path:
    override = (os.environ.get("JIUWENCLAW_RUNTIME_VENV") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_user_workspace_dir() / _DEFAULT_VENV_DIRNAME


def _venv_scripts_dir(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def get_runtime_python(*, ensure: bool = True) -> Path:
    venv_dir = ensure_runtime_venv() if ensure else get_runtime_venv_dir()
    scripts = _venv_scripts_dir(venv_dir)
    name = "python.exe" if os.name == "nt" else "python"
    return scripts / name


def _is_python_executable(path: Path) -> bool:
    return path.name.lower() in {"python", "python.exe", "python3", "python3.exe"}


def _bundled_python_relative() -> Path:
    if os.name == "nt":
        return Path("tools") / "python" / "python.exe"
    return Path("tools") / "python" / "bin" / "python3"


def _install_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        exe = Path(sys.executable).resolve()
        roots.append(exe.parent)
        if exe.parent.name.lower() in {"scripts", "bin"}:
            roots.append(exe.parent.parent)
    except OSError:
        pass

    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _discover_bundled_python() -> Path | None:
    rel = _bundled_python_relative()
    for root in _install_roots():
        candidate = (root / rel).resolve()
        if candidate.is_file():
            return candidate
    return None


def _discover_system_python() -> Path | None:
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    if os.name != "nt":
        return None
    py_launcher = shutil.which("py")
    if not py_launcher:
        return None
    try:
        result = subprocess.run(
            [py_launcher, "-3", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            **_hidden_kwargs(),
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    line = (result.stdout or "").strip()
    if not line:
        return None
    candidate = Path(line).resolve()
    return candidate if candidate.is_file() else None


def resolve_python_base(executable: Path) -> Path:
    """Resolve a host-selected venv to its base without executing it or widening to its parent."""
    seen: set[Path] = set()
    candidate = executable.expanduser().absolute()
    for _ in range(8):
        if not candidate.is_file() or candidate in seen:
            break
        if os.name == "nt" and (candidate.name.lower() not in {"python.exe", "pythonw.exe"}
                                or candidate.resolve().name.lower() not in {"python.exe", "pythonw.exe"}):
            break
        seen.add(candidate)
        root = candidate.parent.parent if candidate.parent.name.lower() in {"scripts", "bin"} else candidate.parent
        config = root / "pyvenv.cfg"
        if not config.is_file():
            if os.name == "nt" and candidate.parent.name.lower() == "scripts":
                break
            return candidate.resolve()
        values = {}
        for line in config.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip().lower()] = value.strip()
        candidates = [Path(values[key]) for key in ("base-executable", "executable") if values.get(key)]
        if values.get("home"):
            names = ("python.exe",) if os.name == "nt" else ("python", "python3")
            candidates.extend(Path(values["home"]) / name for name in names)
        next_candidate = next(
            (path for path in candidates if path.is_absolute() and path.is_file() and path not in seen), None,
        )
        if next_candidate is None:
            break
        candidate = next_candidate
    raise RuntimeError(f"Cannot resolve base Python from {executable}")


def verified_runtime_python_base(venv_dir: Path, *, trusted_base: Path | None = None) -> Path:
    """Validate against a host selection or pin before publishing writable venv metadata."""
    try:
        actual = resolve_python_base(_venv_scripts_dir(venv_dir) / "python.exe")
        pinned = read_python_base_pin(venv_dir)
        expected = trusted_base if trusted_base is not None else pinned
        if expected is None or actual != expected.resolve() or actual.is_relative_to(venv_dir.resolve()):
            raise RuntimePythonTrustError(f"Managed venv base does not match host Python pin: {venv_dir}")
        if pinned != actual:
            write_python_base_pin(venv_dir, actual, replace=trusted_base is not None)
        return actual
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimePythonTrustError(f"Managed venv base validation failed for {venv_dir}: {exc}") from exc


def resolve_base_python() -> Path:
    """Interpreter used as the base for isolation_venv creation."""
    managed = get_runtime_venv_dir()

    def trusted_host_base(candidate: Path) -> Path:
        base = resolve_python_base(candidate)
        if os.name == "nt" and (candidate.absolute().is_relative_to(managed.absolute())
                                or base.is_relative_to(managed.resolve())):
            raise RuntimePythonTrustError("Cannot establish host trust from the writable managed venv")
        return base

    override = (os.environ.get("JIUWENCLAW_BASE_PYTHON") or "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.is_file():
            return trusted_host_base(candidate)
        raise RuntimeError(f"JIUWENCLAW_BASE_PYTHON does not exist: {candidate}")

    if os.name == "nt" and (managed / "pyvenv.cfg").is_file():
        pinned = read_python_base_pin(managed)
        if pinned is not None and pinned.is_file():
            try:
                return verified_runtime_python_base(managed)
            except RuntimePythonTrustError:
                # A host-rebuilt venv may use the current host interpreter. Its
                # metadata must still match that independent selection below.
                logger.info("[pip_env] Rechecking managed venv against host Python selection: %s", managed)

    if os.name != "nt":
        existing = _venv_scripts_dir(managed) / "python"
        if existing.is_file():
            try:
                return resolve_python_base(existing)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("[pip_env] Cannot resolve existing venv base; trying host Python: %s", exc)

    claw_home = (os.environ.get("CLAW_PYTHON_HOME") or "").strip()
    if claw_home:
        name = "python.exe" if os.name == "nt" else "python"
        for candidate in (Path(claw_home).expanduser() / name, _venv_scripts_dir(Path(claw_home)) / name):
            if candidate.is_file():
                return trusted_host_base(candidate)

    executable = Path(sys.executable).resolve()
    if _is_python_executable(executable) and executable.is_file():
        return trusted_host_base(executable)

    bundled = _discover_bundled_python()
    if bundled is not None:
        return trusted_host_base(bundled)

    system_python = _discover_system_python()
    if system_python is not None:
        return trusted_host_base(system_python)

    raise RuntimeError(
        "Cannot resolve base Python for isolation_venv. Set JIUWENCLAW_BASE_PYTHON "
        "or install Python on PATH."
    )


def _virtualenv_cli_args(venv_dir: Path) -> list[str]:
    base_python = resolve_base_python()
    return ["--python", str(base_python), str(venv_dir)]


def _create_runtime_venv_dir(venv_dir: Path) -> None:
    """Create isolation_venv with virtualenv (seeds pip; works on embeddable Python)."""
    from virtualenv.run import cli_run

    argv = _virtualenv_cli_args(venv_dir)
    logger.info("[pip_env] Creating isolation venv at %s (%s)", venv_dir, " ".join(argv))
    try:
        cli_run(argv)
    except RuntimeError as exc:
        raise subprocess.CalledProcessError(1, "virtualenv", " ".join(argv)) from exc

    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    if not pyvenv_cfg.is_file():
        raise subprocess.CalledProcessError(1, "virtualenv", " ".join(argv))


def _ensure_runtime_pip(runtime_py: Path) -> None:
    """Verify pip exists; virtualenv seeds pip during env creation."""
    try:
        subprocess.run(
            [str(runtime_py), "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            **_hidden_kwargs(),
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning(
            "[pip_env] pip unavailable in isolation venv (runtime python=%s)",
            runtime_py,
        )


def ensure_runtime_venv() -> Path:
    global _venv_ready
    venv_dir = get_runtime_venv_dir()
    pyvenv_cfg = venv_dir / "pyvenv.cfg"
    with _venv_lock:
        trusted_base = resolve_base_python() if os.name == "nt" else None
        pinned = read_python_base_pin(venv_dir) if os.name == "nt" else None
        refresh = pinned is not None and (
            not pinned.is_file()
            or (bool(os.environ.get("JIUWENCLAW_BASE_PYTHON", "").strip()) and pinned != trusted_base)
        )
        if _venv_ready == venv_dir and pyvenv_cfg.is_file() and not refresh:
            if os.name == "nt":
                verified_runtime_python_base(venv_dir, trusted_base=trusted_base)
            return venv_dir

        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        if not pyvenv_cfg.is_file() or refresh:
            _create_runtime_venv_dir(venv_dir)

        if os.name == "nt":
            verified_runtime_python_base(venv_dir, trusted_base=trusted_base)

        runtime_py = get_runtime_python(ensure=False)
        if runtime_py.is_file():
            _ensure_runtime_pip(runtime_py)

        _venv_ready = venv_dir
        return venv_dir
