# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Isolated runtime venv for user/agent pip installs under the user workspace."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

_DEFAULT_VENV_DIRNAME = "isolation_venv"
_venv_lock = threading.Lock()
_venv_ready: Path | None = None
_import_path_ready = False

# pip install patterns for shell command rewriting
_PIP_INSTALL_RE = re.compile(
    r"(?i)(?<!\w)(?<!-m )pip3?\s+install\b",
)
_PYTHON_PIP_INSTALL_RE = re.compile(
    r"(?i)(?<!\w)(python3?|py)\s+-m\s+pip\s+install\b",
)
_UV_PIP_INSTALL_RE = re.compile(
    r"(?i)(?<!\w)uv\s+pip\s+install\b",
)
_BUNDLED_PYTHON_PIP_RE = re.compile(
    r'(?i)(?<!\w)"?'
    + re.escape(str(Path(sys.executable).resolve()))
    + r'"?\s+-m\s+pip\s+install\b',
)
_CHAIN_SPLIT_RE = re.compile(r"\s*&&\s*|\s*\|\|\s*|\s*;\s*")

_PIP_INSTALL_ARG_RE = re.compile(
    r"(?i)^(?:pip3?\s+install|(?:python3?|py)\s+-m\s+pip\s+install|uv\s+pip\s+install)\s+(.*)$",
)


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


def _venv_site_packages(venv_dir: Path) -> Path | None:
    if os.name == "nt":
        lib_root = venv_dir / "Lib" / "site-packages"
    else:
        py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        lib_root = venv_dir / "lib" / py_ver / "site-packages"
    return lib_root if lib_root.is_dir() else None


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
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    line = (result.stdout or "").strip()
    if not line:
        return None
    candidate = Path(line).resolve()
    return candidate if candidate.is_file() else None


def resolve_base_python() -> Path:
    """Interpreter used as the base for isolation_venv creation."""
    override = (os.environ.get("JIUWENCLAW_BASE_PYTHON") or "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise RuntimeError(f"JIUWENCLAW_BASE_PYTHON does not exist: {candidate}")

    executable = Path(sys.executable).resolve()
    if _is_python_executable(executable) and executable.is_file():
        return executable

    bundled = _discover_bundled_python()
    if bundled is not None:
        return bundled

    system_python = _discover_system_python()
    if system_python is not None:
        return system_python

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
    if _venv_ready is not None and _venv_ready == venv_dir and pyvenv_cfg.is_file():
        return venv_dir

    with _venv_lock:
        if _venv_ready is not None and _venv_ready == venv_dir and pyvenv_cfg.is_file():
            return venv_dir

        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        if not pyvenv_cfg.is_file():
            _create_runtime_venv_dir(venv_dir)

        runtime_py = get_runtime_python(ensure=False)
        if runtime_py.is_file():
            _ensure_runtime_pip(runtime_py)

        _venv_ready = venv_dir
        return venv_dir


def get_runtime_pip_argv(*extra: str) -> list[str]:
    runtime_py = get_runtime_python()
    return [str(runtime_py), "-m", "pip", *extra]


def ensure_runtime_import_path() -> None:
    global _import_path_ready
    if _import_path_ready:
        return
    venv_dir = ensure_runtime_venv()
    site = _venv_site_packages(venv_dir)
    if site is None:
        return
    site_str = str(site.resolve())
    if site_str not in sys.path:
        sys.path.append(site_str)
    _import_path_ready = True


def runtime_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    venv_dir = ensure_runtime_venv()
    scripts = _venv_scripts_dir(venv_dir)
    env["VIRTUAL_ENV"] = str(venv_dir)
    path_sep = os.pathsep
    existing_path = env.get("PATH", "")
    env["PATH"] = path_sep.join([str(scripts), existing_path]) if existing_path else str(scripts)

    site = _venv_site_packages(venv_dir)
    if site is not None:
        site_str = str(site.resolve())
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = path_sep.join([site_str, existing_pp]) if existing_pp else site_str

    return env


def _quote_for_shell(path: Path) -> str:
    text = str(path)
    if os.name == "nt":
        # Always quote on Windows: cmd.exe misparses unquoted paths (e.g. .office-claw).
        return f'"{text}"'
    return shlex.quote(text)


def _segment_already_uses_runtime_pip(segment: str) -> bool:
    runtime_py_str = str(get_runtime_python().resolve())
    if runtime_py_str.lower() not in segment.lower():
        return False
    return bool(re.search(r"(?i)-m\s+pip\s+install\b", segment))


def _rewrite_pip_segment(segment: str) -> str:
    segment = segment.strip()
    if not segment:
        return segment

    if _segment_already_uses_runtime_pip(segment):
        return segment

    runtime_py = _quote_for_shell(get_runtime_python())
    runtime_py_path = get_runtime_python()

    if _BUNDLED_PYTHON_PIP_RE.search(segment):
        return _BUNDLED_PYTHON_PIP_RE.sub(
            lambda _m: f"{runtime_py} -m pip install",
            segment,
            count=1,
        )

    if _UV_PIP_INSTALL_RE.search(segment):
        return _UV_PIP_INSTALL_RE.sub(
            lambda _m: f"uv pip install --python {runtime_py}",
            segment,
            count=1,
        )

    if _PYTHON_PIP_INSTALL_RE.search(segment):
        return _PYTHON_PIP_INSTALL_RE.sub(
            lambda _m: f"{runtime_py} -m pip install",
            segment,
            count=1,
        )

    if _PIP_INSTALL_RE.search(segment):
        return _PIP_INSTALL_RE.sub(
            lambda _m: f"{runtime_py} -m pip install",
            segment,
            count=1,
        )

    # Rewrite bare python/python3/py invocations (not paths like python.exe).
    runtime_py_str = str(runtime_py_path.resolve())
    if runtime_py_str.lower() not in segment.lower():
        segment = re.sub(
            r"(?i)(?<![\w./\\])(python3?|py)(?=\s+)",
            lambda _m: runtime_py,
            segment,
        )

    return segment


def rewrite_shell_command(command: str) -> str:
    if not (command or "").strip():
        return command

    ensure_runtime_venv()

    parts = re.split(r"(\s*&&\s*|\s*\|\|\s*|\s*;\s*)", command)
    if len(parts) == 1:
        return _rewrite_pip_segment(command)

    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            out.append(_rewrite_pip_segment(part))
        else:
            out.append(part)
    return "".join(out)


def _parse_package_name(spec: str) -> str | None:
    spec = (spec or "").strip()
    if not spec or spec.startswith("-"):
        return None
    # Strip PEP 508 env markers and extras
    name = re.split(r"[<>=!~;\[]", spec, maxsplit=1)[0].strip()
    return name or None


def _extract_pip_install_specs(command: str) -> list[str]:
    specs: list[str] = []
    for segment in _CHAIN_SPLIT_RE.split(command):
        segment = segment.strip()
        match = _PIP_INSTALL_ARG_RE.match(segment)
        if not match:
            continue
        tail = match.group(1) or ""
        try:
            tokens = shlex.split(tail, posix=os.name != "nt")
        except ValueError:
            tokens = tail.split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in {"-r", "--requirement"} and i + 1 < len(tokens):
                i += 2
                continue
            if tok.startswith("-"):
                if tok in {"-e", "--editable"} and i + 1 < len(tokens):
                    i += 2
                    continue
                i += 1
                continue
            specs.append(tok)
            i += 1
    return specs


def _pip_show_version(package: str) -> str | None:
    try:
        result = subprocess.run(
            get_runtime_pip_argv("show", package),
            capture_output=True,
            text=True,
            timeout=60,
            env=runtime_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _package_installed_in_venv(package: str) -> bool:
    return _pip_show_version(package) is not None


def _is_unpinned_install_spec(spec: str) -> bool:
    return "==" not in spec and ">=" not in spec and "<=" not in spec


def check_install_version_warnings(specs: Sequence[str]) -> list[str]:
    warnings: list[str] = []
    ensure_runtime_venv()
    venv_label = str(get_runtime_venv_dir())
    for spec in specs:
        pkg = _parse_package_name(spec)
        if not pkg:
            continue
        installed = _pip_show_version(pkg)
        if not installed:
            continue
        requested_version = None
        version_match = re.search(r"==\s*([^\s,;]+)", spec)
        if version_match:
            requested_version = version_match.group(1).strip()
        if requested_version and requested_version != installed:
            msg = (
                f"isolation_venv 中已安装 {pkg}=={installed}，"
                f"本次将安装 {spec}，可能影响其他 agent"
            )
            logger.warning("[pip_env] %s (venv=%s)", msg, venv_label)
            warnings.append(msg)
        elif requested_version is None and _is_unpinned_install_spec(spec):
            msg = (
                f"isolation_venv 中已安装 {pkg}=={installed}，"
                f"本次 pip install 可能升级/覆盖该版本，可能影响其他 agent"
            )
            logger.warning("[pip_env] %s (venv=%s)", msg, venv_label)
            warnings.append(msg)
    return warnings


def check_command_install_warnings(command: str) -> list[str]:
    specs = _extract_pip_install_specs(command)
    return check_install_version_warnings(specs)


@dataclass
class InstallResult:
    returncode: int
    stdout: str
    stderr: str
    warnings: list[str]


def install_packages(
    packages: list[str],
    *,
    extra_args: list[str] | None = None,
) -> InstallResult:
    if not packages:
        return InstallResult(0, "", "", [])

    warnings = check_install_version_warnings(packages)
    argv = get_runtime_pip_argv("install", *packages, *(extra_args or []))
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=600,
            env=runtime_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return InstallResult(1, "", f"pip install timed out: {exc}", warnings)

    stderr = result.stderr or ""
    stdout = result.stdout or ""
    combined = f"{stdout}\n{stderr}".lower()
    if result.returncode != 0 or any(
        token in combined for token in ("error:", "conflict", "resolutionimpossible")
    ):
        logger.error(
            "[pip_env] pip install failed rc=%s packages=%s stderr=%s",
            result.returncode,
            packages,
            stderr[:500],
        )
    return InstallResult(result.returncode, stdout, stderr, warnings)


def package_installed(package: str) -> bool:
    ensure_runtime_venv()
    if _package_installed_in_venv(package):
        return True
    try:
        import importlib.metadata

        importlib.metadata.version(package)
        return True
    except Exception:
        return False
