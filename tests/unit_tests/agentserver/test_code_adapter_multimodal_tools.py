from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.runner import Runner
from openjiuwen.harness.tools.multimodal import audio as audio_tools
from openjiuwen.harness import (
    AudioModelConfig,
    DeepAgent,
    DeepAgentConfig,
    VisionModelConfig,
)

from jiuwenswarm.agents.harness.code.spec import CodeBuildContext
from jiuwenswarm.agents.harness.code import spec as code_spec
from jiuwenswarm.agents.harness.common.tools import video_tools
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)


class _AbilityManager:
    def __init__(self):
        self.cards = {}

    def add(self, card):
        self.cards[card.name] = card

    def remove(self, name):
        self.cards.pop(name, None)


_ALL_MULTIMODAL_NAMES = {
    "image_ocr", "visual_question_answering",
    "audio_transcription", "audio_question_answering", "audio_metadata",
    "video_understanding", "generate_image", "generate_video",
    "check_video_status", "generate_visual",
}


def _enable_multimodal_models(adapter):
    adapter._vision_model_config = VisionModelConfig(
        api_key="test-key", base_url="https://vision.example/v1", model="vision-model",
    )
    adapter._audio_model_config = AudioModelConfig(
        api_key="test-key", base_url="https://audio.example/v1",
    )
    adapter._video_model_config = True
    adapter._image_gen_model_config = True
    adapter._video_gen_model_config = True
    adapter._visual_gen_model_config = True


@pytest.mark.parametrize("configured", [[], sorted(_ALL_MULTIMODAL_NAMES)])
@pytest.mark.parametrize("retrieval_enabled", [False, True])
def test_code_spec_loads_multimodal_capabilities_without_tool_allowlist(monkeypatch, configured, retrieval_enabled):
    adapter = JiuwenSwarmCodeAdapter()
    _enable_multimodal_models(adapter)
    config = {
        "modes": {"code": {"tools": configured}},
        "progressive_tool_enabled": retrieval_enabled,
        "progressive_tool_optimize": retrieval_enabled,
    }
    adapter._config_base_cache = config
    context = CodeBuildContext(
        adapter=adapter,
        config_base=config,
        tool_owner_id="code-test-owner",
    )
    registered = []
    monkeypatch.setattr(interface_deep, "register_tool", lambda tool, owner: registered.append((tool, owner)))
    monkeypatch.setattr(interface_code, "register_tool", lambda tool, owner: registered.append((tool, owner)))

    cards = code_spec._build_code_tool_bundle(
        {"configured_tools": configured, "include_multimodal": True}, context,
    )

    expected_names = _ALL_MULTIMODAL_NAMES | ({"tool_search"} if retrieval_enabled else set())
    assert {card.name for card in cards} == expected_names
    assert len(cards) == len(expected_names)
    assert registered == [(tool, "code-test-owner") for tool in context.artifacts.tools]
    # Resolving a candidate Spec must leave the live adapter records untouched.
    assert adapter._get_multimodal_tools() == []


@pytest.mark.parametrize("adapter_type", [interface_deep.JiuWenSwarmDeepAdapter, JiuwenSwarmCodeAdapter])
def test_work_and_code_reload_expose_same_multimodal_capabilities(monkeypatch, adapter_type):
    adapter = adapter_type()
    _enable_multimodal_models(adapter)
    adapter._config_base_cache = {"modes": {"code": {"tools": []}}}
    adapter._tool_cards = []
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager(), deep_config=SimpleNamespace(tools=[]))
    monkeypatch.setattr(interface_deep, "register_tool", lambda _tool, _owner: None)
    monkeypatch.setattr(interface_deep, "unregister_tool", lambda _tool: None)

    adapter._sync_multimodal_tools_for_runtime()

    assert set(adapter._instance.ability_manager.cards) == _ALL_MULTIMODAL_NAMES

    adapter._vision_model_config = None
    adapter._audio_model_config = None
    adapter._video_model_config = False
    adapter._image_gen_model_config = False
    adapter._video_gen_model_config = False
    adapter._visual_gen_model_config = False
    adapter._sync_multimodal_tools_for_runtime()

    assert adapter._instance.ability_manager.cards == {}
    assert adapter._tool_cards == []
    assert adapter._get_multimodal_tools() == []


@pytest.mark.parametrize("adapter_type", [interface_deep.JiuWenSwarmDeepAdapter, JiuwenSwarmCodeAdapter])
@pytest.mark.parametrize("capability", ["vision", "audio"])
def test_multimodal_reload_build_failure_preserves_live_tools(monkeypatch, adapter_type, capability):
    adapter = adapter_type()
    adapter.mark_as_session_scoped(f"build-failure-{adapter_type.__name__}-{capability}")
    adapter._vision_model_config = VisionModelConfig(
        api_key="test-key", base_url="https://vision.example/v1", model="vision-model",
    )
    adapter._audio_model_config = AudioModelConfig(
        api_key="test-key", base_url="https://audio.example/v1",
    )
    agent = DeepAgent(AgentCard(name="multimodal-reload"))
    agent.configure(DeepAgentConfig(
        tools=[], auto_create_workspace=False, tool_owner_id=adapter._tool_owner_id(),
    ))
    adapter._instance = agent
    adapter._tool_cards = []
    adapter._code_build_context = CodeBuildContext(adapter=adapter, config_base={})

    try:
        adapter._sync_multimodal_tools_for_runtime()
        live_tools = list(getattr(adapter, f"_{capability}_tools"))
        other_capability = "audio" if capability == "vision" else "vision"
        removed_tools = list(getattr(adapter, f"_{other_capability}_tools"))
        factory = MagicMock(side_effect=RuntimeError("metadata provider build failed"))
        monkeypatch.setattr(interface_deep, f"create_{capability}_tools", factory)
        setattr(adapter, f"_{other_capability}_model_config", None)

        adapter._sync_multimodal_tools_for_runtime()

        assert getattr(adapter, f"_{capability}_tools") == live_tools
        assert {card.name for card in adapter._tool_cards} == {tool.card.name for tool in live_tools}
        for tool in live_tools:
            assert agent.ability_manager.get(tool.card.name) is tool.card
            assert Runner.resource_mgr.get_tool(tool.card.id) is tool
        for tool in removed_tools:
            assert agent.ability_manager.get(tool.card.name) is None
            assert Runner.resource_mgr.get_tool(tool.card.id) is None
        if isinstance(adapter, JiuwenSwarmCodeAdapter):
            assert adapter._code_build_context.artifacts.tools == live_tools
            assert agent.deep_config.tools == [tool.card for tool in live_tools]

        # An explicit disable still removes the group even while its factory fails.
        setattr(adapter, f"_{capability}_model_config", None)
        adapter._sync_multimodal_tools_for_runtime()
        assert adapter._get_multimodal_tools() == []
        for tool in live_tools:
            assert agent.ability_manager.get(tool.card.name) is None
            assert Runner.resource_mgr.get_tool(tool.card.id) is None
    finally:
        agent.ability_manager.teardown_tools()


def test_code_adapter_builds_configured_multimodal_tools(monkeypatch):
    video_tool = SimpleNamespace(
        card=SimpleNamespace(
            name="video_understanding",
            id="video_understanding",
            stateless=False,
        )
    )
    monkeypatch.setattr(interface_deep, "video_understanding", video_tool)

    adapter = JiuwenSwarmCodeAdapter()
    adapter._vision_model_config = VisionModelConfig(
        api_key="test-key",
        base_url="https://vision.example/v1",
        model="vision-model",
    )
    adapter._audio_model_config = AudioModelConfig(
        api_key="test-key",
        base_url="https://audio.example/v1",
    )
    adapter._video_model_config = True

    tools = [
        adapter._get_tool_build_func(name, "code-agent")
        for name in (
            "visual_question_answering",
            "image_ocr",
            "video_understanding",
            "audio_transcription",
        )
    ]

    assert [tool.card.name for tool in tools] == [
        "visual_question_answering",
        "image_ocr",
        "video_understanding",
        "audio_transcription",
    ]
    assert video_tool.card.stateless is True


def test_code_adapter_skips_multimodal_tools_without_model_config():
    adapter = JiuwenSwarmCodeAdapter()
    adapter._vision_model_config = None
    adapter._audio_model_config = None
    adapter._video_model_config = False

    assert all(
        adapter._get_tool_build_func(name, "code-agent") is None
        for name in _ALL_MULTIMODAL_NAMES
    )


def test_code_tool_cards_preserve_ordinary_tools_and_add_multimodal_once(monkeypatch):
    adapter = JiuwenSwarmCodeAdapter()
    _enable_multimodal_models(adapter)
    adapter._config_base_cache = {
        "modes": {"code": {"tools": ["acp_chat", "audio_transcription"]}},
    }
    ordinary_tool = SimpleNamespace(card=SimpleNamespace(name="acp_chat"))
    monkeypatch.setattr(adapter, "_build_acp_chat_tool", lambda _owner: ordinary_tool)
    monkeypatch.setattr(interface_deep, "register_tool", lambda _tool, _owner: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.register_tool",
        lambda _tool, _owner: None,
    )

    with adapter._code_spec_config_scope(adapter._config_base_cache):
        cards = adapter.build_code_tool_cards("code-test-owner")

    assert {card.name for card in cards} == {"acp_chat", *_ALL_MULTIMODAL_NAMES}
    assert len(cards) == len(_ALL_MULTIMODAL_NAMES) + 1


def test_custom_code_bundle_keeps_explicit_tool_selection(monkeypatch):
    adapter = JiuwenSwarmCodeAdapter()
    _enable_multimodal_models(adapter)
    adapter._custom_code_spec_active = True
    context = CodeBuildContext(adapter=adapter, config_base={})
    monkeypatch.setattr(code_spec, "register_tool", lambda _tool, _owner: None)
    unexpected_registration = MagicMock()
    monkeypatch.setattr(interface_deep, "register_tool", unexpected_registration)

    assert code_spec._build_code_tool_bundle({}, context) == []
    cards = code_spec._build_code_tool_bundle(
        {"configured_tools": ["audio_transcription"]}, context,
    )
    adapter._sync_multimodal_tools_for_runtime()

    assert [card.name for card in cards] == ["audio_transcription"]
    assert [tool.card.name for tool in context.artifacts.tools] == ["audio_transcription"]
    unexpected_registration.assert_not_called()


def test_code_spec_rolls_back_failed_capability_without_dropping_other_groups(monkeypatch):
    adapter = JiuwenSwarmCodeAdapter()
    _enable_multimodal_models(adapter)
    context = CodeBuildContext(adapter=adapter, config_base={})
    registered = {}

    def register(tool, _owner_id):
        registered[tool.card.name] = tool
        if tool.card.name == "visual_question_answering":
            raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(interface_deep, "register_tool", register)
    monkeypatch.setattr(interface_deep, "unregister_tool", lambda tool: registered.pop(tool.card.name))

    cards = code_spec._build_code_tool_bundle({"include_multimodal": True}, context)

    expected = _ALL_MULTIMODAL_NAMES - {"image_ocr", "visual_question_answering"}
    assert set(registered) == expected
    assert {card.name for card in cards} == expected
    assert {tool.card.name for tool in context.artifacts.tools} == expected


def test_code_cold_build_skips_failed_group_and_keeps_other_capabilities(monkeypatch):
    adapter = JiuwenSwarmCodeAdapter()
    _enable_multimodal_models(adapter)
    context = CodeBuildContext(adapter=adapter, config_base={})
    registered = []
    monkeypatch.setattr(interface_deep, "register_tool", lambda tool, _owner: registered.append(tool))
    monkeypatch.setattr(interface_deep, "create_audio_tools", MagicMock(side_effect=RuntimeError("audio build failed")))

    cards = code_spec._build_code_tool_bundle({"include_multimodal": True}, context)

    expected = _ALL_MULTIMODAL_NAMES - {"audio_transcription", "audio_question_answering", "audio_metadata"}
    assert {card.name for card in cards} == expected
    assert context.artifacts.tools == registered


@pytest.mark.asyncio
async def test_code_spec_audio_and_video_tools_invoke_shared_implementations(monkeypatch, tmp_path):
    adapter = JiuwenSwarmCodeAdapter()
    _enable_multimodal_models(adapter)
    adapter._audio_model_config.transcription_model = "whisper-1"
    context = CodeBuildContext(adapter=adapter, config_base={})
    monkeypatch.setattr(interface_deep, "register_tool", lambda _tool, _owner: None)
    code_spec._build_code_tool_bundle({"include_multimodal": True}, context)
    tools = {tool.card.name: tool for tool in context.artifacts.tools}

    audio_path = tmp_path / "speech.wav"
    audio_path.write_bytes(b"test audio")
    transcription = MagicMock(return_value=SimpleNamespace(text="test transcript"))
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=transcription)))
    build_client = MagicMock(return_value=client)
    monkeypatch.setattr(audio_tools, "_build_openai_client", build_client)

    result = await tools["audio_transcription"].invoke({"audio_path_or_url": str(audio_path)})

    assert result.success is True
    assert result.data["text"] == "test transcript"
    build_client.assert_called_once_with(adapter._audio_model_config)
    assert transcription.call_args.kwargs["model"] == "whisper-1"

    monkeypatch.setattr(video_tools, "get_config", lambda: {})
    monkeypatch.setattr(video_tools, "apply_video_model_config_from_yaml", lambda _config: None)
    monkeypatch.setenv("VIDEO_API_KEY", "test-key")
    monkeypatch.setenv("VIDEO_API_BASE", "https://video.example/v1")
    monkeypatch.setenv("VIDEO_MODEL_NAME", "glm-4.6v")
    response = SimpleNamespace(
        ok=True, json=lambda: {"choices": [{"message": {"content": "test video answer"}}]},
    )
    post = MagicMock(return_value=response)
    monkeypatch.setattr(video_tools, "_http_post", post)

    result = await tools["video_understanding"].invoke({
        "inputs": {"query": "Describe this video", "video_path": "https://media.example/video.mp4"},
    })

    assert result == "test video answer"
    assert post.call_args.args == ("https://video.example/v1/chat/completions",)
    assert post.call_args.kwargs["json"]["model"] == "glm-4.6v"


def test_code_vision_reload_rolls_back_partial_registration(monkeypatch):
    adapter = JiuwenSwarmCodeAdapter()
    adapter._config_base_cache = {
        "modes": {
            "code": {
                "tools": ["visual_question_answering", "image_ocr"],
            }
        }
    }
    adapter._vision_model_config = VisionModelConfig(
        api_key="test-key",
        base_url="https://vision.example/v1",
        model="vision-model",
    )
    adapter._tool_cards = []
    ability_manager = _AbilityManager()
    adapter._instance = SimpleNamespace(ability_manager=ability_manager)
    registered = []
    removed = []

    def register(tool, _owner_id):
        if tool.card.name == "visual_question_answering":
            raise RuntimeError("simulated registration failure")
        registered.append(tool.card.name)

    def unregister(tool):
        removed.append(tool.card.name)
        if tool.card.name in registered:
            registered.remove(tool.card.name)

    monkeypatch.setattr(interface_deep, "register_tool", register)
    monkeypatch.setattr(interface_deep, "unregister_tool", unregister)

    adapter._sync_multimodal_tools_for_runtime()

    assert registered == []
    assert removed == ["image_ocr", "visual_question_answering"]
    assert adapter._tool_cards == []
    assert ability_manager.cards == {}
    assert adapter._vision_tools == []
    assert adapter._vision_tools_registered is False


def test_code_multimodal_reload_updates_ability_and_spec_context(monkeypatch):
    video_tool = SimpleNamespace(
        card=SimpleNamespace(
            name="video_understanding",
            id="video_understanding",
            stateless=False,
        )
    )
    monkeypatch.setattr(interface_deep, "video_understanding", video_tool)

    adapter = JiuwenSwarmCodeAdapter()
    adapter._config_base_cache = {
        "modes": {
            "code": {
                "tools": [
                    "visual_question_answering",
                    "image_ocr",
                    "video_understanding",
                    "audio_transcription",
                ]
            }
        }
    }
    adapter._vision_model_config = VisionModelConfig(
        api_key="test-key",
        base_url="https://vision.example/v1",
        model="vision-model",
    )
    adapter._audio_model_config = AudioModelConfig(
        api_key="test-key",
        base_url="https://audio.example/v1",
    )
    adapter._video_model_config = True
    adapter._tool_cards = []
    ability_manager = _AbilityManager()
    spec_card = SimpleNamespace(name="web_fetch_webpage")
    adapter._instance = SimpleNamespace(
        ability_manager=ability_manager,
        deep_config=SimpleNamespace(tools=[spec_card]),
    )
    adapter._code_build_context = CodeBuildContext(
        adapter=adapter,
        config_base=adapter._config_base_cache,
    )
    registered = []

    def unregister(tool):
        if not tool.card.stateless:
            registered.remove(tool.card.name)

    monkeypatch.setattr(
        interface_deep,
        "register_tool",
        lambda tool, _owner_id: registered.append(tool.card.name),
    )
    monkeypatch.setattr(interface_deep, "unregister_tool", unregister)

    adapter._sync_multimodal_tools_for_runtime()

    registration_order = [
        "image_ocr",
        "visual_question_answering",
        "audio_transcription",
        "audio_question_answering",
        "audio_metadata",
        "video_understanding",
    ]
    assert registered == registration_order
    assert [card.name for card in adapter._tool_cards] == registration_order
    assert list(ability_manager.cards) == registration_order
    assert [tool.card.name for tool in adapter._code_build_context.artifacts.tools] == registration_order
    assert [card.name for card in adapter._instance.deep_config.tools] == [
        "web_fetch_webpage",
        *registration_order,
    ]

    adapter._vision_model_config = None
    adapter._audio_model_config = None
    adapter._video_model_config = False
    adapter._sync_multimodal_tools_for_runtime()

    assert registered == ["video_understanding"]  # Shared registry entry stays available.
    assert adapter._tool_cards == []
    assert ability_manager.cards == {}
    assert adapter._code_build_context.artifacts.tools == []
    assert adapter._instance.deep_config.tools == [spec_card]


def test_code_full_spec_reload_retires_tools_added_by_multimodal_reload(monkeypatch):
    agent = DeepAgent(AgentCard(name="code"))
    # The live Spec was built before vision was configured.
    agent.configure(DeepAgentConfig(tools=[], auto_create_workspace=False))
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = agent
    adapter._config_base_cache = {
        "modes": {"code": {"tools": ["visual_question_answering", "image_ocr"]}}
    }
    adapter._vision_model_config = VisionModelConfig(
        api_key="test-key",
        base_url="https://vision.example/v1",
        model="vision-model",
    )
    adapter._tool_cards = []
    monkeypatch.setattr(interface_deep, "register_tool", lambda _tool, _owner_id: None)
    monkeypatch.setattr(interface_deep, "unregister_tool", lambda _tool: None)

    adapter._sync_multimodal_tools_for_runtime()
    assert agent.ability_manager.get("visual_question_answering") is not None

    # A later full Spec reload no longer resolves the vision tools.
    agent.configure(DeepAgentConfig(tools=[], auto_create_workspace=False))

    assert agent.ability_manager.get("visual_question_answering") is None
    assert agent.ability_manager.get("image_ocr") is None
