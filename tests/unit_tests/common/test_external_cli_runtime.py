# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for optional external CLI runtime downloads."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable
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
    monkeypatch.setattr(runtime, "_runtime_site_packages", lambda _cli_agent: target)

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
    monkeypatch.setattr(runtime, "_runtime_site_packages", _site_packages)
    monkeypatch.setattr(runtime, "get_external_cli_runtime_status", lambda _agent: {"runtime_state": "current"})
    original_path = list(runtime.sys.path)
    monkeypatch.setattr(runtime.sys, "path", list(original_path))
    monkeypatch.setattr(runtime.logger, "debug", lambda *args: logged.append(args))

    runtime.activate_external_cli_runtime_paths()

    assert runtime.sys.path[: len(original_path)] == original_path
    cli_agents = runtime.external_cli_runtime_names()
    assert runtime.sys.path == original_path
    for cli_agent in cli_agents:
        site_packages = str(_site_packages(cli_agent))
        assert site_packages not in runtime.sys.path
        assert any(
            args[1] == cli_agent
            and str(args[2]) == site_packages
            and isinstance(args[3], OSError)
            for args in logged
        )


def test_windows_sdk_path_is_independent_of_application_layout(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils

    monkeypatch.setattr(runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path / "data")
    for executable in (tmp_path / "app" / "workswarm.exe", tmp_path / "app" / "resources" / "backend" / "workswarm.exe"):
        monkeypatch.setattr(runtime.sys, "executable", str(executable))
        assert runtime.external_cli_site_packages("claude") == tmp_path / "data/runtime/external-cli/claude/site-packages"


def test_optional_runtime_unavailable_on_platform_does_not_block_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(runtime, "_load_manifest", lambda: {"agents": {"claude": {"platforms": {}}}})
    monkeypatch.setattr(runtime, "_runtime_site_packages", lambda _agent: tmp_path / "site-packages")
    original_path = list(runtime.sys.path)
    monkeypatch.setattr(runtime.sys, "path", list(original_path))
    runtime.activate_external_cli_runtime_paths()
    assert runtime.sys.path == original_path


@pytest.mark.parametrize("state", ["current", "update_required", "invalid", "invalid_encoding"])
def test_windows_activation_validates_preserved_sdk_before_import(monkeypatch, tmp_path, state):
    target = tmp_path / "site-packages"
    module = target / "example_sdk"
    module.mkdir(parents=True)
    executable = module / "cli.exe"
    executable.write_bytes(b"fixture")
    artifacts = [_artifact()]
    entry = {"platforms": {"windows-x86_64": artifacts}, "required_modules": ["example_sdk"], "executables": {"windows-x86_64": "example_sdk/cli.exe"}}
    metadata = {"agent": "claude", "platform": "windows-x86_64", "packages": runtime._runtime_package_versions(artifacts)}
    if state == "update_required":
        metadata["packages"][0]["version"] = "0.0.1"
    if state == "invalid":
        executable.unlink()
    (target / runtime._INSTALL_METADATA_FILE).write_text(json.dumps(metadata))
    if state == "invalid_encoding":
        (target / runtime._INSTALL_METADATA_FILE).write_bytes(b"\xff\xfe\x80")
    monkeypatch.setattr(runtime, "_load_manifest", lambda: {"agents": {"claude": entry}})
    monkeypatch.setattr(runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(runtime, "_runtime_platform_key", lambda: "windows-x86_64")
    monkeypatch.setattr(runtime, "_runtime_site_packages", lambda _agent: target)
    monkeypatch.setattr(runtime.sys, "path", list(runtime.sys.path))
    expected_state = "invalid" if state == "invalid_encoding" else state
    assert runtime.get_external_cli_runtime_status("claude")["runtime_state"] == expected_state
    runtime.activate_external_cli_runtime_paths()
    assert (str(target) in runtime.sys.path) == (state == "current")
    if state == "invalid_encoding":
        # An invalid optional runtime must remain repairable by the normal
        # staged install path, without importing its previous contents.
        staging = tmp_path / "staged"
        (staging / "example_sdk").mkdir(parents=True)
        (staging / "example_sdk/cli.exe").write_bytes(b"replacement")
        runtime._complete_staged_runtime_install("claude", "windows-x86_64", artifacts, target, staging)
        runtime.activate_external_cli_runtime_paths()
        assert runtime.get_external_cli_runtime_status("claude")["runtime_state"] == "current"
        assert str(target) in runtime.sys.path


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


_PROVIDER_PLATFORM = "macos-arm64"
_PROVIDER_EXECUTABLE = "openjiuwen_harness_acme/_bundled/bin/acme"


def _provider_wheel(directory: Path, *, entry_point: str | None = "acme") -> Path:
    """Build a minimal third-party harness provider wheel named as the installer expects."""
    wheel = directory / "openjiuwen-harness-acme-0.1.0.whl"
    dist_info = "openjiuwen_harness_acme-0.1.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openjiuwen_harness_acme/__init__.py", "")
        archive.writestr(_PROVIDER_EXECUTABLE, "#!/bin/sh\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: openjiuwen-harness-acme\nVersion: 0.1.0\n",
        )
        if entry_point:
            archive.writestr(
                f"{dist_info}/entry_points.txt",
                f"[openjiuwen.harness_providers]\n{entry_point} = openjiuwen_harness_acme:Provider\n",
            )
    return wheel


def _provider_manifest(wheel: Path) -> dict[str, Any]:
    artifact = {
        "name": "openjiuwen-harness-acme",
        "version": "0.1.0",
        "url": "https://downloads.example/acme.whl",
        "mirror_url": "",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    return {
        "schema_version": 1,
        "agents": {},
        "providers": {
            "acme": {
                "distribution": "openjiuwen-harness-acme",
                "version": "0.1.0",
                "entry_point": "acme",
                "required_modules": ["openjiuwen_harness_acme"],
                "executables": {_PROVIDER_PLATFORM: _PROVIDER_EXECUTABLE},
                "platforms": {_PROVIDER_PLATFORM: [artifact]},
            },
        },
    }


def _use_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest: dict[str, Any]) -> None:
    path = tmp_path / "external_cli_runtime.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runtime, "_manifest_path", lambda: path)


def _install_provider(tmp_path: Path, manifest: dict[str, Any], wheel_directory: Path) -> Path:
    target = tmp_path / "runtime" / "acme" / "site-packages"
    target.parent.mkdir(parents=True)
    artifacts = manifest["providers"]["acme"]["platforms"][_PROVIDER_PLATFORM]
    runtime._install_external_cli_runtime_from_artifacts(
        "acme",
        _PROVIDER_PLATFORM,
        artifacts,
        target,
        wheel_directory,
    )
    return target


def test_bundled_manifest_describes_builtin_runtime_integrity() -> None:
    manifest = runtime._load_manifest()

    assert runtime.external_cli_runtime_names()[:2] == ("claude", "codex")
    assert manifest["agents"]["claude"]["executables"]["macos-arm64"] == "claude_agent_sdk/_bundled/claude"
    assert manifest["agents"]["codex"]["required_modules"] == ["openai_codex", "codex_cli_bin"]


def test_manifest_lists_third_party_providers_after_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _provider_manifest(_provider_wheel(tmp_path))
    manifest["agents"] = {"claude": {"required_modules": ["claude_agent_sdk"], "platforms": {}}}
    _use_manifest(monkeypatch, tmp_path, manifest)

    assert runtime.external_cli_runtime_names() == ("claude", "acme")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda manifest: manifest["agents"].update(acme=manifest["providers"]["acme"]), "duplicate runtime"),
        (lambda manifest: manifest["providers"].update({"../acme": manifest["providers"]["acme"]}), "name must match"),
        (lambda manifest: manifest["providers"]["acme"].update(required_modules=[]), "required_modules"),
        (
            lambda manifest: manifest["providers"]["acme"]["executables"].update({_PROVIDER_PLATFORM: "../acme"}),
            "relative to site-packages",
        ),
        (lambda manifest: manifest["providers"]["acme"].update(entry_point=""), "entry_point"),
    ],
)
def test_manifest_rejects_entries_the_installer_cannot_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    manifest = _provider_manifest(_provider_wheel(tmp_path))
    mutate(manifest)
    _use_manifest(monkeypatch, tmp_path, manifest)

    with pytest.raises(RuntimeError, match=message):
        runtime.external_cli_runtime_names()


def test_unknown_runtime_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_manifest(monkeypatch, tmp_path, _provider_manifest(_provider_wheel(tmp_path)))

    with pytest.raises(ValueError, match="unsupported external cli agent: missing"):
        runtime.external_cli_site_packages("missing")


def test_provider_runtime_installs_and_exposes_its_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _provider_manifest(_provider_wheel(tmp_path))
    _use_manifest(monkeypatch, tmp_path, manifest)

    target = _install_provider(tmp_path, manifest, tmp_path)

    assert (target / _PROVIDER_EXECUTABLE).is_file()
    monkeypatch.setattr(runtime, "_runtime_site_packages", lambda _cli_agent: target)
    monkeypatch.setattr(runtime, "_runtime_platform_key", lambda: _PROVIDER_PLATFORM)
    monkeypatch.setattr(runtime.sys, "path", [*runtime.sys.path, str(target)])
    runtime._verify_installed_runtime("acme")
    status = runtime.get_external_cli_runtime_status("acme")
    assert status["runtime_state"] == "current"
    assert status["installed_packages"] == [{"name": "openjiuwen-harness-acme", "version": "0.1.0"}]


def test_provider_runtime_without_entry_point_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _provider_manifest(_provider_wheel(tmp_path, entry_point=None))
    _use_manifest(monkeypatch, tmp_path, manifest)

    with pytest.raises(RuntimeError, match="entry point openjiuwen.harness_providers:acme"):
        _install_provider(tmp_path, manifest, tmp_path)

    assert not (tmp_path / "runtime" / "acme" / "site-packages").exists()


def test_staged_runtime_requires_the_platform_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    staging = tmp_path / "site-packages"
    (staging / "claude_agent_sdk").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="claude_agent_sdk/_bundled/claude.exe"):
        runtime._validate_staged_runtime("claude", "windows-x86_64", staging)

    (staging / "claude_agent_sdk" / "_bundled").mkdir()
    (staging / "claude_agent_sdk" / "_bundled" / "claude.exe").write_bytes(b"")
    runtime._validate_staged_runtime("claude", "windows-x86_64", staging)
