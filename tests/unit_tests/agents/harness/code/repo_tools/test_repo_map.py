# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the session repo map builder, cache and rail."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs

from jiuwenswarm.agents.harness.code.repo_tools import (
    RepoMapRail,
    build_repo_map,
    cached_repo_map,
    clear_repo_map_cache,
)
from jiuwenswarm.agents.harness.code.repo_tools.repo_map import (
    SECTION_NAME,
    SECTION_PRIORITY,
)


class FakePromptBuilder:
    def __init__(self) -> None:
        self.sections: dict[str, object] = {}
        self.adds = 0

    def add_section(self, section) -> None:
        self.sections[section.name] = section
        self.adds += 1

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def _ctx(agent) -> AgentCallbackContext:
    return AgentCallbackContext(agent=agent, inputs=ModelCallInputs(messages=[], tools=[]))


# ─── builder ────────────────────────────────────────────────


def test_repo_map_summarises_layout_packages_tests_and_commands(fake_repo: Path):
    result = build_repo_map(fake_repo)
    text = result.text
    assert text.startswith("# Repository map: repo/")
    assert "python" in text and "typescript" in text  # language mix
    assert "Manifests at root: pyproject.toml, Makefile" in text
    assert "python: demo (pyproject.toml)" in result.packages
    assert "python package: src/pkg/" in result.packages
    assert result.test_dirs == ["tests/ (1 files)"]
    assert any(c.startswith("python tests: pytest -q") for c in result.commands)
    assert any("make targets: test, lint" in c for c in result.commands)
    # depth-2 layout: dirs with counts, files listed at the leaf level
    assert "src/  (" in text and "  pkg/  (" in text and "cart.py" in text
    # ignored directories never appear
    assert "node_modules" not in text and "secret" not in text
    assert "code_search / find_symbol / symbol_outline" in text
    assert result.truncated is False and result.files_scanned >= 8


def test_repo_map_detects_node_and_go_and_rust(tmp_path: Path):
    root = tmp_path / "poly"
    root.mkdir()
    (root / "package.json").write_text('{"name": "web-app", "scripts": {"test": "jest", "build": "tsc"}}', encoding="utf-8")
    (root / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    (root / "go.mod").write_text("module example.com/svc\n", encoding="utf-8")
    (root / "Cargo.toml").write_text('[package]\nname = "engine"\n', encoding="utf-8")
    (root / "main.go").write_text("package main\n", encoding="utf-8")
    result = build_repo_map(root)
    assert "node: web-app (package.json)" in result.packages
    assert "go module: example.com/svc" in result.packages
    assert "rust crate: engine" in result.packages
    assert "node test: pnpm test  (jest)" in result.commands
    assert "go tests: go test ./..." in result.commands
    assert "rust tests: cargo test" in result.commands


def test_repo_map_respects_max_chars_and_marks_truncation(tmp_path: Path):
    root = tmp_path / "big"
    for i in range(60):
        d = root / f"module_{i:02d}"
        d.mkdir(parents=True)
        for j in range(8):
            (d / f"file_{j}.py").write_text("x = 1\n", encoding="utf-8")
    full = build_repo_map(root, max_chars=100_000)
    assert full.truncated is False and "module_59/" in full.text
    small = build_repo_map(root, max_chars=1200)
    assert small.truncated is True and len(small.text) <= 1200
    assert small.text.startswith("# Repository map: big/")
    assert "more directories omitted" in small.text or small.text.endswith("…")


def test_repo_map_handles_empty_and_binary_dirs(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    result = build_repo_map(root)
    assert "no recognised source files" in result.text
    assert result.commands == [] and result.packages == []


def test_cached_repo_map_reuses_within_ttl(fake_repo: Path):
    clear_repo_map_cache()
    first = cached_repo_map(fake_repo)
    (fake_repo / "new_module.py").write_text("y = 2\n", encoding="utf-8")
    second = cached_repo_map(fake_repo)
    assert second is first  # cached
    clear_repo_map_cache()
    third = cached_repo_map(fake_repo)
    assert third is not first and third.files_scanned == first.files_scanned + 1


# ─── rail ───────────────────────────────────────────────────


def test_rail_injects_once_and_stays_stable(fake_repo: Path):
    clear_repo_map_cache()
    builder = FakePromptBuilder()
    agent = SimpleNamespace(system_prompt_builder=builder, _jiuwenswarm_code_project_dir=str(fake_repo))
    rail = RepoMapRail(depth=2, max_chars=4000)
    rail.init(agent)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    assert SECTION_NAME in builder.sections and builder.adds == 1
    section = builder.sections[SECTION_NAME]
    assert section.priority == SECTION_PRIORITY
    assert section.content["en"].startswith("# Repository map: repo/")
    assert len(section.content["en"]) <= 4000
    # Second and third model calls in the same workspace: no re-injection.
    asyncio.run(rail.before_model_call(_ctx(agent)))
    asyncio.run(rail.before_model_call(_ctx(agent)))
    assert builder.adds == 1
    assert rail.last_map is not None and rail.last_map.root == str(fake_repo.resolve())


def test_rail_replaces_section_when_workspace_changes(fake_repo: Path, tmp_path: Path):
    clear_repo_map_cache()
    other = tmp_path / "other"
    (other / "lib").mkdir(parents=True)
    (other / "lib" / "a.go").write_text("package lib\n", encoding="utf-8")
    builder = FakePromptBuilder()
    agent = SimpleNamespace(system_prompt_builder=builder, _jiuwenswarm_code_project_dir=str(fake_repo))
    rail = RepoMapRail()
    rail.init(agent)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    agent._jiuwenswarm_code_project_dir = str(other)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    assert builder.adds == 2
    assert builder.sections[SECTION_NAME].content["en"].startswith("# Repository map: other/")
    rail.uninit(agent)
    assert SECTION_NAME not in builder.sections


def test_rail_is_inert_without_builder_or_root(tmp_path: Path):
    rail = RepoMapRail()
    agent = SimpleNamespace()  # no builder, no workspace
    rail.init(agent)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    builder = FakePromptBuilder()
    agent = SimpleNamespace(system_prompt_builder=builder, _jiuwenswarm_code_project_dir=str(tmp_path / "missing"))
    rail.init(agent)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    assert builder.sections == {}


def test_rail_falls_back_to_sdk_workspace_root(fake_repo: Path):
    clear_repo_map_cache()
    builder = FakePromptBuilder()
    workspace = SimpleNamespace(root_path=str(fake_repo))
    agent = SimpleNamespace(system_prompt_builder=builder, deep_config=SimpleNamespace(workspace=workspace))
    rail = RepoMapRail()
    rail.init(agent)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    assert SECTION_NAME in builder.sections


def test_rail_clamps_configuration():
    rail = RepoMapRail(depth=99, max_chars=10)
    assert rail.depth == 4 and rail.max_chars == 500


# ─── review fixes: configurable ignore list ────────────────


def test_repo_map_shows_build_dirs_unless_ignored(tmp_path: Path):
    root = tmp_path / "proj"
    for sub in ("build", "logs", "src", "node_modules/x"):
        (root / sub).mkdir(parents=True)
        (root / sub / "m.py").write_text("x = 1\n", encoding="utf-8")
    clear_repo_map_cache()
    default = build_repo_map(root)
    assert "build/" in default.text and "logs/" in default.text and "src/" in default.text
    assert "node_modules" not in default.text
    custom = build_repo_map(root, ignored_dirs=["build", "logs"])
    assert "build/" not in custom.text and "logs/" not in custom.text and "src/" in custom.text
    # The cache key includes the ignore list, so the two variants do not collide.
    assert cached_repo_map(root).text == default.text
    assert cached_repo_map(root, ignored_dirs=["build", "logs"]).text == custom.text


def test_rail_passes_ignored_dirs_through(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "build").mkdir(parents=True)
    (root / "build" / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    clear_repo_map_cache()
    builder = FakePromptBuilder()
    agent = SimpleNamespace(system_prompt_builder=builder, _jiuwenswarm_code_project_dir=str(root))
    rail = RepoMapRail(ignored_dirs=["build"])
    assert rail.ignored_dirs == frozenset({"build"})
    rail.init(agent)
    asyncio.run(rail.before_model_call(_ctx(agent)))
    text = builder.sections[SECTION_NAME].content["en"]
    assert "src/" in text and "build/" not in text
