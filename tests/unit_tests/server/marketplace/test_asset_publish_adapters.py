import json

import pytest
import yaml

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.asset_publish_adapters import (
    PublishValidationError,
    normalize_and_validate,
)


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def package(tmp_path, kind="plugin", **extra):
    root = tmp_path / "demo"
    root.mkdir()
    write(
        root,
        "manifest.json",
        dict(
            package_type=kind,
            id="demo",
            name="demo",
            version="0.1.0",
            description="Example",
            **extra,
        ),
    )
    return root


def run(root, kind="plugin", metadata=None):
    return normalize_and_validate(
        root, PublishIdentity(kind, "demo", "1.0.0"), metadata or {}
    )


def test_skill_preserves_body_roles_and_returns_compatible_wrapper(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    body = "\n# Instructions\nKeep exact body.\n"
    (root / "SKILL.md").write_text(
        "---\nname: old\ndescription: Original\nkind: team-skill\nroles:\n- id: a\n- id: b\n---"
        + body
    )
    result = run(root, "skill", {"description": "New", "tags": ["one"]})
    text = (root / "SKILL.md").read_text()
    assert text.endswith(body)
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == "demo" and front["version"] == "1.0.0"
    assert front["roles"] == [{"id": "a"}, {"id": "b"}]
    assert result["wrapper"]["runtime"] == {"type": "skill"}
    assert result["wrapper"]["metadata"] == {"author": "unknown", "tags": ["one"]}


@pytest.mark.parametrize(
    "header", ["name: x", "name: x\ndescription: d\nkind: team-skill\nroles: [{id: a}]"]
)
def test_rejects_invalid_skill_frontmatter(tmp_path, header):
    (tmp_path / "SKILL.md").write_text("---\n" + header + "\n---\nbody")
    with pytest.raises(PublishValidationError):
        run(tmp_path, "skill")


@pytest.mark.parametrize(
    "key", ["persona", "agent_card", "model", "subagents", "memories", "rubrics"]
)
def test_plugin_forbidden_root_fields(tmp_path, key):
    root = package(tmp_path, **{key: {}})
    with pytest.raises(PublishValidationError) as error:
        run(root)
    assert error.value.field == key


@pytest.mark.parametrize(
    "path", ["../outside.py", "/tmp/tool.py", "C:\\private\\tool.py", "missing.py"]
)
def test_rejects_invalid_declared_paths(tmp_path, path):
    root = package(tmp_path, tools=[{"file": path}])
    with pytest.raises(PublishValidationError):
        run(root)


def test_nested_manifest_rejected(tmp_path):
    root = package(tmp_path)
    write(root, "nested/manifest.json", {})
    with pytest.raises(PublishValidationError) as error:
        run(root)
    assert error.value.code == "MULTIPLE_MANIFESTS"


def test_external_dependency_requires_acquisition_source(tmp_path):
    root = package(tmp_path, mcps=[{"connector": "external"}])
    with pytest.raises(PublishValidationError) as error:
        run(root)
    assert error.value.code == "DEPENDENCY_SOURCE_REQUIRED"
    result = run(
        root,
        metadata={"dependency_sources": {"external": "Install from official catalog"}},
    )
    assert result["dependencies"][0]["source"] == "Install from official catalog"


def test_expert_model_and_subagent_validation(tmp_path):
    root = package(
        tmp_path,
        "agent_template",
        persona={"dir": "persona"},
        model={"file": "model.json"},
        subagents=[{"dir": "child"}],
    )
    (root / "persona").mkdir()
    (root / "persona/p.md").write_text("# Expert")
    write(root, "model.json", {"model": {"model_id": "example"}})
    write(root, "child/.subagent.json", {"agent_name": "child"})
    assert run(root, "agent_template")["wrapper"] is None
    write(root, "child/.subagent.json", {"agent_name": "child", "subagents": []})
    with pytest.raises(PublishValidationError):
        run(root, "agent_template")


def test_agent_group_becomes_hub_expert_team_without_mutating_source(tmp_path):
    root = package(
        tmp_path,
        "agent_group",
        instruction="Coordinate the team",
        agents=["leader", "reviewer"],
        skills=["shared"],
    )
    write(
        root,
        "agents/leader/manifest.json",
        {
            "package_type": "agent_template",
            "name": "Team Lead",
            "description": "Leads the work",
            "persona": {"dir": "."},
        },
    )
    (root / "agents/leader/AGENT.md").write_text("# Lead")
    write(
        root,
        "agents/reviewer/manifest.json",
        {
            "package_type": "agent_template",
            "name": "Reviewer",
            "description": "Reviews the work",
            "persona": {"dir": "persona"},
        },
    )
    (root / "agents/reviewer/persona").mkdir()
    (root / "agents/reviewer/persona/reviewer.md").write_text("# Review")
    (root / "skills/shared").mkdir(parents=True)
    (root / "skills/shared/SKILL.md").write_text(
        "---\nname: shared\ndescription: Shared team skill\n---\n# Shared"
    )

    result = run(
        root,
        "agent_group",
        {"display_name": "Review Team", "description": "Published team"},
    )

    manifest = json.loads((root / "manifest.json").read_text())
    reviewer = json.loads((root / "agents/reviewer/.subagent.json").read_text())
    assert manifest["package_type"] == "agent_template"
    assert manifest["name"] == "demo" and manifest["version"] == "1.0.0"
    assert manifest["display_name"] == "Review Team"
    assert manifest["persona"] == {"dir": "agents/leader"}
    assert manifest["subagents"] == [{"dir": "agents/reviewer"}]
    assert manifest["skills"] == [{"dir": "skills/shared", "mode": "all"}]
    assert reviewer["agent_name"] == "reviewer"
    assert reviewer["persona"] == {"dir": "persona"}
    assert not (root / "agents/leader/manifest.json").exists()
    assert not (root / "agents/reviewer/manifest.json").exists()
    assert result["wrapper"] is None


def test_agent_group_rejects_windows_absolute_member_reference(tmp_path):
    root = package(
        tmp_path,
        "agent_group",
        agents=["leader"],
    )
    write(
        root,
        "agents/leader/manifest.json",
        {
            "package_type": "agent_template",
            "name": "Team Lead",
            "description": "Leads the work",
            "persona": {"dir": r"C:\\private"},
        },
    )

    with pytest.raises(PublishValidationError) as error:
        run(root, "agent_group")

    assert error.value.code == "INVALID_REFERENCE"
    assert error.value.field == "persona.dir"


def mcp(tmp_path, server):
    root = package(
        tmp_path,
        "mcp",
        display_name={"zh": "示例", "en": "Demo"},
        display_description={"zh": "示例", "en": "Demo"},
        integration={"type": "remote-mcp", "file": "mcp.json"},
    )
    write(root, "mcp.json", {"mcpServers": {"demo": server}})
    return root


def test_mcp_placeholder_needs_schema(tmp_path):
    root = mcp(
        tmp_path,
        {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ${TOKEN}"},
        },
    )
    with pytest.raises(PublishValidationError) as error:
        run(root, "mcp")
    assert error.value.code == "UNDEFINED_PLACEHOLDER"
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["credentials"] = {"type": "token", "file": "token-schema.json"}
    write(root, "manifest.json", manifest)
    write(root, "token-schema.json", {"fields": [{"key": "TOKEN", "required": True}]})
    assert run(root, "mcp")["wrapper"] is None


@pytest.mark.parametrize(
    "server",
    [
        {
            "url": "https://example.com",
            "headers": {"Authorization": "Bearer privatevalue"},
        },
        {"url": "https://example.com?api_key=privatevalue"},
        {"url": "https://user:privatevalue@example.com"},
    ],
)
def test_mcp_rejects_credentials_without_echoing_them(tmp_path, server):
    root = mcp(tmp_path, server)
    with pytest.raises(PublishValidationError) as error:
        run(root, "mcp")
    assert "privatevalue" not in str(error.value)


def test_rename_with_self_reference_is_blocked(tmp_path):
    root = package(tmp_path, tools=[{"file": "tool.py"}])
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["id"] = "old"
    write(root, "manifest.json", manifest)
    (root / "tool.py").write_text("from old.tools import Tool")
    with pytest.raises(PublishValidationError) as error:
        run(root)
    assert error.value.code == "UNSAFE_IDENTITY_RENAME"


@pytest.mark.parametrize(
    "server",
    [
        {
            "url": "https://example.com",
            "headers": {"Authorization": "Bearer ${TOKEN}privatevalue"},
        },
        {"url": "https://example.com", "env": {"API_KEY": "${TOKEN}:privatevalue"}},
        {"url": "https://example.com", "args": ["/Users/private/run.js"]},
    ],
)
def test_mcp_rejects_partial_placeholders_and_machine_paths(tmp_path, server):
    root = mcp(tmp_path, server)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["credentials"] = {"type": "cli-oauth"}
    write(root, "manifest.json", manifest)
    with pytest.raises(PublishValidationError):
        run(root, "mcp")


def test_skill_rejects_missing_relative_markdown_reference(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nRead [guide](docs/missing.md)."
    )
    with pytest.raises(PublishValidationError) as error:
        run(tmp_path, "skill")
    assert error.value.code == "MISSING_REFERENCE"


@pytest.mark.parametrize("kind", ["plugin", "agent_template"])
def test_embedded_mcp_directory_rejects_nested_credentials(tmp_path, kind):
    root = package(tmp_path, kind, mcps=[{"dir": "mcps"}])
    write(
        root,
        "mcps/nested/server.json",
        {
            "mcpServers": {
                "demo": {
                    "url": "https://example.com",
                    "headers": {"Authorization": "Bearer privatevalue"},
                }
            }
        },
    )
    with pytest.raises(PublishValidationError) as error:
        run(root, kind)
    assert error.value.code == "SENSITIVE_CONTENT"
    assert "privatevalue" not in str(error.value)


def test_embedded_mcp_directory_accepts_placeholder_config(tmp_path):
    root = package(tmp_path, mcps=[{"dir": "mcps"}])
    write(
        root,
        "mcps/server.json",
        {
            "mcpServers": {
                "demo": {
                    "url": "https://example.com",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                }
            }
        },
    )
    assert run(root)["dependencies"][0]["embedded"] is True


def test_expert_model_rejects_literal_credentials(tmp_path):
    root = package(tmp_path, "agent_template", model={"file": "model.json"})
    write(
        root, "model.json", {"model": {"model_id": "demo", "api_key": "privatevalue"}}
    )
    with pytest.raises(PublishValidationError) as error:
        run(root, "agent_template")
    assert error.value.code == "SENSITIVE_CONTENT"
    assert "privatevalue" not in str(error.value)


@pytest.mark.parametrize("default", ["privatevalue", 0, False])
def test_embedded_token_schema_rejects_stored_defaults(tmp_path, default):
    root = package(tmp_path, mcps=[{"dir": "mcps"}])
    write(
        root,
        "mcps/token-schema.json",
        {"fields": [{"key": "TOKEN", "default": default}]},
    )
    with pytest.raises(PublishValidationError) as error:
        run(root)
    assert error.value.code == "SENSITIVE_CONTENT"
    assert "privatevalue" not in str(error.value)


def test_team_skill_roles_reject_embedded_credentials(tmp_path):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\nkind: team-skill\nroles:\n- id: a\n  model:\n    api_key: privatevalue\n- id: b\n---\nBody"
    )
    with pytest.raises(PublishValidationError) as error:
        run(tmp_path, "skill")
    assert error.value.code == "SENSITIVE_CONTENT"
    assert "privatevalue" not in str(error.value)


@pytest.mark.parametrize(
    "server",
    [
        {"url": "https://example.com", "env": {"API_KEY": 123456}},
        {"url": "https://example.com", "args": ["--api-key", "${TOKEN}privatevalue"]},
    ],
)
def test_mcp_rejects_nonstring_credentials_and_partial_cli_placeholders(
    tmp_path, server
):
    root = mcp(tmp_path, server)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["credentials"] = {"type": "cli-oauth"}
    write(root, "manifest.json", manifest)
    with pytest.raises(PublishValidationError) as error:
        run(root, "mcp")
    assert error.value.code == "SENSITIVE_CONTENT"


def test_remote_mcp_url_requires_http_scheme_before_upload(tmp_path):
    root = mcp(tmp_path, {"url": "${MCP_URL}"})
    with pytest.raises(PublishValidationError) as error:
        run(root, "mcp")
    assert error.value.field == "mcp.url"
