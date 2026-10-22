"""JM-14: each colleague publishes its own Skill, connector and plugin selection."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from itertools import count

from jiuwenswarm.server.jiuwenmate.execution_models import PersistentContractError
from jiuwenswarm.server.jiuwenmate.models import CapabilitySelection, ColleagueSettings, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.resource_assembly import (
    InstalledResourceCatalog,
    KnowledgeSource,
    PluginSource,
    SkillSource,
    ConnectorSource,
    apply_feishu_skills,
)
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.schema.deep_agent_spec import RailSpec, TeamModelConfig

import pytest

NS = "workspace-1"


class FakeModels:
    def default_selection(self):
        return "deepseek#0"

    def resolve(self, selection, *, client_id):
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key="sk-test",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        )


class MapCatalog:
    def __init__(self, skills=None, plugins=None, connectors=None, knowledge=None):
        self._skills = skills or {}
        self._plugins = plugins or {}
        self._connectors = connectors or {}
        self._knowledge = knowledge or {}

    def skill(self, name):
        return self._skills.get(name)

    def plugin(self, name):
        value = self._plugins.get(name)
        if value is None:
            return None
        return PluginSource(name=name, skills=tuple(value))

    def plugin_skills(self, name):
        source = self.plugin(name)
        return None if source is None else source.skills

    def connector(self, name):
        return self._connectors.get(name)

    def knowledge(self, name):
        return self._knowledge.get(name)


def write_skill(directory, name, body="按步骤整理纪要。"):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return directory


def service(tmp_path, catalog, ids):
    ticks = count()
    base = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
    return PersistentAgentService(
        PersistentAgentRepository(tmp_path / "agents.sqlite3"),
        TemplateStore(tmp_path / "templates"),
        models=FakeModels(),
        template_source=lambda _: None,
        clock=lambda: base + timedelta(seconds=next(ticks)),
        id_factory=lambda: next(ids),
        resources=catalog,
    )


def settings(**changes):
    fields = {
        "name": "研发同事",
        "description": "负责整理研发进展。",
        "capabilities": CapabilitySelection(model="deepseek#0"),
    }
    fields.update(changes)
    return ColleagueSettings(**fields)


def test_each_colleague_gets_its_own_skill_copy_and_deletion_keeps_the_others(tmp_path):
    library = tmp_path / "library"
    notes = write_skill(library / "meeting-notes", "meeting-notes", "原始技能")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    os.symlink(outside / "secret.txt", notes / "leaked.txt")
    catalog = MapCatalog(skills={"meeting-notes": SkillSource(name="meeting-notes", directory=notes)})
    ids = iter(["mate_one", "mate_two"])
    host = service(tmp_path, catalog, ids)
    capabilities = CapabilitySelection(model="deepseek#0", skills=("meeting-notes",))

    first = host.create_colleague(NS, settings(name="甲", capabilities=capabilities))
    second = host.create_colleague(NS, settings(name="乙", capabilities=capabilities))
    first_spec = host.current_snapshot(NS, first.agent_id).load_spec()
    second_spec = host.current_snapshot(NS, second.agent_id).load_spec()
    first_copy = Path(first_spec.rails[0].params["skills_dir"]) / "meeting-notes" / "SKILL.md"
    second_copy = Path(second_spec.rails[0].params["skills_dir"]) / "meeting-notes" / "SKILL.md"

    assert first_spec.skills == ["meeting-notes"]
    assert isinstance(first_spec.rails[0], RailSpec)
    built = first_spec.rails[0].build(language="cn")
    assert isinstance(built, SkillUseRail) and built.include_tools is False
    assert first_copy.read_text(encoding="utf-8") == second_copy.read_text(encoding="utf-8")
    assert not (first_copy.parent / "leaked.txt").exists()
    assert first_copy.parent != second_copy.parent
    assert "已装配技能" in first_spec.system_prompt

    first_copy.write_text("只改甲的副本", encoding="utf-8")
    assert "只改甲的副本" not in notes.joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "只改甲的副本" not in second_copy.read_text(encoding="utf-8")

    host.delete_colleague(NS, first.agent_id)

    assert not first_copy.exists()
    assert second_copy.is_file()
    assert notes.joinpath("SKILL.md").is_file()


def test_selected_knowledge_pages_are_copied_and_other_pages_stay_out(tmp_path):
    context = tmp_path / "context"
    chosen = context / "topics" / "description.md"
    chosen.parent.mkdir(parents=True)
    chosen.write_text("# 产品手册\n\n退货规则。\n", encoding="utf-8")
    other = context / "private.md"
    other.write_text("不应复制", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, context / "linked.md")
    catalog = MapCatalog(
        knowledge={"topics/description.md": KnowledgeSource(name="topics/description.md", path=chosen)}
    )
    host = service(tmp_path, catalog, iter(["mate_knowledge"]))

    view = host.create_colleague(
        NS,
        settings(capabilities=CapabilitySelection(model="deepseek#0", knowledge=("topics/description.md",))),
    )
    spec = host.current_snapshot(NS, view.agent_id).load_spec()
    snapshot = host.current_snapshot(NS, view.agent_id)
    office = host.workspaces.path(NS, view.agent_id)
    copies = list((office / "resources" / "knowledge").glob("*/topics/description.md"))

    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == "# 产品手册\n\n退货规则。\n"
    assert "已装配知识" in spec.system_prompt
    assert "topics/description.md" in spec.system_prompt
    assert "飞书私聊仅可读取本目录中的已选知识副本" in spec.system_prompt
    assert snapshot.knowledge_root == copies[0].parent.parent.relative_to(office).as_posix()
    assert type(snapshot).from_dict(snapshot.to_dict()).knowledge_root == snapshot.knowledge_root
    legacy_snapshot = snapshot.to_dict()
    legacy_snapshot.pop("knowledge_root")
    assert type(snapshot).from_dict(legacy_snapshot).knowledge_root == snapshot.knowledge_root
    with pytest.raises(PersistentContractError, match="knowledge scope"):
        replace(snapshot, knowledge_root="resources/knowledge/0102030405060708").load_spec()
    assert not any((office / "resources" / "knowledge").rglob("private.md"))
    assert not any((office / "resources" / "knowledge").rglob("linked.md"))
    copies[0].write_text("只改员工副本", encoding="utf-8")
    assert "只改员工副本" not in chosen.read_text(encoding="utf-8")


def test_an_unpublished_knowledge_page_refuses_publication(tmp_path):
    host = service(tmp_path, MapCatalog(), iter(["mate_missing_knowledge"]))

    with pytest.raises(PersistentAgentError, match="knowledge page is not published") as caught:
        host.create_colleague(
            NS,
            settings(capabilities=CapabilitySelection(model="deepseek#0", knowledge=("missing.md",))),
        )

    assert caught.value.code == "RESOURCE_NOT_INSTALLED"
    assert host.list_colleagues(NS)[0].current_version is None


def test_installed_catalog_lists_only_regular_pages_inside_the_context_root(tmp_path):
    context = tmp_path / "context"
    page = context / "handbook.md"
    page.parent.mkdir()
    article = context / "AI软件工程" / "需求到代码可追溯.md"
    article.parent.mkdir()
    page.write_text("手册", encoding="utf-8")
    article.write_text("专栏", encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, context / "linked.md")
    (context / "notes.txt").write_text("不是页面", encoding="utf-8")
    catalog = InstalledResourceCatalog(knowledge_root=context)

    assert [item.name for item in catalog.list_knowledge()] == ["AI软件工程/需求到代码可追溯.md", "handbook.md"]
    assert catalog.knowledge("../secret.md") is None
    assert catalog.knowledge("linked.md") is None
    assert catalog.knowledge("AI软件工程\\需求到代码可追溯.md") is None


def test_a_plugin_copies_only_its_own_skill(tmp_path):
    library = tmp_path / "library"
    chosen = write_skill(library / "report-writer", "report-writer")
    write_skill(library / "other-skill", "other-skill", "不应复制")
    catalog = MapCatalog(
        plugins={"report-pack": (SkillSource(name="report-writer", directory=chosen),)},
    )
    host = service(tmp_path, catalog, iter(["mate_plugin"]))

    view = host.create_colleague(
        NS,
        settings(capabilities=CapabilitySelection(model="deepseek#0", plugins=("report-pack",))),
    )
    root = Path(host.current_snapshot(NS, view.agent_id).load_spec().rails[0].params["skills_dir"])

    assert (root / "report-writer" / "SKILL.md").is_file()
    assert not (root / "other-skill").exists()


def test_connector_manuals_stay_out_of_the_employee_skill_list(tmp_path):
    library = tmp_path / "library"
    chosen = write_skill(library / "weekend-getaway", "weekend-getaway", "周末去哪玩")
    bundled = write_skill(library / "lark-doc", "lark-doc", "飞书文档")
    catalog = MapCatalog(
        skills={"weekend-getaway": SkillSource(name="weekend-getaway", directory=chosen)},
        connectors={
            "feishu": ConnectorSource(
                name="feishu",
                skill_directories=(SkillSource(name="lark-doc", directory=bundled),),
            )
        },
    )
    host = service(tmp_path, catalog, iter(["mate_feishu"]))

    view = host.create_colleague(
        NS,
        settings(
            capabilities=CapabilitySelection(
                model="deepseek#0",
                skills=("weekend-getaway",),
                connectors=("feishu",),
            )
        ),
    )
    spec = host.current_snapshot(NS, view.agent_id).load_spec()
    office = host.workspaces.path(NS, view.agent_id)
    skill_root = Path(spec.rails[0].params["skills_dir"])

    assert spec.skills == ["weekend-getaway"]
    assert (skill_root / "weekend-getaway" / "SKILL.md").is_file()
    assert not (skill_root / "lark-doc").exists()
    manuals = list((office / "resources" / "connector-skills").glob("*/lark-doc/SKILL.md"))
    assert len(manuals) == 1
    assert "连接器附带说明" in spec.system_prompt
    assert "lark-doc" not in spec.system_prompt


def test_feishu_skill_prompt_uses_shared_agent_user_identity(tmp_path):
    skill = tmp_path / "installed" / "lark-calendar"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("calendar skill", encoding="utf-8")

    result = apply_feishu_skills(
        {"system_prompt": "base prompt", "skills": [], "rails": []},
        tmp_path / "office",
        (skill.parent,),
    )

    prompt = result["system_prompt"]
    assert "显式使用 `--as user`" in prompt
    assert "供该 Agent 的私聊和群聊共同使用" in prompt
    assert "群内结果直接回复当前群，群成员均可见" in prompt
    assert "群聊必须先由提问者私聊确认" not in prompt


def test_connectors_keep_separate_credentials_and_do_not_touch_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "from-process")
    before = dict(os.environ)
    entry = {
        "name": "docs",
        "transport": "streamable-http",
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer ${TOKEN}"},
    }
    catalog = MapCatalog(
        connectors={
            "docs": ConnectorSource(name="docs", entry=entry, secrets={"TOKEN": "stored-secret"}),
        }
    )
    ids = iter(["mate_a", "mate_b"])
    host = service(tmp_path, catalog, ids)
    capabilities = CapabilitySelection(model="deepseek#0", connectors=("docs",))

    first = host.create_colleague(NS, settings(name="甲", capabilities=capabilities))
    second = host.create_colleague(NS, settings(name="乙", capabilities=capabilities))
    first_mcp = host.current_snapshot(NS, first.agent_id).load_spec().mcps[0]
    second_mcp = host.current_snapshot(NS, second.agent_id).load_spec().mcps[0]

    assert first_mcp.server_id != second_mcp.server_id
    assert first_mcp.auth_headers["Authorization"] == "Bearer stored-secret"
    assert os.environ["TOKEN"] == "from-process"
    assert os.environ.keys() == before.keys()


def test_a_missing_connector_secret_is_not_taken_from_the_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "from-process")
    entry = {
        "name": "docs",
        "transport": "streamable-http",
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": "Bearer ${TOKEN}"},
    }
    host = service(
        tmp_path,
        MapCatalog(connectors={"docs": ConnectorSource(name="docs", entry=entry, secrets={})}),
        iter(["mate_missing"]),
    )

    with pytest.raises(PersistentAgentError) as denied:
        host.create_colleague(
            NS,
            settings(capabilities=CapabilitySelection(model="deepseek#0", connectors=("docs",))),
        )

    assert denied.value.code == "CREDENTIAL_MISSING"
    assert os.environ["TOKEN"] == "from-process"


def test_republishing_changed_skill_content_keeps_the_previous_copy(tmp_path):
    library = tmp_path / "library"
    notes = write_skill(library / "meeting-notes", "meeting-notes", "第一版")
    catalog = MapCatalog(skills={"meeting-notes": SkillSource(name="meeting-notes", directory=notes)})
    host = service(tmp_path, catalog, iter(["mate_notes"]))
    view = host.create_colleague(
        NS,
        settings(capabilities=CapabilitySelection(model="deepseek#0", skills=("meeting-notes",))),
    )
    first = host.current_snapshot(NS, view.agent_id)
    first_root = Path(first.load_spec().rails[0].params["skills_dir"])

    assert host.publish(NS, view.agent_id).config_version == first.config_version

    notes.joinpath("SKILL.md").write_text("---\nname: meeting-notes\n---\n第二版\n", encoding="utf-8")
    second = host.publish(NS, view.agent_id)
    second_root = Path(host.current_snapshot(NS, view.agent_id).load_spec().rails[0].params["skills_dir"])

    assert second.config_version == "v2"
    assert first_root != second_root
    assert "第一版" in (first_root / "meeting-notes" / "SKILL.md").read_text(encoding="utf-8")
    assert "第二版" in (second_root / "meeting-notes" / "SKILL.md").read_text(encoding="utf-8")


def test_an_uninstalled_skill_refuses_publication(tmp_path):
    host = service(tmp_path, MapCatalog(), iter(["mate_none"]))

    with pytest.raises(PersistentAgentError) as denied:
        host.create_colleague(
            NS,
            settings(capabilities=CapabilitySelection(model="deepseek#0", skills=("missing",))),
        )

    assert denied.value.code == "RESOURCE_NOT_INSTALLED"
    assert list((tmp_path / "workspaces").rglob(".staging-*")) == []


def test_installed_catalog_reads_a_plugin_from_the_skill_library(tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills / "report-writer", "report-writer")
    state = skills / "skills_state.json"
    state.write_text(
        json.dumps({"installed_plugins": [{"name": "report-pack", "skills": ["report-writer"]}]}),
        encoding="utf-8",
    )
    catalog = InstalledResourceCatalog(skills_dir=skills, builtin_dir=tmp_path / "missing-builtin", state_file=state)

    assert catalog.skill("report-writer").directory == (skills / "report-writer").resolve()
    assert catalog.plugin_skills("report-pack")[0].name == "report-writer"
    assert catalog.skill("absent") is None
    assert catalog.plugin_skills("absent") is None


def test_installed_catalog_reads_skills_bundled_in_a_plugin_package(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    package = tmp_path / "plugins" / "plugin_packages" / "local" / "office-pack"
    write_skill(package / "skills" / "doc-flow", "doc-flow", "整理办公文档。")
    (package / "manifest.json").write_text(
        json.dumps({"package_type": "plugin", "id": "office-pack", "skills": [{"dir": "./skills/doc-flow"}]}),
        encoding="utf-8",
    )
    catalog = InstalledResourceCatalog(skills_dir=skills, builtin_dir=tmp_path / "missing-builtin")

    sources = catalog.plugin_skills("office-pack")

    assert sources is not None and sources[0].name == "doc-flow"
    assert sources[0].directory == (package / "skills" / "doc-flow").resolve()


def test_installed_catalog_resolves_a_hub_asset_id_to_its_package(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    package = tmp_path / "plugins" / "plugin_packages" / "local" / "office-pack"
    write_skill(package / "skills" / "doc-flow", "doc-flow", "整理办公文档。")
    (package / "manifest.json").write_text(
        json.dumps({"package_type": "plugin", "id": "office-pack", "skills": [{"dir": "./skills/doc-flow"}]}),
        encoding="utf-8",
    )
    (tmp_path / "plugins" / "plugin_packages" / "hub_state.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "asset_id": "440c3c6e0e654b1eb9ec32c17bcd2923",
                        "kind": "plugin",
                        "version": "1.0.0",
                        "checksum_sha256": "abc",
                        "installed_at": "2026-10-19T00:00:00Z",
                        "package_id": "office-pack",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog = InstalledResourceCatalog(skills_dir=skills, builtin_dir=tmp_path / "missing-builtin")

    sources = catalog.plugin_skills("440c3c6e0e654b1eb9ec32c17bcd2923")

    assert sources is not None and sources[0].name == "doc-flow"


def test_a_plugin_without_skills_publishes_its_tools_and_rails(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    package = tmp_path / "plugins" / "plugin_packages" / "local" / "coding-guard"
    (package / "tools").mkdir(parents=True)
    (package / "rails").mkdir()
    (package / "tools" / "review.py").write_text("class Review:\n    pass\n", encoding="utf-8")
    (package / "rails" / "guard.py").write_text("class Guard:\n    pass\n", encoding="utf-8")
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "plugin",
                "id": "coding-guard",
                "name": "编程防护",
                "description": "审查代码",
                "tools": [{"file": "./tools/review.py", "class": "Review"}],
                "rails": [{"file": "./rails/guard.py", "class": "Guard"}],
            }
        ),
        encoding="utf-8",
    )
    host = service(
        tmp_path,
        InstalledResourceCatalog(skills_dir=skills, builtin_dir=tmp_path / "missing-builtin"),
        iter(["mate_guard"]),
    )

    view = host.create_colleague(
        NS,
        settings(capabilities=CapabilitySelection(model="deepseek#0", plugins=("coding-guard",))),
    )
    spec = host.current_snapshot(NS, view.agent_id).load_spec()

    plugin = spec.tools[0].params["tools"][0]
    assert plugin["type"] == "harness.tool.file"
    assert plugin["params"]["class_name"] == "Review"
    assert plugin["params"]["file_path"] == str((package / "tools" / "review.py").resolve())
    assert spec.rails[0].type == "harness.rail.file"
    assert spec.rails[0].params["class_name"] == "Guard"
    assert "已装配插件" in spec.system_prompt
