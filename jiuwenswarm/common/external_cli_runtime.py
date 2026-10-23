# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Manage optional external CLI Python runtimes for frozen applications."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jiuwenswarm.common._build_config import DISPLAY_NAME


logger = logging.getLogger(__name__)


class _LazyHttpx:
    """Preserve the module-level mock seam without importing httpx at startup."""

    _module: Any | None = None

    def _load(self) -> Any:
        if self._module is None:
            import httpx as httpx_module

            self._module = httpx_module
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


# Kept as a module attribute because installer tests and downstream extensions
# patch ``external_cli_runtime.httpx.Client``. The proxy loads the real module
# only when a download actually needs it.
httpx = _LazyHttpx()

# Manifest sections sharing one entry schema: ``agents`` holds the vendor SDK
# runtimes of providers built into openjiuwen (Claude Code, Codex);
# ``providers`` holds third-party harness provider packages, which carry their
# vendor CLI themselves. A runtime name is unique across both sections.
_MANIFEST_SECTIONS = ("agents", "providers")
# openjiuwen resolves third-party harness providers through this entry point
# group, so an installed provider package must declare its name in it.
_PROVIDER_ENTRY_POINT_GROUP = "openjiuwen.harness_providers"
# Runtime names become directory names under ``runtime/external-cli``.
_RUNTIME_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
_INSTALL_METADATA_FILE = ".jiuwenswarm-runtime.json"
_DOWNLOAD_TIMEOUT_SECONDS = 120
_DOWNLOAD_CHUNK_SIZE_BYTES = 64 * 1024
_DOWNLOAD_SPEED_SMOOTHING_FACTOR = 0.25
_DOWNLOAD_ATTEMPTS_PER_SOURCE = 5
_DOWNLOAD_RETRY_BACKOFF_SECONDS = 1.0
_RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429}
_ELEVATED_INSTALL_TIMEOUT_MILLISECONDS = 30 * 60 * 1000
_ELEVATED_INSTALL_TERMINATION_WAIT_MILLISECONDS = 5 * 1000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102

_LogCallback = Callable[[str], None]
_ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class _RuntimeInstallContext:
    """Hold shared state for a managed runtime installation."""

    cli_agent: str
    platform_key: str
    target: Path
    emit: _LogCallback
    report_progress: _ProgressCallback


@dataclass(frozen=True)
class _ArtifactOperationContext:
    """Hold shared state for one artifact operation."""

    destination: Path
    emit: _LogCallback
    report_progress: _ProgressCallback
    artifact_index: int
    artifact_count: int


@dataclass(frozen=True)
class _DownloadAttemptContext:
    """Describe one artifact download attempt."""

    artifact: dict[str, str]
    operation: _ArtifactOperationContext
    url: str
    wheel_path: Path
    download_attempt: int
    switching_source: bool


class _RetryableDownloadError(RuntimeError):
    """Represent a transient download failure that can be retried."""


class _NonRetryableDownloadError(RuntimeError):
    """Represent a source failure that should immediately use the next source."""


class _ArtifactIntegrityError(RuntimeError):
    """Represent a downloaded artifact that failed integrity verification."""


def _wait_for_elevated_windows_installer(
    process_handle: Any,
    wait_for_single_object: Callable[[Any, int], int],
    terminate_process: Callable[[Any, int], int],
    get_last_error: Callable[[], int],
) -> None:
    """Wait for an elevated installer after UAC authorization completes."""
    wait_result = wait_for_single_object(
        process_handle,
        _ELEVATED_INSTALL_TIMEOUT_MILLISECONDS,
    )
    if wait_result == _WAIT_TIMEOUT:
        if not terminate_process(process_handle, 1):
            raise OSError(
                get_last_error(),
                "failed to terminate timed-out elevated external CLI installer",
            )
        wait_for_single_object(
            process_handle,
            _ELEVATED_INSTALL_TERMINATION_WAIT_MILLISECONDS,
        )
        raise RuntimeError("elevated external CLI installer timed out")
    if wait_result != _WAIT_OBJECT_0:
        raise OSError(
            get_last_error(),
            "failed to wait for elevated external CLI installer",
        )


def _download_ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context from OS and bundled public CA roots."""
    import certifi

    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=certifi.where())
    except OSError as exc:
        raise RuntimeError(f"failed to load bundled CA certificates: {exc}") from exc
    return context


def get_external_cli_runtime_status(cli_agent: str) -> dict[str, Any]:
    """Return the managed runtime compatibility state for an optional CLI."""
    entry = _runtime_entry(cli_agent)
    platform_key = _runtime_platform_key()
    artifacts = entry.get("platforms", {}).get(platform_key)
    if not artifacts:
        raise RuntimeError(f"{cli_agent} runtime is not available for {platform_key}")

    target_packages = _runtime_package_versions(artifacts)
    target = external_cli_site_packages(cli_agent)
    result: dict[str, Any] = {
        "runtime_state": "not_installed",
        "managed": True,
        "installed_platform": "",
        "target_platform": platform_key,
        "installed_packages": [],
        "target_packages": target_packages,
    }
    if not target.is_dir() or not any(target.iterdir()):
        return result

    metadata_path = target / _INSTALL_METADATA_FILE
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        result["runtime_state"] = "invalid"
        return result
    if not isinstance(metadata, dict):
        result["runtime_state"] = "invalid"
        return result

    installed_packages = metadata.get("packages")
    result["installed_platform"] = str(metadata.get("platform") or "")
    result["installed_packages"] = (
        installed_packages if isinstance(installed_packages, list) else []
    )
    expected_metadata = {
        "agent": cli_agent,
        "platform": platform_key,
        "packages": target_packages,
    }
    if metadata != expected_metadata:
        result["runtime_state"] = "update_required"
        return result
    if _missing_runtime_files(entry, platform_key, target):
        result["runtime_state"] = "invalid"
        return result

    result["runtime_state"] = "current"
    return result


def external_cli_runtime_names() -> tuple[str, ...]:
    """Return every runtime the bundled manifest describes.

    Built-in agent SDK runtimes come first, then third-party harness provider
    packages.

    Raises:
        RuntimeError: When the manifest is missing or invalid.
    """
    return tuple(_runtime_entries(_load_manifest()))


def external_cli_site_packages(cli_agent: str) -> Path:
    """Return the stable site-packages directory for an optional CLI agent."""
    _runtime_entry(cli_agent)
    return _runtime_site_packages(cli_agent)


def _runtime_site_packages(cli_agent: str) -> Path:
    if _is_frozen_windows():
        from jiuwenswarm.common.utils import get_user_workspace_dir

        return (
            get_user_workspace_dir()
            / "runtime"
            / "external-cli"
            / cli_agent
            / "site-packages"
        )
    if _is_frozen_macos():
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / DISPLAY_NAME
            / "runtime"
            / "external-cli"
            / cli_agent
            / "site-packages"
        )
    raise RuntimeError(
        "managed external CLI runtimes require a frozen Windows or macOS application"
    )


def activate_external_cli_runtime_paths() -> None:
    """Expose optional runtime directories before business modules are imported.

    Never raises: activation serves an optional capability, so any unexpected
    failure is logged at warning level and treated as the optional SDKs being
    unavailable rather than propagated to the caller.
    """
    if not (_is_frozen_windows() or _is_frozen_macos()):
        return
    try:
        cli_agents = external_cli_runtime_names()
    except RuntimeError as exc:
        # Optional runtimes must never block startup; installing one reports
        # the same manifest error to the user.
        logger.warning("Optional external CLI runtimes are unavailable: %s", exc)
        return
    try:
        for cli_agent in cli_agents:
            site_packages = _runtime_site_packages(cli_agent)
            # Preserved SDKs can belong to a different release. Never expose an
            # incompatible or incomplete runtime to provider imports.
            try:
                if _is_frozen_windows() and get_external_cli_runtime_status(cli_agent)["runtime_state"] != "current":
                    continue
                site_packages.mkdir(parents=True, exist_ok=True)
            except (OSError, RuntimeError) as exc:
                logger.debug(
                    "Failed to activate optional %s runtime directory %s: %s",
                    cli_agent,
                    site_packages,
                    exc,
                )
                continue
            site_packages_text = str(site_packages)
            if site_packages_text not in sys.path:
                sys.path.append(site_packages_text)
        importlib.invalidate_caches()
    except Exception:  # noqa: BLE001 - optional capability must not break the caller
        logger.warning("Managed external CLI runtime activation failed", exc_info=True)


def install_external_cli_runtime(
    cli_agent: str,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Download and install a pinned optional CLI runtime."""
    import portalocker

    entry = _runtime_entry(cli_agent)
    emit = log_callback or (lambda _message: None)
    report_progress = progress_callback or (lambda _progress: None)
    platform_key = _runtime_platform_key()
    artifacts = entry.get("platforms", {}).get(platform_key)
    if not artifacts:
        raise RuntimeError(f"{cli_agent} runtime is not available for {platform_key}")

    activate_external_cli_runtime_paths()
    if _is_frozen_windows():
        with tempfile.TemporaryDirectory(
            prefix=f"{DISPLAY_NAME}-{cli_agent}-runtime-",
        ) as temporary_directory:
            artifact_directory = Path(temporary_directory)
            _download_external_cli_runtime_artifacts(
                artifacts,
                artifact_directory,
                emit,
                report_progress,
            )
            report_progress(
                {
                    "phase": "installing",
                    "bytes_per_second": 0.0,
                    "eta_seconds": 0.0,
                },
            )
            try:
                _install_external_cli_runtime_artifacts_with_lock(
                    cli_agent,
                    platform_key,
                    artifacts,
                    artifact_directory,
                )
            except PermissionError:
                emit(
                    f"Current user cannot update the {cli_agent} runtime; "
                    "requesting administrator permission"
                )
                _run_elevated_windows_runtime_installer(cli_agent, artifact_directory)
    else:
        target = external_cli_site_packages(cli_agent)
        agent_root = target.parent
        agent_root.mkdir(parents=True, exist_ok=True)
        lock_path = agent_root / ".install.lock"
        try:
            with portalocker.Lock(str(lock_path), mode="a+", timeout=1):
                _download_and_install_external_cli_runtime_files(
                    artifacts,
                    _RuntimeInstallContext(
                        cli_agent=cli_agent,
                        platform_key=platform_key,
                        target=target,
                        emit=emit,
                        report_progress=report_progress,
                    ),
                )
        except portalocker.exceptions.LockException as exc:
            raise RuntimeError(
                f"another {cli_agent} runtime installation is already running"
            ) from exc

    report_progress(
        {
            "phase": "verifying",
            "bytes_per_second": 0.0,
            "eta_seconds": 0.0,
        },
    )
    activate_external_cli_runtime_paths()
    _verify_installed_runtime(cli_agent)
    emit(f"{cli_agent} optional runtime installed successfully")


def install_external_cli_runtime_from_artifacts(
    cli_agent: str, artifact_directory: Path
) -> None:
    """Install a verified Windows runtime from previously downloaded wheels."""
    import portalocker

    entry = _runtime_entry(cli_agent)
    if not _is_frozen_windows():
        raise RuntimeError(
            "elevated external CLI runtime installation requires frozen Windows"
        )
    platform_key = _runtime_platform_key()
    artifacts = entry.get("platforms", {}).get(platform_key)
    if not artifacts:
        raise RuntimeError(f"{cli_agent} runtime is not available for {platform_key}")

    activate_external_cli_runtime_paths()
    _install_external_cli_runtime_artifacts_with_lock(
        cli_agent,
        platform_key,
        artifacts,
        artifact_directory,
    )
    activate_external_cli_runtime_paths()
    _verify_installed_runtime(cli_agent)


def _install_external_cli_runtime_artifacts_with_lock(
    cli_agent: str,
    platform_key: str,
    artifacts: list[dict[str, str]],
    artifact_directory: Path,
) -> None:
    """Install downloaded runtime artifacts while holding the agent lock."""
    import portalocker

    target = external_cli_site_packages(cli_agent)
    agent_root = target.parent
    agent_root.mkdir(parents=True, exist_ok=True)
    lock_path = agent_root / ".install.lock"
    try:
        with portalocker.Lock(str(lock_path), mode="a+", timeout=1):
            _install_external_cli_runtime_from_artifacts(
                cli_agent,
                platform_key,
                artifacts,
                target,
                artifact_directory,
            )
    except portalocker.exceptions.LockException as exc:
        raise RuntimeError(
            f"another {cli_agent} runtime installation is already running"
        ) from exc


def _download_and_install_external_cli_runtime_files(
    artifacts: list[dict[str, str]],
    context: _RuntimeInstallContext,
) -> None:
    agent_root = context.target.parent
    staging_root = _create_runtime_staging_root(agent_root)
    staging_site_packages = staging_root / "site-packages"
    staging_site_packages.mkdir()
    try:
        artifact_count = len(artifacts)
        for artifact_index, artifact in enumerate(artifacts, start=1):
            _install_wheel_artifact(
                artifact,
                staging_site_packages,
                _ArtifactOperationContext(
                    destination=staging_root,
                    emit=context.emit,
                    report_progress=context.report_progress,
                    artifact_index=artifact_index,
                    artifact_count=artifact_count,
                ),
            )
        _complete_staged_runtime_install(
            context.cli_agent,
            context.platform_key,
            artifacts,
            context.target,
            staging_site_packages,
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _download_external_cli_runtime_artifacts(
    artifacts: list[dict[str, str]],
    artifact_directory: Path,
    emit: Callable[[str], None],
    report_progress: Callable[[dict[str, Any]], None],
) -> None:
    artifact_directory.mkdir(parents=True, exist_ok=True)
    artifact_count = len(artifacts)
    for artifact_index, artifact in enumerate(artifacts, start=1):
        _download_wheel_artifact(
            artifact,
            _ArtifactOperationContext(
                destination=artifact_directory,
                emit=emit,
                report_progress=report_progress,
                artifact_index=artifact_index,
                artifact_count=artifact_count,
            ),
        )


def _install_external_cli_runtime_from_artifacts(
    cli_agent: str,
    platform_key: str,
    artifacts: list[dict[str, str]],
    target: Path,
    artifact_directory: Path,
) -> None:
    if not artifact_directory.is_dir():
        raise RuntimeError(
            f"external CLI artifact directory does not exist: {artifact_directory}"
        )
    agent_root = target.parent
    staging_root = _create_runtime_staging_root(agent_root)
    staging_site_packages = staging_root / "site-packages"
    staging_site_packages.mkdir()
    try:
        for artifact in artifacts:
            wheel_filename = _wheel_filename(artifact)
            downloaded_wheel_path = artifact_directory / wheel_filename
            trusted_wheel_path = staging_root / wheel_filename
            shutil.copyfile(downloaded_wheel_path, trusted_wheel_path)
            _verify_wheel_artifact(artifact, trusted_wheel_path)
            _safe_extract_wheel(trusted_wheel_path, staging_site_packages)
        _complete_staged_runtime_install(
            cli_agent,
            platform_key,
            artifacts,
            target,
            staging_site_packages,
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _create_runtime_staging_root(agent_root: Path) -> Path:
    """Create a unique staging directory that inherits its parent permissions."""
    staging_root = agent_root / f".install-{uuid.uuid4().hex}"
    staging_root.mkdir()
    return staging_root


def _complete_staged_runtime_install(
    cli_agent: str,
    platform_key: str,
    artifacts: list[dict[str, str]],
    target: Path,
    staging_site_packages: Path,
) -> None:
    _validate_staged_runtime(cli_agent, platform_key, staging_site_packages)
    metadata = {
        "agent": cli_agent,
        "platform": platform_key,
        "packages": _runtime_package_versions(artifacts),
    }
    (staging_site_packages / _INSTALL_METADATA_FILE).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _replace_runtime_directory(target, staging_site_packages)


def _run_elevated_windows_runtime_installer(
    cli_agent: str,
    artifact_directory: Path,
) -> None:
    if not _is_frozen_windows():
        raise RuntimeError(
            "elevated external CLI runtime installation requires frozen Windows"
        )

    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cb_size", wintypes.DWORD),
            ("f_mask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lp_verb", wintypes.LPCWSTR),
            ("lp_file", wintypes.LPCWSTR),
            ("lp_parameters", wintypes.LPCWSTR),
            ("lp_directory", wintypes.LPCWSTR),
            ("n_show", ctypes.c_int),
            ("h_inst_app", wintypes.HINSTANCE),
            ("lp_id_list", wintypes.LPVOID),
            ("lp_class", wintypes.LPCWSTR),
            ("hkey_class", wintypes.HANDLE),
            ("dw_hot_key", wintypes.DWORD),
            ("h_icon_or_monitor", wintypes.HANDLE),
            ("h_process", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell_execute_ex = shell32.ShellExecuteExW
    shell_execute_ex.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell_execute_ex.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL

    parameters = subprocess.list2cmdline(
        [
            "--desktop-install-external-cli",
            cli_agent,
            str(artifact_directory.resolve()),
        ],
    )
    execute_info = ShellExecuteInfo()
    execute_info.cb_size = ctypes.sizeof(ShellExecuteInfo)
    execute_info.f_mask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    execute_info.lp_verb = "runas"
    execute_info.lp_file = str(Path(sys.executable).resolve())
    execute_info.lp_parameters = parameters
    execute_info.lp_directory = str(Path(sys.executable).resolve().parent)
    execute_info.n_show = 0  # SW_HIDE
    if not shell_execute_ex(ctypes.byref(execute_info)):
        error_code = ctypes.get_last_error()
        if error_code == 1223:  # ERROR_CANCELLED
            raise RuntimeError("administrator permission was cancelled")
        raise OSError(error_code, "failed to start elevated external CLI installer")
    if not execute_info.h_process:
        raise RuntimeError(
            "elevated external CLI installer did not return a process handle"
        )

    try:
        _wait_for_elevated_windows_installer(
            execute_info.h_process,
            wait_for_single_object,
            terminate_process,
            ctypes.get_last_error,
        )
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(execute_info.h_process, ctypes.byref(exit_code)):
            raise OSError(
                ctypes.get_last_error(),
                "failed to read elevated external CLI installer status",
            )
        if exit_code.value != 0:
            raise RuntimeError(
                f"elevated external CLI installer failed with exit code {exit_code.value}"
            )
    finally:
        close_handle(execute_info.h_process)


def _runtime_entry(cli_agent: str) -> dict[str, Any]:
    entry = _runtime_entries(_load_manifest()).get(cli_agent)
    if entry is None:
        raise ValueError(f"unsupported external cli agent: {cli_agent}")
    return entry


def _runtime_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for section in _MANIFEST_SECTIONS:
        entries.update(manifest.get(section, {}))
    return entries


def _is_frozen_windows() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def _is_frozen_macos() -> bool:
    return sys.platform == "darwin" and bool(getattr(sys, "frozen", False))


def _verify_installed_runtime(cli_agent: str) -> None:
    entry = _runtime_entry(cli_agent)
    importlib.invalidate_caches()
    target = _runtime_site_packages(cli_agent)
    try:
        next(target.iterdir(), None)
    except OSError as exc:
        raise RuntimeError(
            f"installed {cli_agent} runtime directory is not readable: {target}: {exc}"
        ) from exc
    missing = [
        module
        for module in entry.get("required_modules", ())
        if importlib.util.find_spec(module) is None
    ]
    entry_point = entry.get("entry_point")
    if entry_point and not importlib.metadata.entry_points(
        group=_PROVIDER_ENTRY_POINT_GROUP,
        name=entry_point,
    ):
        missing.append(f"entry point {_PROVIDER_ENTRY_POINT_GROUP}:{entry_point}")
    if missing:
        raise RuntimeError(
            f"installed {cli_agent} runtime is unavailable: {', '.join(missing)}"
        )


def _runtime_package_versions(artifacts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"name": artifact["name"], "version": artifact["version"]}
        for artifact in artifacts
    ]


def pinned_sdk_requirements(cli_agent: str) -> list[str]:
    """Return ``name==version`` requirements for an agent's SDK packages.

    Reads the managed-runtime manifest (the single source of truth for the SDK
    versions this product ships, already used by the frozen-app installer) so
    source installs resolve the exact same versions instead of whatever the
    index happens to list as latest. Returns [] when the manifest has no
    entry for the agent, letting the plain extra resolve as before.
    """
    try:
        manifest = _load_manifest()
    except RuntimeError:
        # An unreadable manifest should not break source installs; fall back
        # to the unpinned extra.
        logger.debug("Falling back to unpinned %s SDK requirements", cli_agent)
        return []
    entry = _runtime_entries(manifest).get(cli_agent)
    if entry is None:
        raise ValueError(f"unsupported external cli agent: {cli_agent}")
    platforms = entry.get("platforms", {})
    # SDK versions are identical across platforms in the manifest; take any.
    for artifacts in platforms.values():
        return [
            f"{artifact['name']}=={artifact['version']}"
            for artifact in artifacts
            if artifact.get("name") and artifact.get("version")
        ]
    return []


def _missing_runtime_files(
    entry: dict[str, Any],
    platform_key: str,
    site_packages: Path,
) -> list[str]:
    """List what an extracted runtime lacks according to its manifest entry.

    Args:
        entry: The runtime's manifest entry.
        platform_key: Platform whose bundled executable must be present.
        site_packages: Directory the runtime's wheels were extracted into.

    Returns:
        Missing modules, executable and provider entry point; empty when the
        runtime is complete.
    """
    missing = [
        module
        for module in entry.get("required_modules", ())
        if not _module_present(site_packages, module)
    ]
    executable = entry.get("executables", {}).get(platform_key)
    if executable and not site_packages.joinpath(*PurePosixPath(executable).parts).is_file():
        missing.append(executable)
    entry_point = entry.get("entry_point")
    if entry_point and not _declares_provider_entry_point(site_packages, entry_point):
        missing.append(f"entry point {_PROVIDER_ENTRY_POINT_GROUP}:{entry_point}")
    return missing


def _module_present(site_packages: Path, module: str) -> bool:
    return (site_packages / module).is_dir() or (site_packages / f"{module}.py").is_file()


def _declares_provider_entry_point(site_packages: Path, name: str) -> bool:
    for dist_info in site_packages.glob("*.dist-info"):
        entry_points = importlib.metadata.Distribution.at(dist_info).entry_points
        if entry_points.select(group=_PROVIDER_ENTRY_POINT_GROUP, name=name):
            return True
    return False


def _manifest_path() -> Path:
    return (
        Path(__file__).resolve().parents[1] / "resources" / "external_cli_runtime.json"
    )


def _load_manifest() -> dict[str, Any]:
    try:
        payload = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"failed to load external CLI runtime manifest: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("invalid external CLI runtime manifest")
    seen: set[str] = set()
    for section in _MANIFEST_SECTIONS:
        entries = payload.get(section, {})
        if not isinstance(entries, dict):
            raise RuntimeError(f"invalid external CLI runtime manifest: {section} must be an object")
        for name, entry in entries.items():
            if name in seen:
                raise RuntimeError(f"invalid external CLI runtime manifest: duplicate runtime {name!r}")
            _validate_manifest_entry(name, entry)
            seen.add(name)
    return payload


def _validate_manifest_entry(name: str, entry: Any) -> None:
    """Reject a manifest entry the installer cannot act on safely.

    Raises:
        RuntimeError: When the name or any field is malformed.
    """
    prefix = f"invalid external CLI runtime manifest entry {name!r}"
    if not _RUNTIME_NAME_PATTERN.fullmatch(name):
        raise RuntimeError(f"{prefix}: name must match {_RUNTIME_NAME_PATTERN.pattern}")
    if not isinstance(entry, dict) or not isinstance(entry.get("platforms"), dict):
        raise RuntimeError(f"{prefix}: platforms must be an object")
    modules = entry.get("required_modules")
    if not isinstance(modules, list) or not modules:
        raise RuntimeError(f"{prefix}: required_modules must be a non-empty list")
    if not all(_is_text(module) for module in modules):
        raise RuntimeError(f"{prefix}: required_modules must hold module names")
    executables = entry.get("executables", {})
    if not isinstance(executables, dict) or not all(_is_text(path) for path in executables.values()):
        raise RuntimeError(f"{prefix}: executables must map platforms to paths")
    if not all(_is_relative_posix_path(path) for path in executables.values()):
        raise RuntimeError(f"{prefix}: executables must be relative to site-packages")
    entry_point = entry.get("entry_point")
    if entry_point is not None and not _is_text(entry_point):
        raise RuntimeError(f"{prefix}: entry_point must be a non-empty string")


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_relative_posix_path(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _runtime_platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture_aliases = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    architecture = architecture_aliases.get(machine)
    if architecture is None:
        raise RuntimeError(
            f"unsupported processor architecture: {machine or 'unknown'}"
        )
    system_aliases = {"windows": "windows", "darwin": "macos", "linux": "linux"}
    operating_system = system_aliases.get(system)
    if operating_system is None:
        raise RuntimeError(f"unsupported operating system: {system or 'unknown'}")
    if operating_system == "linux" and platform.libc_ver()[0].lower() == "musl":
        operating_system = "linux-musl"
    return f"{operating_system}-{architecture}"


def _install_wheel_artifact(
    artifact: dict[str, str],
    staging_site_packages: Path,
    context: _ArtifactOperationContext,
) -> None:
    wheel_path = _download_wheel_artifact(artifact, context)
    _safe_extract_wheel(wheel_path, staging_site_packages)


def _wheel_filename(artifact: dict[str, str]) -> str:
    return f"{artifact['name']}-{artifact['version']}.whl"


def _download_wheel_artifact(
    artifact: dict[str, str],
    context: _ArtifactOperationContext,
) -> Path:
    name = artifact["name"]
    version = artifact["version"]
    wheel_path = context.destination / _wheel_filename(artifact)
    context.emit(f"Downloading {name} {version}")
    urls = [url for url in (artifact.get("mirror_url"), artifact["url"]) if url]
    last_error: RuntimeError | None = None
    for source_index, url in enumerate(urls, start=1):
        for attempt in range(1, _DOWNLOAD_ATTEMPTS_PER_SOURCE + 1):
            switching_source = source_index > 1 and attempt == 1
            context.report_progress(
                {
                    "phase": "downloading",
                    "current_package": name,
                    "current_version": version,
                    "artifact_index": context.artifact_index,
                    "artifact_count": context.artifact_count,
                    "download_attempt": attempt,
                    "download_max_attempts": _DOWNLOAD_ATTEMPTS_PER_SOURCE,
                    "switching_source": switching_source,
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "bytes_per_second": 0.0,
                    "eta_seconds": 0.0,
                },
            )
            if attempt > 1:
                time.sleep(_DOWNLOAD_RETRY_BACKOFF_SECONDS * (attempt - 1))
            try:
                _download_wheel_url(
                    _DownloadAttemptContext(
                        artifact=artifact,
                        operation=context,
                        url=url,
                        wheel_path=wheel_path,
                        download_attempt=attempt,
                        switching_source=switching_source,
                    ),
                )
                _verify_wheel_artifact(artifact, wheel_path)
            except _RetryableDownloadError as exc:
                last_error = exc
                wheel_path.unlink(missing_ok=True)
                if attempt >= _DOWNLOAD_ATTEMPTS_PER_SOURCE:
                    break
                context.emit(
                    f"Download attempt {attempt}/{_DOWNLOAD_ATTEMPTS_PER_SOURCE} failed "
                    f"for {name} {version}; retrying"
                )
                continue
            except (_NonRetryableDownloadError, _ArtifactIntegrityError) as exc:
                last_error = exc
                wheel_path.unlink(missing_ok=True)
                break
            except OSError:
                wheel_path.unlink(missing_ok=True)
                raise
            context.emit(f"Verified {name} {version}")
            return wheel_path
        if source_index < len(urls):
            context.report_progress(
                {
                    "phase": "downloading",
                    "current_package": name,
                    "current_version": version,
                    "artifact_index": context.artifact_index,
                    "artifact_count": context.artifact_count,
                    "download_attempt": 0,
                    "download_max_attempts": _DOWNLOAD_ATTEMPTS_PER_SOURCE,
                    "switching_source": True,
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "bytes_per_second": 0.0,
                    "eta_seconds": 0.0,
                },
            )
            context.emit(
                f"Download source failed for {name} {version}; trying fallback"
            )
            continue
        detail = f": {last_error}" if last_error is not None else ""
        raise RuntimeError(
            f"failed to download {name} {version} from all configured sources{detail}"
        ) from last_error
    raise RuntimeError(f"no download source configured for {name} {version}")


def _download_wheel_url(
    context: _DownloadAttemptContext,
) -> None:
    name = context.artifact["name"]
    version = context.artifact["version"]
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http") or proxies.get("all")
    try:
        with httpx.Client(
            http2=True,
            proxy=proxy,
            verify=_download_ssl_context(),
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "JiuwenSwarm-ExternalCliInstaller/1"},
        ) as client:
            with client.stream("GET", context.url) as response:
                response.raise_for_status()
                total_header = response.headers.get("Content-Length")
                total_bytes = (
                    int(total_header) if total_header and total_header.isdigit() else 0
                )
                downloaded_bytes = 0
                last_sample_bytes = 0
                last_sample_at = time.monotonic()
                smoothed_speed = 0.0
                progress_base: dict[str, Any] = {
                    "phase": "downloading",
                    "current_package": name,
                    "current_version": version,
                    "artifact_index": context.operation.artifact_index,
                    "artifact_count": context.operation.artifact_count,
                    "total_bytes": total_bytes,
                    "download_attempt": context.download_attempt,
                    "download_max_attempts": _DOWNLOAD_ATTEMPTS_PER_SOURCE,
                    "switching_source": context.switching_source,
                }
                context.operation.report_progress(
                    {
                        **progress_base,
                        "downloaded_bytes": 0,
                        "bytes_per_second": 0.0,
                        "eta_seconds": 0.0,
                    }
                )
                with context.wheel_path.open("wb") as output:
                    for chunk in response.iter_raw(
                        chunk_size=_DOWNLOAD_CHUNK_SIZE_BYTES
                    ):
                        output.write(chunk)
                        downloaded_bytes += len(chunk)
                        sampled_at = time.monotonic()
                        sample_elapsed = sampled_at - last_sample_at
                        if sample_elapsed > 0:
                            sample_speed = (
                                downloaded_bytes - last_sample_bytes
                            ) / sample_elapsed
                            smoothed_speed = (
                                sample_speed
                                if smoothed_speed == 0
                                else (
                                    _DOWNLOAD_SPEED_SMOOTHING_FACTOR * sample_speed
                                    + (1 - _DOWNLOAD_SPEED_SMOOTHING_FACTOR)
                                    * smoothed_speed
                                )
                            )
                        last_sample_at = sampled_at
                        last_sample_bytes = downloaded_bytes
                        remaining_bytes = max(total_bytes - downloaded_bytes, 0)
                        eta_seconds = (
                            remaining_bytes / smoothed_speed
                            if total_bytes and smoothed_speed
                            else 0.0
                        )
                        context.operation.report_progress(
                            {
                                **progress_base,
                                "downloaded_bytes": downloaded_bytes,
                                "bytes_per_second": smoothed_speed,
                                "eta_seconds": eta_seconds,
                            }
                        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        message = f"download returned HTTP {status_code}"
        if status_code in _RETRYABLE_HTTP_STATUS_CODES or status_code >= 500:
            raise _RetryableDownloadError(message) from exc
        raise _NonRetryableDownloadError(message) from exc
    except httpx.TimeoutException as exc:
        raise _RetryableDownloadError("download timed out") from exc
    except httpx.HTTPError as exc:
        raise _RetryableDownloadError(
            f"download failed with {type(exc).__name__}"
        ) from exc


def _verify_wheel_artifact(artifact: dict[str, str], wheel_path: Path) -> None:
    name = artifact["name"]
    version = artifact["version"]
    expected_sha256 = artifact["sha256"].lower()
    digest = hashlib.sha256()
    try:
        with wheel_path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"failed to read {name} {version}: {exc}") from exc
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise _ArtifactIntegrityError(
            f"checksum mismatch for {name} {version}: expected {expected_sha256}, got {actual_sha256}"
        )


def _safe_extract_wheel(wheel_path: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            for member in archive.infolist():
                relative_path = PurePosixPath(member.filename)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise RuntimeError(f"unsafe wheel entry: {member.filename}")
                target = destination.joinpath(*relative_path.parts).resolve()
                if os.path.commonpath((destination_resolved, target)) != str(
                    destination_resolved
                ):
                    raise RuntimeError(f"unsafe wheel entry: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = member.external_attr >> 16 & 0o777
                if mode:
                    target.chmod(mode)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"failed to extract {wheel_path.name}: {exc}") from exc


def _validate_staged_runtime(
    cli_agent: str,
    platform_key: str,
    staging_site_packages: Path,
) -> None:
    missing = _missing_runtime_files(_runtime_entry(cli_agent), platform_key, staging_site_packages)
    if missing:
        raise RuntimeError(
            f"downloaded {cli_agent} runtime is incomplete: {', '.join(missing)}"
        )


def _replace_runtime_directory(target: Path, staging: Path) -> None:
    backup = target.parent / f".site-packages-backup-{uuid.uuid4().hex}"
    target_existed = target.exists()
    try:
        if target_existed:
            os.replace(target, backup)
        os.replace(staging, target)
    except OSError:
        if target_existed and backup.exists() and not target.exists():
            try:
                os.replace(backup, target)
            except OSError:
                logger.exception(
                    "Failed to restore optional runtime backup; keeping backup at %s",
                    backup,
                )
                raise
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
