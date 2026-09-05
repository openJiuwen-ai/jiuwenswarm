import argparse
import asyncio
import base64
import logging
import os
import random
import re
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

from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_image_gen_model_config_from_yaml,
    apply_vision_model_config_from_yaml,
    _get_model_config,
)
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.http_proxy_config import requests_get
from jiuwenswarm.common.local_env_config import get_local_config


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
    k = str(get_local_config("VISION_API_KEY", "") or get_local_config("API_KEY", "") or "")
    b = str(get_local_config("VISION_API_BASE", "") or get_local_config("API_BASE", "") or "")
    m = str(get_local_config("VISION_MODEL_NAME", "gpt-4o") or "gpt-4o")
    return k, b, m


def _get_image_gen_api_credentials():
    """Get image generation API credentials from environment variables.

    Default provider: DashScope
    Default api_base: https://dashscope.aliyuncs.com/api/v1
    Default model: wanx-v1
    """
    k = str(get_local_config("IMAGE_GEN_API_KEY", "") or get_local_config("API_KEY", "") or "")
    b = (
        str(get_local_config("IMAGE_GEN_API_BASE", "") or "")
        or str(get_local_config("API_BASE", "") or "")
        or "https://dashscope.aliyuncs.com/api/v1"
    )
    m = str(get_local_config("IMAGE_GEN_MODEL_NAME", "wanx-v1") or "wanx-v1")
    p = str(get_local_config("IMAGE_GEN_PROVIDER", "DashScope") or "DashScope")
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
    gemini_key = str(get_local_config("GEMINI_API_KEY", "") or "")
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
                    r = requests_get(src, headers={"User-Agent": ua}, verify=get_requests_verify())
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
        "分析并理解图片内容。用户提供图片文件路径（如 .jpg、.png、.gif）"
        "或图片 URL 并询问图片内容时使用，例如描述物体、场景、文字（OCR）或人物。"
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


# ---------------------------------------------------------------------------
# Image generation: provider-aware size normalization
# ---------------------------------------------------------------------------

_DEFAULT_SIZE_DASHSCOPE = "1920*1080"
_DEFAULT_SIZE_OPENAI = "1024x1024"
_DEFAULT_SIZE_HUAWEI_MAAS = "1024x1024"
_SIZE_DIMENSIONS_RE = re.compile(r"^(\d+)\s*[*xX×]\s*(\d+)$")
_OPENAI_STANDARD_SIZES = frozenset({(1024, 1024), (1792, 1024), (1024, 1792)})
_DASHSCOPE_PROVIDER_NAMES = frozenset({"dashscope"})
_HUAWEI_MAAS_API_MARKERS = ("modelarts-maas.com", "modelarts-maas.cn")
_OUTPUT_SUBDIR = "generated_images"


def _is_dashscope_provider(provider: str) -> bool:
    return provider.strip().lower() in _DASHSCOPE_PROVIDER_NAMES


def _is_huawei_maas_api_base(api_base: str) -> bool:
    base = (api_base or "").lower()
    if any(marker in base for marker in _HUAWEI_MAAS_API_MARKERS):
        return True
    return "modelarts" in base and "maas" in base


def _is_huawei_maas_config(provider: str, api_base: str) -> bool:
    """Huawei MaaS image API is OpenAI-path compatible; detect by api_base."""
    _ = provider
    return _is_huawei_maas_api_base(api_base)


def _default_size_for_provider(provider: str, api_base: str = "") -> str:
    if _is_dashscope_provider(provider):
        return _DEFAULT_SIZE_DASHSCOPE
    if _is_huawei_maas_config(provider, api_base):
        return _DEFAULT_SIZE_HUAWEI_MAAS
    return _DEFAULT_SIZE_OPENAI


def _map_to_openai_compatible_size(width: int, height: int) -> str:
    """Map arbitrary dimensions to common OpenAI-compatible size enums."""
    if (width, height) in _OPENAI_STANDARD_SIZES:
        return f"{width}x{height}"
    if width == height:
        return _DEFAULT_SIZE_OPENAI
    if width > height:
        return "1792x1024"
    return "1024x1792"


def normalize_image_size(
    size: str | None,
    provider: str,
    *,
    api_base: str = "",
) -> str:
    """Normalize size for DashScope (*), Huawei MaaS (pass-through x), or OpenAI enums."""
    raw = str(size or "").strip()
    if not raw:
        return _default_size_for_provider(provider, api_base)

    match = _SIZE_DIMENSIONS_RE.match(raw)
    if not match:
        return raw

    width, height = int(match.group(1)), int(match.group(2))
    if _is_dashscope_provider(provider):
        return f"{width}*{height}"
    if _is_huawei_maas_config(provider, api_base):
        return f"{width}x{height}"
    return _map_to_openai_compatible_size(width, height)


# ---------------------------------------------------------------------------
# Image generation: provider-specific gen kwargs
# ---------------------------------------------------------------------------

def _parse_optional_seed(inputs: dict[str, Any]) -> int | None:
    """Parse seed from inputs dict; ignore invalid values with a debug log."""
    if "seed" not in inputs:
        return None
    raw = inputs["seed"]
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.debug("[generate_image] ignoring invalid seed value: %r", raw)
        return None


def _build_image_gen_kwargs(
    provider: str,
    inputs: dict[str, Any],
    *,
    size: str,
    n: int,
    api_base: str = "",
) -> dict[str, Any]:
    gen_kwargs: dict[str, Any] = {
        "size": normalize_image_size(size, provider, api_base=api_base),
        "n": n,
    }
    if _is_huawei_maas_config(provider, api_base):
        # Huawei MaaS: single image, b64_json only; supports seed.
        gen_kwargs["n"] = 1
        gen_kwargs["response_format"] = "b64_json"
        parsed_seed = _parse_optional_seed(inputs)
        if parsed_seed is not None:
            gen_kwargs["seed"] = parsed_seed
        return gen_kwargs

    if not _is_dashscope_provider(provider):
        return gen_kwargs

    gen_kwargs["prompt_extend"] = bool(inputs.get("prompt_extend", True))
    negative_prompt = inputs.get("negative_prompt")
    if negative_prompt is not None and str(negative_prompt).strip():
        gen_kwargs["negative_prompt"] = str(negative_prompt).strip()
    parsed_seed = _parse_optional_seed(inputs)
    if parsed_seed is not None:
        gen_kwargs["seed"] = parsed_seed
    return gen_kwargs


# ---------------------------------------------------------------------------
# Image generation: response normalization & multi-image saving
# ---------------------------------------------------------------------------

def _append_url_or_b64(items: list[dict[str, Any]], url: Any, b64: Any = None) -> None:
    url_s = str(url or "").strip()
    b64_s = str(b64 or "").strip()
    if url_s or b64_s:
        items.append({"url": url_s, "b64_json": b64_s})


def _extend_from_image_list(items: list[dict[str, Any]], block: Any) -> None:
    if not isinstance(block, list):
        return
    for entry in block:
        if isinstance(entry, str):
            text = entry.strip()
            if text.startswith("data:") and "," in text:
                _append_url_or_b64(items, None, text.split(",", 1)[1])
            elif text.startswith("http://") or text.startswith("https://"):
                _append_url_or_b64(items, text)
            elif text:
                _append_url_or_b64(items, None, text)
        elif isinstance(entry, dict):
            url = entry.get("url") or entry.get("image")
            b64 = entry.get("b64_json") or entry.get("b64") or entry.get("image_base64")
            _append_url_or_b64(items, url, b64)
        else:
            url = getattr(entry, "url", None) or getattr(entry, "image", None)
            b64 = (
                getattr(entry, "b64_json", None)
                or getattr(entry, "b64", None)
                or getattr(entry, "image_base64", None)
            )
            _append_url_or_b64(items, url, b64)


def _try_model_dump(response: Any) -> dict[str, Any] | None:
    """Best-effort pydantic model_dump; returns None when unavailable or failing."""
    dump_fn = getattr(response, "model_dump", None)
    if not callable(dump_fn):
        return None
    try:
        dumped = dump_fn()
    except (TypeError, ValueError, AttributeError) as exc:
        logger.debug("[generate_image] model_dump failed: %s", exc)
        return None
    return dumped if isinstance(dumped, dict) else None


def _extend_from_nested_output(items: list[dict[str, Any]], node: Any) -> None:
    if node is None:
        return
    if isinstance(node, list):
        _extend_from_image_list(items, node)
        return
    if isinstance(node, dict):
        if node.get("url") or node.get("b64_json") or node.get("image"):
            items.append(
                {
                    "url": str(node.get("url") or node.get("image") or "").strip(),
                    "b64_json": str(
                        node.get("b64_json") or node.get("b64") or node.get("image_base64") or ""
                    ).strip(),
                }
            )
        for key in ("results", "data", "images", "choices"):
            _extend_from_nested_output(items, node.get(key))
        output = node.get("output")
        if isinstance(output, dict):
            _extend_from_nested_output(items, output.get("results"))
            for choice in output.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or {}
                if isinstance(message, dict):
                    _extend_from_nested_output(items, message.get("content"))
        return
    message = getattr(node, "output", None)
    if message is not None and message is not node:
        _extend_from_nested_output(items, message)


def _iter_response_image_items(response: Any) -> list[dict[str, Any]]:
    """Normalize openjiuwen ImageGenerationResponse and vendor payloads to url/b64 items."""
    items: list[dict[str, Any]] = []

    # openjiuwen ImageGenerationResponse: images / images_base64 are List[str]
    _extend_from_image_list(items, getattr(response, "images", None))
    _extend_from_image_list(items, getattr(response, "images_base64", None))

    data = getattr(response, "data", None)
    if isinstance(data, list):
        _extend_from_image_list(items, data)

    if not items:
        _extend_from_nested_output(items, response)

    if not items:
        dumped = _try_model_dump(response)
        if dumped:
            _extend_from_image_list(items, dumped.get("images"))
            _extend_from_image_list(items, dumped.get("images_base64"))
            _extend_from_image_list(items, dumped.get("data"))
            _extend_from_nested_output(items, dumped)

    return items


def _describe_response_for_error(response: Any) -> str:
    dumped = _try_model_dump(response)
    if dumped is not None:
        return str(dumped)[:500]
    return repr(response)[:500]


def _sanitize_filename_part(value: str, max_len: int = 48) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", value.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("_")
    if not cleaned:
        return "image"
    return cleaned[:max_len]


def _resolve_default_output_dir() -> Path:
    """解析默认输出目录：优先请求级 effective_project_dir，降级到 agent workspace。

    请求级 effective_project_dir 在 interface_deep.py 请求开始时通过
    set_effective_request_workspace_dir() 设置，对应用户的项目目录。
    """
    try:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            get_effective_request_workspace_dir,
        )

        req_ws = get_effective_request_workspace_dir()
        if req_ws:
            return Path(req_ws)
    except ImportError:
        logger.debug(
            "subagent_executor.context_vars unavailable, "
            "falling back to agent workspace dir",
            exc_info=True,
        )
    return get_agent_workspace_dir()


def _save_generated_images(response: Any, *, prompt: str, output_dir: Path | None = None) -> list[Path]:
    items = _iter_response_image_items(response)
    if not items:
        raise ValueError(
            "Image generation API returned no image data. "
            f"Response preview: {_describe_response_for_error(response)}"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = _sanitize_filename_part(prompt)
    saved: list[Path] = []

    if output_dir is None:
        output_dir = _resolve_default_output_dir() / _OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, item in enumerate(items, start=1):
        url = str(item.get("url") or "").strip()
        b64 = str(item.get("b64_json") or item.get("b64") or "").strip()
        suffix = f"{stamp}_{slug}_{idx}"
        dest = output_dir / f"{suffix}.png"

        if url:
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
            response_dl = requests_get(url, headers={"User-Agent": ua}, verify=get_requests_verify())
            response_dl.raise_for_status()
            dest.write_bytes(response_dl.content)
            if dest.stat().st_size == 0:
                raise ValueError(f"Downloaded empty image from URL: {url}")
            saved.append(dest.resolve())
            continue
        if b64:
            payload = b64.strip()
            if payload.startswith("data:") and "," in payload:
                payload = payload.split(",", 1)[1]
            dest.write_bytes(base64.b64decode(payload))
            saved.append(dest.resolve())
            continue

    if not saved:
        raise ValueError("Image generation response did not contain url or base64 payloads.")
    return saved


# ---------------------------------------------------------------------------
# Image generation: core invocation
# ---------------------------------------------------------------------------

async def _invoke_model_image_generation(inputs: dict[str, Any]) -> dict:
    """
    Generate image using internal Model class (DashScope, Huawei MaaS, OpenAI, etc.).

    Args:
        inputs: Dict with keys: prompt (required), size, quality, n,
                negative_prompt, prompt_extend, seed, save_dir

    Returns:
        dict with 'image_paths' list or 'error' key
    """
    from openjiuwen.core.foundation.llm import ModelClientConfig, Model, UserMessage, ModelRequestConfig

    # 与主链路（apply_image_gen_model_config_from_yaml）同源：直接读 config.yaml
    # 的 models.image_gen.model_client_config，避免与主链路配置脱节。
    from jiuwenswarm.common.config import get_config
    mc = _get_model_config(get_config() or {}, "image_gen")
    api_key = str(
        mc.get("api_key")
        or get_local_config("IMAGE_GEN_API_KEY", "")
        or get_local_config("API_KEY", "")
        or ""
    ).strip()
    api_base = str(
        mc.get("api_base")
        or get_local_config("IMAGE_GEN_API_BASE", "")
        or get_local_config("API_BASE", "")
        or "https://dashscope.aliyuncs.com/api/v1"
    ).strip()
    if not api_key:
        return {"error": "[ERROR]: IMAGE_GEN_API_KEY or API_KEY is not configured for image generation."}

    model = str(
        mc.get("model_name")
        or mc.get("model")
        or get_local_config("IMAGE_GEN_MODEL_NAME", "")
        or "wanx-v1"
    ).strip()
    provider = str(
        mc.get("client_provider")
        or mc.get("model_provider")
        or get_local_config("IMAGE_GEN_PROVIDER", "")
        or "DashScope"
    ).strip()

    prompt = str(inputs.get("prompt", "") or "").strip()
    if not prompt:
        return {"error": "[ERROR]: prompt cannot be empty."}

    size_raw = str(inputs.get("size") or "").strip()
    n_raw = inputs.get("n", 1)
    try:
        n = max(1, min(int(n_raw), 4))
    except (TypeError, ValueError):
        n = 1

    save_dir = inputs.get("save_dir")

    try:
        model_client_config = ModelClientConfig(
            client_id="image_gen_client",
            client_provider=provider,
            api_key=api_key,
            api_base=api_base,
            verify_ssl=mc.get("verify_ssl", True),
            ssl_cert=mc.get("ssl_cert"),
            timeout=mc.get("timeout", 1800),
        )

        model_config = ModelRequestConfig(
            model=model,
        )

        model_instance = Model(
            model_config=model_config,
            model_client_config=model_client_config
        )

        messages = [UserMessage(content=prompt)]

        gen_kwargs = _build_image_gen_kwargs(
            provider,
            inputs,
            size=size_raw or _default_size_for_provider(provider, api_base),
            n=n,
            api_base=api_base,
        )

        async def _call():
            return await model_instance.generate_image(messages=messages, model=model, **gen_kwargs)

        # DashScope 等客户端内部是同步 SDK 调用（MultiModalConversation.call），
        # 直接 await 会阻塞整个事件循环（实测单次 45-60s，重试可达 300s+），
        # 导致 WS 心跳停发、客户端断连。整体挪到工作线程的独立事件循环执行。
        result = await asyncio.to_thread(
            lambda: asyncio.run(_RetryExecutor.with_backoff(_call, max_tries=3))
        )

        out_dir = Path(save_dir) if save_dir else None
        saved_paths = await asyncio.to_thread(
            _save_generated_images, result, prompt=prompt, output_dir=out_dir
        )

        return {
            "image_paths": [str(p) for p in saved_paths],
            "revised_prompt": prompt,
        }

    except Exception as ex:
        return {"error": f"[ERROR]: Image generation failed: {ex}"}


@tool(
    name="generate_image",
    description=(
        "Generate an image from a text description using AI image generation models. "
        "Use this tool when the user wants to create an image based on a text prompt. "
        "Supports provider-specific options: DashScope (prompt_extend, negative_prompt, seed), "
        "Huawei MaaS (seed, b64_json response), OpenAI-compatible (standard sizes). "
        "Returns the paths to the saved generated image file(s)."
    ),
)
async def generate_image(inputs: dict[str, Any], **kwargs) -> str:
    """
    Generate an image from text description.

    Args:
        inputs: Dict with keys:
            prompt (required): Text description of the image to generate
            size: Image size, e.g., "1024x1024", "1792x1024", "1920*1080" (DashScope uses *)
            quality: Image quality, "standard" or "hd"
            n: Number of images to generate (1-4; Huawei MaaS forces 1)
            negative_prompt: Negative prompt to exclude elements (DashScope only)
            prompt_extend: Whether to extend the prompt automatically (DashScope only, default True)
            seed: Random seed for reproducibility (DashScope, Huawei MaaS)
            save_dir: Optional directory to save images (defaults to workspace/generated_images)

    Returns:
        Paths to the generated image file(s) or error message
    """
    _ = kwargs
    from jiuwenswarm.common.config import get_config
    try:
        apply_image_gen_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply image_gen model config from yaml", exc_info=True)

    _, _, model, provider = _get_image_gen_api_credentials()
    logger.info(
        "[generate_image] using model: %s, provider: %s, size: %s, n: %s",
        model, provider, inputs.get("size", ""), inputs.get("n", 1),
    )

    try:
        result = await _invoke_model_image_generation(inputs or {})
    except Exception as exc:
        logger.warning("[generate_image] failed: %s", exc, exc_info=True)
        return f"[ERROR]: Image generation failed: {exc}"

    if "error" in result:
        return result["error"]

    image_paths = result["image_paths"]
    prompt = str(inputs.get("prompt", "") or "").strip()
    revised_prompt = result.get("revised_prompt", prompt)

    response_parts = [
        f"Generated {len(image_paths)} image(s) successfully!",
        "Local file paths (use for attachments or send_file):",
    ]
    response_parts.extend(f"- {p}" for p in image_paths)
    response_parts.append(f"Prompt: {prompt}")
    if revised_prompt != prompt:
        response_parts.append(f"Revised prompt: {revised_prompt}")

    return "\n".join(response_parts)

