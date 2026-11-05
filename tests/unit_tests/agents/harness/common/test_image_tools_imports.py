"""The original image tool stays shared while Flash defers SDK initialization."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]


def run_fresh(tmp_path, code):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text("{}\n", encoding="utf-8")
    (config / ".env").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        PYTHONIOENCODING="utf-8",
        JIUWENSWARM_DATA_DIR=str(tmp_path),
        JIUWENCLAW_DATA_DIR=str(tmp_path),
        JIUWENSWARM_CONFIG_DIR=str(config),
        JIUWENSWARM_HOME=str(tmp_path / "home"),
        JIUWENSWARM_LOG_CONSOLE_ENABLED="false",
        JIUWENSWARM_LOG_FILE_ENABLED="false",
        OTEL_SDK_DISABLED="true",
    )
    script = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(code)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_flash_startup_keeps_image_sdks_unloaded(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.server import app_agentserver
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
        from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image
        assert generate_image.card.name == "generate_image"
        assert generate_image.card.input_params
        heavy = [name for name in sys.modules
                 if name == "fastmcp" or name.startswith("fastmcp.")
                 or name == "google.genai" or name.startswith("google.genai.")]
        assert not heavy, f"Text startup loaded image SDKs: {heavy}"
    """)


def test_unconfigured_gemini_keeps_original_error(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        from jiuwenswarm.agents.harness.common.tools import image_tools
        image_tools.get_local_config = lambda name, default="": default
        answer = asyncio.run(image_tools._invoke_gemini_vision("unused.png", "describe"))
        assert "GEMINI_API_KEY is not configured" in answer
        assert "google.genai" not in sys.modules
        assert "fastmcp" not in sys.modules
    """)


def test_gemini_exports_preserve_vision_request(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        from pathlib import Path
        from types import SimpleNamespace
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert "google.genai" not in sys.modules
        from jiuwenswarm.agents.harness.common.tools.image_tools import genai, types
        from google import genai as original
        assert genai is original
        assert types is original.types
        image = Path("fixture.png")
        image.write_bytes(b"image-fixture")
        image_tools.get_local_config = lambda name, default="": "test-key" if name == "GEMINI_API_KEY" else default
        def generate_content(*, model, contents):
            assert model == "gemini-2.5-pro"
            assert contents[0].inline_data.data == b"image-fixture"
            assert contents[0].inline_data.mime_type == "image/png"
            assert contents[1].text == "describe"
            return SimpleNamespace(text="fixture description")
        def client(*, api_key):
            assert api_key == "test-key"
            return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        genai.Client = client
        assert asyncio.run(image_tools._invoke_gemini_vision(str(image), "describe")) == "fixture description"
        assert "fastmcp" in sys.modules
    """)


def test_explicit_mcp_access_initializes_once_and_is_shared(tmp_path):
    run_fresh(tmp_path, """
        from concurrent.futures import ThreadPoolExecutor
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert "fastmcp" not in sys.modules
        with ThreadPoolExecutor(max_workers=4) as pool:
            servers = list(pool.map(lambda _: image_tools.mcp, range(4)))
        from fastmcp import FastMCP
        assert image_tools.FastMCP is FastMCP
        assert all(server is servers[0] for server in servers)
        assert isinstance(servers[0], FastMCP)
        assert "google.genai" in sys.modules
    """)


def test_gemini_call_initializes_vision_sdks_on_demand(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert "fastmcp" not in sys.modules
        assert "google.genai" not in sys.modules
        image_tools.get_local_config = lambda name, default="": "fixture-key" if name == "GEMINI_API_KEY" else default
        answer = asyncio.run(image_tools._invoke_gemini_vision("home/user/offline.png", "describe"))
        assert "cannot access to sandbox file" in answer, answer
        assert "fastmcp" in sys.modules
        assert "google.genai" in sys.modules
    """)


def test_star_import_preserves_historical_public_names(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.agents.harness.common.tools import image_tools
        exported = {}
        exec("from jiuwenswarm.agents.harness.common.tools.image_tools import *", exported)
        expected = {"FastMCP", "mcp", "genai", "types", "generate_image",
                    "visual_question_answering", "normalize_image_size", "OpenAI", "Runner", "threading"}
        assert expected <= exported.keys(), expected - exported.keys()
        assert all(exported[name] is getattr(image_tools, name) for name in expected)
    """)


def test_inspection_advertises_exports_without_initializing(tmp_path):
    run_fresh(tmp_path, """
        from jiuwenswarm.agents.harness.common.tools import image_tools
        assert {"FastMCP", "mcp", "genai", "types"} <= set(dir(image_tools))
        assert not hasattr(image_tools, "unknown_image_attribute")
        assert not {"FastMCP", "mcp", "genai", "types"} & vars(image_tools).keys()
        assert "fastmcp" not in sys.modules
        assert "google.genai" not in sys.modules
    """)


@pytest.mark.parametrize("missing_module", ["fastmcp", "google.genai"])
def test_missing_optional_dependencies_only_leave_flash_startup_available(tmp_path, missing_module):
    run_fresh(tmp_path, f"missing_module = {missing_module!r}\n" + textwrap.dedent("""
        import asyncio
        from importlib.abc import MetaPathFinder
        class MissingImageDependencies(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == missing_module or fullname.startswith(missing_module + "."):
                    raise ModuleNotFoundError("missing optional image dependency", name=fullname)
        sys.meta_path.insert(0, MissingImageDependencies())
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter
        from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image
        flash = JiuwenSwarmFlashAdapter()
        assert generate_image.card.name == "generate_image"
        from jiuwenswarm.agents.harness.common.tools import image_tools
        try:
            image_tools._ensure_image_dependencies()
        except ImportError:
            pass
        else:
            raise AssertionError("Explicit initialization must require image dependencies")
        assert not {"FastMCP", "mcp", "genai", "types"} & vars(image_tools).keys()
        sys.meta_path.pop(0)
        image_tools._ensure_image_dependencies()
        assert image_tools.generate_image is generate_image
        assert "google.genai" in sys.modules
    """))


def test_image_generation_runs_worker_and_saves_result(tmp_path):
    run_fresh(tmp_path, """
        import asyncio
        import base64
        from pathlib import Path
        from openjiuwen.core.foundation import llm
        from jiuwenswarm.agents.harness.common.tools import image_tools
        image_tools.get_local_config = lambda name, default="": "fixture-key" if name == "IMAGE_GEN_API_KEY" else default
        payload = b"fixture-image-bytes"
        class OfflineImageModel:
            def __init__(self, *, model_config, model_client_config):
                assert model_client_config.use_shared_llm_http_client is False
            async def generate_image(self, *, messages, model, **kwargs):
                assert messages[0].content == "test landscape"
                return {"data": [{"b64_json": base64.b64encode(payload).decode()}]}
        llm.Model = OfflineImageModel
        result = asyncio.run(image_tools._invoke_model_image_generation({
            "prompt": "test landscape", "save_dir": str(Path("images").resolve())
        }))
        assert "error" not in result, result
        assert len(result["image_paths"]) == 1
        assert Path(result["image_paths"][0]).read_bytes() == payload
        assert result["revised_prompt"] == "test landscape"
    """)
