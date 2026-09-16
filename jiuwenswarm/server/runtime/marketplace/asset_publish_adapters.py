# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline validation and normalization of private publish snapshots.

Callers own snapshot acquisition. This module never resolves installed assets,
reads user configuration, imports packaged code, or connects to an MCP server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit, parse_qsl, unquote

import yaml

from .asset_publish_models import PublishIdentity
from ..mcp.package_manifest import McpPackageError, load_mcp_package


class PublishValidationError(ValueError):
    def __init__(
        self, code: str, field: str = "", message: str = "Package validation failed"
    ):
        self.code = code
        self.field = field
        super().__init__(message)


def _fail(code="INVALID_PACKAGE", field="", message="Package validation failed"):
    raise PublishValidationError(code, field, message)


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        _fail(field=field, message="A non-empty text field is required")
    return value


def _json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail(field=path.name, message="A required JSON file is missing or invalid")
    if not isinstance(value, dict):
        _fail(field=path.name, message="JSON must contain an object")
    return value


def _path(root, value, field, directory=False):
    _text(value, field)
    relative = Path(value)
    invalid_root = relative.is_absolute() or bool(PureWindowsPath(value).drive)
    if (
        invalid_root
        or "\\" in value
        or ".." in relative.parts
    ):
        _fail("INVALID_REFERENCE", field, "Reference must stay inside the package")
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            _fail("INVALID_REFERENCE", field, "Symbolic links are not allowed")
    if not (candidate.is_dir() if directory else candidate.is_file()):
        _fail("MISSING_REFERENCE", field, "A declared file or directory is missing")
    return candidate


def _entries(data, field):
    value = data.get(field, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _fail(
            field=field, message="Capability declarations must be an array of objects"
        )
    return value


def _capabilities(root, data, metadata, dependencies):
    for key in ("skills", "tools", "rails", "mcps"):
        for entry in _entries(data, key):
            if key == "mcps" and "connector" in entry:
                connector = _text(entry["connector"], key)
                sources = metadata.get("dependency_sources", {})
                source = sources.get(connector) if isinstance(sources, dict) else None
                if not isinstance(source, str) or not source.strip():
                    _fail(
                        "DEPENDENCY_SOURCE_REQUIRED",
                        "mcps",
                        "External connector requires an acquisition source",
                    )
                dependencies.append(
                    {"kind": "mcp", "connector": connector, "source": source}
                )
                continue
            ref = "dir" if "dir" in entry else "file"
            if key == "skills" and ref != "dir":
                _fail(field=key, message="Skills must declare a directory")
            path = _path(root, entry.get(ref), key, directory=ref == "dir")
            if key == "skills":
                if not any(path.rglob("SKILL.md")):
                    _fail(field=key, message="Skill directory must contain SKILL.md")
                dependencies.append(
                    {"kind": "skill", "path": entry[ref], "embedded": True}
                )
            if key == "mcps":
                dependencies.append(
                    {"kind": "mcp", "path": entry[ref], "embedded": True}
                )
                configs = [path] if ref == "file" else sorted(path.rglob("*.json"))
                for config_path in configs:
                    if config_path.is_file():
                        _check_config(_json(config_path))


def _expert(root, data, metadata, dependencies, child=False):
    _text(data.get("agent_name" if child else "name"), "name")
    if not child:
        _text(data.get("description"), "description")
    if "persona" in data:
        persona = data["persona"]
        if not isinstance(persona, dict):
            _fail(field="persona")
        directory = _path(root, persona.get("dir"), "persona.dir", True)
        if not any(directory.rglob("*.md")):
            _fail(field="persona", message="Persona requires Markdown content")
    if "model" in data:
        model = data["model"]
        if not isinstance(model, dict):
            _fail(field="model")
        content = _json(_path(root, model.get("file"), "model.file"))
        _check_config(content)
        if not isinstance(content.get("model"), dict):
            _fail(field="model", message="Model file must contain a model object")
    if child and "subagents" in data:
        _fail(
            field="subagents",
            message="Subagents may only be declared by the root expert",
        )
    _capabilities(root, data, metadata, dependencies)
    for subagent in _entries(data, "subagents"):
        directory = _path(root, subagent.get("dir"), "subagents.dir", True)
        _expert(
            directory,
            _json(directory / ".subagent.json"),
            metadata,
            dependencies,
            child=True,
        )


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|credential|cookie)", re.I
)


def _check_config(value, key=""):
    """Reject credential literals in integration configs without reflecting values."""
    if isinstance(value, dict):
        # Token schema fields are declarative; no saved values belong in them.
        if isinstance(value.get("fields"), list):
            for entry in value["fields"]:
                if isinstance(entry, dict) and "key" in entry:
                    if any(
                        name in entry and entry[name] is not None and entry[name] != ""
                        for name in ("default", "value")
                    ):
                        _fail(
                            "SENSITIVE_CONTENT",
                            "credentials",
                            "Credential schema must not contain stored values",
                        )
        for child_key, child_value in value.items():
            _check_config(child_value, str(child_key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_config(item, key)
            if (
                isinstance(item, str)
                and item.startswith("-")
                and _SENSITIVE.search(item)
            ):
                secret = (
                    item.split("=", 1)[1]
                    if "=" in item
                    else (value[index + 1] if index + 1 < len(value) else "")
                )
                if secret and not _PLACEHOLDER.fullmatch(str(secret)):
                    _fail(
                        "SENSITIVE_CONTENT",
                        "integration",
                        "Replace credentials with declared placeholders",
                    )
    elif not isinstance(value, str) and value is not None and _SENSITIVE.search(key):
        _fail(
            "SENSITIVE_CONTENT", "integration", "Credential values must be placeholders"
        )
    elif isinstance(value, str):
        if (
            _SENSITIVE.search(key)
            and value
            and not re.fullmatch(
                r"(?:Bearer |Basic )?\$\{[A-Za-z_][A-Za-z0-9_]*\}", value
            )
        ):
            _fail(
                "SENSITIVE_CONTENT",
                "integration",
                "Replace credentials with declared placeholders",
            )
        if value.startswith(("http://", "https://")):
            try:
                url = urlsplit(value)
                if url.username or url.password:
                    _fail(
                        "SENSITIVE_CONTENT",
                        "integration",
                        "URLs must not contain credentials",
                    )
                for query_key, query_value in parse_qsl(url.query):
                    _check_config(query_value, query_key)
            except ValueError:
                _fail(field="integration", message="Invalid integration URL")
        if key in ("command", "cwd", "args") and (
            Path(value).is_absolute() or PureWindowsPath(value).drive
        ):
            _fail(
                "INVALID_REFERENCE",
                "integration",
                "Integration must not depend on an absolute local path",
            )


def _skill(root, identity, metadata):
    path = _path(root, "SKILL.md", "SKILL.md")
    try:
        raw = path.read_text(encoding="utf-8")
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?=\r?\n|$)", raw, re.S)
        if not match:
            _fail(field="SKILL.md", message="SKILL.md requires YAML frontmatter")
        front = yaml.safe_load(match.group(1))
    except (OSError, UnicodeError, yaml.YAMLError):
        _fail(field="SKILL.md", message="Invalid skill frontmatter")
    if not isinstance(front, dict):
        _fail(field="SKILL.md")
    _text(front.get("name"), "name")
    _text(front.get("description"), "description")
    if front.get("kind") == "team-skill" or metadata.get("skill_type") == "teamskills":
        roles = front.get("roles")
        if (
            not isinstance(roles, list)
            or len(roles) < 2
            or any(
                not isinstance(role, dict)
                or not isinstance(role.get("id"), str)
                or not role["id"].strip()
                for role in roles
            )
        ):
            _fail(field="roles", message="Team skills require at least two valid roles")
        if len({role["id"].strip() for role in roles}) != len(roles):
            _fail(field="roles", message="Team skill role IDs must be unique")
    _check_config(front.get("roles", []))
    for target in re.findall(
        r"!?\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", raw[match.end():]
    ):
        target = target.strip("<>")
        if target.startswith("#") or urlsplit(target).scheme:
            continue
        relative = unquote(target.split("#", 1)[0])
        if relative:
            _path(root, relative, "SKILL.md.references")
    changes = []
    for key, value in {
        "name": identity.package_name,
        "version": identity.version,
        **{
            k: metadata[k]
            for k in ("display_name", "description", "tags")
            if k in metadata
        },
    }.items():
        if front.get(key) != value:
            front[key] = value
            changes.append("SKILL.md." + key)
    _text(front["description"], "description")
    tags = front.get("tags", ["teamskills"])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        _fail(field="tags")
    wrapper = {
        "name": identity.package_name,
        "version": identity.version,
        "display_name": front.get("display_name") or identity.package_name,
        "description": front["description"],
        "runtime": {"type": "skill"},
        "metadata": {"author": front.get("author") or "unknown", "tags": tags},
    }
    path.write_text(
        "---\n"
        + yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
        + "---"
        + raw[match.end():],
        encoding="utf-8",
    )
    return {"normalizations": changes, "dependencies": [], "wrapper": wrapper}


def normalize_and_validate(
    snapshot: Path, identity: PublishIdentity, metadata: dict[str, object]
) -> dict[str, object]:
    """Normalize identity only in an already private, bounded snapshot."""
    root = Path(snapshot)
    if (
        root.is_symlink()
        or not root.is_dir()
        or any(path.is_symlink() for path in root.rglob("*"))
    ):
        _fail(
            "INVALID_REFERENCE",
            message="Snapshot must be a directory without symbolic links",
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identity.package_name):
        _fail(field="package_name", message="Publish name must be a machine identifier")
    if identity.kind == "skill":
        return _skill(root, identity, metadata)
    if len(list(root.rglob("manifest.json"))) != 1:
        _fail(
            "MULTIPLE_MANIFESTS",
            "manifest.json",
            "Installer requires exactly one root manifest",
        )
    data = _json(root / "manifest.json")
    if data.get("package_type") != identity.kind:
        _fail(
            field="package_type",
            message="Package type does not match the publishing type",
        )
    field = "name" if identity.kind == "agent_template" else "id"
    old = _text(data.get(field), field)
    if old != identity.package_name:
        other_fields = {
            key: value
            for key, value in data.items()
            if key not in (field, "description", "display_name", "display_description")
        }
        unsafe = old in json.dumps(other_fields, ensure_ascii=False)
        for path in root.rglob("*"):
            if (
                path.is_file()
                and path.name != "manifest.json"
                and path.suffix not in (".md", ".png", ".jpg", ".jpeg", ".gif", ".ico")
            ):
                if old.encode("utf-8") in path.read_bytes():
                    unsafe = True
        if unsafe:
            _fail(
                "UNSAFE_IDENTITY_RENAME",
                field,
                "Internal identity references require manual adjustment",
            )
    changes = []
    for key, value in ((field, identity.package_name), ("version", identity.version)):
        if data.get(key) != value:
            changes.append("manifest." + key)
    data[field], data["version"] = identity.package_name, identity.version
    dependencies = []
    if identity.kind == "plugin":
        for forbidden in (
            "persona",
            "agent_card",
            "model",
            "subagents",
            "memories",
            "rubrics",
        ):
            if forbidden in data:
                _fail(
                    field=forbidden, message="This root field is forbidden in plugins"
                )
        _capabilities(root, data, metadata, dependencies)
    elif identity.kind == "agent_template":
        _expert(root, data, metadata, dependencies)
    (root / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if identity.kind == "mcp":
        try:
            package = load_mcp_package(root, expected_id=identity.package_name)
        except McpPackageError:
            _fail(
                field="manifest.json",
                message="MCP package does not satisfy the local package contract",
            )
        config = _json(package.integration_file) if package.integration_file else {}
        _check_config(config)
        for server in config.get("mcpServers", {}).values():
            if isinstance(server, dict) and "url" in server:
                endpoint = server["url"]
                if not isinstance(endpoint, str) or not endpoint.startswith(
                    ("http://", "https://")
                ):
                    _fail(
                        field="mcp.url",
                        message=("MCP URL must start with http:// or https://; "
                                 "keep the scheme outside declared placeholders"),
                    )
        placeholders = set(_PLACEHOLDER.findall(json.dumps(config)))
        defined = set()
        if package.credentials_file:
            schema = _json(package.credentials_file)
            for entry in _entries(schema, "fields"):
                key = _text(entry.get("key"), "credentials.fields.key")
                if key in defined or not re.fullmatch("[A-Za-z_][A-Za-z0-9_]*", key):
                    _fail(field="credentials.fields.key")
                defined.add(key)
                if any(
                    name in entry and entry[name] is not None and entry[name] != ""
                    for name in ("default", "value")
                ):
                    _fail(
                        "SENSITIVE_CONTENT",
                        "credentials",
                        "Credential schema must not contain stored values",
                    )
        if placeholders - defined and package.credentials_type != "cli-oauth":
            _fail(
                "UNDEFINED_PLACEHOLDER",
                "credentials",
                "Every integration placeholder requires a token schema field",
            )
        if package.icon_file and not package.icon_file.read_bytes().startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            _fail(field="icon", message="Icon must contain PNG data")
    return {"normalizations": changes, "dependencies": dependencies, "wrapper": None}
