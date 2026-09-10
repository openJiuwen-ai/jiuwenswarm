#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build the reproducible, browser-free Playwright MCP runtime archive.

The resulting archive and manifest are committed to the repository. Release
builds consume those files directly and therefore never need npm access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "third_party" / "playwright-mcp"
RESOURCE_DIR = PROJECT_ROOT / "jiuwenswarm" / "resources" / "runtime" / "playwright-mcp"
PACKAGE_NAME = "@playwright/mcp"
PACKAGE_VERSION = "0.0.78"
MINIMUM_NODE_VERSION = 20
CLI_RELATIVE_PATH = "node_modules/@playwright/mcp/cli.js"
ARCHIVE_NAME = f"playwright-mcp-{PACKAGE_VERSION}.zip"
MANIFEST_NAME = "manifest.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
NPM_INSTALL_ARGUMENTS = (
    "ci",
    "--omit=dev",
    "--omit=optional",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
    "--bin-links=false",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _resolve_executable(name: str, explicit: str | None) -> str:
    candidate = explicit or shutil.which(name)
    if not candidate:
        raise RuntimeError(f"{name} was not found in PATH")
    return str(Path(candidate).resolve())


def _validate_runtime(node: str) -> None:
    node_result = subprocess.run(
        [node, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    node_version = (node_result.stdout or node_result.stderr).strip().lstrip("v")
    try:
        node_major = int(node_version.split(".", 1)[0])
    except ValueError as exc:
        raise RuntimeError(f"Could not determine Node.js version: {node_version!r}") from exc
    if node_result.returncode != 0 or node_major < MINIMUM_NODE_VERSION:
        raise RuntimeError(
            f"Node.js {MINIMUM_NODE_VERSION}+ is required; found {node_version!r}"
        )

    package_json = SOURCE_DIR / "node_modules" / "@playwright" / "mcp" / "package.json"
    cli = SOURCE_DIR / CLI_RELATIVE_PATH
    if not package_json.is_file() or not cli.is_file():
        raise RuntimeError(f"Installed Playwright MCP CLI is missing: {cli}")

    installed = json.loads(package_json.read_text(encoding="utf-8"))
    if installed.get("version") != PACKAGE_VERSION:
        raise RuntimeError(
            f"Expected {PACKAGE_NAME}@{PACKAGE_VERSION}, found {installed.get('version')!r}"
        )

    forbidden_paths: list[str] = []
    browser_executable_names = {
        "chrome",
        "chrome.exe",
        "headless_shell",
        "headless_shell.exe",
        "firefox",
        "firefox.exe",
        "playwright.exe",
    }
    for path in (SOURCE_DIR / "node_modules").rglob("*"):
        lowered_parts = {part.lower() for part in path.parts}
        if ".local-browsers" in lowered_parts:
            forbidden_paths.append(str(path.relative_to(SOURCE_DIR)))
        elif path.is_file() and path.name.lower() in browser_executable_names:
            forbidden_paths.append(str(path.relative_to(SOURCE_DIR)))
    if forbidden_paths:
        sample = ", ".join(forbidden_paths[:5])
        raise RuntimeError(f"Browser binaries must not be bundled; found: {sample}")

    help_result = subprocess.run(
        [node, str(cli), "--help"],
        cwd=SOURCE_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    help_output = (help_result.stdout + help_result.stderr).lower()
    if help_result.returncode != 0 or not any(
        marker in help_output for marker in ("playwright mcp", "playwright-mcp")
    ):
        detail = (help_result.stderr or help_result.stdout).strip()[-1000:]
        raise RuntimeError(f"Playwright MCP CLI verification failed: {detail}")


def _production_provenance(lock: dict[str, Any]) -> list[dict[str, str]]:
    provenance: list[dict[str, str]] = []
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise RuntimeError("package-lock.json does not contain a packages map")
    for package_path, metadata in sorted(packages.items()):
        if not package_path.startswith("node_modules/") or not isinstance(metadata, dict):
            continue
        if metadata.get("dev") is True:
            continue
        package_dir = SOURCE_DIR / package_path
        package_json_path = package_dir / "package.json"
        package_json: dict[str, Any] = {}
        if package_json_path.is_file():
            package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
        name = str(package_json.get("name") or package_path.removeprefix("node_modules/"))
        entry = {
            "name": name,
            "version": str(metadata.get("version") or package_json.get("version") or ""),
            "license": str(package_json.get("license") or metadata.get("license") or "UNKNOWN"),
            "resolved": str(metadata.get("resolved") or ""),
            "integrity": str(metadata.get("integrity") or ""),
        }
        provenance.append(entry)
    return provenance


def _is_generated_npm_path(relative: Path) -> bool:
    """Return whether *relative* is platform-dependent npm install metadata."""
    if relative.as_posix() == ".package-lock.json":
        return True

    parts = relative.parts
    return any(
        part == ".bin" and (index == 0 or parts[index - 1] == "node_modules")
        for index, part in enumerate(parts)
    )


def _archive_files(node_modules: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(
        node_modules.rglob("*"),
        key=lambda candidate: candidate.relative_to(node_modules).as_posix(),
    ):
        relative = path.relative_to(node_modules)
        if _is_generated_npm_path(relative):
            continue
        # pathlib follows symlinks for is_file(). Check first so a package
        # cannot silently make the archive depend on the host link layout.
        if path.is_symlink():
            raise RuntimeError(
                "Symbolic links outside npm-generated .bin directories must not be "
                f"bundled: {path.relative_to(SOURCE_DIR).as_posix()}"
            )
        if path.is_file():
            files.append(path)
    return files


def _write_deterministic_zip(output: Path) -> None:
    node_modules = SOURCE_DIR / "node_modules"
    files = _archive_files(node_modules)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_suffix(output.suffix + ".tmp")
    temp_output.unlink(missing_ok=True)
    with zipfile.ZipFile(
        temp_output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in files:
            relative = path.relative_to(SOURCE_DIR).as_posix()
            info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            # The runtime is invoked as ``node cli.js``; no archive member needs
            # an executable bit. A fixed mode keeps output identical on Windows,
            # macOS, and Linux.
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with path.open("rb") as source:
                archive.writestr(
                    info,
                    source.read(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
    os.replace(temp_output, output)


def build(*, npm_executable: str | None = None, node_executable: str | None = None) -> None:
    npm = _resolve_executable("npm", npm_executable)
    node = _resolve_executable("node", node_executable)
    lock_path = SOURCE_DIR / "package-lock.json"
    if not lock_path.is_file():
        raise RuntimeError(
            f"Missing {lock_path}. Generate and review the lockfile before building the archive."
        )

    build_env = os.environ.copy()
    build_env.update(
        {
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
            "PLAYWRIGHT_BROWSERS_PATH": "0",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
        }
    )
    _run([npm, *NPM_INSTALL_ARGUMENTS], cwd=SOURCE_DIR, env=build_env)
    _validate_runtime(node)

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    archive_path = RESOURCE_DIR / ARCHIVE_NAME
    _write_deterministic_zip(archive_path)
    archive_hash = _sha256(archive_path)
    manifest = {
        "schema_version": 1,
        "package": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "minimum_node_version": MINIMUM_NODE_VERSION,
        "cli_relative_path": CLI_RELATIVE_PATH,
        "archive": ARCHIVE_NAME,
        "archive_sha256": archive_hash,
        "provenance": {
            "source_manifest": "third_party/playwright-mcp/package.json",
            "source_lockfile": "third_party/playwright-mcp/package-lock.json",
            "source_lockfile_sha256": _sha256(lock_path),
            "install_command": f"npm {' '.join(NPM_INSTALL_ARGUMENTS)}",
            "browser_downloads_disabled": True,
            "dependencies": _production_provenance(lock),
        },
    }
    manifest_path = RESOURCE_DIR / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    LOGGER.info("Wrote %s (%s)", archive_path.relative_to(PROJECT_ROOT), archive_hash)
    LOGGER.info("Wrote %s", manifest_path.relative_to(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npm", help="Path to npm/npm.cmd")
    parser.add_argument("--node", help="Path to node executable")
    args = parser.parse_args()
    try:
        build(npm_executable=args.npm, node_executable=args.node)
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        LOGGER.error("error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
