"""Vision tools for a persistent colleague.

The published colleague does not carry DeepAgent's vision tools. They are
attached to the same Invocation that builds Bash, and they only read image
files inside the office or a WeLink incoming cache.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.harness.schema.config import VisionModelConfig, is_vision_model_config_complete
from openjiuwen.harness.tools.multimodal.vision import create_vision_tools

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_TRUE = {"1", "true", "yes", "on"}


def vision_config_from_env() -> VisionModelConfig | None:
    """Read the dedicated vision slot. A chat API key is not reused."""
    if os.getenv("VISION_ENABLED", "").strip().lower() not in _TRUE:
        return None
    api_key = os.getenv("VISION_API_KEY", "").strip()
    base_url = (os.getenv("VISION_BASE_URL") or os.getenv("VISION_API_BASE") or "").strip()
    model = (os.getenv("VISION_MODEL") or os.getenv("VISION_MODEL_NAME") or "").strip()
    if not api_key or not base_url or not model:
        return None
    raw_retries = os.getenv("VISION_MAX_RETRIES", "").strip()
    try:
        retries = int(raw_retries) if raw_retries else 3
    except ValueError:
        retries = 3
    config = VisionModelConfig(api_key=api_key, base_url=base_url, model=model, max_retries=max(retries, 1))
    if not is_vision_model_config_complete(config):
        return None
    return config


def vision_guidance() -> str:
    """Tell this Invocation how to look at a saved group image."""
    if vision_config_from_env() is None:
        return ""
    return (
        "## 图片\n\n"
        "群里发来的图片保存在办公空间的 incoming/ 目录。"
        "要看画面，调用 visual_question_answering："
        "image_path_or_url 填消息中的工作空间相对路径，question 填用户的问题。"
    )


def office_vision_tools(office: Path, *, language: str = "cn", agent_id: str | None = None) -> list[Tool]:
    """Build path-checked vision tools, or nothing when the vision slot is off."""
    config = vision_config_from_env()
    if config is None:
        return []
    try:
        built = create_vision_tools(language=language, vision_model_config=config, agent_id=agent_id)
    except Exception:
        logger.info("[JiuwenMate] vision tools were not attached")
        return []
    return [_OfficeImageTool(tool, office) for tool in built]


def resolve_office_image(office: Path, raw: str, *, media_root: Path | None = None) -> str:
    """Resolve a workspace-relative image, or an already downloaded WeLink image."""
    value = raw.strip() if isinstance(raw, str) else ""
    if not value or "\x00" in value:
        raise ValueError("图片路径无效")
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value

    office_root = office.resolve()
    cache = (media_root or (Path.home() / ".jiuwenswarm" / "welink")).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        normalized = value.replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
        if not parts or ".." in parts or normalized.startswith("/"):
            raise ValueError("图片路径必须是办公空间内的相对路径")
        candidate = office_root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError("图片路径无效") from exc
    if not resolved.is_file() or resolved.suffix.lower() not in _IMAGE_SUFFIXES:
        raise ValueError("图片文件不存在")
    if _within(resolved, office_root) or _incoming_image(resolved, cache):
        return str(resolved)
    raise ValueError("图片路径必须是办公空间内的相对路径")


class _OfficeImageTool(Tool):
    """Forward a vision tool after turning a workspace path into a local file."""

    def __init__(self, base: Tool, office: Path) -> None:
        card = base.card.model_copy(deep=True)
        if card.name == "visual_question_answering":
            card.description = (
                "使用配置的视觉模型看图片并回答问题。"
                "image_path_or_url 填消息给出的工作空间相对路径，例如 incoming/<消息>/<文件名.jpg>。"
                "question 填用户的问题。"
            )
        elif card.name == "image_ocr":
            card.description = (
                "使用配置的视觉模型提取图片中的可见文字。"
                "image_path_or_url 填消息给出的工作空间相对路径。"
            )
        super().__init__(card)
        self._base = base
        self._office = office

    async def invoke(self, inputs: dict[str, Any] | None, **kwargs: Any):
        data = dict(inputs or {})
        raw = data.get("image_path_or_url")
        if isinstance(raw, str):
            data["image_path_or_url"] = resolve_office_image(self._office, raw)
        return await self._base.invoke(data, **kwargs)

    async def stream(self, inputs: dict[str, Any] | None, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    def render_for_llm(self, output: Any) -> str:
        renderer = getattr(self._base, "render_for_llm", None)
        if callable(renderer):
            return renderer(output)
        return super().render_for_llm(output)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _incoming_image(path: Path, media_root: Path) -> bool:
    return _within(path, media_root) and "incoming" in path.parts and path.suffix.lower() in _IMAGE_SUFFIXES
