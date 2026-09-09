import argparse
import asyncio
import base64
import logging
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from google import genai
from google.genai import types
from openai import OpenAI
from openjiuwen.core.foundation.tool import McpServerConfig, tool
from openjiuwen.core.runner import Runner
import requests

from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_image_gen_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    _get_model_config,
)
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify


logger = logging.getLogger(__name__)
load_dotenv(verbose=True, override=True)

_SANDBOX_MARKER = "home/user"

mcp = FastMCP("vision-mcp-server")


class _PathHelper:
    @staticmethod
    def is_sandbox(p: str) -> bool:
        return _SANDBOX_MARKER in p

    @staticmethod
    def to_https(u: str) -> str:
        if u.startswith("http://"):
            return u.replace("http://", "https://", 1)
        if not u.startswith("https://"):
            return "https://" + u
        return u


class _MimeResolver:
    _EXT_MAP = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }

    @classmethod
    def from_path(cls, path: str) -> str:
        _, ext = os.path.splitext(path)
        return cls._EXT_MAP.get(ext.lower(), "image/jpeg")


class _RetryExecutor:
    @staticmethod
    async def with_backoff(
        coro_factory,
        max_tries: int,
        base_delay: int = 4,
        on_failure=None,
    ) -> Any:
        last_err = None
        for i in range(1, max_tries + 1):
            try:
                return await coro_factory()
            except Exception as e:
                last_err = e
                if i == max_tries:
                    if on_failure:
                        return on_failure(max_tries, e)
                    raise
                await asyncio.sleep(base_delay ** i)
        if on_failure and last_err:
            return on_failure(max_tries, last_err)
        raise RuntimeError("Retry exhausted")


def _get_vision_api_credentials():
    k = os.environ.get("VISION_API_KEY") or os.environ.get("API_KEY", "")
    b = os.environ.get("VISION_API_BASE") or os.environ.get("API_BASE", "")
    m = os.environ.get("VISION_MODEL_NAME") or "gpt-4o"
    return k, b, m


def _get_image_gen_api_credentials():
    """Get image generation API credentials from environment variables.

    Default provider: DashScope
    Default api_base: https://dashscope.aliyuncs.com/api/v1
    Default model: wanx-v1
    """
    k = os.environ.get("IMAGE_GEN_API_KEY") or os.environ.get("API_KEY", "")
    b = (
        os.environ.get("IMAGE_GEN_API_BASE")
        or os.environ.get("API_BASE", "")
        or "https://dashscope.aliyuncs.com/api/v1"
    )
    m = os.environ.get("IMAGE_GEN_MODEL_NAME") or "wanx-v1"
    p = os.environ.get("IMAGE_GEN_PROVIDER") or "DashScope"
    return k, b, m, p


def _make_sandbox_error_msg() -> str:
    return (
        "The visual_question_answering tool cannot access to sandbox file, "
        "please use the local path provided by original instruction"
    )


def _make_missing_key_error() -> str:
    return (
        "[ERROR]: VISION_API_KEY or API_KEY is not configured "
        "for vision question answering."
    )


async def _invoke_openai_vision(src: str, q: str) -> str:
    api_key, api_base, model = _get_vision_api_credentials()
    if not api_key:
        return _make_missing_key_error()

    try:
        if os.path.exists(src):
            with open(src, "rb") as img_f:
                img_bytes = img_f.read()
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            mime = _MimeResolver.from_path(src)
            img_block = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        elif _PathHelper.is_sandbox(src):
            return _make_sandbox_error_msg()
        else:
            img_block = {"type": "image_url", "image_url": {"url": src}}

        msgs = [{"role": "user", "content": [{"type": "text", "text": q}, img_block]}]

        async def _call():
            cli = OpenAI(api_key=api_key, base_url=api_base)
            r = cli.chat.completions.create(model=model, messages=msgs)
            content = r.choices[0].message.content
            if not content or not content.strip():
                raise Exception("Response text is empty or None")
            return content

        def _on_err(tries, exc):
            return f"Visual Question Answering (Client) failed after {tries} retries: {exc}\n"

        return await _RetryExecutor.with_backoff(_call, max_tries=3, on_failure=_on_err)

    except Exception as ex:
        return f"[ERROR]: OpenAI Error: {ex}"


async def _invoke_gemini_vision(src: str, q: str) -> str:
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return "[ERROR]: GEMINI_API_KEY is not configured for Gemini vision."

    try:
        mime = _MimeResolver.from_path(src)
        if os.path.exists(src):
            with open(src, "rb") as f:
                data = f.read()
            part = types.Part.from_bytes(data=data, mime_type=mime)
        elif _PathHelper.is_sandbox(src):
            return _make_sandbox_error_msg()
        else:
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
            data = None
            for attempt in range(4):
                try:
                    r = requests.get(src, headers={"User-Agent": ua}, verify=get_requests_verify())
                    r.raise_for_status()
                    data = r.content
                    break
                except Exception as err:
                    if attempt == 3:
                        raise err
                    delays = [5, 15, 60]
                    await asyncio.sleep(delays[attempt])
            part = types.Part.from_bytes(data=data, mime_type=mime)
    except Exception as e:
        return (
            f"[ERROR]: Failed to get image data {src}: {e}.\n"
            "Note: The visual_question_answering tool cannot access to sandbox file, "
            "please use the local path provided by original instruction or http url. "
            "If you are using http url, make sure it is an image file url."
        )

    retries = 0
    max_r = 3
    while retries <= max_r:
        try:
            cli = genai.Client(api_key=gemini_key)
            resp = cli.models.generate_content(
                model="gemini-2.5-pro",
                contents=[part, types.Part(text=q)],
            )
            if not resp.text or not resp.text.strip():
                raise Exception("Response text is None or empty")
            return resp.text
        except Exception as e:
            err_str = str(e)
            retry_codes = ["503", "429", "500", "Response text is None or empty"]
            if any(c in err_str for c in retry_codes):
                retries += 1
                if retries > max_r:
                    return f"[ERROR]: Gemini Error after {retries} retries: {e}"
                if retries == 1:
                    wt = random.randint(60, 300)
                elif retries == 2:
                    wt = random.randint(60, 180)
                else:
                    wt = 60
                await asyncio.sleep(wt)
            else:
                return f"[ERROR]: Gemini Error: {e}"


_OCR_INSTRUCTIONS = (
    "You are an expert OCR engine. Examine the provided image thoroughly and "
    "transcribe every piece of visible text with high fidelity.\n\n"
    "GUIDELINES:\n"
    "- Perform a full sweep of the image — check every region including margins, "
    "corners, and overlapping areas.\n"
    "- Capture everything: titles, subtitles, annotations, footnotes, stamps, "
    "logos with text, watermarks, and any other textual elements.\n"
    "- Keep the original layout: respect paragraph breaks, indentation, and "
    "visual hierarchy.\n"
    "- Do not skip digits, punctuation marks, or special symbols.\n"
    "- After the first pass, re-examine the image to catch anything overlooked.\n"
    "- For illegible or partially hidden text, provide your best interpretation "
    "rather than omitting it. Note the uncertainty when applicable.\n\n"
    "The output will be consumed by a downstream system that has no visual "
    "access to this image. Therefore, err on the side of inclusion — report "
    "even tentative readings so that no information is silently dropped.\n\n"
    "Output the transcribed text only, preserving the original structure. "
    "Reply 'No text found' when the image contains no text whatsoever. "
    "For regions that might contain text but cannot be reliably read, "
    "include a brief description of what you observe."
)


def _build_vqa_prompt(ocr_result: str, question: str) -> str:
    return (
        f"You are a detail-oriented visual analyst. Study the image carefully "
        f"and compose a well-reasoned answer to the user's question.\n\n"
        f"ANALYSIS GUIDELINES:\n"
        f"- Inspect the image repeatedly to notice subtle details — objects, "
        f"spatial layout, colors, text, and any faint or partially visible elements.\n"
        f"- Cross-validate your visual observations against the OCR transcript "
        f"provided below to ensure factual consistency.\n"
        f"- Reason through the question incrementally before giving a final answer; "
        f"this is especially important for questions involving multiple objects.\n"
        f"- Consider alternative interpretations of ambiguous regions before "
        f"committing to a single conclusion.\n"
        f"- Revisit specific areas of the image to confirm or revise your "
        f"initial impressions.\n"
        f"- Favor concrete, specific descriptions over vague generalizations.\n"
        f"- When you encounter blurry, occluded, or uncertain content, describe "
        f"what you observe in words instead of skipping it. It is better to "
        f"include a tentative observation than to omit potentially relevant information.\n\n"
        f"CONTEXT — OCR transcript (may be partial or contain errors):\n"
        f"{ocr_result}\n\n"
        f"QUESTION:\n"
        f"{question}\n\n"
        f"Deliver a thorough response grounded in careful observation. "
        f"Highlight any elements you are uncertain about.\n"
        f"If the subject is an animal, apply the following naming conventions:\n\n"
        f"ANIMAL NAMING RULES:\n"
        f"- Use only the simplest common name. Omit species or regional qualifiers "
        f"unless the user specifically asks for them. For example, say 'puffin' "
        f"instead of 'Atlantic puffin'.\n"
        f"- When multiple species are plausible, prefer the broader category.\n"
        f"- If you cannot determine the exact species, give the generic name and "
        f"only mention uncertainty when species-level identification is requested.\n"
    )


@tool(
    name="visual_question_answering",
    description=(
        "Analyze and understand image content. Use this tool when the user provides "
        "an image file path (e.g., .jpg, .png, .gif) or image URL and asks questions "
        "about the image content, such as describing objects, scenes, text (OCR), "
        "or people in the image."
    ),
)
async def visual_question_answering(image_path_or_url: str, question: str) -> str:
    from jiuwenswarm.common.config import get_config
    try:
        apply_vision_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply vision model config from yaml", exc_info=True)

    vision_api_key, vision_api_base, vision_model = _get_vision_api_credentials()
    logger.info("[visual_question_answering] using model: %s (api_base: %s)", vision_model, vision_api_base)

    ocr_out = await _invoke_openai_vision(image_path_or_url, _OCR_INSTRUCTIONS)
    vqa_out = await _invoke_openai_vision(image_path_or_url, _build_vqa_prompt(ocr_out, question))
    logger.info("Visual Question Answering tool called via OpenRouter (Gemini model)")
    logger.info(f"OCR results: {ocr_out}")
    logger.info(f"VQA results: {vqa_out}")
    return f"OCR results:\n{ocr_out}\n\nVQA result:\n{vqa_out}"


async def _invoke_model_image_generation(prompt: str, size: str = "1024x1024", quality: str = "standard") -> dict:
    """
    Generate image using internal Model class (DashScope, etc.).

    Args:
        prompt: The text description for image generation
        size: Image size, e.g., "256x256", "512x512", "1024x1024"
        quality: Image quality, "standard" or "hd"

    Returns:
        dict with 'image_path' or 'error' key
    """
    from openjiuwen.core.foundation.llm import ModelClientConfig, Model, UserMessage, ModelRequestConfig

    # 与主链路（apply_image_gen_model_config_from_yaml）同源：直接读 config.yaml
    # 的 models.image_gen.model_client_config，避免与主链路配置脱节。
    from jiuwenswarm.common.config import get_config

    mc = _get_model_config(get_config() or {}, "image_gen")
    api_key = str(mc.get("api_key") or os.getenv("IMAGE_GEN_API_KEY") or os.getenv("API_KEY") or "").strip()
    api_base = str(
        mc.get("api_base")
        or os.getenv("IMAGE_GEN_API_BASE")
        or os.getenv("API_BASE")
        or "https://dashscope.aliyuncs.com/api/v1"
    ).strip()
    if not api_key:
        return {"error": "[ERROR]: IMAGE_GEN_API_KEY or API_KEY is not configured for image generation."}

    model = str(mc.get("model_name") or mc.get("model") or os.getenv("IMAGE_GEN_MODEL_NAME") or "wanx-v1").strip()
    provider = str(mc.get("client_provider") or mc.get("model_provider")
                   or os.getenv("IMAGE_GEN_PROVIDER") or "DashScope").strip()
    # 新声明下 DashScope 不再是独立 client_provider，而是 OpenAI + endpoint_profile=dashscope。
    # 兼容旧 IMAGE_GEN_PROVIDER=DashScope：归一为 OpenAI 并补 dashscope profile。
    # 缺少 endpoint_profile=dashscope 时 OpenAIModelClient 会拒绝生图(方案 8.6)。
    endpoint_profile = str(mc.get("endpoint_profile") or "").strip().lower()
    if provider in ("DashScope", "dashscope"):
        provider = "OpenAI"
        endpoint_profile = endpoint_profile or "dashscope"

    try:
        _mcc_kwargs: dict[str, Any] = dict(
            client_id="image_gen_client",
            client_provider=provider,
            api_key=api_key,
            api_base=api_base,
            verify_ssl=mc.get("verify_ssl", True),
            ssl_cert=mc.get("ssl_cert"),
            timeout=mc.get("timeout", 1800),
        )
        if endpoint_profile:
            _mcc_kwargs["endpoint_profile"] = endpoint_profile
        model_client_config = ModelClientConfig(**_mcc_kwargs)

        model_config = ModelRequestConfig(
            model=model,
        )

        model_instance = Model(
            model_config=model_config,
            model_client_config=model_client_config
        )

        messages = [UserMessage(content=prompt)]

        async def _call():
            return await model_instance.generate_image(messages=messages, model=model)

        result = await _RetryExecutor.with_backoff(_call, max_tries=3)

        output_dir = get_agent_workspace_dir()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        random_suffix = random.randint(1000, 9999)
        output_path = output_dir / f"generated_{timestamp}_{random_suffix}.png"

        # Handle ImageGenerationResponse object
        # result is ImageGenerationResponse with images (URLs) or images_base64
        image_url = None
        image_base64 = None

        # Handle ImageGenerationResponse object
        if hasattr(result, 'images') and result.images and len(result.images) > 0:
            image_url = result.images[0]
        elif hasattr(result, 'images_base64') and result.images_base64 and len(result.images_base64) > 0:
            image_base64 = result.images_base64[0]

        if image_base64:
            # Save base64 image to file
            img_bytes = base64.b64decode(image_base64)
            with open(output_path, "wb") as f:
                f.write(img_bytes)

            return {
                "image_path": str(output_path.absolute()),
                "revised_prompt": prompt,
            }
        elif image_url:
            # Download image from URL and save locally
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
            response = requests.get(image_url, headers={"User-Agent": ua})
            response.raise_for_status()

            with open(output_path, "wb") as f:
                f.write(response.content)

            return {
                "image_path": str(output_path.absolute()),
                "revised_prompt": prompt,
                "original_url": image_url,
            }

        return {"error": "[ERROR]: No valid image data in response"}

    except Exception as ex:
        return {"error": f"[ERROR]: Image generation failed: {ex}"}


@tool(
    name="generate_image",
    description=(
        "Generate an image from a text description using AI image generation models. "
        "Use this tool when the user wants to create an image based on a text prompt. "
        "Returns the path to the saved generated image file."
    ),
)
async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    quality: str = "standard",
    save_dir: str | None = None,
) -> str:
    """
    Generate an image from text description.

    Args:
        prompt: Text description of the image to generate
        size: Image size, options: "256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"
        quality: Image quality, "standard" or "hd"
        save_dir: Optional directory to save the image (defaults to "generated_images")

    Returns:
        Path to the generated image file or error message
    """
    from jiuwenswarm.common.config import get_config
    try:
        apply_image_gen_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply image_gen model config from yaml", exc_info=True)

    _, _, model, provider = _get_image_gen_api_credentials()
    logger.info("[generate_image] using model: %s, provider: %s, size: %s, quality: %s", model, provider, size, quality)

    result = await _invoke_model_image_generation(prompt, size=size, quality=quality)

    if "error" in result:
        return result["error"]

    image_path = result["image_path"]

    # Move to custom save directory if specified
    if save_dir:
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)
        new_path = save_path / Path(image_path).name
        Path(image_path).rename(new_path)
        image_path = str(new_path.absolute())

    revised_prompt = result.get("revised_prompt", prompt)
    original_url = result.get("original_url", "")

    response_parts = [
        f"Image generated successfully!",
        f"Saved to: {image_path}",
        f"Prompt: {prompt}",
    ]
    if revised_prompt != prompt:
        response_parts.append(f"Revised prompt: {revised_prompt}")
    if original_url:
        response_parts.append(f"Original URL: {original_url}")

    try:
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
            deliver_file_to_user,
        )

        delivery = await deliver_file_to_user(image_path)
        if delivery:
            response_parts.append(f"Delivered to user: {delivery}")
    except Exception as deliver_err:
        logger.warning(
            "[generate_image] auto chat.file delivery failed: %s", deliver_err
        )
        response_parts.append(
            "Note: image was saved but automatic delivery failed; "
            "use send_file_to_user with the saved path if needed."
        )

    return "\n".join(response_parts)

