from types import SimpleNamespace

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness import (
    AudioModelConfig,
    DeepAgent,
    DeepAgentConfig,
    VisionModelConfig,
)

from jiuwenswarm.agents.harness.code.spec import CodeBuildContext
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


def test_code_adapter_builds_configured_multimodal_tools(monkeypatch):
    video_tool = SimpleNamespace(
        card=SimpleNamespace(
            name="video_understanding",
            id="video_understanding",
            stateless=False,
        )
    )
    monkeypatch.setattr(interface_code, "video_understanding", video_tool)

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
        for name in (
            "visual_question_answering",
            "image_ocr",
            "video_understanding",
            "audio_transcription",
        )
    )


def test_code_multimodal_reload_registers_only_configured_tools(monkeypatch):
    video_tool = SimpleNamespace(
        card=SimpleNamespace(
            name="video_understanding",
            id="video_understanding",
            stateless=False,
        )
    )
    monkeypatch.setattr(interface_code, "video_understanding", video_tool)

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

    synced: dict[str, list[str]] = {}

    def sync_group(**kwargs):
        tools = kwargs["create_fn"]() if kwargs["enabled"] else []
        synced[kwargs["warn_label"]] = [tool.card.name for tool in tools]
        return tools, bool(tools)

    monkeypatch.setattr(adapter, "_sync_tool_group", sync_group)

    adapter._sync_multimodal_tools_for_runtime()

    assert synced == {
        "Code vision tools": ["visual_question_answering", "image_ocr"],
        "Code audio transcription tool": ["audio_transcription"],
        "Code video understanding tool": ["video_understanding"],
    }


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
        if tool.card.name == "image_ocr":
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
    assert removed == ["visual_question_answering", "image_ocr"]
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
    monkeypatch.setattr(interface_code, "video_understanding", video_tool)

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

    configured_order = [
        "visual_question_answering",
        "image_ocr",
        "video_understanding",
        "audio_transcription",
    ]
    registration_order = [
        "visual_question_answering",
        "image_ocr",
        "audio_transcription",
        "video_understanding",
    ]
    assert registered == registration_order
    assert [card.name for card in adapter._tool_cards] == registration_order
    assert list(ability_manager.cards) == registration_order
    assert [tool.card.name for tool in adapter._code_build_context.artifacts.tools] == configured_order
    assert [card.name for card in adapter._instance.deep_config.tools] == [
        "web_fetch_webpage",
        *configured_order,
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
