"""Helpers for constructing manifest-compliant MCP package fixtures."""

from __future__ import annotations

import json
from pathlib import Path


def write_manifest(
    package: Path,
    integration_type: str,
    *,
    credentials_type: str | None = None,
    skills: bool = False,
) -> None:
    integration: dict[str, str] = {"type": integration_type}
    if integration_type in {"stdio-mcp", "remote-mcp"}:
        integration["file"] = "mcp.json"
    elif integration_type == "cli":
        integration["file"] = "cli.json"
    manifest = {
        "version": "1.0.0",
        "package_type": "mcp",
        "id": package.name,
        "name": package.name,
        "description": f"{package.name} test package",
        "display_name": {"zh": package.name, "en": package.name},
        "display_description": {
            "zh": f"{package.name} test package",
            "en": f"{package.name} test package",
        },
        "integration": integration,
        "skills": [{"dir": "skills", "mode": "all"}] if skills else [],
    }
    if credentials_type is not None:
        credentials: dict[str, str] = {"type": credentials_type}
        if credentials_type == "token":
            credentials["file"] = "token-schema.json"
        manifest["credentials"] = credentials
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
