"""Shared image tool card; only Flash imports it without the eager implementation."""

from typing import Any

from dotenv import load_dotenv
from openjiuwen.core.foundation.tool import tool


# Keep the legacy image environment setup before the adapter's runtime dotenv
# override/reset. Importing the heavy implementation later must not reapply it.
load_dotenv(verbose=True, override=True)


@tool(
    name="generate_image",
    description=(
        "Generate an image from a text description, or edit an input image "
        "(e.g. watermark removal, style transfer, element changes) when the "
        "configured model supports it (Huawei MaaS qwen-image-edit). "
        "Supports provider-specific options: DashScope (prompt_extend, negative_prompt, seed), "
        "Huawei MaaS (seed, b64_json response), OpenAI-compatible (standard sizes). "
        "Returns the paths to the saved generated image file(s)."
    ),
    # NOTE: docstring 的 "Args: inputs: Dict with keys: ..." 嵌套子键无法被
    # schema 解析器提取（LLM 只能看到 "Dict with keys:"），因此显式声明
    # input_params，让 LLM 直接看到每个键的名称、类型与用途。
    input_params={
        "type": "object",
        "properties": {
            "inputs": {
                "type": "object",
                "description": "Image generation / editing parameters",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Required. Text description of the image to generate, "
                            "or the editing instruction when an input image is provided"
                        ),
                    },
                    "image": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": (
                            "Optional input image for editing (e.g. watermark removal): "
                            "a local file path, an http(s) URL, or a data: URL; a list of "
                            "these for multi-image fusion. Huawei MaaS qwen-image-edit "
                            "models only. When editing, size defaults to the input image "
                            "dimensions unless specified"
                        ),
                    },
                    "size": {
                        "type": "string",
                        "description": 'Image size, e.g. "1024x1024", "1792x1024", "1920*1080" (DashScope uses *)',
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["standard", "hd"],
                        "description": "Image quality",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of images to generate (1-4; Huawei MaaS forces 1)",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Negative prompt to exclude elements (DashScope only)",
                    },
                    "prompt_extend": {
                        "type": "boolean",
                        "description": "Whether to extend the prompt automatically (DashScope only, default True)",
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Random seed for reproducibility (DashScope, Huawei MaaS)",
                    },
                    "save_dir": {
                        "type": "string",
                        "description": "Optional directory to save images (defaults to workspace/generated_images)",
                    },
                },
                "required": ["prompt"],
            }
        },
        "required": ["inputs"],
    },
)
async def generate_image(inputs: dict[str, Any], **kwargs) -> str:
    """Load the existing image implementation only when this tool is invoked."""
    from jiuwenswarm.agents.harness.common.tools.image_tools import _generate_image_impl

    return await _generate_image_impl(inputs, **kwargs)
