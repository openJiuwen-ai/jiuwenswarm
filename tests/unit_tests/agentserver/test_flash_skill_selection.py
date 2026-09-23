"""Flash retrieval lifecycle, removable integration, and native loading boundary."""
from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from openjiuwen.core.single_agent.skills.skill_manager import Skill
from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder
from openjiuwen.harness.prompts.sections.skills import build_skills_section
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.tools import ToolOutput
from openjiuwen.harness.tools.skills.skill_tool import SkillTool

from jiuwenswarm.agents.harness.flash.skill_selection import execution, service
from jiuwenswarm.agents.harness.flash.skill_selection.catalog import directory_id, read_catalog
from jiuwenswarm.agents.harness.flash.skill_selection.config import SelectionSettings
from jiuwenswarm.agents.harness.flash.skill_selection.explicit import explicit_request
from jiuwenswarm.agents.harness.flash.skill_selection.rail import SkillSelectionRail
from jiuwenswarm.agents.harness.flash.skill_selection.tool import (
    SYSTEM_GUIDANCE, TOOL_GUIDANCE, SkillSearchInput, SkillSearchTool,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash


def write_skill(root, name, description="制作演示文稿 幻灯片 presentation slides", **extra):
    directory = root / name
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump({"name": name, "description": description, **extra}, allow_unicode=True)
        + "---\n# Instructions\nFollow the user's task.\n", encoding="utf-8",
    )
    return directory


class NativeCatalog(SkillUseRail):
    def __init__(self, root):
        super().__init__([str(root)], include_tools=False)
        self.reload_count = 0
        self.scan()

    @staticmethod
    def normalize_skill_dirs(roots):
        return roots

    def scan(self):
        self.skills = []
        for root in self.skills_dir:
            for path in Path(root).glob("*/SKILL.md"):
                meta = yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
                self.skills.append(Skill(name=meta["name"], description=meta["description"], directory=path.parent))

    async def reload_skills(self):
        self.reload_count += 1
        self.scan()


class Abilities:
    def __init__(self):
        self.tools = {}
        self.execute = AsyncMock()

    def add_ability(self, card, tool):
        self.tools[card.name] = tool

    def remove_ability(self, name):
        self.tools.pop(name, None)

    async def list_tool_info(self, names):
        return [SimpleNamespace(name=name) for name in names if name in self.tools]


def native_output(directory):
    return ToolOutput(success=True, data={
        "skill_directory": str(directory),
        "skill_content": (directory / "SKILL.md").read_text(encoding="utf-8"),
    })


def native_loader(h):
    async def read_file(path):
        return SimpleNamespace(code=0, data=SimpleNamespace(content=Path(path).read_text(encoding="utf-8")))

    operation = SimpleNamespace(fs=lambda: SimpleNamespace(read_file=read_file))
    return SkillTool(operation=operation, get_skills=h.native.get_skills_for_session)


def skill_session():
    state = {}
    return SimpleNamespace(get_state=state.get, update_state=state.update)


@pytest.fixture
def harness(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    write_skill(root, "slides")
    native = NativeCatalog(root)
    config = {"flash": {"skill_selection": {"enabled": True}}}
    builder = SystemPromptBuilder()
    builder.add_section(build_skills_section(skill_lines="0. slides: presentation slides"))
    abilities = Abilities()
    abilities.execute.return_value = [(native_output(root / "slides"), None)]
    agent = SimpleNamespace(system_prompt_builder=builder, ability_manager=abilities)
    rail = SkillSelectionRail(config_provider=lambda: config, skill_rail_provider=lambda: native)
    rail.init(agent)
    yield SimpleNamespace(rail=rail, config=config, native=native, root=root, agent=agent, abilities=abilities)
    rail.uninit(agent)


@pytest.fixture(autouse=True)
def isolated_retrieval_runtime(monkeypatch):
    runtime = execution.SelectionRuntime()
    monkeypatch.setattr(execution, "_runtime", runtime)
    monkeypatch.setattr(service, "_services", service.OrderedDict())
    yield
    runtime.shutdown()


def context(text="制作三页幻灯片", session=None):
    return SimpleNamespace(
        inputs=SimpleNamespace(messages=[{"role": "user", "content": text}],
                               tools=[SimpleNamespace(name="skill_tool"), SimpleNamespace(name="bash")]),
        extra={}, session=session or SimpleNamespace(session_id="test-session"),
    )


async def search(h, ctx, query="制作演示文稿", keywords=None):
    # Tests install fixtures after rail.init; publish that simulated install,
    # then emulate the SDK's native catalog hook before the model runs.
    h.rail.refresh(force=True)
    await h.rail.service.current_snapshot()
    h.native.scan()
    await h.rail.before_model_call(ctx)
    await h.rail.after_model_call(ctx)
    return await h.rail._search_from_model(query, keywords or ["presentation", "幻灯片"], ctx)


async def load(h, search_id, name, source):
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    call = ToolCall(id="load-test", type="function", name="search_installed_skills",
                    arguments=json.dumps({"action": "load", "search_id": search_id, "skill_name": name}))
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_call=call, tool_name=call.name, tool_args=call.arguments),
                          extra=source.extra, session=source.session)
    await h.rail.load_rail.before_tool_call(ctx)
    await h.rail.before_tool_call(ctx)
    if ctx.inputs.tool_name != 'skill_tool':
        try:
            return await h.rail.handle_action(
                SkillSearchInput(action='load', search_id=search_id, skill_name=name), ctx)
        finally:
            await h.rail.after_tool_call(ctx)
    result, message = (await h.abilities.execute(ctx, call, ctx.session, parallel_tool_calls=False))[0]
    if isinstance(result, BaseException):
        raise result
    ctx.inputs.tool_result, ctx.inputs.tool_msg = result, message
    await h.rail.after_tool_call(ctx)
    result = ctx.inputs.tool_result
    return result.data if isinstance(result.data, dict) else {"status": "load_failed", "loaded": False}


def test_defaults_and_flash_only_config():
    assert not SelectionSettings.from_config({}).enabled
    assert not SelectionSettings.from_config({"dolores": {"skill_selection": {"enabled": True}}}).enabled
    assert not SelectionSettings.from_config({"skill_selection": {"enabled": True}}).enabled
    assert SelectionSettings.from_config({"flash": {"skill_selection": {"enabled": True}}}).candidate_k == 5


@pytest.mark.parametrize("enabled", [True, False])
async def test_flash_selection_survives_startup_cleanup_and_config_read(
    tmp_path, monkeypatch, harness, enabled,
):
    from jiuwenswarm.common import config as config_module, utils

    config_file = tmp_path / "config" / "config.yaml"
    config_file.parent.mkdir()
    selection = {
        "enabled": enabled,
        "catalog_check_interval_s": 5.0, "candidate_k": 3, "text_max_chars": 1700,
        "max_query_chars": 900, "query_timeout_s": 7.0, "startup_timeout_s": 45.0,
        "k1": 1.2, "b": 0.6, "name_weight": 0.7,
    }
    override = {"version": 1.0, "flash": {"enabled": True, "skill_selection": selection}}
    # Old user files are cleaned without disabling retrieval or resetting tuning.
    legacy = {**override, "flash": {**override["flash"], "skill_selection": {
        **selection, "prefetch": True, "prefetch_timeout_s": 0.8,
    }}}
    config_file.write_text(yaml.safe_dump(legacy), encoding="utf-8")
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(utils, "get_config_file", lambda: config_file)
    monkeypatch.setattr(config_module, "get_config_file", lambda: config_file)
    monkeypatch.setattr(utils, "_find_package_root", lambda: Path(utils.__file__).resolve().parent.parent)

    # Exercise the real startup path twice; a restart must not erase the switch
    # or the tuning values before the adapter reads the merged configuration.
    for _ in range(2):
        utils.update_config()
        assert yaml.safe_load(config_file.read_text(encoding="utf-8")) == override
        merged = config_module.get_merged_config_dict()
        assert merged["flash"]["skill_selection"] == selection

    harness.config.clear()
    harness.config.update(merged)
    ctx = context()
    await harness.rail.before_model_call(ctx)
    assert (SkillSearchTool.TOOL_NAME in harness.abilities.tools) is enabled
    if enabled:
        assert harness.rail.service.settings.candidate_k == 3
        result = await harness.rail._search_from_model("制作演示文稿", ["slides"], ctx)
        assert result["status"] == "candidates"
    else:
        assert harness.rail.service is None
    await harness.rail.after_model_call(ctx)


def test_shipped_flash_selection_defaults_are_disabled_and_complete():
    from dataclasses import asdict
    from jiuwenswarm.common.utils import load_yaml_dict, resolve_shipped_template_config_path

    template = load_yaml_dict(resolve_shipped_template_config_path())
    assert template["flash"]["skill_selection"] == asdict(SelectionSettings())
    assert not SelectionSettings.from_config(template).enabled


@pytest.mark.parametrize("settings", [{"enabled": "true"}, {"enabled": True, "candidate_k": 6},
                                      {"enabled": True, "query_timeout_s": float("nan")}])
def test_invalid_config_rejected(settings):
    with pytest.raises(ValueError):
        SelectionSettings.from_config({"flash": {"skill_selection": settings}})


async def test_search_then_native_load(harness):
    h = harness
    ctx = context()
    result = await search(h, ctx)
    assert result["status"] == "candidates" and not result["loaded"]
    assert result["candidates"][0]["name"] == "slides"
    h.abilities.execute.assert_not_awaited()
    chosen = await load(h, result["search_id"], "slides", ctx)
    assert chosen["loaded"] and "Follow the user's task." in chosen["instructions"]["skill_content"]
    call = h.abilities.execute.await_args
    assert call.args[0].extra is ctx.extra and call.args[2] is ctx.session
    assert call.args[1].name == "skill_tool"
    assert json.loads(call.args[1].arguments) == {"skill_name": "slides", "relative_file_path": "SKILL.md"}


async def test_only_top_five_and_allowed_skills(harness):
    h = harness
    for n in range(8):
        write_skill(h.root, f"slides-{n}")
    h.native.disabled_skills = {"slides", "slides-0"}
    result = await search(h, context())
    names = [c["name"] for c in result["candidates"]]
    assert len(names) == 5 and not ({"slides", "slides-0"} & set(names))


async def test_cross_request_ticket_and_forged_name_rejected(harness):
    h = harness
    first, second = context(), context()
    result = await search(h, first)
    assert (await load(h, result["search_id"], "slides", second))["status"] == "invalid_search"
    assert (await load(h, result["search_id"], "../other", first))["status"] == "invalid_choice"
    h.abilities.execute.assert_not_awaited()


async def test_rejected_call_does_not_contaminate_another_call_in_same_request(harness):
    h = harness
    source = context()
    result = await search(h, source)
    rejected = SimpleNamespace(extra=source.extra, session=source.session)
    h.rail.load_rail.reject(rejected, 'invalid_choice', 'Invalid candidate')
    request = SkillSearchInput(action='load', search_id=result['search_id'], skill_name='missing')
    assert (await h.rail.handle_action(request, rejected))['status'] == 'invalid_choice'
    assert (await load(h, result['search_id'], 'slides', source))['loaded'] is True


def test_invalid_utf8_skill_does_not_discard_readable_catalog(tmp_path):
    write_skill(tmp_path, 'slides')
    invalid = tmp_path / 'invalid-utf8'
    invalid.mkdir()
    (invalid / 'SKILL.md').write_bytes(b'\xff\xfeinvalid')
    documents = read_catalog([tmp_path], text_max_chars=2000)
    assert [document.name for document in documents] == ['slides']


@pytest.mark.parametrize("change", ["disabled", "file", "config", "catalog"])
async def test_revocation_or_change_before_load(harness, change):
    h = harness
    ctx = context()
    result = await search(h, ctx)
    if change == "disabled":
        h.native.disabled_skills = {"slides"}
    elif change == "file":
        write_skill(h.root, "slides", "changed instructions")
    elif change == "config":
        h.config["flash"]["skill_selection"]["enabled"] = False
    else:
        h.native.skills_dir = [str(h.root / "other-tenant")]
    assert (await load(h, result["search_id"], "slides", ctx))["status"] == "changed"
    h.abilities.execute.assert_not_awaited()


async def test_native_denial_is_not_loaded(harness):
    h = harness
    ctx = context()
    result = await search(h, ctx)
    h.abilities.execute.return_value = [(ToolOutput(success=False, error="permission denied"), None)]
    chosen = await load(h, result["search_id"], "slides", ctx)
    assert chosen["status"] == "load_failed" and not chosen["loaded"]


async def test_permission_interrupt_propagates(harness):
    from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
    from openjiuwen.core.single_agent.interrupt.response import InterruptRequest

    h = harness
    ctx = context()
    result = await search(h, ctx)
    interrupt = ToolInterruptException(InterruptRequest(message="confirm"))
    h.abilities.execute.return_value = [(interrupt, None)]
    with pytest.raises(ToolInterruptException):
        await load(h, result["search_id"], "slides", ctx)


async def test_repeated_load_rechecks_native_permission(harness):
    h = harness
    ctx = context()
    result = await search(h, ctx)
    first = await load(h, result["search_id"], "slides", ctx)
    assert first["loaded"]
    h.abilities.execute.return_value = [(ToolOutput(success=False, error="revoked"), None)]
    assert not (await load(h, result["search_id"], "slides", ctx))["loaded"]
    assert h.abilities.execute.await_count == 2


async def test_load_rejects_different_session_skill_directory(harness):
    h = harness
    old_root = h.root.parent / "old-skills"
    old_root.mkdir()
    old_dir = write_skill(old_root, "slides", "old instructions")
    ctx = context(session=skill_session())
    h.native._save_session_baseline(ctx.session, [Skill(name="slides", directory=old_dir)])
    tool = native_loader(h)
    native = await tool.invoke({"skill_name": "slides"}, session=ctx.session)
    assert native.success and native.data["skill_directory"] == str(old_dir)
    assert "old instructions" in native.data["skill_content"]

    async def execute(_ctx, call, session, **_kwargs):
        return [(await tool.invoke(json.loads(call.arguments), session=session), None)]

    h.abilities.execute.side_effect = execute
    result = await search(h, ctx)
    assert result["status"] == "candidates"
    chosen = await load(h, result["search_id"], "slides", ctx)
    assert chosen["status"] == "changed" and not chosen["loaded"]
    h.abilities.execute.assert_not_awaited()
    assert not ctx.extra[h.rail.REUSE].loaded


@pytest.mark.parametrize("change", ["directory", "body"])
async def test_load_rejects_native_result_changed_only_during_invoke(harness, change):
    h = harness
    ctx = context(session=skill_session())
    h.native._save_session_baseline(ctx.session, h.native.skills)
    path = h.root / "slides" / "SKILL.md"
    original = path.read_text(encoding="utf-8")
    old_root = h.root.parent / "old-skills"
    old_root.mkdir()
    old_dir = write_skill(old_root, "slides", "different instructions")
    tool = native_loader(h)

    async def execute(_ctx, call, session, **_kwargs):
        if change == "directory":
            h.native._save_session_baseline(session, [Skill(name="slides", directory=old_dir)])
        else:
            path.write_text(original + "\nDifferent instructions.\n", encoding="utf-8")
        try:
            output = await tool.invoke(json.loads(call.arguments), session=session)
            assert output.success
            return [(output, None)]
        finally:
            # Both live views match again before finish; only the actual return
            # value reveals the file that the native loader read.
            h.native._save_session_baseline(session, h.native.skills)
            path.write_text(original, encoding="utf-8")

    h.abilities.execute.side_effect = execute
    result = await search(h, ctx)
    chosen = await load(h, result["search_id"], "slides", ctx)
    assert chosen["status"] == "changed" and not chosen["loaded"]
    assert "instructions" not in chosen
    assert not ctx.extra[h.rail.REUSE].loaded


@pytest.mark.parametrize("style", ["plain", "bom_crlf", "media"])
async def test_load_accepts_matching_native_skill_result(harness, style):
    h = harness
    path = h.root / "slides" / "SKILL.md"
    if style == "bom_crlf":
        raw = "\ufeff" + path.read_text(encoding="utf-8")
        path.write_bytes(raw.replace("\n", "\r\n").encode("utf-8"))
    elif style == "media":
        raw = path.read_text(encoding="utf-8") + "\n![Example](assets/example.png)\n"
        path.write_text(raw, encoding="utf-8")
    ctx = context(session=skill_session())
    h.native._save_session_baseline(ctx.session, h.native.skills)
    tool = native_loader(h)

    async def execute(_ctx, call, session, **_kwargs):
        return [(await tool.invoke(json.loads(call.arguments), session=session), None)]

    h.abilities.execute.side_effect = execute
    result = await search(h, ctx)
    chosen = await load(h, result["search_id"], "slides", ctx)
    assert chosen["loaded"] and ctx.extra[h.rail.REUSE].loaded
    assert chosen["instructions"]["skill_directory"] == str(path.parent)
    if style == "media":
        assert chosen["instructions"]["content"] != chosen["instructions"]["skill_content"]


@pytest.mark.parametrize("query", [
    "请用 python-docx 生成 Word 报告。",
    "请用 my_library 处理表格。",
    "请用 `python-docx` 生成 Word 报告。",
    '请用 "季度总结" 作为标题。',
    "请用“职业技能”作为标题。",
    "请用 python-docx 编写技能使用说明。",
    'Please use "quarterly summary" as the title.',
])
async def test_ordinary_named_objects_keep_skill_retrieval(harness, query):
    h = harness
    assert explicit_request(query, query, ["slides"]) is None
    ctx = context(query)
    result = await search(h, ctx)
    assert result["status"] == "candidates"
    assert h.rail.EXPLICIT not in ctx.extra
    assert (await load(h, result["search_id"], "slides", ctx))["loaded"]


@pytest.mark.parametrize("query,name", [
    ("请用 slides 制作幻灯片。", "slides"),
    ("请用 `slides` 制作幻灯片。", "slides"),
    ("请用 python-docx 生成 Word 报告。", "python-docx"),
    ("请使用技能 missing-skill。", "missing-skill"),
    ("请用 missing-skill 技能生成报告。", "missing-skill"),
    ("请用不存在技能。", "不存在"),
    ("Please use skill missing-skill to write a report.", "missing-skill"),
    ("Please use missing-skill skill to write a report.", "missing-skill"),
])
def test_explicit_skill_evidence_is_preserved(query, name):
    request = explicit_request(query, query, ["slides", "python-docx"])
    assert request is not None and request.names == (name,)


async def test_frontend_unknown_skill_still_blocks_substitution(harness):
    h = harness
    envelope = {"content": "生成 Word 报告", "skills_to_use": ["python-docx"],
                "type": "user input", "source": "web", "preferred_response_language": "zh"}
    ctx = context("你收到一条消息：\n" + json.dumps(envelope))
    await h.rail.before_model_call(ctx)
    assert ctx.extra[h.rail.EXPLICIT]["error"] == "missing_or_not_allowed"
    assert "skill_tool" not in {t.name for t in ctx.inputs.tools}
    assert not ctx.extra[h.rail.REUSE].tickets
    await h.rail.after_model_call(ctx)


async def test_prompt_tools_disable_and_reenable(harness):
    h = harness
    ctx = context()
    builder = h.agent.system_prompt_builder
    native = builder.get_section("skills")
    await h.rail.before_model_call(ctx)
    assert 'slides' not in builder.get_section("skills").render()
    assert builder.get_section("skills") is not native
    assert builder.get_section(h.rail.SECTION) is not None
    assert "search_installed_skills" in [t.name for t in ctx.inputs.tools]
    await h.rail.after_model_call(ctx)
    assert builder.get_section("skills") == native
    h.config["flash"]["skill_selection"]["enabled"] = False
    await h.rail.before_model_call(ctx)
    assert not h.abilities.tools and h.rail.service is None
    assert builder.get_section("skills") == native
    h.config["flash"]["skill_selection"]["enabled"] = True
    await h.rail.before_model_call(ctx)
    assert "search_installed_skills" in h.abilities.tools
    await h.rail.on_invoke_exception(ctx)
    assert builder.get_section("skills") == native
    assert h.rail.REUSE not in ctx.extra


@pytest.mark.parametrize('language', ['cn', 'en'])
@pytest.mark.parametrize('catalog', ['', '0. slides: presentation slides\n\n1. finance: financial report'])
async def test_skill_entry_adapted_only_during_retrieval(harness, language, catalog):
    h = harness
    builder = h.agent.system_prompt_builder
    builder.language = language
    native = build_skills_section(skill_lines=catalog, language=language)
    builder.add_section(native)
    original = native.render(language)
    assert 'read_file' in original
    ctx = context()
    await h.rail.before_model_call(ctx)
    assert not ctx.extra.get(h.rail.FALLBACK)
    active = builder.get_section('skills').render(language)
    assert 'read_file' not in active
    assert 'slides' not in active and 'finance' not in active
    assert ('技能检索补充说明' if language == 'cn' else 'skill retrieval guidance') in active
    if catalog:
        assert ('选择最相关的技能，先阅读其 SKILL.md 再执行。' if language == 'cn'
                else 'Select the most relevant skill by reading its SKILL.md first.') in active
    assert native.render(language) == original  # Original object stays untouched.
    await h.rail.on_model_exception(ctx)
    assert builder.get_section('skills') is native
    assert builder.get_section('skills').render(language) == original
    assert not builder.has_section(h.rail.SECTION)


async def test_unknown_native_prompt_falls_back_without_rewriting_rules(harness):
    h = harness
    builder = h.agent.system_prompt_builder
    native = PromptSection(name='skills', content={'cn': 'custom native skill rules\ncustom skill catalog'})
    builder.add_section(native)
    ctx = context()
    original_tools = list(ctx.inputs.tools)
    await h.rail.before_model_call(ctx)
    assert ctx.extra[h.rail.FALLBACK]
    assert builder.get_section('skills') is native
    assert not builder.has_section(h.rail.SECTION)
    assert ctx.inputs.tools == original_tools


@pytest.mark.parametrize("legacy_options", [{}, {"prefetch": True, "prefetch_timeout_s": 0.001}])
async def test_model_request_excludes_full_catalog_before_and_after_search(harness, legacy_options):
    """Capture actual SDK model-call arguments, including runtime attachments."""
    from uuid import uuid4
    from openjiuwen.core.context_engine.base import ContextWindow
    from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, ToolMessage, UserMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.foundation.tool import ToolInfo
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent, ModelCallInputs
    from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager
    from openjiuwen.harness.rails import SkillUseRail

    h = harness
    h.config["flash"]["skill_selection"].update(legacy_options)
    hidden = {f"catalog-skill-{i:03d}": f"catalog-description-{i:03d}" for i in range(299)}
    for name, description in hidden.items():
        write_skill(h.root, name, description)
    h.native.scan()
    assert len(h.native.skills) == 300
    records = [SimpleNamespace(name=name, description=description) for name, description in hidden.items()]
    records.append(SimpleNamespace(name="slides", description="presentation slides"))
    callbacks = AgentCallbackManager("flash-prompt-test-" + uuid4().hex)
    agent = object.__new__(ReActAgent)
    agent._agent_callback_manager = callbacks
    agent._config = ReActAgentConfig(model_name="offline-capture")
    agent._ability_manager = AbilityManager()
    agent.prompt_builder = agent.system_prompt_builder = SystemPromptBuilder()
    default_sections = [
        PromptSection(name='identity', priority=0, content={'cn': '原生身份规则\n保留原有任务判断。'}),
        PromptSection(name='tools', priority=30, content={'cn': '原生工具规则\n使用 read_file 读取普通资料文件。'}),
        PromptSection(name='safety', priority=20, content={'cn': '原生权限规则\n遵循现有授权流程。'}),
    ]
    for section in default_sections:
        agent.system_prompt_builder.add_section(section)
    agent.prompt_attachment_manager = PromptAttachmentManager()
    # KV transport is unrelated to prompt isolation; no provider or server runs.
    agent._kv_cache_model_call_hook = SimpleNamespace(
        resolve_runtime=lambda *args: None,
        resolve_lineage=lambda *args: ("prompt-test-session", None),
        handle_context_window_change=AsyncMock(), build_invoke_kwargs=lambda **kwargs: {},
    )
    captured = []

    async def capture(**kwargs):
        captured.append(json.dumps({
            "messages": [message.model_dump(mode="json") for message in kwargs["messages"]],
            "tools": [tool.model_dump(mode="json") for tool in kwargs.get("tools") or []],
        }, ensure_ascii=False))
        return AssistantMessage(content="offline response")

    agent._llm = SimpleNamespace(invoke=capture)
    session = SimpleNamespace(get_session_id=lambda: "prompt-test-session")
    raw_request = ('原始材料：' + '客户订单、数据看板与需求评审。' * 300
                   + '```\\n基于上方素材制作三页幻灯片，不要网页，交付 .pptx。')
    history = [UserMessage(content=raw_request)]
    execution_tools = [ToolInfo(name='bash', description='execution-only-contract' * 100),
                       ToolInfo(name='web', description='web-only-contract' * 100),
                       ToolInfo(name='tools_search', description='discover ordinary tools'),
                       ToolInfo(name='invoke_tool', description='invoke a discovered tool'),
                       ToolInfo(name='skill_tool', description='native-skill-loader'),
                       ToolInfo(name='list_skills', description='native-catalog-list')]
    ordinary_names = ['bash', 'web', 'tools_search', 'invoke_tool']

    async def context_window(**kwargs):
        window = ContextWindow(system_messages=kwargs["system_messages"], context_messages=list(history),
                               tools=kwargs.get("tools") or [])
        for mutate in kwargs.get("window_mutators", []):
            window = await mutate(model_context, window)
        return window

    model_context = SimpleNamespace(session_id=session.get_session_id, get_context_window=context_window)
    ctx = AgentCallbackContext(agent=agent, session=session, context=model_context)
    rail = SkillSelectionRail(config_provider=lambda: h.config, skill_rail_provider=lambda: h.native)
    rail.init(agent)
    # The harness began indexing before we added 299 files; finish that simulated
    # installation before checking the normal (non-fallback) model-call path.
    rail.refresh(force=True)
    await rail.service.current_snapshot()

    async def native_catalog_prompt(model_ctx):
        native = SimpleNamespace(skills=records, skill_mode="all", SKILL_MODE_ALL="all",
                                 system_prompt_builder=agent.system_prompt_builder)
        section = SkillUseRail._build_skills_section(native)
        agent.system_prompt_builder.add_section(section)
        writer = agent.prompt_attachment_manager.bind_context(model_ctx)
        await writer.add_section(section="skills.runtime_changes", content=section.render("cn"),
                                 kind="skill", source="skill_use_rail")
        await writer.add_section(section="runtime.setting", content="keep-runtime-context",
                                 kind="runtime", source="test")

    await callbacks.register_callback(AgentCallbackEvent.BEFORE_MODEL_CALL, native_catalog_prompt, priority=95)
    await callbacks.register_rail(rail, agent)

    async def call_model():
        ctx.inputs = ModelCallInputs(messages=list(history), tools=list(execution_tools), model_context=model_context)
        await agent._railed_model_call(ctx)

    try:
        await call_model()
        assert "search_installed_skills" in captured[-1] and "keep-runtime-context" in captured[-1]
        assert ctx.extra[rail.REUSE].searches == 0
        assert not ctx.extra[rail.REUSE].tickets
        assert all(name not in captured[-1] and desc not in captured[-1] for name, desc in hidden.items())
        assert agent.system_prompt_builder.get_section("skills") is not None  # Restored only after sending.
        assert [t['name'] for t in json.loads(captured[-1])['tools']] == [*ordinary_names, SkillSearchTool.TOOL_NAME]
        assert not ctx.extra.get(rail.FALLBACK)
        # The extension appends retrieval guidance without replacing native
        # general system rules. Only the Skill catalog and legacy loading entry
        # are adapted; ordinary file-reading instructions stay unchanged.
        first_request = json.loads(captured[-1])
        first_prompt = '\n'.join(m.get('content') or '' for m in first_request['messages'] if m['role'] == 'system')
        for section in default_sections:
            assert section.render() in first_prompt
            assert agent.system_prompt_builder.get_section(section.name) is section
        native_section = agent.system_prompt_builder.get_section('skills')
        assert '选择最相关的技能，先阅读其 SKILL.md 再执行。' in first_prompt
        assert '执行前先用 read_file 阅读相关 SKILL.md。' not in first_prompt
        assert '执行前按技能检索补充说明加载相关 SKILL.md。' in first_prompt
        assert '不使用 read_file、bash 等工具绕过技能加载入口读取 SKILL.md' in first_prompt
        assert "search_installed_skills(action='search', query, keywords)" in first_prompt
        assert 'Office 文档处理与交付' in first_prompt
        assert '不要仅因数据少、任务简单' in first_prompt
        assert '无需为使用加速器先检索本地技能' in first_prompt
        assert '用户明确要求不使用技能时遵从' in first_prompt
        assert SYSTEM_GUIDANCE in first_prompt
        assert first_request['tools'][-1]['description'] == TOOL_GUIDANCE
        assert rail._tool.card.description == TOOL_GUIDANCE
        assert '普通工具能完成时直接调用' not in first_prompt
        assert '按用户最终任务目标选择路径' not in first_prompt

        # A normal tool result must not start Skill selection or trigger fallback.
        history.extend([
            AssistantMessage(tool_calls=[ToolCall(id='ordinary-read', type='function', name='bash', arguments='{}')]),
            ToolMessage(content='source material from an ordinary tool', tool_call_id='ordinary-read'),
        ])
        await call_model()
        assert [t['name'] for t in json.loads(captured[-1])['tools']] == [*ordinary_names, SkillSearchTool.TOOL_NAME]
        assert ctx.extra[rail.REUSE].searches == 0 and not ctx.extra[rail.REUSE].tickets
        assert not ctx.extra.get(rail.FALLBACK)
        assert all(name not in captured[-1] and desc not in captured[-1] for name, desc in hidden.items())

        result = await rail._search_from_model("制作演示文稿", ["presentation"], ctx)
        assert result["status"] == "candidates", result
        assert [candidate["name"] for candidate in result["candidates"]] == ["slides"]
        history.append(ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id="search-test"))
        await call_model()
        assert result["search_id"] in captured[-1]
        assert all(name not in captured[-1] and desc not in captured[-1] for name, desc in hidden.items())
        assert "skills.runtime_changes" not in captured[-1]
        first, second = json.loads(captured[0]), json.loads(captured[-1])
        assert [t['name'] for t in first['tools']] == [*ordinary_names, SkillSearchTool.TOOL_NAME]
        assert [t['name'] for t in second['tools']] == [SkillSearchTool.TOOL_NAME]
        assert first['tools'][-1] == second['tools'][0]  # Only the retrieval schema stays identical.
        assert first['messages'][0] == second['messages'][0]
        assert any(m.get('content') == raw_request for m in first['messages'])
        assert 'execution-only-contract' in captured[0]
        assert 'execution-only-contract' not in captured[-1]
        assert ctx.inputs.tools == execution_tools  # Restored after each call.

        # Native loading restores ordinary tools, with the same retrieval
        # description/schema and system guidance as both selection rounds.
        loaded = await load(SimpleNamespace(rail=rail, abilities=h.abilities),
                            result['search_id'], 'slides', ctx)
        assert loaded['loaded']
        await call_model()
        execution_request = json.loads(captured[-1])
        execution_search_tool = next(t for t in execution_request['tools']
                                     if t['name'] == SkillSearchTool.TOOL_NAME)
        assert execution_search_tool == first['tools'][-1]
        assert execution_request['messages'][0] == first['messages'][0]
        assert {*ordinary_names, 'skill_tool'} <= {t['name'] for t in execution_request['tools']}
        assert all(name not in captured[-1] and desc not in captured[-1] for name, desc in hidden.items())

        await rail.handle_action(
            SkillSearchInput(action='fallback'), ctx)
        await call_model()
        assert 'execution-only-contract' in captured[-1]
        assert all(name in captured[-1] and desc in captured[-1] for name, desc in hidden.items())
        assert '执行前先用 read_file 阅读相关 SKILL.md。' in captured[-1]
        assert '执行前按技能检索补充说明加载相关 SKILL.md。' not in captured[-1]

        # Disabling the optional extension still restores the native catalog.
        h.config["flash"]["skill_selection"]["enabled"] = False
        await call_model()
        assert all(name in captured[-1] and desc in captured[-1] for name, desc in hidden.items())
        assert '本节补充已安装技能' not in captured[-1]
        assert 'Office 文档处理与交付' not in captured[-1]
        disabled_prompt = '\n'.join(m.get('content') or '' for m in json.loads(captured[-1])['messages']
                                    if m['role'] == 'system')
        assert native_section.render() in disabled_prompt
        assert all(section.render() in disabled_prompt for section in default_sections)
    finally:
        rail.uninit(agent)
        await callbacks.clear()


async def test_excel_retrieval_is_independent_of_ppt_only_accelerator(harness):
    from openjiuwen.core.foundation.tool import ToolInfo
    h = harness
    write_skill(h.root, 'xlsx-craft', 'Create Excel xlsx spreadsheets with formulas and charts')
    h.abilities.execute.return_value = [(native_output(h.root / 'xlsx-craft'), None)]
    ctx = context('请生成 Excel 工作簿，录入月销售额，使用公式计算合计和平均值，并生成柱状图，交付 .xlsx。')
    ctx.inputs.tools.append(ToolInfo(name='skill_acceleration_exec', description='Only supports ppt-craft'))
    # The query below stands in for the model's output; this is a retrieval and
    # loading test, not a claim that every model will follow the routing policy.
    result = await search(h, ctx, query='生成 Excel 销售额工作簿，含公式和柱状图，交付 xlsx',
                          keywords=['Excel', 'xlsx', 'spreadsheet', 'formulas', 'charts'])
    assert result['status'] == 'candidates' and result['candidates'][0]['name'] == 'xlsx-craft'
    assert not ctx.extra.get(h.rail.FALLBACK)
    await h.rail.before_model_call(ctx)
    assert [t.name for t in ctx.inputs.tools] == [SkillSearchTool.TOOL_NAME]
    await h.rail.after_model_call(ctx)
    assert (await load(h, result['search_id'], 'xlsx-craft', ctx))['loaded']
    await h.rail.before_model_call(ctx)
    assert {'bash', 'skill_tool', 'skill_acceleration_exec'} <= {t.name for t in ctx.inputs.tools}
    await h.rail.after_model_call(ctx)


async def test_explicit_choice_uses_native_loader_later(harness):
    h = harness
    ctx = context('你收到一条消息：\n' + json.dumps({"content": "制作幻灯片", "skills_to_use": ["slides"],
                                                      "type": "user input", "source": "web",
                                                      "preferred_response_language": "zh"}))
    await h.rail.before_model_call(ctx)
    assert ctx.extra[h.rail.EXPLICIT]["ids"] == {directory_id(h.root / "slides")}
    h.abilities.execute.assert_not_awaited()  # No hidden read bypassing permissions.
    assert h.native.reload_count == 0  # Explicit selection never queries BM25.
    assert {'skill_tool', 'bash'} <= {t.name for t in ctx.inputs.tools}
    prompt = h.agent.system_prompt_builder.build()
    assert '用户明确指定以下技能，无需先检索。请通过原生 skill_tool 加载' in prompt
    assert '执行前先用 read_file 阅读相关 SKILL.md。' not in prompt
    assert not ctx.extra[h.rail.REUSE].tickets and not ctx.extra.get(h.rail.FALLBACK)
    await h.rail.after_model_call(ctx)


async def test_failed_index_restores_native_flow(harness, monkeypatch):
    h = harness
    ctx = context()
    await h.rail.before_model_call(ctx)
    await h.rail.after_model_call(ctx)
    monkeypatch.setattr(h.rail.service, "current_snapshot", AsyncMock(side_effect=RuntimeError("broken index")))
    result = await h.rail._search_from_model("slides", ["slides"], ctx)
    assert result["status"] == "failed" and not result["loaded"]
    await h.rail.before_model_call(ctx)
    assert h.agent.system_prompt_builder.get_section("skills") is not None
    assert h.agent.system_prompt_builder.get_section(h.rail.SECTION) is None


async def test_invalid_runtime_config_keeps_native_prompt(harness):
    h = harness
    h.config["flash"]["skill_selection"]["candidate_k"] = 999
    await h.rail.before_model_call(context())
    assert h.agent.system_prompt_builder.get_section("skills") is not None
    assert not h.abilities.tools


async def test_disabled_tool_keeps_native_prompt(harness):
    h = harness
    h.config["react"] = {"disabled_tools": ["search_installed_skills"]}
    await h.rail.before_model_call(context())
    assert h.agent.system_prompt_builder.get_section("skills") is not None
    assert not h.abilities.tools


async def test_hot_add_modify_delete_catalog(harness):
    h = harness
    ctx = context()
    result = await search(h, ctx, "音频 transcription", ["transcription"])
    assert result["status"] == "no_match"
    directory = write_skill(h.root, "audio", "audio transcription 音频转写")
    h.rail.refresh(force=True)
    result = await h.rail._search_from_model("音频 transcription", ["transcription"], ctx)
    assert result["candidates"][0]["name"] == "audio"
    write_skill(h.root, "audio", "财务 spreadsheet 表格")
    h.rail.refresh(force=True)
    result = await h.rail._search_from_model("音频 transcription", ["transcription"], ctx)
    assert result["status"] == "no_match"
    (directory / "SKILL.md").unlink()
    h.rail.refresh(force=True)
    assert (await h.rail._search_from_model("财务 spreadsheet", ["spreadsheet"], ctx))["status"] == "no_match"


async def test_concurrent_requests_do_not_share_choices(harness):
    h = harness
    a, b = context(session=object()), context(session=object())
    for ctx in (a, b):
        await h.rail.before_model_call(ctx)
        await h.rail.after_model_call(ctx)
    first, second = await asyncio.gather(
        h.rail._search_from_model("slides", ["presentation"], a),
        h.rail._search_from_model("slides", ["presentation"], b),
    )
    assert first["search_id"] != second["search_id"]
    assert (await load(h, first["search_id"], "slides", b))["status"] == "invalid_search"


async def test_tool_context_is_bound_to_own_session(harness):
    h = harness
    ctx = context()
    await h.rail.before_model_call(ctx)
    await h.rail.after_model_call(ctx)
    tool = h.rail._tool
    tool.bind(ctx)
    result = await tool.invoke({"query": "slides", "keywords": ["slides"]}, session=object())
    assert not result.success
    result = await tool.invoke({"query": "slides", "keywords": ["slides"]}, session=ctx.session)
    assert result.data["status"] == "candidates"
    assert (await tool.invoke({"query": "slides", "keywords": ["slides"]}, session=ctx.session)).data["status"] == "unavailable"


def test_removing_module_does_not_break_flash(monkeypatch):
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    original = builtins.__import__

    def without_module(name, *args, **kwargs):
        if name == "jiuwenswarm.agents.harness.flash.skill_selection":
            raise ModuleNotFoundError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_module)
    assert adapter._build_flash_skill_selection_rail({}) is None
    native = object()
    monkeypatch.setattr(interface_deep.JiuWenSwarmDeepAdapter, "_instantiate_rails", lambda *args: [native])
    assert adapter._instantiate_rails([], {}) == [native]
    old = SimpleNamespace(load_rail=object())
    adapter._flash_skill_selection_rail = old
    monkeypatch.setattr(interface_deep.JiuWenSwarmDeepAdapter, "_get_current_agent_rails", lambda *args: ([native], []))
    rails, removed = adapter._get_current_agent_rails({}, {})
    assert rails == [native] and removed == [old.load_rail, old]
    assert adapter._flash_skill_selection_rail is None


def test_flash_reload_keeps_optional_rail_and_uses_new_snapshot(monkeypatch):
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    adapter._config_base_cache = {"flash": {"skill_selection": {"enabled": True}}}
    adapter._skill_rail = None
    rail = adapter._build_flash_skill_selection_rail(adapter._config_base_cache)
    monkeypatch.setattr(interface_deep.JiuWenSwarmDeepAdapter, "_get_current_agent_rails", lambda *args: ([], []))
    adapter._config_base_cache = {"flash": {"skill_selection": {"enabled": False}}}
    rails, _ = adapter._get_current_agent_rails({}, adapter._config_base_cache)
    assert rails == [rail.load_rail, rail]
    assert not SelectionSettings.from_config(rail._config_provider()).enabled


def test_other_adapters_have_no_retrieval_extension_hook():
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    assert not hasattr(interface_deep.JiuWenSwarmDeepAdapter, "_build_flash_skill_selection_rail")
    assert not hasattr(JiuwenSwarmCodeAdapter, "_build_flash_skill_selection_rail")


def test_disabled_rail_does_not_create_catalog(monkeypatch):
    monkeypatch.setattr(service, "get_service", lambda *args: pytest.fail("disabled retrieval created an index"))
    rail = SkillSelectionRail(config_provider=lambda: {}, skill_rail_provider=lambda: None)
    abilities = Abilities()
    rail.init(SimpleNamespace(ability_manager=abilities))
    assert not abilities.tools and rail.service is None


@pytest.mark.parametrize("operation", ["skill", "ordinary"])
@pytest.mark.parametrize("policy", ["allow", "deny", "interrupt"])
async def test_real_sdk_dispatch_preserves_native_hooks(harness, monkeypatch, policy, operation):
    """Use the installed SDK dispatcher/callback ordering, without an LLM/server."""
    from uuid import uuid4
    from openjiuwen.core.foundation.llm.schema.message import ToolMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
    from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent

    h = harness
    ctx = context()
    if operation == 'skill':
        result = await search(h, ctx)
        call = ToolCall(id="load-native", type="function", name="search_installed_skills",
                        arguments=json.dumps({"action": "load", "search_id": result["search_id"], "skill_name": "slides",
                                              "query": "制作幻灯片", "keywords": ["presentation"]}))
        native_name = 'skill_tool'
    else:
        await h.rail.before_model_call(ctx)
        assert 'bash' in {t.name for t in ctx.inputs.tools}
        await h.rail.after_model_call(ctx)
        call = ToolCall(id="load-native", type="function", name="bash", arguments='{}')
        native_name = 'bash'
    callbacks = AgentCallbackManager("flash-selection-test-" + uuid4().hex)
    dispatch_agent = SimpleNamespace(agent_callback_manager=callbacks)
    manager = AbilityManager()
    events = []

    async def native_permission(tool_ctx):
        events.append(("permission", tool_ctx.inputs.tool_name))
        assert tool_ctx.inputs.tool_name == native_name
        if policy == "interrupt":
            from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail
            BaseInterruptRail()._raise_interrupt(native_name, tool_ctx.inputs.tool_call,
                                               InterruptRequest(message="confirm tool"))
        if policy == "deny":
            tool_ctx.extra["_skip_tool"] = True
            tool_ctx.inputs.tool_result = ToolOutput(success=False, error="permission denied")
            tool_ctx.inputs.tool_msg = ToolMessage(content="permission denied", tool_call_id="load-native")

    async def native_execute(tool_call, session, tag=None):
        events.append(("execute", tool_call.name))
        output = (native_output(h.root / "slides") if operation == "skill"
                  else ToolOutput(success=True, data="native instructions"))
        return output, ToolMessage(content=str(output), tool_call_id=tool_call.id)

    async def native_active_state(tool_ctx):
        events.append(("active_state", tool_ctx.inputs.tool_name))

    monkeypatch.setattr(manager, "_execute_single_tool_call", native_execute)
    await callbacks.register_rail(h.rail.load_rail, dispatch_agent)
    await callbacks.register_rail(h.rail, dispatch_agent)
    await callbacks.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, native_permission, priority=95)
    await callbacks.register_callback(AgentCallbackEvent.AFTER_TOOL_CALL, native_active_state, priority=25)
    sdk_ctx = AgentCallbackContext(agent=dispatch_agent, extra=ctx.extra, session=ctx.session)
    try:
        output, message = (await manager.execute(sdk_ctx, call, ctx.session, parallel_tool_calls=False))[0]
        assert events[0] == ("permission", native_name)
        if policy == "interrupt":
            assert isinstance(output, ToolInterruptException)
            assert output.tool_call.name == native_name and output.tool_call.id == "load-native"
            assert ("execute", native_name) not in events
        elif policy == "deny":
            assert not output.success and "denied" in message.content
            assert ("execute", native_name) not in events
        else:
            assert events == [("permission", native_name), ("execute", native_name), ("active_state", native_name)]
            if operation == 'skill':
                assert output.data["loaded"] and json.loads(message.content)["selected"] == "slides"
            else:
                assert output.success and output.data == 'native instructions'
        if operation == 'ordinary':
            assert ctx.extra[h.rail.REUSE].searches == 0
            assert not ctx.extra[h.rail.REUSE].loaded and not ctx.extra.get(h.rail.FALLBACK)
            ctx.inputs = context().inputs
            await h.rail.before_model_call(ctx)
            assert {t.name for t in ctx.inputs.tools} == {'bash', SkillSearchTool.TOOL_NAME}
            assert 'slides' not in h.agent.system_prompt_builder.get_section('skills').render()
            await h.rail.after_model_call(ctx)
    finally:
        await callbacks.clear()


async def test_cancellation_releases_request_and_native_cache_flag(harness, monkeypatch):
    h = harness
    ctx = context()
    await search(h, ctx)
    write_skill(h.root, 'new-slides')
    h.rail.refresh(force=True)
    entered = asyncio.Event()

    async def blocked_reload():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(h.native, "reload_skills", blocked_reload)
    pending = asyncio.create_task(h.rail._search_from_model("slides", ["slides"], ctx))
    await asyncio.wait_for(entered.wait(), timeout=5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert h.native.enable_cache is True
    await h.rail.after_invoke(ctx)
    assert h.rail.REUSE not in ctx.extra
    assert h.agent.system_prompt_builder.get_section("skills") is not None


async def warm_catalog(h):
    h.rail.refresh(force=True)
    await h.rail.service.current_snapshot()


async def test_two_round_selection_and_execution_metrics_are_separate(harness, monkeypatch):
    from jiuwenswarm.agents.harness.flash.skill_selection import metrics
    h = harness
    await warm_catalog(h)
    events = []
    monkeypatch.setattr(metrics, 'emit', lambda logger, event, trace, **fields:
                        events.append(dict(event=event, **fields)))
    ctx = context()
    await h.rail.before_model_call(ctx)
    state = ctx.extra[h.rail.REUSE]
    assert state.searches == 0 and not state.tickets
    ctx.inputs.response = SimpleNamespace(usage_metadata=dict(
        input_tokens=200, cache_tokens=120, output_tokens=10, reasoning_tokens=3))
    await h.rail.after_model_call(ctx)
    result = await h.rail.handle_action(
        SkillSearchInput(action='search', query='制作幻灯片', keywords=['slides']), ctx)
    assert result['candidates'][0]['name'] == 'slides'
    assert h.native.reload_count == 0
    h.abilities.execute.assert_not_awaited()
    await h.rail.before_model_call(ctx)
    ctx.inputs.response = SimpleNamespace(usage_metadata=dict(
        input_tokens=250, cache_tokens=200, output_tokens=15, reasoning_tokens=4))
    await h.rail.after_model_call(ctx)
    assert (await load(h, result['search_id'], 'slides', ctx))['loaded']
    selection = next(e for e in events if e['event'] == 'selection_stage')
    assert selection['model_calls'] == 2 and selection['input_tokens'] == 450
    assert selection['cache_tokens'] == 320 and selection['output_tokens'] == 25
    assert selection['first_load_ms'] is not None
    # Subsequent execution rounds count towards the full task, not selection.
    ctx.inputs = context().inputs
    await h.rail.before_model_call(ctx)
    assert state.searches == 1
    ctx.inputs.response = SimpleNamespace(usage_metadata=dict(
        input_tokens=300, cache_tokens=200, output_tokens=30, reasoning_tokens=5))
    await h.rail.after_model_call(ctx)
    await h.rail.after_invoke(ctx)
    summary = next(e for e in events if e['event'] == 'request_summary')
    assert summary['model_calls'] == 3 and summary['input_tokens'] == 750
    assert summary['cache_tokens'] == 520 and summary['uncached_input_tokens'] == 230
    assert summary['usage_complete'] and summary['outcome'] == 'invoke_returned'
    assert [e['call_stage'] for e in events if e['event'] == 'request_progress'] == ['route', 'select', 'execute']


@pytest.mark.parametrize('text', ['你好', '谢谢！', '23 + 48 = ?', '继续'])
async def test_direct_answers_do_not_trigger_automatic_search(harness, text, monkeypatch):
    h = harness
    search = AsyncMock(side_effect=AssertionError('unnecessary search'))
    monkeypatch.setattr(h.rail, '_search_from_model', search)
    ctx = context(text)
    await h.rail.before_model_call(ctx)
    search.assert_not_awaited()
    await h.rail.after_model_call(ctx)


async def test_stale_candidate_file_change_is_rejected(harness):
    h = harness
    await warm_catalog(h)
    write_skill(h.root, 'slides', 'changed body after snapshot')
    ctx = context()
    await h.rail.before_model_call(ctx)
    await h.rail.after_model_call(ctx)
    result = await h.rail.handle_action(
        SkillSearchInput(action='search', query='制作幻灯片', keywords=['slides']), ctx)
    assert (await load(h, result['search_id'], 'slides', ctx))['status'] == 'changed'
    h.abilities.execute.assert_not_awaited()


async def test_missing_usage_and_failed_request_are_not_reported_as_zero_or_success(harness, monkeypatch):
    h = harness
    ctx = context()
    await h.rail.before_model_call(ctx)
    await h.rail.on_model_exception(ctx)
    from jiuwenswarm.agents.harness.flash.skill_selection import metrics
    events = []
    monkeypatch.setattr(metrics, 'emit', lambda logger, event, trace, **fields: events.append(dict(event=event, **fields)))
    await h.rail.on_invoke_exception(ctx)
    summary = next(e for e in events if e['event'] == 'request_summary')
    assert summary['input_tokens'] is None and not summary['usage_complete']
    assert summary['model_errors'] == 1 and summary['outcome'] == 'interrupted_or_failed'


async def test_search_serves_snapshot_while_background_check_runs(harness, monkeypatch):
    import threading
    h = harness
    await warm_catalog(h)
    svc = h.rail.service
    original = svc._prepare_checked
    entered, release = threading.Event(), threading.Event()
    def delayed_check(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(svc, '_prepare_checked', delayed_check)
    svc._checked_at = 0
    try:
        snapshot = await asyncio.wait_for(svc.current_snapshot(allow_stale=True), timeout=0.2)
        assert snapshot[0] is not None
        assert await asyncio.to_thread(entered.wait, 1)
        assert not release.is_set()
    finally:
        release.set()
    await svc.current_snapshot()


@pytest.mark.parametrize("force_refresh", [False, True])
async def test_snapshot_handles_failed_build_with_pending_refresh(tmp_path, monkeypatch, force_refresh):
    """A waiter follows the forced replacement, but an unrecovered failure stays visible."""
    import threading

    root = tmp_path / "skills"
    root.mkdir()
    write_skill(root, "slides")
    svc = service.SelectionService((root,), SelectionSettings())
    prepare = svc._prepare_checked
    entered, release = threading.Event(), threading.Event()
    attempts = 0

    def interrupted_install(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(timeout=5)
            raise execution.SelectionBusy("Skill files are changing")
        return prepare(*args)

    monkeypatch.setattr(svc, "_prepare_checked", interrupted_install)
    pending = None
    try:
        svc.refresh()
        assert await asyncio.to_thread(entered.wait, 5)
        pending = asyncio.create_task(svc.current_snapshot())
        # Let the waiter capture the first build before the install completes.
        await asyncio.sleep(0)
        if force_refresh:
            write_skill(root, "installed")
            svc.refresh(force=True)
        release.set()
        if force_refresh:
            pipeline, generation = await asyncio.wait_for(pending, 5)
            assert attempts == 2 and generation == 1
            assert len(pipeline.documents) == 2
            assert svc._error is None
        else:
            with pytest.raises(execution.SelectionBusy, match="Skill files are changing"):
                await asyncio.wait_for(pending, 5)
            assert attempts == 1 and not svc._ready
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


async def test_live_mixed_load_arguments_succeed_without_another_model_turn(harness):
    """Replay the exact extra-field pattern observed in all three live tasks."""
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from jiuwenswarm.agents.harness.flash.skill_selection.tool import SkillSearchInput
    h = harness
    ctx = context()
    result = await search(h, ctx)
    schema = h.rail._tool.card.input_params
    assert {'query', 'keywords'} <= schema['properties'].keys()
    assert schema['properties']['action']['enum'] == ['search', 'load', 'fallback']
    args = dict(action='load', search_id=result['search_id'], skill_name='slides',
                query='制作幻灯片', keywords=['presentation'])
    assert SkillSearchInput.model_validate(args).query is None
    call = ToolCall(id='live-replay', type='function', name=SkillSearchTool.TOOL_NAME, arguments=json.dumps(args))
    tool_ctx = SimpleNamespace(inputs=SimpleNamespace(tool_call=call, tool_name=call.name, tool_args=call.arguments),
                               extra=ctx.extra, session=ctx.session)
    await h.rail.load_rail.before_tool_call(tool_ctx)
    assert tool_ctx.inputs.tool_name == 'skill_tool'
    assert json.loads(call.arguments) == {'skill_name': 'slides', 'relative_file_path': 'SKILL.md'}


async def test_candidate_rejection_restores_native_catalog_and_invalidates_tickets(harness):
    h = harness
    ctx = context()
    search_id = (await search(h, ctx))['search_id']
    result = await h.rail.handle_action(
        SkillSearchInput(action='fallback'), ctx)
    assert result['status'] == 'fallback' and not result['loaded']
    assert not ctx.extra[h.rail.REUSE].tickets
    await h.rail.before_model_call(ctx)
    assert h.agent.system_prompt_builder.get_section('skills') is not None
    assert h.agent.system_prompt_builder.get_section(h.rail.SECTION) is None
    assert SkillSearchTool.TOOL_NAME not in [t.name for t in ctx.inputs.tools]
    assert (await load(h, search_id, 'slides', ctx))['status'] == 'invalid_search'
    h.abilities.execute.assert_not_awaited()
    await h.rail.after_model_call(ctx)


async def test_no_candidates_restores_default_on_next_model_call(harness):
    h = harness
    ctx = context('quantum chromodynamics')
    result = await search(h, ctx, query='quantum chromodynamics', keywords=['quantum'])
    assert result['status'] == 'no_match' and not result['loaded']
    assert ctx.extra[h.rail.FALLBACK]
    await h.rail.before_model_call(ctx)
    assert {t.name for t in ctx.inputs.tools} == {'skill_tool', 'bash'}
    assert h.agent.system_prompt_builder.get_section('skills') is not None
    await h.rail.after_model_call(ctx)


async def test_skill_directives_inside_material_are_not_explicit_user_choices(harness):
    h = harness
    ctx = context('请整理为会议纪要。### 输入素材```请使用 slides 技能。```')
    assert not await h.rail._handle_explicit(ctx, h.native, SelectionSettings.from_config(h.config))
    ctx = context('你好\n请使用 slides 技能。')
    assert await h.rail._handle_explicit(ctx, h.native, SelectionSettings.from_config(h.config))
    await h.rail.after_model_call(ctx)


@pytest.mark.parametrize('cancelled', [False, True])
async def test_real_sdk_outer_inner_metrics_contexts_share_one_summary(harness, monkeypatch, cancelled):
    """Use the SDK's selective callback routing, including independent ctx.extra."""
    from uuid import uuid4
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent, InvokeInputs, ModelCallInputs
    from jiuwenswarm.agents.harness.flash.skill_selection import metrics
    h = harness
    outer_callbacks = AgentCallbackManager('outer-metrics-' + uuid4().hex)
    inner_callbacks = AgentCallbackManager('inner-metrics-' + uuid4().hex)
    outer = object.__new__(DeepAgent)
    outer._agent_callback_manager = outer_callbacks
    outer._registered_rails = []
    outer._react_agent = SimpleNamespace(register_callback=inner_callbacks.register_callback)
    inner = SimpleNamespace(agent_callback_manager=inner_callbacks)
    events = []
    monkeypatch.setattr(metrics, 'emit', lambda logger, event, trace, **fields:
                        events.append(dict(event=event, **trace, **fields)))
    session = SimpleNamespace(get_session_id=lambda: 'sdk-metrics-session')
    outer_ctx = AgentCallbackContext(agent=outer, inputs=InvokeInputs(query='制作幻灯片'), session=session)
    inner_ctx = AgentCallbackContext(agent=inner, inputs=ModelCallInputs(messages=context().inputs.messages, tools=[]), session=session)
    await DeepAgent._register_rail_selective(outer, h.rail)
    try:
        await outer_ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
        await inner_ctx.fire(AgentCallbackEvent.BEFORE_MODEL_CALL)
        inner_ctx.inputs.response = SimpleNamespace(usage_metadata=dict(input_tokens=200, cache_tokens=100, output_tokens=20, reasoning_tokens=5))
        await inner_ctx.fire(AgentCallbackEvent.AFTER_MODEL_CALL)
        if cancelled:
            inner_ctx.inputs.response = None
            await inner_ctx.fire(AgentCallbackEvent.BEFORE_MODEL_CALL)
            # SDK cancellation skips both AFTER_MODEL_CALL and ON_MODEL_EXCEPTION.
        else:
            outer_ctx.inputs.result = {'output': 'done'}
        await outer_ctx.fire(AgentCallbackEvent.AFTER_INVOKE)
        progress = next(e for e in events if e['event'] == 'request_progress')
        summary = next(e for e in events if e['event'] == 'request_summary')
        assert summary['request_id'] == progress['request_id']
        assert summary['input_tokens'] == 200 and summary['model_calls'] == (2 if cancelled else 1)
        assert summary['usage_complete'] is not cancelled
        assert summary['outcome'] == ('interrupted_or_failed' if cancelled else 'invoke_returned')
        assert not h.rail._metrics.active
        assert h.agent.system_prompt_builder.get_section('skills') is not None
    finally:
        await outer_callbacks.clear()
        await inner_callbacks.clear()


async def test_two_round_views_restore_execution_and_keep_request_intact(harness, monkeypatch):
    h = harness
    text = '素材中的 Excel、旅游和订单。' * 300 + '```\\n真正要求：制作三页幻灯片，交付 .pptx，不要 HTML。'
    ctx = context(text)
    original_tools = list(ctx.inputs.tools)
    h.rail.refresh(force=True)
    await h.rail.service.current_snapshot()
    # No retrieval may run before the model has supplied query and keywords.
    with monkeypatch.context() as first_round:
        automatic_search = AsyncMock(side_effect=AssertionError('query must come from the model'))
        first_round.setattr(h.rail, '_search_from_model', automatic_search)
        await h.rail.before_model_call(ctx)
        automatic_search.assert_not_awaited()
    first_schema = next(t.model_dump() for t in ctx.inputs.tools if t.name == SkillSearchTool.TOOL_NAME)
    assert ctx.inputs.messages[0]['content'] == text
    assert [t.name for t in ctx.inputs.tools] == ['bash', SkillSearchTool.TOOL_NAME]
    assert ctx.extra[h.rail.STATE + '.model_stage'] == 'route'
    assert ctx.extra[h.rail.REUSE].searches == 0
    await h.rail.after_model_call(ctx)
    assert ctx.inputs.tools == original_tools
    result = await h.rail.handle_action(
        SkillSearchInput(action='search', query='制作三页幻灯片，交付 pptx，不要 HTML',
                         keywords=['presentation', 'slides', 'pptx']), ctx)
    assert result['status'] == 'candidates'
    await h.rail.before_model_call(ctx)
    assert [t.name for t in ctx.inputs.tools] == [SkillSearchTool.TOOL_NAME]
    assert ctx.inputs.tools[0].model_dump() == first_schema
    assert ctx.extra[h.rail.STATE + '.model_stage'] == 'select'
    await h.rail.after_model_call(ctx)
    assert (await load(h, result['search_id'], 'slides', ctx))['loaded']
    await h.rail.before_model_call(ctx)
    assert {'bash', 'skill_tool'} <= {t.name for t in ctx.inputs.tools}
    assert 'slides' not in h.agent.system_prompt_builder.get_section('skills').render()
    await h.rail.after_model_call(ctx)


async def test_two_round_warm_query_does_not_rescan_and_revocation_is_live(harness, monkeypatch):
    h = harness
    ctx = context()
    h.rail.refresh(force=True)
    await h.rail.service.current_snapshot()
    monkeypatch.setattr(h.native, 'reload_skills', AsyncMock(side_effect=AssertionError('duplicate native scan')))
    with monkeypatch.context() as query_patch:
        query_patch.setattr(h.rail.service, '_submit_build', lambda: pytest.fail('fresh catalog rechecked'))
        await h.rail.before_model_call(ctx)
        await h.rail.after_model_call(ctx)
        result = await h.rail.handle_action(
            SkillSearchInput(action='search', query='制作幻灯片', keywords=['slides']), ctx)
    assert result['status'] == 'candidates'
    h.native.disabled_skills = {'slides'}
    assert (await load(h, result['search_id'], 'slides', ctx))['status'] == 'changed'
    h.abilities.execute.assert_not_awaited()


async def test_two_round_timeout_restores_all_tools_and_catalog(harness, monkeypatch):
    h = harness
    h.config['flash']['skill_selection']['query_timeout_s'] = 0.1
    ctx = context()
    await h.rail.before_model_call(ctx)
    await h.rail.after_model_call(ctx)
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(h.rail.service, 'current_snapshot', slow)
    result = await asyncio.wait_for(h.rail.handle_action(
        SkillSearchInput(action='search', query='制作幻灯片', keywords=['slides']), ctx), 2)
    assert result['status'] == 'timeout' and cancelled.is_set()
    await h.rail.before_model_call(ctx)
    assert {t.name for t in ctx.inputs.tools} == {'bash', 'skill_tool'}
    assert h.agent.system_prompt_builder.get_section('skills') is not None
    assert SkillSearchTool.TOOL_NAME not in h.abilities.tools


async def test_two_round_unsuitable_candidates_do_not_search_again(harness, monkeypatch):
    h = harness
    ctx = context()
    result = await search(h, ctx)
    monkeypatch.setattr(h.rail, '_search_once', AsyncMock(side_effect=AssertionError('must not search again')))
    assert (await h.rail.handle_action(
        SkillSearchInput(action='fallback'), ctx))['status'] == 'fallback'
    assert not ctx.extra[h.rail.REUSE].tickets
    await h.rail.before_model_call(ctx)
    assert {t.name for t in ctx.inputs.tools} == {'bash', 'skill_tool'}
    assert h.agent.system_prompt_builder.get_section('skills') is not None


def test_compact_candidates_preserve_negative_capabilities_and_tail_restrictions():
    from jiuwenswarm.agents.harness.flash.skill_selection.presentation import candidate_view, compact_record
    from jiuwenswarm.agents.harness.flash.skill_selection.types import SkillDocument
    record = {'supported': [['create', 'docx'], ['create', 'html'], ['read', 'docx']],
              'denied': [['read', 'doc'], ['edit', 'doc']], 'outputs': ['docx', 'html'], 'closed_outputs': True}
    document = SkillDocument('a', 'writer', '', '', '',
                             '生成文档。' + '详细描述。' * 150 + '仅支持本地文件，不支持云服务。', record)
    view = candidate_view(document)
    assert len(view['description']) == 640 and view['description_truncated']
    assert any('不支持云服务' in s for s in view['restrictions'])
    compact = compact_record(record)
    for field in ('supported', 'denied'):
        unpacked = {(operation, fmt) for operation, formats in compact[field].items() for fmt in formats}
        assert unpacked == {tuple(item) for item in record[field]}
    assert compact['closed_outputs'] is True and compact['outputs'] == record['outputs']


def test_inverted_bm25_preserves_original_scores():
    import math
    import random
    from collections import Counter
    from jiuwenswarm.agents.harness.flash.skill_selection.bm25 import BM25Okapi
    rng = random.Random(1729)
    corpus = [[rng.choice(['word', 'slides', '表格', '会议', '合同']) for _ in range(rng.randrange(0, 60))]
              for _ in range(150)]
    index = BM25Okapi(corpus)
    terms = ['absent', 'slides', '会议', 'slides', 'word']
    frequencies = Counter(term for doc in corpus for term in set(doc))
    avg = sum(map(len, corpus)) / len(corpus)
    expected = []
    for doc in corpus:
        counts = Counter(doc)
        score = 0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency:
                idf = math.log(1 + (len(corpus) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += idf * frequency * 1.9 / (frequency + 0.9 * (0.6 + 0.4 * len(doc) / avg))
        expected.append(score)
    assert index.get_scores(terms) == pytest.approx(expected, rel=1e-14)
    assert BM25Okapi([]).get_scores(terms) == []
