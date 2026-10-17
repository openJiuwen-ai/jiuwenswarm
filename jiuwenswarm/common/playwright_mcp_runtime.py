# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve and safely materialize the bundled Playwright MCP runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, MutableMapping, Sequence

import portalocker

from jiuwenswarm.common.utils import get_user_workspace_dir


logger = logging.getLogger(__name__)

PLAYWRIGHT_MCP_VERSION = "0.0.78"
MINIMUM_NODE_VERSION = 20
PINNED_NPX_COMMAND = "npx"
PINNED_NPX_ARGS = ("-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}")
LEGACY_TEMPLATE_COMMAND = "npx"
LEGACY_TEMPLATE_ARGS = "-y @playwright/mcp@latest"
MANIFEST_FILENAME = "manifest.json"
INTERNAL_SOURCE_ENV = "JIUWENSWARM_PLAYWRIGHT_MCP_LAUNCH_SOURCE"
INTERNAL_COMMAND_ENV = "JIUWENSWARM_PLAYWRIGHT_MCP_MANAGED_COMMAND"
INTERNAL_ARGS_ENV = "JIUWENSWARM_PLAYWRIGHT_MCP_MANAGED_ARGS"
_INTERNAL_LAUNCH_SOURCES = frozenset({"bundled", "pinned-npx-fallback"})


class PlaywrightMcpRuntimeError(RuntimeError):
    """The bundled Playwright MCP runtime could not be used safely."""


@dataclass(frozen=True)
class PlaywrightMcpLaunch:
    source: str
    command: str
    args: tuple[str, ...]
    version: str
    runtime_display_path: str = ""


def parse_playwright_mcp_args(value: str | None) -> list[str]:
    """Parse a JSON argument list or a shell-style argument string."""
    raw = (value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return shlex.split(raw)


def serialize_playwright_mcp_args(args: Sequence[str]) -> str:
    """Serialize arguments without losing path boundaries or spaces."""
    return json.dumps([str(arg) for arg in args], ensure_ascii=False)


def _resource_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "resources" / "runtime" / "playwright-mcp"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(resource_dir: Path) -> dict[str, object]:
    manifest_path = resource_dir / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlaywrightMcpRuntimeError(f"invalid runtime manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise PlaywrightMcpRuntimeError("Playwright MCP runtime manifest must be a JSON object")

    expected = {
        "version": PLAYWRIGHT_MCP_VERSION,
        "minimum_node_version": MINIMUM_NODE_VERSION,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise PlaywrightMcpRuntimeError(
                f"runtime manifest {key} must be {value!r}, found {manifest.get(key)!r}"
            )
    for key in ("archive", "archive_sha256", "cli_relative_path"):
        if not isinstance(manifest.get(key), str) or not str(manifest[key]).strip():
            raise PlaywrightMcpRuntimeError(f"runtime manifest is missing {key}")
    archive_name = str(manifest["archive"])
    if Path(archive_name).name != archive_name:
        raise PlaywrightMcpRuntimeError("runtime manifest archive must be a filename")
    archive_hash = str(manifest["archive_sha256"]).lower()
    if re.fullmatch(r"[0-9a-f]{64}", archive_hash) is None:
        raise PlaywrightMcpRuntimeError("runtime manifest archive_sha256 is invalid")
    _safe_relative_path(str(manifest["cli_relative_path"]), label="CLI path")
    return manifest


def _safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    if "\\" in value:
        raise PlaywrightMcpRuntimeError(f"{label} contains a backslash")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PlaywrightMcpRuntimeError(f"{label} is not a safe relative path: {value!r}")
    if ":" in path.parts[0]:
        raise PlaywrightMcpRuntimeError(f"{label} contains a drive prefix: {value!r}")
    return path


def _cache_marker_matches(target: Path, *, version: str, archive_hash: str, cli_path: str) -> bool:
    if target.is_symlink():
        return False
    marker = target / ".complete.json"
    cli_relative = _safe_relative_path(cli_path, label="CLI path")
    cli = target.joinpath(*cli_relative.parts)
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        target_resolved = target.resolve(strict=True)
        cli_resolved = cli.resolve(strict=True)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(metadata, dict)
        and metadata.get("version") == version
        and metadata.get("archive_sha256") == archive_hash
        and cli.is_file()
        and not cli.is_symlink()
        and target_resolved in cli_resolved.parents
    )


def _extract_archive_safely(archive_path: Path, temporary_root: Path) -> None:
    seen: set[str] = set()
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PlaywrightMcpRuntimeError(f"invalid runtime archive: {archive_path}") from exc
    with archive:
        for info in archive.infolist():
            member = _safe_relative_path(info.filename.rstrip("/"), label="archive member")
            normalized = member.as_posix()
            if normalized in seen:
                raise PlaywrightMcpRuntimeError(f"duplicate runtime archive member: {normalized}")
            seen.add(normalized)
            member_mode = info.external_attr >> 16
            if stat.S_ISLNK(member_mode):
                raise PlaywrightMcpRuntimeError(f"runtime archive contains a symlink: {normalized}")
            destination = temporary_root.joinpath(*member.parts)
            temporary_resolved = temporary_root.resolve()
            destination_resolved = destination.resolve(strict=False)
            if temporary_resolved not in destination_resolved.parents:
                raise PlaywrightMcpRuntimeError(f"runtime archive escapes extraction root: {normalized}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            if member_mode & 0o111:
                destination.chmod(destination.stat().st_mode | 0o111)


def _remove_invalid_cache_target(target: Path, version_root: Path) -> None:
    target_parent = target.parent.resolve(strict=True)
    if target_parent != version_root.resolve(strict=True):
        raise PlaywrightMcpRuntimeError(f"refusing to replace cache outside {version_root}")
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def materialize_bundled_runtime(
    *,
    resource_dir: Path | None = None,
    user_workspace_dir: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Verify and atomically extract the bundled runtime, returning its CLI."""
    resources = (resource_dir or _resource_dir()).resolve()
    manifest = _load_manifest(resources)
    archive_path = resources / str(manifest["archive"])
    expected_hash = str(manifest["archive_sha256"]).lower()
    try:
        actual_hash = _sha256(archive_path)
    except OSError as exc:
        raise PlaywrightMcpRuntimeError(f"runtime archive is missing: {archive_path}") from exc
    if actual_hash != expected_hash:
        raise PlaywrightMcpRuntimeError(
            f"runtime archive hash mismatch: expected {expected_hash}, found {actual_hash}"
        )

    workspace = (user_workspace_dir or get_user_workspace_dir()).expanduser()
    version = str(manifest["version"])
    hash_prefix = expected_hash[:12]
    version_root = workspace / "runtime" / "playwright-mcp" / version
    target = version_root / hash_prefix
    version_root.mkdir(parents=True, exist_ok=True)
    lock_path = version_root / ".extract.lock"
    cli_relative_path = str(manifest["cli_relative_path"])

    with portalocker.Lock(str(lock_path), mode="a", timeout=60):
        if not _cache_marker_matches(
            target,
            version=version,
            archive_hash=expected_hash,
            cli_path=cli_relative_path,
        ):
            temporary = Path(tempfile.mkdtemp(prefix=f".{hash_prefix}.tmp-", dir=version_root))
            try:
                _extract_archive_safely(archive_path, temporary)
                cli_relative = _safe_relative_path(cli_relative_path, label="CLI path")
                staged_cli = temporary.joinpath(*cli_relative.parts)
                if not staged_cli.is_file() or staged_cli.is_symlink():
                    raise PlaywrightMcpRuntimeError(
                        f"runtime archive does not contain CLI: {cli_relative_path}"
                    )
                marker = {
                    "version": version,
                    "archive_sha256": expected_hash,
                    "cli_relative_path": cli_relative_path,
                }
                (temporary / ".complete.json").write_text(
                    json.dumps(marker, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                _remove_invalid_cache_target(target, version_root)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)

    cli_relative = _safe_relative_path(cli_relative_path, label="CLI path")
    cli = target.joinpath(*cli_relative.parts)
    if not _cache_marker_matches(
        target,
        version=version,
        archive_hash=expected_hash,
        cli_path=cli_relative_path,
    ):
        raise PlaywrightMcpRuntimeError(f"materialized runtime cache is incomplete: {target}")
    return cli, manifest


def resolve_node_executable(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    minimum_major: int = MINIMUM_NODE_VERSION,
) -> str:
    """Resolve the PATH-preferred Node executable and enforce Node 20+."""
    candidate = which("node")
    if not candidate:
        raise PlaywrightMcpRuntimeError(
            f"Node.js {minimum_major}+ is required for browser runtime support"
        )
    node = str(Path(candidate).resolve())
    try:
        result = run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlaywrightMcpRuntimeError(f"failed to execute Node.js at {node}") from exc
    version_text = (result.stdout or result.stderr or "").strip()
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){1,2}(?:[-+].*)?", version_text)
    if result.returncode != 0 or match is None:
        raise PlaywrightMcpRuntimeError(f"could not determine Node.js version at {node}")
    if int(match.group(1)) < minimum_major:
        raise PlaywrightMcpRuntimeError(
            f"Node.js {minimum_major}+ is required for browser runtime support; found {version_text}"
        )
    return node


def _environment_uses_legacy_template_defaults(environ: Mapping[str, str]) -> bool:
    """Recognize the exact external launch defaults shipped by older releases."""
    return (
        "PLAYWRIGHT_MCP_COMMAND" in environ
        and "PLAYWRIGHT_MCP_ARGS" in environ
        and environ.get("PLAYWRIGHT_MCP_COMMAND", "").strip() == LEGACY_TEMPLATE_COMMAND
        and environ.get("PLAYWRIGHT_MCP_ARGS", "").strip() == LEGACY_TEMPLATE_ARGS
    )


def _environment_has_override(environ: Mapping[str, str]) -> bool:
    command_present = "PLAYWRIGHT_MCP_COMMAND" in environ
    args_present = "PLAYWRIGHT_MCP_ARGS" in environ
    if not command_present and not args_present:
        return False
    if _environment_uses_legacy_template_defaults(environ):
        return False
    source = environ.get(INTERNAL_SOURCE_ENV, "")
    if source not in _INTERNAL_LAUNCH_SOURCES:
        return True
    return not (
        environ.get("PLAYWRIGHT_MCP_COMMAND", "") == environ.get(INTERNAL_COMMAND_ENV, "")
        and environ.get("PLAYWRIGHT_MCP_ARGS", "") == environ.get(INTERNAL_ARGS_ENV, "")
    )


def resolve_playwright_mcp_launch(
    *,
    environ: Mapping[str, str] | None = None,
    resource_dir: Path | None = None,
    user_workspace_dir: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> PlaywrightMcpLaunch:
    """Resolve override, bundled, then pinned-npx launch precedence."""
    environment = environ if environ is not None else os.environ
    if _environment_uses_legacy_template_defaults(environment):
        logger.warning(
            "Ignoring legacy Playwright MCP .env defaults (%s %s) so the bundled "
            "Playwright MCP %s runtime can be used. Custom command or argument "
            "overrides remain supported.",
            LEGACY_TEMPLATE_COMMAND,
            LEGACY_TEMPLATE_ARGS,
            PLAYWRIGHT_MCP_VERSION,
        )
    if _environment_has_override(environment):
        command = (environment.get("PLAYWRIGHT_MCP_COMMAND") or PINNED_NPX_COMMAND).strip()
        raw_args = environment.get("PLAYWRIGHT_MCP_ARGS")
        args = parse_playwright_mcp_args(raw_args) if (raw_args or "").strip() else list(PINNED_NPX_ARGS)
        return PlaywrightMcpLaunch(
            source="override",
            command=command or PINNED_NPX_COMMAND,
            args=tuple(args),
            version="administrator-override",
        )

    try:
        cli, manifest = materialize_bundled_runtime(
            resource_dir=resource_dir,
            user_workspace_dir=user_workspace_dir,
        )
        node = resolve_node_executable(which=which, run=run)
        archive_hash = str(manifest["archive_sha256"])
        return PlaywrightMcpLaunch(
            source="bundled",
            command=node,
            args=(str(cli.resolve()),),
            version=str(manifest["version"]),
            runtime_display_path=(
                f"runtime/playwright-mcp/{manifest['version']}/{archive_hash[:12]}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 - bundle failures are intentionally browser-scoped
        logger.warning(
            "Bundled Playwright MCP %s is unavailable (%s). Falling back to "
            "pinned npx resolution; this path may access the npm registry.",
            PLAYWRIGHT_MCP_VERSION,
            exc,
        )
        return PlaywrightMcpLaunch(
            source="pinned-npx-fallback",
            command=PINNED_NPX_COMMAND,
            args=PINNED_NPX_ARGS,
            version=PLAYWRIGHT_MCP_VERSION,
        )


def record_managed_launch_environment(
    environ: MutableMapping[str, str],
    launch: PlaywrightMcpLaunch,
    serialized_args: str,
) -> None:
    """Record values JiuWenSwarm generated so they are not mistaken for overrides."""
    environ[INTERNAL_SOURCE_ENV] = launch.source
    if launch.source in _INTERNAL_LAUNCH_SOURCES:
        environ[INTERNAL_COMMAND_ENV] = launch.command
        environ[INTERNAL_ARGS_ENV] = serialized_args
    else:
        environ.pop(INTERNAL_COMMAND_ENV, None)
        environ.pop(INTERNAL_ARGS_ENV, None)


def clear_managed_launch_environment(environ: MutableMapping[str, str]) -> None:
    """Clear generated launch values without deleting administrator overrides."""
    source = environ.get(INTERNAL_SOURCE_ENV, "")
    if source in _INTERNAL_LAUNCH_SOURCES:
        if environ.get("PLAYWRIGHT_MCP_COMMAND", "") == environ.get(
            INTERNAL_COMMAND_ENV, ""
        ):
            environ.pop("PLAYWRIGHT_MCP_COMMAND", None)
        if environ.get("PLAYWRIGHT_MCP_ARGS", "") == environ.get(INTERNAL_ARGS_ENV, ""):
            environ.pop("PLAYWRIGHT_MCP_ARGS", None)
    environ.pop(INTERNAL_SOURCE_ENV, None)
    environ.pop(INTERNAL_COMMAND_ENV, None)
    environ.pop(INTERNAL_ARGS_ENV, None)
