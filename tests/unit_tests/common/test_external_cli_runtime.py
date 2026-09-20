# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for optional external CLI runtime downloads."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiuwenswarm.common import external_cli_runtime as runtime


def _artifact(*, mirror_url: str = "") -> dict[str, str]:
    return {
        "name": "example-runtime",
        "version": "1.0.0",
        "url": "https://primary.example/runtime.whl",
        "mirror_url": mirror_url,
        "sha256": hashlib.sha256(b"valid-wheel").hexdigest(),
    }


def _download_artifact(
    artifact: dict[str, str],
    destination: Path,
    progress: list[dict[str, Any]],
) -> Path:
    return runtime._download_wheel_artifact(
        artifact,
        runtime._ArtifactOperationContext(
            destination=destination,
            emit=lambda _message: None,
            report_progress=progress.append,
            artifact_index=1,
            artifact_count=1,
        ),
    )


def _configure_frozen_windows_install(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    artifact = _artifact()
    monkeypatch.setattr(runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(runtime, "_runtime_platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(
        runtime,
        "_load_manifest",
        lambda: {
            "agents": {
                "claude": {
                    "platforms": {"windows-x86_64": [artifact]},
                }
            }
        },
    )
    monkeypatch.setattr(runtime, "activate_external_cli_runtime_paths", lambda: None)
    monkeypatch.setattr(runtime, "_download_external_cli_runtime_artifacts", lambda *_args: None)
    monkeypatch.setattr(runtime, "_verify_installed_runtime", lambda _cli_agent: None)
    return artifact


def test_frozen_windows_installs_runtime_without_elevation_when_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _configure_frozen_windows_install(monkeypatch)
    direct_calls: list[tuple[str, str, list[dict[str, str]]]] = []
    elevated_calls: list[str] = []

    def _install_direct(
        cli_agent: str,
        platform_key: str,
        artifacts: list[dict[str, str]],
        _artifact_directory: Path,
    ) -> None:
        direct_calls.append((cli_agent, platform_key, artifacts))

    monkeypatch.setattr(runtime, "_install_external_cli_runtime_artifacts_with_lock", _install_direct)
    monkeypatch.setattr(
        runtime,
        "_run_elevated_windows_runtime_installer",
        lambda cli_agent, _artifact_directory: elevated_calls.append(cli_agent),
    )

    runtime.install_external_cli_runtime("claude")

    assert direct_calls == [("claude", "windows-x86_64", [artifact])]
    assert elevated_calls == []


def test_frozen_windows_elevates_only_after_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_frozen_windows_install(monkeypatch)
    emitted: list[str] = []
    elevated_calls: list[str] = []

    def _raise_permission_error(*_args: object) -> None:
        raise PermissionError("runtime directory is not writable")

    monkeypatch.setattr(
        runtime,
        "_install_external_cli_runtime_artifacts_with_lock",
        _raise_permission_error,
    )
    monkeypatch.setattr(
        runtime,
        "_run_elevated_windows_runtime_installer",
        lambda cli_agent, _artifact_directory: elevated_calls.append(cli_agent),
    )

    runtime.install_external_cli_runtime("claude", log_callback=emitted.append)

    assert elevated_calls == ["claude"]
    assert any("requesting administrator permission" in message for message in emitted)


def test_frozen_windows_does_not_elevate_for_non_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_frozen_windows_install(monkeypatch)
    elevated_calls: list[str] = []

    def _raise_validation_error(*_args: object) -> None:
        raise RuntimeError("runtime checksum validation failed")

    monkeypatch.setattr(
        runtime,
        "_install_external_cli_runtime_artifacts_with_lock",
        _raise_validation_error,
    )
    monkeypatch.setattr(
        runtime,
        "_run_elevated_windows_runtime_installer",
        lambda cli_agent, _artifact_directory: elevated_calls.append(cli_agent),
    )

    with pytest.raises(RuntimeError, match="checksum validation failed"):
        runtime.install_external_cli_runtime("claude")

    assert elevated_calls == []


def test_runtime_staging_root_is_created_under_agent_root(tmp_path: Path) -> None:
    staging_root = runtime._create_runtime_staging_root(tmp_path)

    assert staging_root.parent == tmp_path
    assert staging_root.name.startswith(".install-")
    assert staging_root.is_dir()


def test_verify_runtime_reports_unreadable_install_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "site-packages"
    target.mkdir()
    monkeypatch.setattr(runtime, "external_cli_site_packages", lambda _cli_agent: target)

    def _raise_permission_error(_path: Path) -> object:
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "iterdir", _raise_permission_error)

    with pytest.raises(RuntimeError, match="runtime directory is not readable"):
        runtime._verify_installed_runtime("claude")


def test_activate_runtime_paths_logs_directory_creation_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    logged: list[tuple[object, ...]] = []

    def _site_packages(cli_agent: str) -> Path:
        return blocked_parent / cli_agent / "site-packages"

    monkeypatch.setattr(runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(runtime, "_is_frozen_macos", lambda: False)
    monkeypatch.setattr(runtime, "external_cli_site_packages", _site_packages)
    original_path = list(runtime.sys.path)
    monkeypatch.setattr(runtime.sys, "path", list(original_path))
    monkeypatch.setattr(runtime.logger, "debug", lambda *args: logged.append(args))

    runtime.activate_external_cli_runtime_paths()

    assert runtime.sys.path[: len(original_path)] == original_path
    assert runtime.sys.path[-len(runtime._SUPPORTED_AGENTS) :] == [
        str(_site_packages(cli_agent)) for cli_agent in runtime._SUPPORTED_AGENTS
    ]
    for cli_agent in runtime._SUPPORTED_AGENTS:
        site_packages = str(_site_packages(cli_agent))
        assert site_packages in runtime.sys.path
        assert any(
            args[1] == cli_agent
            and str(args[2]) == site_packages
            and isinstance(args[3], OSError)
            for args in logged
        )


def test_elevated_installer_timeout_terminates_started_process() -> None:
    wait_timeouts: list[int] = []
    termination_calls: list[tuple[object, int]] = []
    process_handle = object()

    def _wait(handle: object, timeout: int) -> int:
        assert handle is process_handle
        wait_timeouts.append(timeout)
        if len(wait_timeouts) == 1:
            return runtime._WAIT_TIMEOUT
        return runtime._WAIT_OBJECT_0

    def _terminate(handle: object, exit_code: int) -> int:
        termination_calls.append((handle, exit_code))
        return 1

    with pytest.raises(RuntimeError, match="elevated external CLI installer timed out"):
        runtime._wait_for_elevated_windows_installer(
            process_handle,
            _wait,
            _terminate,
            lambda: 0,
        )

    assert wait_timeouts == [
        runtime._ELEVATED_INSTALL_TIMEOUT_MILLISECONDS,
        runtime._ELEVATED_INSTALL_TERMINATION_WAIT_MILLISECONDS,
    ]
    assert termination_calls == [(process_handle, 1)]


def test_replace_runtime_keeps_backup_when_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "site-packages"
    staging = tmp_path / "staging-site-packages"
    target.mkdir()
    staging.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    (staging / "new.txt").write_text("new", encoding="utf-8")
    original_replace = runtime.os.replace
    replace_count = 0
    logged: list[tuple[object, ...]] = []

    def _replace(source: Path, destination: Path) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 1:
            original_replace(source, destination)
            return
        if replace_count == 2:
            raise OSError("failed to install staged runtime")
        raise OSError("failed to restore runtime backup")

    monkeypatch.setattr(runtime.os, "replace", _replace)
    monkeypatch.setattr(runtime.logger, "exception", lambda *args: logged.append(args))

    with pytest.raises(OSError, match="failed to restore runtime backup"):
        runtime._replace_runtime_directory(target, staging)

    backups = list(tmp_path.glob(".site-packages-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "old"
    assert not target.exists()
    assert staging.exists()
    assert logged and logged[0][1] == backups[0]


def test_download_retries_same_source_five_times_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts: list[str] = []
    progress: list[dict[str, Any]] = []

    def _download(context: runtime._DownloadAttemptContext) -> None:
        attempts.append(context.url)
        if len(attempts) < runtime._DOWNLOAD_ATTEMPTS_PER_SOURCE:
            raise runtime._RetryableDownloadError("temporary network failure")
        context.wheel_path.write_bytes(b"valid-wheel")

    monkeypatch.setattr(runtime, "_download_wheel_url", _download)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    wheel_path = _download_artifact(_artifact(), tmp_path, progress)

    assert wheel_path.read_bytes() == b"valid-wheel"
    assert attempts == ["https://primary.example/runtime.whl"] * 5
    assert [item["download_attempt"] for item in progress] == [1, 2, 3, 4, 5]
    assert all(item["download_max_attempts"] == 5 for item in progress)


def test_download_switches_source_after_five_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mirror_url = "https://mirror.example/runtime.whl"
    attempts: list[str] = []
    progress: list[dict[str, Any]] = []

    def _download(context: runtime._DownloadAttemptContext) -> None:
        attempts.append(context.url)
        if context.url == mirror_url:
            raise runtime._RetryableDownloadError("temporary network failure")
        context.wheel_path.write_bytes(b"valid-wheel")

    monkeypatch.setattr(runtime, "_download_wheel_url", _download)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    _download_artifact(_artifact(mirror_url=mirror_url), tmp_path, progress)

    assert attempts == [mirror_url] * 5 + ["https://primary.example/runtime.whl"]
    assert any(item["switching_source"] for item in progress)
    assert progress[-1]["download_attempt"] == 1
    assert progress[-1]["switching_source"] is True


def test_non_retryable_download_error_switches_source_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mirror_url = "https://mirror.example/runtime.whl"
    attempts: list[str] = []

    def _download(context: runtime._DownloadAttemptContext) -> None:
        attempts.append(context.url)
        if context.url == mirror_url:
            raise runtime._NonRetryableDownloadError("download returned HTTP 404")
        context.wheel_path.write_bytes(b"valid-wheel")

    monkeypatch.setattr(runtime, "_download_wheel_url", _download)

    _download_artifact(_artifact(mirror_url=mirror_url), tmp_path, [])

    assert attempts == [mirror_url, "https://primary.example/runtime.whl"]


def test_checksum_mismatch_switches_source_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mirror_url = "https://mirror.example/runtime.whl"
    attempts: list[str] = []

    def _download(context: runtime._DownloadAttemptContext) -> None:
        attempts.append(context.url)
        context.wheel_path.write_bytes(
            b"invalid-wheel" if context.url == mirror_url else b"valid-wheel"
        )

    monkeypatch.setattr(runtime, "_download_wheel_url", _download)

    wheel_path = _download_artifact(_artifact(mirror_url=mirror_url), tmp_path, [])

    assert attempts == [mirror_url, "https://primary.example/runtime.whl"]
    assert wheel_path.read_bytes() == b"valid-wheel"


def test_download_failure_removes_partial_wheel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts: list[str] = []

    def _download(context: runtime._DownloadAttemptContext) -> None:
        attempts.append(context.url)
        context.wheel_path.write_bytes(b"partial")
        raise runtime._RetryableDownloadError("download timed out")

    monkeypatch.setattr(runtime, "_download_wheel_url", _download)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    artifact = _artifact(mirror_url="https://mirror.example/runtime.whl")

    with pytest.raises(RuntimeError, match="download timed out"):
        _download_artifact(artifact, tmp_path, [])

    assert len(attempts) == 10
    assert not (tmp_path / "example-runtime-1.0.0.whl").exists()


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (404, runtime._NonRetryableDownloadError),
        (503, runtime._RetryableDownloadError),
    ],
)
def test_download_url_classifies_http_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
    expected_error: type[RuntimeError],
) -> None:
    request = httpx.Request("GET", "https://primary.example/runtime.whl")
    response = httpx.Response(status_code, request=request)

    class _ResponseContext:
        def __enter__(self) -> httpx.Response:
            raise httpx.HTTPStatusError(
                "request failed", request=request, response=response
            )

        def __exit__(self, *args: object) -> None:
            return None

    class _Client:
        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _ResponseContext:
            del method, url
            return _ResponseContext()

    monkeypatch.setattr(runtime.httpx, "Client", lambda **_kwargs: _Client())

    with pytest.raises(expected_error, match=f"HTTP {status_code}"):
        runtime._download_wheel_url(
            runtime._DownloadAttemptContext(
                artifact=_artifact(),
                operation=runtime._ArtifactOperationContext(
                    destination=tmp_path,
                    emit=lambda _message: None,
                    report_progress=lambda _progress: None,
                    artifact_index=1,
                    artifact_count=1,
                ),
                url="https://primary.example/runtime.whl",
                wheel_path=tmp_path / "runtime.whl",
                download_attempt=1,
                switching_source=False,
            ),
        )
