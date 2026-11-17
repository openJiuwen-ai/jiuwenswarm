# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Turn an authorized custom configuration into a portable, credential-free source.

No connection, process launch, credential lookup or local file copy is performed.
Only the explicit runtime allowlist is copied; connection/account state is omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import urlsplit

from .asset_publish_models import PublishIdentity


@dataclass(frozen=True)
class CustomMcpSource:
    config: dict


def convert_mcp_config(
    config: dict, identity: PublishIdentity, metadata: dict, output_dir: Path
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identity.package_name):
        raise ValueError("invalid_custom_mcp_name")
    description = metadata.get("description")
    if (
        identity.kind != "mcp"
        or not isinstance(description, str)
        or not description.strip()
    ):
        raise ValueError("custom_mcp_description_required")
    if not isinstance(config, dict):
        raise ValueError("invalid_custom_mcp")
    if "mcpServers" in config:
        servers = config["mcpServers"]
        if not isinstance(servers, dict) or len(servers) != 1:
            raise ValueError("custom_mcp_requires_one_server")
        config = next(iter(servers.values()))
    if not isinstance(config, dict):
        raise ValueError("invalid_custom_mcp")
    fields = {}

    def placeholder(value, prefix):
        if not isinstance(value, str):
            raise ValueError("invalid_custom_mcp_value")
        existing = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        key = existing[1] if existing else f"{prefix}_{len(fields) + 1}"
        fields[key] = {
            "key": key,
            "label": key,
            "type": "password",
            "required": True,
            "secret": True,
        }
        return "${" + key + "}"

    def local_path(value):
        return (
            value.startswith(("~", "./", "../"))
            or Path(value).is_absolute()
            or bool(PureWindowsPath(value).drive)
        )

    server = {}
    if config.get("cwd"):
        raise ValueError("custom_mcp_local_path")
    if config.get("url"):
        if config.get("command"):
            raise ValueError("invalid_custom_mcp")
        url = urlsplit(config["url"])
        host = url.hostname or ""
        has_credentials = bool(url.username or url.password or url.fragment)
        if (
            url.scheme != "https"
            or not host
            or has_credentials
        ):
            raise ValueError("custom_mcp_nonportable_url")
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError("custom_mcp_nonportable_url")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError("custom_mcp_nonportable_url")
        except ValueError as exc:
            if str(exc) == "custom_mcp_nonportable_url":
                raise
        # A seemingly ordinary path or hostname can contain a personal access
        # credential. Never infer that a custom endpoint is safe to disclose.
        # The existing installer resolves placeholders in the entire URL string.
        server["url"] = "https://" + placeholder("${MCP_ENDPOINT}", "URL")
        fields["MCP_ENDPOINT"].update(
            label="HTTPS 连接地址（不含 https://）",
            label_en="HTTPS endpoint (without https://)",
            description="填写域名、路径和查询参数；系统会添加 https:// 前缀。",
            description_en="Enter host, path and query; https:// is added automatically.",
        )
        transport = config.get("type", config.get("transport", "streamable-http"))
        if transport not in ("sse", "http", "streamable-http"):
            raise ValueError("invalid_custom_mcp_transport")
        server["type"] = transport
        integration = "remote-mcp"
    else:
        command = config.get("command")
        if command not in ("npx", "uvx", "python", "python3"):
            raise ValueError("custom_mcp_command_requires_review")
        args = config.get("args", [])
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or local_path(arg) for arg in args
        ):
            raise ValueError("custom_mcp_local_path")
        server["command"] = command
        converted = []
        package_seen = False
        expect_value = False
        if command in ("python", "python3"):
            if (
                len(args) < 2
                or args[0] != "-m"
                or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", args[1]
                )
            ):
                raise ValueError("custom_mcp_module_required")
            converted.extend(args[:2])
            args = args[2:]
            package_seen = True
        for arg in args:
            if not package_seen:
                if arg in ("-y", "--yes") and command == "npx":
                    converted.append(arg)
                    continue
                if not re.fullmatch(
                    r"(?:@[a-z0-9_.-]+/)?[a-z0-9][a-z0-9_.-]*(?:@[a-zA-Z0-9_.^~-]+)?",
                    arg,
                ):
                    raise ValueError("custom_mcp_package_requires_review")
                converted.append(arg)
                package_seen = True
            elif arg.startswith("--"):
                key, sep, value = arg.partition("=")
                if not re.fullmatch(r"--[a-zA-Z][a-zA-Z0-9-]*", key):
                    raise ValueError("custom_mcp_argument_requires_review")
                converted.append(key + "=" + placeholder(value, "ARG") if sep else key)
                expect_value = not sep
            elif expect_value:
                converted.append(placeholder(arg, "ARG"))
                expect_value = False
            else:
                raise ValueError("custom_mcp_argument_requires_review")
        if not package_seen:
            raise ValueError("custom_mcp_package_required")
        server["args"] = converted
        integration = "stdio-mcp"
    for name in ("env", "headers"):
        values = config.get(name, {})
        if not isinstance(values, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key)
            for key in values
        ):
            raise ValueError("invalid_custom_mcp_value")
        if values:
            server[name] = {
                key: (
                    "Bearer " + placeholder(value[7:], "HEADER")
                    if name == "headers"
                    and isinstance(value, str)
                    and value.startswith("Bearer ")
                    else placeholder(value, name.upper())
                )
                for key, value in values.items()
            }
    display = metadata.get("display_name") or identity.package_name
    if not isinstance(display, str):
        raise ValueError("invalid_custom_mcp_display_name")
    manifest = {
        "id": identity.package_name,
        "name": identity.package_name,
        "version": identity.version,
        "package_type": "mcp",
        "description": description,
        "display_name": {"zh": display, "en": display},
        "display_description": {"zh": description, "en": description},
        "integration": {"type": integration, "file": "mcp.json"},
        "skills": [],
    }
    if fields:
        manifest["credentials"] = {"type": "token", "file": "token-schema.json"}
    root = Path(output_dir) / identity.package_name
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, value in [
        ("manifest.json", manifest),
        ("mcp.json", {"mcpServers": {identity.package_name: server}}),
        ("token-schema.json", {"fields": list(fields.values())}),
    ]:
        (root / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (root / "README.md").write_text(
        f"# {display}\n\n{description}\n\n安装前请填写 token-schema.json 声明的参数。原账号凭证未包含在包中。\n"
        "Supply your own values for the declared parameters before connecting.\n"
        + (
            "Requires " + server["command"] + " on PATH.\n"
            if integration == "stdio-mcp"
            else (
                "MCP_ENDPOINT：填写 HTTPS 连接地址中 https:// 之后的部分（域名、路径和查询参数），不要重复填写协议。\n"
                "MCP_ENDPOINT: supply the host, path and query of your HTTPS endpoint, "
                "without the https:// prefix.\n"
            )
        ),
        encoding="utf-8",
    )
    return root
