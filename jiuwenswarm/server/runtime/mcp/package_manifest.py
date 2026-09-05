# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validated, manifest-driven access to an MCP asset package."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SAFE_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_INTEGRATION_TYPES = frozenset({"stdio-mcp", "remote-mcp", "cli", "skill-only"})
_CREDENTIAL_TYPES = frozenset({"token", "cli-oauth"})

logger = logging.getLogger(__name__)


class McpPackageError(ValueError):
    """Raised when an MCP package does not satisfy the package contract."""


@dataclass(frozen=True)
class McpPackageManifest:
    """Validated MCP package metadata and resolved declared paths."""

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


def _required_text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise McpPackageError(f"manifest field {field} must be a non-empty string")
    return value.strip()


def _required_localized_text(data: dict[str, Any], field: str) -> dict[str, str]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise McpPackageError(f"manifest field {field} must be an object")
    result: dict[str, str] = {}
    for language in ("zh", "en"):
        text = value.get(language)
        if not isinstance(text, str) or not text.strip():
            raise McpPackageError(
                f"manifest field {field}.{language} must be a non-empty string"
            )
        result[language] = text.strip()
    return result


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
    """Load and validate one MCP package rooted at *package_dir*.

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

    package_id = _required_text(raw, "id")
    if not _SAFE_PACKAGE_ID.fullmatch(package_id) or package_id in {".", ".."}:
        raise McpPackageError("manifest field id must be a safe directory name")
    if root.name != package_id:
        raise McpPackageError("manifest id must match the package directory name")
    if expected_id is not None and package_id != str(expected_id).strip():
        raise McpPackageError(
            f"manifest id does not match expected package id: {expected_id}"
        )

    version = _required_text(raw, "version")
    name = _required_text(raw, "name")
    description = _required_text(raw, "description")
    _required_localized_text(raw, "display_name")
    _required_localized_text(raw, "display_description")
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
        raise McpPackageError("skill-only integration must not declare integration.file")

    credentials = raw.get("credentials")
    credentials_type = "none"
    credentials_file: Path | None = None
    if credentials is not None:
        if not isinstance(credentials, dict):
            raise McpPackageError("manifest field credentials must be an object")
        credentials_type = str(credentials.get("type") or "").strip()
        if credentials_type not in _CREDENTIAL_TYPES:
            raise McpPackageError(
                "manifest credentials.type must be token or cli-oauth"
            )
        if credentials_type == "token" and credentials.get("file") is None:
            raise McpPackageError("token credentials must declare credentials.file")
        if credentials_type == "cli-oauth" and credentials.get("file") is not None:
            raise McpPackageError("cli-oauth credentials must not declare credentials.file")
        if credentials.get("file") is not None:
            credentials_file = _declared_path(
                root, credentials.get("file"), "credentials.file"
            )
            _read_json(credentials_file, "credentials.file")

    icon_file = None
    if raw.get("icon") is not None:
        icon_file = _declared_path(root, raw.get("icon"), "icon")
        if icon_file.suffix.lower() != ".png":
            raise McpPackageError("manifest field icon must reference a PNG file")

    skills = raw.get("skills", [])
    if not isinstance(skills, list):
        raise McpPackageError("manifest field skills must be an array")
    skill_dirs: list[Path] = []
    for index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            raise McpPackageError(f"manifest field skills[{index}] must be an object")
        skill_dirs.append(
            _declared_path(root, skill.get("dir"), f"skills[{index}].dir", directory=True)
        )
        if not _contains_skill(skill_dirs[-1]):
            raise McpPackageError(
                f"manifest field skills[{index}].dir must contain at least one SKILL.md"
            )
    if integration_type == "skill-only" and not skill_dirs:
        raise McpPackageError("skill-only integration requires at least one declared SKILL.md")

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
    """Load all visible package directories from the supplied roots.

    Invalid packages are logged and skipped so one corrupt marketplace entry
    cannot hide the rest of the list. Direct resolution remains strict.
    Duplicate runtime package IDs are rejected instead of silently choosing a
    built-in or Hub copy. The installer must prevent such collisions before a
    Hub package reaches its final directory.
    """
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
                raise McpPackageError(
                    f"duplicate MCP package id {package.package_id}: "
                    f"{previous} and {package.root}"
                )
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
    matches: list[McpPackageManifest] = []
    for package_root in package_roots:
        candidate = Path(package_root) / target
        if candidate.is_dir():
            matches.append(load_mcp_package(candidate, expected_id=target))
    if len(matches) > 1:
        raise McpPackageError(
            f"duplicate MCP package id {target}: "
            + " and ".join(str(package.root) for package in matches)
        )
    return matches[0] if matches else None
