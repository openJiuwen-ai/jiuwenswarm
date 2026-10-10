import json

import pytest

from jiuwenswarm.server.runtime.marketplace.asset_mcp_publish_converter import (
    convert_mcp_config,
)
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.asset_package_builder import (
    build_asset_package,
)


def test_remote_redacts_every_header_env_and_query_value(tmp_path):
    config = {
        "url": "https://example.com/mcp?key=private-query",
        "headers": {"Authorization": "Bearer private-header"},
        "env": {"OTHER": "private-env"},
    }
    root = convert_mcp_config(
        config,
        PublishIdentity("mcp", "demo", "1.0.0"),
        {"description": "User approved description"},
        tmp_path / "source",
    )
    contents = "\n".join(p.read_text() for p in root.iterdir())
    assert "private-" not in contents
    assert config["env"]["OTHER"] == "private-env"
    package = build_asset_package(
        root, PublishIdentity("mcp", "demo", "1.0.0"), {}, tmp_path / "artifacts"
    )
    assert package.artifact_path.exists()
    assert json.loads((root / "token-schema.json").read_text())["fields"]


@pytest.mark.parametrize(
    "config",
    [
        {"command": "/Users/me/bin/server"},
        {"command": "node", "args": ["/Users/me/private.js"]},
        {"command": "node", "args": ["relative.js"]},
        {"command": "sh", "args": ["-c", "secret shell command"]},
        {"url": "http://localhost:1234/mcp"},
        {"url": "https://user:secret@example.com/mcp"},
    ],
)
def test_blocks_nonportable_config(tmp_path, config):
    with pytest.raises(ValueError):
        convert_mcp_config(
            config,
            PublishIdentity("mcp", "demo", "1.0.0"),
            {"description": "Description"},
            tmp_path,
        )


def test_stdio_package_and_secret_arguments(tmp_path):
    root = convert_mcp_config(
        {
            "command": "npx",
            "args": ["-y", "@example/server", "--token", "private-arg"],
            "env": {"TOKEN": "private-env"},
        },
        PublishIdentity("mcp", "demo", "1.0.0"),
        {"description": "Description"},
        tmp_path,
    )
    text = (root / "mcp.json").read_text()
    assert "private-" not in text
    assert "@example/server" in text
    build_asset_package(
        root, PublishIdentity("mcp", "demo", "1.0.0"), {}, tmp_path / "artifacts"
    )


def test_requires_user_description(tmp_path):
    with pytest.raises(ValueError):
        convert_mcp_config(
            {"url": "https://example.com/mcp"},
            PublishIdentity("mcp", "demo", "1.0.0"),
            {},
            tmp_path,
        )


def test_portable_python_module(tmp_path):
    root = convert_mcp_config(
        {"command": "python3", "args": ["-m", "example.server", "--api-key=private"]},
        PublishIdentity("mcp", "demo", "1.0.0"),
        {"description": "Description"},
        tmp_path,
    )
    assert "private" not in (root / "mcp.json").read_text()
    build_asset_package(
        root, PublishIdentity("mcp", "demo", "1.0.0"), {}, tmp_path / "artifacts"
    )


def test_cannot_write_outside_private_source(tmp_path):
    with pytest.raises(ValueError):
        convert_mcp_config(
            {"url": "https://example.com/mcp"},
            PublishIdentity("mcp", "../escape", "1.0.0"),
            {"description": "Description"},
            tmp_path,
        )
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com/mcp/opaque-personal-access-credential",
        "https://example.com/api/v2/mcp?key=private-query",
    ],
)
def test_remote_endpoint_is_installer_parameter_without_path_secrets(
    tmp_path, endpoint
):
    root = convert_mcp_config(
        {"url": endpoint},
        PublishIdentity("mcp", "demo", "1.0.0"),
        {"description": "Description"},
        tmp_path,
    )
    text = "\n".join(path.read_text() for path in root.iterdir())
    assert endpoint not in text
    assert "opaque-personal-access-credential" not in text
    assert "private-query" not in text
    server = json.loads((root / "mcp.json").read_text())["mcpServers"]["demo"]
    assert server["url"] == "https://${MCP_ENDPOINT}"
    fields = json.loads((root / "token-schema.json").read_text())["fields"]
    assert any(field["key"] == "MCP_ENDPOINT" and field["required"] for field in fields)
    assert "MCP_ENDPOINT" in (root / "README.md").read_text()
    build_asset_package(
        root, PublishIdentity("mcp", "demo", "1.0.0"), {}, tmp_path / "artifacts"
    )
    from jiuwenswarm.server.runtime.mcp.credential import resolve_placeholders

    class Tokens:
        def get_all(self, name):
            return {"MCP_ENDPOINT": "installer.example.com/own/mcp"}

    resolved = resolve_placeholders(server, Tokens(), "demo")
    assert resolved["url"] == "https://installer.example.com/own/mcp"
