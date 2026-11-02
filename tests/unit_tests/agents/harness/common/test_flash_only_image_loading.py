"""Flash image startup must not change the eager contract of other entrypoints."""

import importlib.util
from pathlib import Path

import pytest

_helper_spec = importlib.util.spec_from_file_location(
    "image_import_test_helpers", Path(__file__).with_name("test_image_tools_imports.py")
)
_helpers = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(_helpers)
run_fresh = _helpers.run_fresh


@pytest.mark.parametrize("mode", ["flash", "deep"])
def test_image_implementation_does_not_reapply_dotenv_after_runtime_config(tmp_path, mode):
    run_fresh(tmp_path, f"mode = {mode!r}\n" + """
import os
from pathlib import Path
import dotenv.main
from jiuwenswarm.server import app_agentserver
ancestor = Path("ancestor.env").resolve()
ancestor.write_text("FREE_SEARCH_DDG_ENABLED=true\\nIMAGE_IMPORT_ORDER_TEST=ancestor\\n", encoding="utf-8")
(Path(os.environ["JIUWENSWARM_CONFIG_DIR"]) / ".env").write_text("IMAGE_IMPORT_ORDER_TEST=runtime\\n", encoding="utf-8")
dotenv.main.find_dotenv = lambda *args, **kwargs: str(ancestor)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
adapter = (JiuwenSwarmFlashAdapter if mode == "flash" else JiuWenSwarmDeepAdapter)()
from jiuwenswarm.agents.harness.common.tools import image_tools
image_tools._ensure_image_dependencies()
assert os.environ["IMAGE_IMPORT_ORDER_TEST"] == "runtime"
assert os.environ["FREE_SEARCH_DDG_ENABLED"] == "false"
""")


def test_code_constructor_keeps_image_dependencies_eager(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
        adapter = JiuwenSwarmCodeAdapter()
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
    """)


def test_deep_then_flash_preserves_tool_identity(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        deep = JiuWenSwarmDeepAdapter()
        from jiuwenswarm.agents.harness.common.tools import image_tools
        original_mcp = image_tools.mcp
        from jiuwenswarm.server.runtime.agent_adapter import interface_flash
        flash = interface_flash.JiuwenSwarmFlashAdapter()
        assert interface_flash.generate_image is image_tools.generate_image
        assert image_tools.mcp is original_mcp
    """)


def test_concurrent_flash_and_deep_keep_single_image_tool(tmp_path):
    run_fresh(tmp_path, """
        from concurrent.futures import ThreadPoolExecutor
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
        with ThreadPoolExecutor(max_workers=2) as pool:
            adapters = list(pool.map(lambda cls: cls(), [JiuwenSwarmFlashAdapter, JiuWenSwarmDeepAdapter]))
        from jiuwenswarm.agents.harness.common.tools import image_tools
        from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
        assert image_tools.generate_image is interface_deep.generate_image is interface_flash.generate_image
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
    """)


def test_skill_turbo_keeps_eager_image_entrypoint(tmp_path):
    run_fresh(tmp_path, """
        from types import SimpleNamespace
        from jiuwenswarm.server.runtime.skill_turbo.tools_loader import _load_jw_image_gen
        tools = _load_jw_image_gen(SimpleNamespace(image_gen_enabled=True))
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert tools == [image_tools.generate_image]
    """)


def test_swarm_keeps_eager_image_entrypoint(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.agents.swarm.providers import tools as swarm_tools
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert swarm_tools.generate_image is image_tools.generate_image
    """)


def test_flash_image_registration_after_reload_stays_light(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        from jiuwenswarm.server.runtime.agent_adapter import interface_flash
        adapter = interface_flash.JiuwenSwarmFlashAdapter()
        adapter._image_gen_model_config = True
        cards = asyncio.run(adapter._get_tool_cards("flash-image-test"))
        assert sum(card.name == "generate_image" for card in cards) == 1
        assert adapter._image_gen_tool_registered
        adapter._image_gen_model_config = False
        adapter._sync_multimodal_tools_for_runtime()
        assert not adapter._image_gen_tool_registered
        adapter._image_gen_model_config = True
        adapter._sync_multimodal_tools_for_runtime()
        assert adapter._image_gen_tool_registered
        adapter._sync_multimodal_tools_for_runtime()
        assert adapter._image_gen_tool_registered
        assert "fastmcp" not in sys.modules
        assert "google.genai" not in sys.modules
    """)


def test_flash_image_generation_saves_without_vision_sdks(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        import base64
        import threading
        from importlib.abc import MetaPathFinder
        from pathlib import Path
        from openjiuwen.core.foundation import llm
        from jiuwenswarm.agents.harness.common.tools import image_tools as local_config
        from jiuwenswarm.server.runtime.agent_adapter import interface_flash
        adapter = interface_flash.JiuwenSwarmFlashAdapter()
        assert "fastmcp" not in sys.modules
        class RejectVisionDependencies(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "fastmcp" or fullname.startswith("fastmcp.") or fullname == "google.genai" or fullname.startswith("google.genai."):
                    raise ModuleNotFoundError("Pure image generation must not load vision dependencies", name=fullname)
        sys.meta_path.insert(0, RejectVisionDependencies())
        original_get = local_config.get_local_config
        local_config.get_local_config = lambda name, default="": "fixture-key" if name == "IMAGE_GEN_API_KEY" else original_get(name, default)
        main_thread = threading.get_ident()
        payload = b"offline-image-bytes"
        class OfflineImageModel:
            def __init__(self, *, model_config, model_client_config):
                assert model_client_config.use_shared_llm_http_client is False
            async def generate_image(self, *, messages, model, **kwargs):
                assert threading.get_ident() != main_thread
                assert messages[0].content == "test landscape"
                return {"data": [{"b64_json": base64.b64encode(payload).decode()}]}
        llm.Model = OfflineImageModel
        result = asyncio.run(interface_flash.generate_image.invoke({"inputs": {
            "prompt": "test landscape", "save_dir": str(Path("images").resolve())
        }}))
        assert "Generated 1 image(s) successfully!" in result, result
        saved = list(Path("images").iterdir())
        assert len(saved) == 1
        assert saved[0].read_bytes() == payload
        assert "fastmcp" not in sys.modules
        assert "google.genai" not in sys.modules
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert image_tools.generate_image is interface_flash.generate_image
        async def failing_call(inputs):
            raise ValueError("offline test error")
        image_tools._invoke_model_image_generation = failing_call
        result = asyncio.run(interface_flash.generate_image.invoke({"inputs": {"prompt": "fail"}}))
        assert "offline test error" in result
        async def cancelled_call(inputs):
            raise asyncio.CancelledError()
        image_tools._invoke_model_image_generation = cancelled_call
        try:
            asyncio.run(interface_flash.generate_image.invoke({"inputs": {"prompt": "cancel"}}))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Tool wrapper swallowed cancellation")
    """)


def test_public_image_module_initializes_explicitly(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert "fastmcp" not in sys.modules
        image_tools._ensure_image_dependencies()
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
        assert {"FastMCP", "mcp", "genai", "types"} <= vars(image_tools).keys()
        assert image_tools.mcp.name == "vision-mcp-server"
    """)


def test_deep_constructor_keeps_image_dependencies_eager(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        adapter = JiuWenSwarmDeepAdapter()
        assert "fastmcp" in sys.modules, "Deep must initialize its original image dependencies"
        assert "google.genai" in sys.modules
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert "mcp" in vars(image_tools)
    """)


def test_flash_constructor_keeps_image_dependencies_deferred(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.server import app_agentserver
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
        adapter = JiuwenSwarmFlashAdapter()
        assert "fastmcp" not in sys.modules
        assert "google.genai" not in sys.modules
        from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image
        assert generate_image.card.name == "generate_image"
        assert "jiuwenswarm.agents.harness.common.tools.image_tools" in sys.modules
        assert "jiuwenswarm.agents.harness.common.tools.image_generation_tool" not in sys.modules
    """)


def test_flash_then_deep_initializes_legacy_module_with_same_tool(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        flash = JiuwenSwarmFlashAdapter()
        assert "fastmcp" not in sys.modules
        deep = JiuWenSwarmDeepAdapter()
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
        from jiuwenswarm.agents.harness.common.tools import image_tools
        from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
        assert image_tools.generate_image is interface_deep.generate_image is interface_flash.generate_image
    """)


def test_tool_definition_stays_in_original_file():
    import ast
    original = _helpers.ROOT / "jiuwenswarm/agents/harness/common/tools/image_tools.py"
    assert not original.with_name("image_generation_tool.py").exists()
    tree = ast.parse(original.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate_image"]
    assert len(functions) == 1 and len(functions[0].decorator_list) == 1


def test_actual_adapter_type_controls_loading_independent_of_mode_config(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.common.config import get_config
        config = get_config()
        config.setdefault("flash", {})["enabled"] = False
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
        flash = JiuwenSwarmFlashAdapter()
        assert "fastmcp" not in sys.modules
        config["flash"]["enabled"] = True
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
        deep = JiuWenSwarmDeepAdapter()
        assert "fastmcp" in sys.modules
    """)


def test_disabled_skill_turbo_image_loader_keeps_sdks_unloaded(tmp_path):
    run_fresh(tmp_path, """
        from types import SimpleNamespace
        from jiuwenswarm.server.runtime.skill_turbo.tools_loader import _load_jw_image_gen
        assert _load_jw_image_gen(SimpleNamespace(image_gen_enabled=False)) == []
        assert "fastmcp" not in sys.modules and "google.genai" not in sys.modules
    """)
