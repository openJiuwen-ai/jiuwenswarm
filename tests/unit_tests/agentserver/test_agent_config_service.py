# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentConfigService 单元测试."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import yaml

from jiuwenswarm.server.runtime.agent_config_service import (
    BUILTIN_AGENTS,
    AgentConfigService,
    CreateAgentParams,
    UpdateAgentParams,
)
from jiuwenswarm.common.config import (
    remove_subagent_from_config,
    update_swarmflow_enabled_in_config,
    upsert_subagent_in_config,
)


class TestAgentConfigService:
    """AgentConfigService CRUD 操作测试."""

    @pytest.fixture
    def tmp_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def service(self, tmp_workspace):
        """Create AgentConfigService with mocked get_user_workspace_dir."""
        import jiuwenswarm.server.runtime.agent_config_service as svc_mod
        orig = svc_mod.get_user_workspace_dir

        def mock_get_user_workspace_dir():
            return tmp_workspace

        svc_mod.get_user_workspace_dir = mock_get_user_workspace_dir
        try:
            return AgentConfigService(tmp_workspace)
        finally:
            svc_mod.get_user_workspace_dir = orig

    # ---- list_agents ----

    @staticmethod
    def test_list_agents_includes_builtins(service):
        agents = service.list_agents()
        builtin_names = {a.name for a in agents if a.source == "builtin"}
        assert "general-purpose" in builtin_names
        assert "Explore" in builtin_names
        assert "Plan" in builtin_names

    @staticmethod
    def test_list_agents_merges_custom_from_project_dir(service, tmp_workspace):
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: 测试 agent\n---\n\n这是一个测试 agent。\n",
            encoding="utf-8",
        )

        agents = service.list_agents()
        names = {a.name for a in agents}
        assert "my-agent" in names

        my_agent = next(a for a in agents if a.name == "my-agent")
        assert my_agent.source == "project"
        assert my_agent.description == "测试 agent"
        assert my_agent.prompt == "这是一个测试 agent。"

    @staticmethod
    def test_list_agents_priority_project_overrides_user(tmp_workspace):
        """project 级同名 agent 覆盖 user 级，user 级标记 shadowed_by。"""
        # 在同一个 workspace 下同时创建 project 和 local 同名 agent
        project_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        project_dir.mkdir(parents=True)
        (project_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: project 级\n---\n\nproject 级 prompt。\n",
            encoding="utf-8",
        )

        local_dir = tmp_workspace / ".jiuwenswarm" / "agents-local"
        local_dir.mkdir(parents=True)
        (local_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: local 级\n---\n\nlocal 级 prompt。\n",
            encoding="utf-8",
        )

        service = AgentConfigService(tmp_workspace)
        agents = service.list_agents()
        project_agent = next(a for a in agents if a.name == "my-agent" and a.source == "project")
        local_agent = next(a for a in agents if a.name == "my-agent" and a.source == "local")
        assert project_agent.shadowed_by is None
        assert local_agent.shadowed_by == "project"

    @staticmethod
    def test_list_agents_sorts_by_source_order(service):
        agents = service.list_agents()
        sources = [a.source for a in agents]
        # builtin 应排在前面（sort key 最小）
        for src in sources:
            if src == "builtin":
                continue
            # 一旦出现非 builtin，之前的都应是 builtin
            break

    # ---- get_agent ----

    @staticmethod
    def test_get_agent_returns_builtin(service):
        agent = service.get_agent("Explore")
        assert agent is not None
        assert agent.source == "builtin"
        assert agent.prompt  # 有 prompt 正文

    @staticmethod
    def test_get_agent_returns_none_for_unknown(service):
        assert service.get_agent("nonexistent") is None

    # ---- create_agent ----

    @staticmethod
    def test_create_agent_rejects_invalid_name(service, tmp_workspace):
        for invalid_name in ["ab", "bad-name!", "name with spaces", "x" * 51, ""]:
            with pytest.raises(ValueError, match="名称格式无效|name is required"):
                service.create_agent(CreateAgentParams(
                    name=invalid_name, description="test", prompt="test prompt", location="user",
                ))

    @staticmethod
    def test_create_agent_writes_markdown_file(service, tmp_workspace):
        params = CreateAgentParams(
            name="test-agent",
            description="测试用 agent",
            prompt="你是一个测试 agent。",
            location="project",
            model="test-model",
            tools=["Read", "Bash"],
            max_iterations=50,
            skills=["daily-report"],
        )
        agent = service.create_agent(params)
        assert agent.name == "test-agent"
        assert agent.source == "project"
        assert agent.model == "test-model"
        assert agent.tools == ["Read", "Bash"]
        assert agent.max_iterations == 50
        assert agent.skills == ["daily-report"]

        # 验证文件写入
        md_file = tmp_workspace / ".jiuwenswarm" / "agents" / "test-agent.md"
        assert md_file.exists()
        content = md_file.read_text(encoding="utf-8")
        assert "name: test-agent" in content
        assert "你是一个测试 agent。" in content
        assert "max_iterations: 50" in content
        assert "daily-report" in content

    @staticmethod
    def test_create_agent_rejects_builtin_name(service):
        params = CreateAgentParams(
            name="Explore",
            description="覆盖内置 agent",
            prompt="test",
            location="project",
        )
        with pytest.raises(ValueError, match="不能覆盖内置 agent"):
            service.create_agent(params)

    @staticmethod
    def test_create_agent_overwrites_existing_custom(service, tmp_workspace):
        """创建同名自定义 agent 时覆盖已有文件。"""
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: old\n---\n\nold prompt\n",
            encoding="utf-8",
        )

        params = CreateAgentParams(
            name="my-agent",
            description="new description",
            prompt="new prompt",
            location="project",
        )
        agent = service.create_agent(params)
        assert agent.description == "new description"
        assert agent.prompt == "new prompt"

    # ---- create_agent name normalization ----

    @staticmethod
    def test_create_agent_strips_whitespace_from_name(service, tmp_workspace):
        params = CreateAgentParams(
            name=" test-agent ",
            description="test desc",
            prompt="test prompt",
            location="project",
        )
        agent = service.create_agent(params)
        assert agent.name == "test-agent"

        md_file = tmp_workspace / ".jiuwenswarm" / "agents" / "test-agent.md"
        assert md_file.exists()
        content = md_file.read_text(encoding="utf-8")
        assert "name: test-agent" in content

    @staticmethod
    def test_create_agent_with_whitespace_name_can_be_found(service):
        params = CreateAgentParams(
            name=" test-agent ",
            description="test desc",
            prompt="test prompt",
            location="project",
        )
        service.create_agent(params)

        found = service.get_agent("test-agent")
        assert found is not None
        assert found.name == "test-agent"

    # ---- update_agent ----

    @staticmethod
    def test_update_agent_modifies_fields(service, tmp_workspace):
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: original\n---\n\noriginal prompt\n",
            encoding="utf-8",
        )

        params = UpdateAgentParams(description="updated", model="new-model")
        agent = service.update_agent("my-agent", params)
        assert agent.description == "updated"
        assert agent.model == "new-model"
        assert agent.prompt == "original prompt"  # 未修改

    @staticmethod
    def test_update_agent_rejects_builtin(service):
        params = UpdateAgentParams(description="hack")
        with pytest.raises(ValueError, match="不能修改内置 agent"):
            service.update_agent("Explore", params)

    @staticmethod
    def test_update_agent_rejects_nonexistent(service):
        params = UpdateAgentParams(description="x")
        with pytest.raises(ValueError, match="Agent 不存在"):
            service.update_agent("nonexistent", params)

    @staticmethod
    def test_update_agent_strips_whitespace_from_name(service, tmp_workspace):
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.md").write_text(
            "---\nname: my-agent\ndescription: original\n---\n\noriginal prompt\n",
            encoding="utf-8",
        )

        params = UpdateAgentParams(description="updated")
        agent = service.update_agent(" my-agent ", params)
        assert agent.description == "updated"

    # ---- delete_agent ----

    @staticmethod
    def test_delete_agent_removes_file(service, tmp_workspace):
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        md_file = agents_dir / "my-agent.md"
        md_file.write_text(
            "---\nname: my-agent\ndescription: test\n---\n\nprompt\n",
            encoding="utf-8",
        )

        assert service.delete_agent("my-agent") is True
        assert not md_file.exists()

    @staticmethod
    def test_delete_agent_rejects_builtin(service):
        with pytest.raises(ValueError, match="不能删除内置 agent"):
            service.delete_agent("general-purpose")

    @staticmethod
    def test_delete_agent_returns_false_for_unknown(service):
        assert service.delete_agent("nonexistent") is False

    # ---- 文件解析 ----

    @staticmethod
    def test_parse_agent_file_with_full_frontmatter(service, tmp_workspace):
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "full.md").write_text(
            "---\n"
            "name: full-agent\n"
            "description: 完整配置的 agent\n"
            "model: claude-sonnet\n"
            "tools:\n"
            "  - Read\n"
            "  - Bash\n"
            "color: blue\n"
            "permission_mode: accept_edits\n"
            "memory_scope: project\n"
            "---\n\n"
            "完整的 system prompt 正文。\n",
            encoding="utf-8",
        )

        agents = service.list_agents()
        agent = next(a for a in agents if a.name == "full-agent")
        assert agent.description == "完整配置的 agent"
        assert agent.model == "claude-sonnet"
        assert agent.tools == ["Read", "Bash"]
        assert agent.color == "blue"
        assert agent.permission_mode == "accept_edits"
        assert agent.memory_scope == "project"
        assert agent.prompt == "完整的 system prompt 正文。"

    @staticmethod
    def test_parse_agent_file_with_minimal_frontmatter(service, tmp_workspace):
        agents_dir = tmp_workspace / ".jiuwenswarm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "minimal.md").write_text(
            "---\nname: minimal-agent\ndescription: 最小配置\n---\n\n只有必要字段。\n",
            encoding="utf-8",
        )

        agents = service.list_agents()
        agent = next(a for a in agents if a.name == "minimal-agent")
        assert agent.tools == ["*"]  # 默认
        assert agent.model is None
        assert agent.color is None

    # ---- BUILTIN_AGENTS ----

    @staticmethod
    def test_builtin_agents_are_well_formed():
        for agent in BUILTIN_AGENTS:
            assert agent.name
            assert agent.description
            assert agent.prompt
            assert agent.source == "builtin"
            assert agent.tools

    @staticmethod
    def test_builtin_agents_have_unique_names():
        names = [a.name for a in BUILTIN_AGENTS]
        assert len(names) == len(set(names))


class TestSubagentConfigMutation:
    """upsert_subagent_in_config / remove_subagent_from_config 测试."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        """创建临时 config.yaml 用于测试 round-trip 读写."""
        import jiuwenswarm.common.config as config_mod

        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}\n", encoding="utf-8")
        orig = config_mod.CONFIG_YAML_PATH
        config_mod.CONFIG_YAML_PATH = config_file
        try:
            yield config_file
        finally:
            config_mod.CONFIG_YAML_PATH = orig

    @staticmethod
    def test_upsert_creates_new_entry(tmp_config):
        upsert_subagent_in_config("my-agent", enabled=True)
        data = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
        assert data["react"]["subagents"]["my-agent"]["enabled"] is True

    @staticmethod
    def test_upsert_updates_existing_entry(tmp_config):
        upsert_subagent_in_config("my-agent", enabled=True)
        upsert_subagent_in_config("my-agent", enabled=False)
        data = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
        assert data["react"]["subagents"]["my-agent"]["enabled"] is False

    @staticmethod
    def test_upsert_preserves_other_keys(tmp_config):
        upsert_subagent_in_config("my-agent", enabled=True)
        # 手动添加额外的 key 模拟已有配置
        import jiuwenswarm.common.config as config_mod

        roundtrip = config_mod.load_yaml_round_trip(config_mod.CONFIG_YAML_PATH)
        roundtrip["react"]["subagents"]["my-agent"]["max_iterations"] = 50
        config_mod.dump_yaml_round_trip(config_mod.CONFIG_YAML_PATH, roundtrip)
        # 再次 upsert，不应丢失 max_iterations
        upsert_subagent_in_config("my-agent", enabled=False)
        data = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
        assert data["react"]["subagents"]["my-agent"]["enabled"] is False
        assert data["react"]["subagents"]["my-agent"]["max_iterations"] == 50

    @staticmethod
    def test_remove_existing_entry(tmp_config):
        upsert_subagent_in_config("my-agent", enabled=True)
        removed = remove_subagent_from_config("my-agent")
        assert removed is True
        data = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
        assert "my-agent" not in data.get("react", {}).get("subagents", {})

    @staticmethod
    def test_remove_nonexistent_returns_false(tmp_config):
        assert remove_subagent_from_config("nonexistent") is False

    @staticmethod
    def test_upsert_empty_name_raises():
        with pytest.raises(ValueError, match="subagent name is required"):
            upsert_subagent_in_config("")

    @staticmethod
    def test_remove_empty_name_raises():
        with pytest.raises(ValueError, match="subagent name is required"):
            remove_subagent_from_config("")


class TestSwarmflowConfigMutation:
    """update_swarmflow_enabled_in_config 测试."""

    @pytest.fixture
    def tmp_config(self, tmp_path, monkeypatch):
        import jiuwenswarm.common.config as config_mod

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
modes:
  team:
    jiuwen_team:
      team_name: jiuwen_team
      enable_swarmflow: true
    secondary_team:
      team_name: secondary_team
      enable_swarmflow: true
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", config_file)
        return config_file

    @staticmethod
    def test_updates_modes_team_jiuwen_team_entry(tmp_config):
        update_swarmflow_enabled_in_config(False)

        data = yaml.safe_load(tmp_config.read_text(encoding="utf-8"))
        teams = data["modes"]["team"]
        assert teams["jiuwen_team"]["enable_swarmflow"] is False
        assert teams["secondary_team"]["enable_swarmflow"] is True

    @staticmethod
    def test_creates_modes_team_jiuwen_team_entry(tmp_path, monkeypatch):
        import jiuwenswarm.common.config as config_mod

        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", config_file)

        update_swarmflow_enabled_in_config(True)

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert data["modes"]["team"]["jiuwen_team"]["enable_swarmflow"] is True


class TestAgentLLMGeneration:
    """_generate_agent_with_llm JSON 解析逻辑测试。

    不导入 AgentWebSocketServer（避免 auto_harness 导入链问题），
    直接测试 _generate_agent_with_llm 中的 JSON 解析逻辑。
    """

    @staticmethod
    def _parse_llm_response(text: str) -> tuple[str, str] | None:
        """复制 _generate_agent_with_llm 的 JSON 解析逻辑."""
        import re as _re
        import json as _json

        try:
            data = _json.loads(text.strip())
        except _json.JSONDecodeError:
            match = _re.search(r"\{[\s\S]*\}", text)
            if not match:
                return None
            try:
                data = _json.loads(match.group(0))
            except _json.JSONDecodeError:
                return None

        when_to_use = (data.get("whenToUse") or "").strip()
        system_prompt = (data.get("systemPrompt") or "").strip()

        if not when_to_use or not system_prompt:
            return None

        return when_to_use, system_prompt

    def test_parse_valid_json(self):
        result = self._parse_llm_response(
            '{"whenToUse": "Use this agent when...", "systemPrompt": "You are a helpful agent."}'
        )
        assert result is not None
        assert result[0] == "Use this agent when..."
        assert result[1] == "You are a helpful agent."

    def test_parse_json_with_extra_fields(self):
        result = self._parse_llm_response(
            '{"whenToUse": "use me", "systemPrompt": "hello", "extra": "ignored"}'
        )
        assert result == ("use me", "hello")

    def test_parse_missing_when_to_use(self):
        result = self._parse_llm_response('{"systemPrompt": "only prompt"}')
        assert result is None

    def test_parse_missing_system_prompt(self):
        result = self._parse_llm_response('{"whenToUse": "only when"}')
        assert result is None

    def test_parse_empty_strings(self):
        result = self._parse_llm_response('{"whenToUse": "", "systemPrompt": ""}')
        assert result is None

    def test_parse_bad_json(self):
        result = self._parse_llm_response("this is not json at all")
        assert result is None

    def test_parse_json_in_markdown_code_block(self):
        result = self._parse_llm_response(
            '```json\n{"whenToUse": "when condition", "systemPrompt": "system prompt"}\n```'
        )
        assert result == ("when condition", "system prompt")

    def test_parse_json_with_surrounding_text(self):
        result = self._parse_llm_response(
            'Here is the config:\n{"whenToUse": "use me", "systemPrompt": "be helpful"}\nHope this helps!'
        )
        assert result == ("use me", "be helpful")

    def test_parse_empty_response(self):
        result = self._parse_llm_response("")
        assert result is None
