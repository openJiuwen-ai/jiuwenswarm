"""Helpers for constructing manifest-compliant MCP package fixtures."""

from __future__ import annotations

import base64
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


def write_canva_oauth_package(package: Path, *, legacy_header: bool = False) -> Path:
    """Create a synthetic OAuth fixture, not a distributable Canva connector."""
    package.mkdir(parents=True)
    server = {"type": "streamable-http", "url": "https://mcp.canva.cn/mcp", "timeout": 600}
    if legacy_header:
        server["headers"] = {"Authorization": "Bearer ${OAUTH_ACCESS_TOKEN}"}
    (package / "mcp.json").write_text(
        json.dumps({"mcpServers": {package.name: server}}), encoding="utf-8"
    )
    write_manifest(package, "remote-mcp", credentials_type="oauth")
    path = package / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["icon"] = "icon.png"
    manifest["skills"] = [{"dir": "skills/canva-guide", "mode": "all"}]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (package / "icon.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
    ))
    skill = package / "skills/canva-guide/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: mock-canva\ndescription: Synthetic OAuth test skill\n---\n# Mock Canva\n",
        encoding="utf-8",
    )
    return package
