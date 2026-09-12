# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jiuwenswarm.runtime.agent_catalog import (
    AgentCatalogError,
    get_agent,
    list_agent_tools,
    list_agents,
    resolve_agent_catalog_scope,
)


def _definition(
    root: Path,
    *,
    name: str = "Explore",
    description: str,
    prompt: str,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{name}.md"
    target.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "tools:\n  - Read\n"
        "---\n\n"
        f"{prompt}\n",
        encoding="utf-8",
    )
    return target


def _scope(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return resolve_agent_catalog_scope(
        str(project),
        trusted_dirs=(str(tmp_path / "stale"), str(tmp_path)),
    )


def _patch_user_root(monkeypatch: pytest.MonkeyPatch, user_root: Path) -> None:
    from jiuwenswarm.server.runtime import agent_config_service

    monkeypatch.setattr(
        agent_config_service,
        "get_user_workspace_dir",
        lambda: user_root,
    )


def test_catalog_preserves_builtin_local_user_project_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    user_root = tmp_path / "user"
    _patch_user_root(monkeypatch, user_root)
    _definition(
        scope.project_dir / ".jiuwenswarm" / "agents-local",
        description="local",
        prompt="local prompt",
    )
    _definition(
        user_root / "agents",
        description="user",
        prompt="user prompt",
    )
    _definition(
        scope.project_dir / ".jiuwenswarm" / "agents",
        description="project",
        prompt="project prompt",
    )

    matches = [agent for agent in list_agents(scope).agents if agent.name == "Explore"]

    assert [agent.source for agent in matches] == [
        "builtin",
        "local",
        "user",
        "project",
    ]
    assert [agent.shadowed_by for agent in matches] == [
        "project",
        "project",
        "project",
        None,
    ]
    assert get_agent(scope, "Explore").description == "project"


def test_catalog_skips_malformed_files_and_never_exposes_prompt_or_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    _patch_user_root(monkeypatch, tmp_path / "user")
    agents_dir = scope.project_dir / ".jiuwenswarm" / "agents"
    _definition(
        agents_dir,
        name="safe-agent",
        description="safe description",
        prompt="sk-agent-prompt-secret",
    )
    (agents_dir / "malformed.md").write_text("not frontmatter", encoding="utf-8")
    (agents_dir / "broken.md").write_text("---\nname: [\n---", encoding="utf-8")

    result = list_agents(scope)
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)

    assert get_agent(scope, "safe-agent").description == "safe description"
    assert "malformed.md" not in serialized
    assert "broken.md" not in serialized
    assert "sk-agent-prompt-secret" not in serialized
    assert "prompt" not in serialized
    assert "file_path" not in serialized
    descriptor = get_agent(scope, "safe-agent").to_dict()
    assert descriptor["tools"] == ["Read"]
    assert descriptor["disallowed_tools"] == []
    assert descriptor["skills"] == []


def test_project_must_exist_inside_real_trusted_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    collision = tmp_path / "trusted-other"
    project.mkdir(parents=True)
    collision.mkdir()

    resolved = resolve_agent_catalog_scope(
        str(project),
        trusted_dirs=(str(tmp_path / "missing"), str(trusted)),
    )
    assert resolved.project_dir == project.resolve()

    with pytest.raises(AgentCatalogError) as caught:
        resolve_agent_catalog_scope(
            str(collision),
            trusted_dirs=(str(trusted),),
        )
    assert caught.value.code == "FORBIDDEN"

    with pytest.raises(AgentCatalogError) as caught:
        resolve_agent_catalog_scope(
            str(tmp_path / "missing-project"),
            trusted_dirs=(str(trusted),),
        )
    assert caught.value.code == "NOT_FOUND"


def test_project_directory_link_cannot_escape_trusted_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    link = trusted / "project"
    trusted.mkdir()
    outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    with pytest.raises(AgentCatalogError) as caught:
        resolve_agent_catalog_scope(str(link), trusted_dirs=(str(trusted),))
    assert caught.value.code == "FORBIDDEN"


def test_agent_file_link_cannot_escape_source_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    _patch_user_root(monkeypatch, tmp_path / "user")
    agents_dir = scope.project_dir / ".jiuwenswarm" / "agents"
    agents_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    _definition(
        tmp_path,
        name="outside",
        description="outside",
        prompt="outside prompt",
    )
    link = agents_dir / "escaped.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file links unavailable: {exc}")

    assert all(agent.name != "outside" for agent in list_agents(scope).agents)


def test_read_only_operations_do_not_write_reload_or_initialize_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    _patch_user_root(monkeypatch, tmp_path / "user")
    _definition(
        scope.project_dir / ".jiuwenswarm" / "agents",
        name="safe-agent",
        description="safe",
        prompt="prompt",
    )

    def reject_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("catalog must not write")

    monkeypatch.setattr(Path, "write_text", reject_write)
    assert list_agents(scope).agents
    assert get_agent(scope, "safe-agent").name == "safe-agent"


def test_tools_are_whitelisted_without_agent_initialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.runtime.agent_config_service import AgentConfigService

    scope = _scope(tmp_path)
    _patch_user_root(monkeypatch, tmp_path / "user")
    monkeypatch.setattr(
        AgentConfigService,
        "list_available_tools",
        staticmethod(
            lambda: {
                "tools": [
                    {
                        "name": "Read",
                        "internal_name": "read_file",
                        "description": "read",
                        "group": "files",
                        "api_key": "must-not-leak",
                    }
                ],
                "groups": ["files"],
                "disallowed_for_subagents": ["danger"],
                "secret": "must-not-leak",
            }
        ),
    )

    serialized = json.dumps(list_agent_tools(scope).to_dict())
    assert "Read" in serialized
    assert "must-not-leak" not in serialized
    assert "api_key" not in serialized


def test_runtime_agent_catalog_has_no_transport_schema_imports() -> None:
    import jiuwenswarm.runtime.agent_catalog as catalog

    tree = ast.parse(Path(catalog.__file__).read_text(encoding="utf-8"))
    forbidden = ("gateway", "websocket", "schema.agent", "process_cli")
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not any(token in module for module in imports for token in forbidden)
