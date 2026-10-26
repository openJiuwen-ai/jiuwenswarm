"""Jiuwen interpreter reuse and isolated child environment for DeepResearch."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import zipfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from jiuwenswarm.agents.harness.common.tools.deepresearch.path_safety import (
    is_direct_directory,
    is_direct_regular_file,
    private_mode_is_compatible,
)
from jiuwenswarm.common.local_env_config import export_spawn_environ

_ALLOWED_PROXY_KEYS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)
_FORBIDDEN_INHERITED_KEYS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "ALL_PROXY",
    "all_proxy",
)
_BRIDGE_ENV_KEYS = ("LLM_SSL_VERIFY", "TOOL_SSL_VERIFY")
_BRIDGE_INPUT_MAX_BYTES = 64 * 1024 * 1024
_BRIDGE_STDOUT_MAX_BYTES = 64 * 1024
_BRIDGE_STDERR_TAIL_MAX_BYTES = 20_000
_BRIDGE_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
_BRIDGE_ZIP_MAX_MEMBERS = 1024
_BRIDGE_ZIP_MEMBER_MAX_BYTES = 64 * 1024 * 1024
_BRIDGE_JSON_MAX_DEPTH = 64
_BRIDGE_JSON_MAX_NODES = 100_000
_BRIDGE_JSON_MAX_CONTAINER = 20_000
_BRIDGE_REQUEST_SCHEMA_VERSION = 2
_STYLE_PHASES = frozenset({
    "build_style_context",
    "apply_prompt",
    "invoke_llm",
    "extract_css_response",
    "normalize_css_output",
    "append_cover_title_contrast_safeguard",
    "inject_css",
})
_STYLE_REASON_CODES = frozenset({
    "style_context_failed",
    "style_prompt_failed",
    "llm_call_failed",
    "css_response_invalid",
    "css_response_empty",
    "css_safeguard_failed",
    "css_injection_failed",
})


class DeepResearchRuntimeError(RuntimeError):
    """Raised when the DeepResearch child runtime is unusable."""


@dataclass(frozen=True, slots=True)
class _BridgeArtifact:
    path: Path
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int]
    descriptor: int


@dataclass(frozen=True, slots=True)
class StyledReportArchiveResult:
    """Validated report archive plus the SDK's styling outcome."""

    path: Path
    style_applied: bool
    style_status: str
    style_phase: str | None = None
    style_reason_code: str | None = None


def _virtualenv_root(executable: Path) -> Path | None:
    root = executable.parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def resolve_python_executable() -> Path:
    """Return the interpreter that is running JiuwenSwarm."""
    candidate = Path(sys.executable)
    if not candidate.is_absolute():
        raise DeepResearchRuntimeError("runtime_python_invalid")

    lexical_path = Path(os.path.abspath(os.fspath(candidate)))
    if not lexical_path.is_file() or not os.access(lexical_path, os.X_OK):
        raise DeepResearchRuntimeError("runtime_python_invalid")
    return lexical_path


def build_child_env(executable: Path) -> dict[str, str]:
    """Build the minimal process environment for an isolated DeepResearch child."""
    python = Path(executable)
    bin_dir = python.parent
    venv_root = _virtualenv_root(python)
    child_env = export_spawn_environ()

    for key in _FORBIDDEN_INHERITED_KEYS:
        child_env.pop(key, None)
    for key in _ALLOWED_PROXY_KEYS:
        value = os.environ.get(key)
        if value is not None:
            child_env[key] = value
        else:
            child_env.pop(key, None)

    inherited_path = child_env.get("PATH", "")
    child_env["PATH"] = (
        os.pathsep.join((str(bin_dir), inherited_path))
        if inherited_path
        else str(bin_dir)
    )
    if venv_root is None:
        child_env.pop("VIRTUAL_ENV", None)
    else:
        child_env["VIRTUAL_ENV"] = str(venv_root)
    return child_env


def _bridge_script() -> Path:
    script = Path(__file__).with_name("sdk_bridge.py")
    try:
        metadata = script.lstat()
    except OSError as exc:
        raise DeepResearchRuntimeError("sdk_bridge_missing") from exc
    invalid_script = (
        not script.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    )
    if invalid_script:
        raise DeepResearchRuntimeError("sdk_bridge_invalid")
    return script


def _create_bridge_artifact() -> _BridgeArtifact:
    directory = Path(tempfile.mkdtemp(prefix="deepresearch-sdk-bridge-"))
    descriptor: int | None = None
    try:
        directory.chmod(0o700)
        directory_metadata = directory.lstat()
        if (
            not is_direct_directory(directory_metadata)
            or not private_mode_is_compatible(directory_metadata, 0o700)
        ):
            raise OSError("unsafe bridge directory")
        path = directory / "styled.zip"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        file_metadata = os.fstat(descriptor)
        if (
            not is_direct_regular_file(file_metadata)
            or file_metadata.st_nlink != 1
            or not private_mode_is_compatible(file_metadata, 0o600)
        ):
            raise OSError("unsafe bridge output")
        return _BridgeArtifact(
            path=path,
            directory_identity=(directory_metadata.st_dev, directory_metadata.st_ino),
            file_identity=(file_metadata.st_dev, file_metadata.st_ino),
            descriptor=descriptor,
        )
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            (directory / "styled.zip").unlink()
        with suppress(OSError):
            directory.rmdir()
        raise


def _remove_bridge_artifact(artifact: _BridgeArtifact) -> None:
    with suppress(OSError):
        os.close(artifact.descriptor)
    try:
        metadata = artifact.path.lstat()
    except OSError:
        metadata = None
    owned_file = (
        metadata is not None
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_nlink == 1
        and (metadata.st_dev, metadata.st_ino) == artifact.file_identity
    )
    if owned_file:
        with suppress(OSError):
            artifact.path.unlink()
    try:
        directory_metadata = artifact.path.parent.lstat()
    except OSError:
        return
    if (
        stat.S_ISDIR(directory_metadata.st_mode)
        and not stat.S_ISLNK(directory_metadata.st_mode)
        and (directory_metadata.st_dev, directory_metadata.st_ino)
        == artifact.directory_identity
    ):
        with suppress(OSError):
            artifact.path.parent.rmdir()


def _encode_bridge_request(
    final_result: dict[str, Any],
    llm_config: dict[str, Any],
    llm_auth: dict[str, str],
    tls: dict[str, bool],
) -> bytes:
    _validate_bridge_llm_auth(llm_auth)
    request = {
        "schema_version": _BRIDGE_REQUEST_SCHEMA_VERSION,
        "final_result": final_result,
        "llm_config": llm_config,
        "llm_auth": llm_auth,
        "tls": tls,
    }
    _validate_json_tree(request)
    try:
        payload = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid") from exc
    if len(payload) > _BRIDGE_INPUT_MAX_BYTES:
        raise DeepResearchRuntimeError("sdk_bridge_request_too_large")
    return payload


def _validate_bridge_llm_auth(llm_auth: object) -> None:
    if not isinstance(llm_auth, dict):
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
    if not llm_auth:
        return
    if set(llm_auth) != {"default_headers"}:
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
    raw_headers = llm_auth.get("default_headers")
    if not isinstance(raw_headers, str) or not raw_headers.strip():
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
    try:
        headers = json.loads(raw_headers)
    except (TypeError, ValueError) as exc:
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid") from exc
    if not isinstance(headers, dict):
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
    authorization = [
        value
        for key, value in headers.items()
        if isinstance(key, str) and key.lower() == "authorization"
    ]
    if len(headers) != 1 or len(authorization) != 1:
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
    authorization_value = authorization[0]
    if not isinstance(authorization_value, str) or not authorization_value.strip():
        raise DeepResearchRuntimeError("sdk_bridge_request_invalid")


def _validate_json_tree(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    visited: set[int] = set()
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _BRIDGE_JSON_MAX_NODES or depth > _BRIDGE_JSON_MAX_DEPTH:
            raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
        if item is None or isinstance(item, (str, int, bool, float)):
            continue
        if not isinstance(item, (dict, list)):
            raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
        identity = id(item)
        if identity in visited or len(item) > _BRIDGE_JSON_MAX_CONTAINER:
            raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
        visited.add(identity)
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise DeepResearchRuntimeError("sdk_bridge_request_invalid")
            stack.extend((child, depth + 1) for child in item.values())
        else:
            stack.extend((child, depth + 1) for child in item)


async def _await_cleanup_task(task: asyncio.Task[Any]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    with suppress(BaseException):
        task.result()


async def _stop_bridge_process(process: Any, timeout: float = 10.0) -> None:
    if getattr(process, "returncode", None) is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if getattr(process, "returncode", None) is None:
                with suppress(ProcessLookupError):
                    process.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=timeout)
    else:
        with suppress(Exception):
            await process.wait()


async def _write_bridge_request(process: Any, payload: bytes) -> None:
    if process.stdin is None:
        raise DeepResearchRuntimeError("sdk_bridge_protocol_invalid")
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    finally:
        process.stdin.close()
        wait_closed = getattr(process.stdin, "wait_closed", None)
        if callable(wait_closed):
            with suppress(BrokenPipeError, ConnectionResetError):
                await wait_closed()


async def _bounded_read(stream: Any, limit: int) -> bytes:
    if stream is None:
        return b""
    data = bytearray()
    while True:
        chunk = await stream.read(min(8192, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise DeepResearchRuntimeError("sdk_bridge_protocol_invalid")


async def _drain_stderr_tail(stream: Any, limit: int) -> bytes:
    """Drain without backpressure while retaining only a bounded diagnostic tail."""
    if stream is None:
        return b""
    tail = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(tail)
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]


def _validate_bridge_output(artifact: _BridgeArtifact) -> None:
    metadata = artifact.path.lstat()
    invalid_output = (
        not is_direct_regular_file(metadata)
        or metadata.st_nlink != 1
        or not private_mode_is_compatible(metadata, 0o600)
        or (metadata.st_dev, metadata.st_ino) != artifact.file_identity
        or metadata.st_size <= 0
        or metadata.st_size > _BRIDGE_ARCHIVE_MAX_BYTES
    )
    if invalid_output:
        raise DeepResearchRuntimeError("sdk_bridge_output_invalid")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            members = archive.infolist()
            if len(members) > _BRIDGE_ZIP_MAX_MEMBERS:
                raise DeepResearchRuntimeError("sdk_bridge_output_invalid")
            total = 0
            for member in members:
                if member.file_size > _BRIDGE_ZIP_MEMBER_MAX_BYTES:
                    raise DeepResearchRuntimeError("sdk_bridge_output_invalid")
                total += member.file_size
                if total > _BRIDGE_ARCHIVE_MAX_BYTES:
                    raise DeepResearchRuntimeError("sdk_bridge_output_invalid")
            if archive.testzip() is not None:
                raise DeepResearchRuntimeError("sdk_bridge_output_invalid")
    except (OSError, zipfile.BadZipFile) as exc:
        raise DeepResearchRuntimeError("sdk_bridge_output_invalid") from exc


@asynccontextmanager
async def stylize_report_archive(
    *,
    final_result: dict[str, Any],
    llm_config: dict[str, Any],
    llm_auth: dict[str, str],
    tls: dict[str, bool],
    manager: Any,
    session_id: str,
) -> AsyncIterator[StyledReportArchiveResult]:
    """Yield one validated private SDK ZIP and clean it after installation."""
    executable = resolve_python_executable()
    script = _bridge_script()
    payload = _encode_bridge_request(final_result, llm_config, llm_auth, tls)
    artifact = _create_bridge_artifact()
    process = None
    tracked = False
    stderr_task: asyncio.Task[bytes] | None = None
    try:
        child_env = build_child_env(executable)
        for key in _BRIDGE_ENV_KEYS:
            child_env.pop(key, None)
        process = await asyncio.create_subprocess_exec(
            str(executable),
            str(script),
            "stylize-report",
            "--config-stdin",
            "--output",
            str(artifact.path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        stderr_task = asyncio.create_task(
            _drain_stderr_tail(process.stderr, _BRIDGE_STDERR_TAIL_MAX_BYTES)
        )
        try:
            manager.track_process(session_id, process)
            tracked = True
        except BaseException:
            cleanup = asyncio.create_task(_stop_bridge_process(process))
            await _await_cleanup_task(cleanup)
            raise
        await _write_bridge_request(process, payload)
        stdout = await _bounded_read(process.stdout, _BRIDGE_STDOUT_MAX_BYTES)
        returncode = await process.wait()
        if stderr_task is not None:
            await stderr_task
        try:
            decoded = stdout.decode("utf-8")
            if not decoded.endswith("\n") or decoded.count("\n") != 1:
                raise ValueError
            result = json.loads(decoded)
        except ValueError as exc:
            raise DeepResearchRuntimeError("sdk_bridge_protocol_invalid") from exc
        required = {
            "schema_version",
            "status",
            "output_path",
            "style_applied",
            "style_status",
        }
        optional = {"style_phase", "style_reason_code"}
        result_keys = set(result) if isinstance(result, dict) else set()
        style_phase = result.get("style_phase") if isinstance(result, dict) else None
        style_reason_code = (
            result.get("style_reason_code") if isinstance(result, dict) else None
        )
        invalid_result = (
            returncode != 0
            or not isinstance(result, dict)
            or not required.issubset(result_keys)
            or not result_keys.issubset(required | optional)
            or not isinstance(result.get("schema_version"), int)
            or isinstance(result.get("schema_version"), bool)
            or result.get("schema_version").__class__ is not int
            or result.get("schema_version") != 1
            or result.get("status") != "completed"
            or result.get("output_path") != str(artifact.path)
            or not isinstance(result.get("style_applied"), bool)
            or result.get("style_status") not in {"applied", "fallback"}
            or (style_phase is None) != (style_reason_code is None)
            or (
                style_phase is not None
                and (
                    not isinstance(style_phase, str)
                    or style_phase not in _STYLE_PHASES
                )
            )
            or (
                style_reason_code is not None
                and (
                    not isinstance(style_reason_code, str)
                    or style_reason_code not in _STYLE_REASON_CODES
                )
            )
            or (
                result.get("style_status") != "fallback"
                and style_phase is not None
            )
        )
        if invalid_result:
            raise DeepResearchRuntimeError("sdk_bridge_failed")
        _validate_bridge_output(artifact)
        yield StyledReportArchiveResult(
            path=artifact.path,
            style_applied=result["style_applied"],
            style_status=result["style_status"],
            style_phase=style_phase,
            style_reason_code=style_reason_code,
        )
    except asyncio.CancelledError:
        if process is not None:
            cleanup = asyncio.create_task(_stop_bridge_process(process))
            await _await_cleanup_task(cleanup)
        raise
    finally:
        if process is not None:
            cleanup = asyncio.create_task(_stop_bridge_process(process))
            await _await_cleanup_task(cleanup)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                await _await_cleanup_task(stderr_task)
            if tracked:
                with suppress(BaseException):
                    manager.untrack_process(session_id, process)
        _remove_bridge_artifact(artifact)


__all__ = [
    "DeepResearchRuntimeError",
    "StyledReportArchiveResult",
    "build_child_env",
    "resolve_python_executable",
    "stylize_report_archive",
]
