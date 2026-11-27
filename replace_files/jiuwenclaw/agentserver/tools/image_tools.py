import argparse
import asyncio
import base64
import logging
import os
import sys
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

from jiuwenclaw.agentserver.tools.multimodal_config import apply_vision_model_config_from_yaml
from jiuwenclaw.agentserver.tools.ssl_config import get_requests_verify


logger = logging.getLogger(__name__)
load_dotenv(verbose=True)

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
            txt = r.choices[0].message.content
            if not txt or not txt.strip():
                raise Exception("Response text is None or empty")
            return txt

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
    from jiuwenclaw.config import get_config
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

