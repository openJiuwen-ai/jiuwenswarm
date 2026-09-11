"""Exercise file creation with real templates and the SDK directory builder."""

from pathlib import Path

import pytest
import yaml
from openjiuwen.core.sys_operation import LocalWorkConfig, SysOperation, SysOperationCard
from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec, WorkspaceSpec
from openjiuwen.harness.workspace.directory_builder import DirectoryBuilder

from jiuwenswarm.agents.harness.common.memory.workspace import configure_workspace_memory
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common.utils import prepare_workspace
from jiuwenswarm.server.workspace_initialization import should_prepare_workspace


@pytest.fixture(autouse=True)
def isolated_memory_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("MEMORY_ENGINE", "MEMORY_EXTERNAL_PROVIDER", "MEMORY_MODE"):
        monkeypatch.delenv(key, raising=False)


def _config(engine="external", provider="celia", mode="cloud", agent=False, code=False):
    return {
        "memory": {"engine": engine, "mode": mode, "external": {"provider": provider}},
        "modes": {"agent": {"memory": {"enabled": agent}}, "code": {"memory": {"enabled": code}}},
    }


def _write_user_config(data, config):
    config_dir = data / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.yaml"
    existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(config)
    path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_new_celia_initialization_does_not_create_legacy_memory(tmp_path, language, overwrite):
    data = tmp_path / "data"
    prepare_workspace(overwrite=overwrite, workspace_dir=data, preferred_language=language)
    prepare_workspace(overwrite=False, workspace_dir=data, preferred_language=language)
    workspace = data / "agent/workspace"
    for relative in ("USER.md", "MEMORY.md", "memory", "coding_memory"):
        assert not (workspace / relative).exists(), relative
    for filename in ("AGENT.md", "SOUL.md", "HEARTBEAT.md", "IDENTITY.md"):
        assert (workspace / filename).is_file()
    assert (workspace / "skills").is_dir()
    assert not (data / "celia/bin").exists()
    assert not should_prepare_workspace(data / "config/config.yaml", workspace, data / "agent/jiuwenclaw_workspace")


@pytest.mark.parametrize("overwrite", [False, True])
def test_disabling_legacy_memory_preserves_existing_files(tmp_path, overwrite):
    data = tmp_path / "data"
    workspace = data / "agent/workspace"
    files = ("USER.md", "MEMORY.md", "memory/MEMORY.md", "memory/daily_memory/2026-09-11.md",
             "coding_memory/MEMORY.md", "memory/celia_memory/celia_memory.db")
    for relative in files:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"existing:{relative}")
    prepare_workspace(overwrite=overwrite, workspace_dir=data)
    for relative in files:
        assert (workspace / relative).read_text() == f"existing:{relative}"


@pytest.mark.parametrize("config", [_config(provider="old-celia", mode="local"), _config(engine="builtin", mode="local", agent=True)])
def test_enabling_legacy_memory_initializes_templates(tmp_path, config):
    data = tmp_path / "data"
    # 先落模板并写戳，再改 yaml：无戳会被当成升级 copy2，memory 不在白名单会被冲掉。
    prepare_workspace(overwrite=False, workspace_dir=data)
    _write_user_config(data, config)
    prepare_workspace(overwrite=False, workspace_dir=data)
    workspace = data / "agent/workspace"
    assert (workspace / "USER.md").is_file()
    assert (workspace / "memory/MEMORY.md").is_file()
    assert (workspace / "MEMORY.md").exists() is (config["memory"]["external"]["provider"] == "old-celia")


def test_old_daily_files_are_only_migrated_when_legacy_memory_is_enabled(tmp_path):
    data = tmp_path / "data"
    old = data / "agent/memory"
    old.mkdir(parents=True)
    for filename in ("USER.md", "MEMORY.md", "2026-09-11.md"):
        (old / filename).write_text(filename)
    prepare_workspace(overwrite=False, workspace_dir=data)
    assert (old / "2026-09-11.md").read_text() == "2026-09-11.md"
    workspace = data / "agent/workspace"
    assert not (workspace / "memory").exists()
    _write_user_config(data, _config(engine="builtin", mode="local", agent=True))
    prepare_workspace(overwrite=False, workspace_dir=data)
    assert (workspace / "USER.md").read_text() == "USER.md"
    assert (workspace / "memory/daily_memory/2026-09-11.md").read_text() == "2026-09-11.md"
    assert not old.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("assembly", ["adapter", "team", "code.team", "design.team"])
@pytest.mark.parametrize("config,files_enabled,code_enabled", [
    (_config(), False, False),
    (_config(mode="local", agent=True, code=True), False, False),
    (_config(engine="none", provider="old-celia", mode="local", agent=True, code=True), False, False),
    (_config(provider="old-celia", mode="local"), True, False),
    (_config(engine="builtin", mode="local", agent=True, code=True), True, True),
    (_config(engine="builtin", mode="local", code=True), False, True),
    (_config(engine="builtin", mode="cloud", agent=True, code=True), False, False),
])
async def test_sdk_initialization_respects_memory_switches(tmp_path, assembly, config, files_enabled, code_enabled):
    spec = WorkspaceSpec(root_path=str(tmp_path / "workspace"), language="cn")
    if assembly == "adapter":
        workspace = configure_workspace_memory(spec.build(), config)
    else:
        # Resolve the real spec so the check includes context derivation before SDK initialization.
        context = SwarmBuildContext.from_seed({"mode": assembly}, config=config, trajectory_registry=None)
        parts = DeepAgentSpec(workspace=spec).resolve_parts(context=context)
        workspace = parts.config.workspace
    operation = SysOperation(SysOperationCard(id="workspace-memory", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)])))
    await DirectoryBuilder(operation, workspace.root_path).build(workspace.directories)
    root = Path(workspace.root_path)
    assert (root / "USER.md").exists() is files_enabled
    assert (root / "memory/MEMORY.md").exists() is files_enabled
    assert (root / "memory/daily_memory").exists() is files_enabled
    assert (root / "coding_memory/MEMORY.md").exists() is code_enabled
    assert (root / "skills").is_dir()
    # Filtering one instance must not alter the SDK's shared defaults.
    assert any(node["name"] == "USER.md" for node in spec.build().directories)


@pytest.mark.parametrize("config,expected", [
    (_config(), False),
    (_config(provider="old-celia", mode="local"), True),
    (_config(engine="builtin", mode="local", agent=True), True),
    (_config(engine="none", provider="old-celia"), False),
])
def test_missing_user_file_only_triggers_initialization_for_legacy_memory(tmp_path, config, expected):
    data = tmp_path / "data"
    _write_user_config(data, config)
    config_file = data / "config/config.yaml"
    workspace = data / "agent/workspace"
    workspace.mkdir(parents=True)
    for filename in ("AGENT.md", "SOUL.md", "HEARTBEAT.md", "IDENTITY.md"):
        (workspace / filename).touch()
    assert should_prepare_workspace(config_file, workspace, data / "agent/jiuwenclaw_workspace") is expected
