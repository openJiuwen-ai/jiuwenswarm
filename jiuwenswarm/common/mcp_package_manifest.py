# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest-driven access to an MCP asset package.

Manifest metadata is advisory: a bad value is logged and degraded instead of
blocking installation. Only ``package_type``, the integration declaration and
path safety reject a package.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_INTEGRATION_TYPES = frozenset({"stdio-mcp", "remote-mcp", "cli", "skill-only"})
_CREDENTIAL_TYPES = frozenset({"token", "cli-oauth", "oauth"})

logger = logging.getLogger(__name__)


class McpPackageError(ValueError):
    """Raised when an MCP package does not satisfy the package contract."""


@dataclass(frozen=True)
class McpPackageManifest:
    """MCP package metadata and resolved declared paths."""

    root: Path
    package_id: str
    version: str
    name: str
    description: str
    integration_type: str
    integration_file: Path | None
    credentials_type: str
    credentials_file: Path | None
    icon_file: Path | None
    skill_dirs: tuple[Path, ...]
    server_name: str | None
    server_config: dict[str, Any] | None
    raw: dict[str, Any]


def _read_json(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise McpPackageError(f"{field} does not exist: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise McpPackageError(f"{field} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise McpPackageError(f"{field} must contain a JSON object")
    return value


def _optional_text(data: dict[str, Any], field: str, fallback: str) -> str:
    """Return *field* as text, falling back to *fallback* when absent or blank."""
    value = data.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    logger.warning("manifest field %s is missing; falling back to %r", field, fallback)
    return fallback


def _warn_localized_text(data: dict[str, Any], field: str, fallback: str) -> None:
    """Warn when *field* is not usable ``{zh, en}`` display text; never fails."""
    value = data.get(field)
    if not isinstance(value, dict):
        logger.warning(
            "manifest field %s is missing or not an object; falling back to %r",
            field,
            fallback,
        )
        return
    for language in ("zh", "en"):
        text = value.get(language)
        if not isinstance(text, str) or not text.strip():
            logger.warning(
                "manifest field %s.%s is missing; falling back to %r",
                field,
                language,
                fallback,
            )


def _declared_path(root: Path, value: Any, field: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise McpPackageError(f"manifest field {field} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise McpPackageError(f"manifest field {field} must stay inside the package")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise McpPackageError(
                f"manifest field {field} must not reference a symbolic link"
            )
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise McpPackageError(f"manifest field {field} must stay inside the package") from exc
    exists = resolved.is_dir() if directory else resolved.is_file()
    if not exists:
        expected = "directory" if directory else "file"
        raise McpPackageError(f"manifest field {field} must reference an existing {expected}")
    return resolved


def _contains_skill(directory: Path) -> bool:
    return any(path.is_file() for path in directory.rglob("SKILL.md"))


def _load_server(integration_file: Path, integration_type: str) -> tuple[str, dict[str, Any]]:
    raw = _read_json(integration_file, "integration.file")
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        raise McpPackageError("integration.file must declare one mcpServers entry")
    if len(servers) != 1:
        raise McpPackageError("an MCP package must declare exactly one mcpServers entry")
    server_name, server_config = next(iter(servers.items()))
    if not isinstance(server_name, str) or not server_name.strip() or not isinstance(server_config, dict):
        raise McpPackageError("integration.file contains an invalid mcpServers entry")
    required_key = "command" if integration_type == "stdio-mcp" else "url"
    if not isinstance(server_config.get(required_key), str) or not server_config[required_key].strip():
        raise McpPackageError(f"{integration_type} integration requires server {required_key}")
    return server_name, server_config


def load_mcp_package(package_dir: Path, *, expected_id: str | None = None) -> McpPackageManifest:
    """Load one MCP package rooted at *package_dir*.

    Every capability path comes from ``manifest.json``. Fixed filenames that
    are present but undeclared do not participate in package classification.
    """
    root = Path(package_dir)
    if not root.is_dir():
        raise McpPackageError(f"MCP package directory does not exist: {root}")
    if root.is_symlink():
        raise McpPackageError("MCP package directory must not be a symbolic link")
    if (root / "manifest.json").is_symlink():
        raise McpPackageError("manifest.json must not be a symbolic link")
    raw = _read_json(root / "manifest.json", "manifest.json")
    if raw.get("package_type") != "mcp":
        raise McpPackageError("manifest field package_type must equal mcp")

    package_id = root.name
    declared_id = str(raw.get("id") or "").strip()
    if declared_id != package_id:
        logger.warning(
            "manifest id %r does not match package directory %r; using the directory name",
            declared_id or None,
            package_id,
        )
    if expected_id is not None and package_id != str(expected_id).strip():
        raise McpPackageError(
            f"package directory does not match expected package id: {expected_id}"
        )

    version = _optional_text(raw, "version", "")
    name = _optional_text(raw, "name", package_id)
    description = _optional_text(raw, "description", "")
    _warn_localized_text(raw, "display_name", name)
    _warn_localized_text(raw, "display_description", description)
    integration = raw.get("integration")
    if not isinstance(integration, dict):
        raise McpPackageError("manifest field integration must be an object")
    integration_type = str(integration.get("type") or "").strip()
    if integration_type not in _INTEGRATION_TYPES:
        raise McpPackageError(
            "manifest integration.type must be stdio-mcp, remote-mcp, cli, or skill-only"
        )

    integration_file: Path | None = None
    server_name: str | None = None
    server_config: dict[str, Any] | None = None
    if integration_type in {"stdio-mcp", "remote-mcp", "cli"}:
        integration_file = _declared_path(root, integration.get("file"), "integration.file")
        if integration_type in {"stdio-mcp", "remote-mcp"}:
            server_name, server_config = _load_server(integration_file, integration_type)
        else:
            _read_json(integration_file, "integration.file")
    elif integration.get("file") is not None:
        logger.warning("skill-only integration declares integration.file; ignoring it")

    credentials = raw.get("credentials")
    credentials_type = "none"
    credentials_file: Path | None = None
    if credentials is not None:
        if not isinstance(credentials, dict):
            logger.warning("manifest field credentials is not an object; ignoring it")
            credentials = {}
        declared_type = str(credentials.get("type") or "").strip()
        if declared_type not in _CREDENTIAL_TYPES:
            if declared_type:
                logger.warning(
                    "manifest credentials.type %r is not supported; treating it as none",
                    declared_type,
                )
            declared_type = "none"
        declared_file = credentials.get("file")
        if declared_type == "token" and declared_file is None:
            logger.warning(
                "token credentials declare no credentials.file; treating them as none"
            )
            declared_type = "none"
        elif declared_type in {"cli-oauth", "oauth"} and declared_file is not None:
            logger.warning(
                "%s credentials must not declare credentials.file; ignoring the file",
                declared_type,
            )
            declared_file = None
        credentials_type = declared_type
        if declared_file is not None:
            try:
                credentials_file = _declared_path(
                    root, declared_file, "credentials.file"
                )
                _read_json(credentials_file, "credentials.file")
            except McpPackageError as exc:
                logger.warning("ignoring credentials.file: %s", exc)
                credentials_file = None

    icon_file = None
    if raw.get("icon") is not None:
        try:
            candidate = _declared_path(root, raw.get("icon"), "icon")
        except McpPackageError as exc:
            logger.warning("ignoring icon: %s", exc)
        else:
            if candidate.suffix.lower() == ".png":
                icon_file = candidate
            else:
                logger.warning("manifest icon is not a PNG file; ignoring %s", candidate)

    skills = raw.get("skills", [])
    if not isinstance(skills, list):
        logger.warning("manifest field skills is not an array; ignoring it")
        skills = []
    skill_dirs: list[Path] = []
    for index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            logger.warning("manifest field skills[%d] is not an object; skipping it", index)
            continue
        try:
            skill_dir = _declared_path(
                root, skill.get("dir"), f"skills[{index}].dir", directory=True
            )
        except McpPackageError as exc:
            logger.warning("skipping skills[%d]: %s", index, exc)
            continue
        if not _contains_skill(skill_dir):
            logger.warning("skills[%d].dir contains no SKILL.md; skipping it", index)
            continue
        skill_dirs.append(skill_dir)
    if integration_type == "skill-only" and not skill_dirs:
        logger.warning("skill-only integration declares no usable SKILL.md")

    return McpPackageManifest(
        root=root.resolve(),
        package_id=package_id,
        version=version,
        name=name,
        description=description,
        integration_type=integration_type,
        integration_file=integration_file,
        credentials_type=credentials_type,
        credentials_file=credentials_file,
        icon_file=icon_file,
        skill_dirs=tuple(skill_dirs),
        server_name=server_name,
        server_config=server_config,
        raw=raw,
    )


def iter_mcp_packages(*package_roots: Path) -> list[McpPackageManifest]:
    """Load visible packages with root precedence; invalid packages are skipped."""
    packages: list[McpPackageManifest] = []
    seen: dict[str, Path] = {}
    for package_root in package_roots:
        root = Path(package_root)
        if not root.is_dir():
            continue
        for package_dir in sorted(root.iterdir(), key=lambda path: path.name):
            if not package_dir.is_dir() or package_dir.name.startswith("."):
                continue
            try:
                package = load_mcp_package(package_dir)
            except McpPackageError as exc:
                logger.warning("invalid MCP package %s skipped: %s", package_dir, exc)
                continue
            previous = seen.get(package.package_id)
            if previous is not None:
                logger.warning(
                    "duplicate MCP package id %s ignored at %s; using %s",
                    package.package_id,
                    package.root,
                    previous,
                )
                continue
            seen[package.package_id] = package.root
            packages.append(package)
    return packages


def resolve_mcp_package(
    package_id: str,
    *package_roots: Path,
) -> McpPackageManifest | None:
    """Resolve one runtime package ID across built-in and Hub package roots."""
    target = str(package_id or "").strip()
    if not target:
        return None
    for package_root in package_roots:
        candidate = Path(package_root) / target
        if candidate.is_dir():
            return load_mcp_package(candidate, expected_id=target)
    return None
