"""Vision tools for a persistent colleague.

The published colleague does not carry DeepAgent's vision tools. They are
attached to the same Invocation that builds Bash, and they read only the host-authorized inbound media and current session output images.
"""

from __future__ import annotations

import asyncio
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
    """Resolve the unified saved vision slot; support env-only legacy workspaces."""
    from jiuwenswarm.common.config import config_write_lock
    from jiuwenswarm.common.multimodal_store import MultimodalStore
    from jiuwenswarm.common.multimodal_models import MultimodalError
    from jiuwenswarm.common.utils import get_config_file

    path = get_config_file()
    try:
        with config_write_lock(config_path=path):
            _, state = MultimodalStore.load_catalogue_locked(path)
    except MultimodalError as exc:
        if exc.code != "MM_NOT_CONFIGURED":
            raise
    else:
        binding = state['tools']['vision']
        if not binding['enabled']:
            return None
        selected = next((m for m in state['models'] if m['model_id'] == binding['model_id']), None)
        if selected is None:
            return None
        connection = selected['connection']
        config = VisionModelConfig(api_key=connection.get('api_key', ''), base_url=connection.get('api_base', ''),
                                   model=connection.get('model_name', ''), max_retries=1)
        return config if is_vision_model_config_complete(config) else None
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
        "图片引用或工作空间相对路径由本次任务提供，不要猜测路径。"
        "要看画面，调用 visual_question_answering："
        "image_path_or_url 填本次任务给出的图片编号或工作空间相对路径，question 填用户的问题。"
    )


def office_vision_tools(office: Path, *, language: str = "cn", agent_id: str | None = None, image_scope=None) -> list[Tool]:
    """Build path-checked vision tools, or nothing when the vision slot is off."""
    config = vision_config_from_env()
    if config is None:
        return []
    try:
        built = create_vision_tools(language=language, vision_model_config=config, agent_id=agent_id)
    except Exception:
        logger.info("[JiuwenMate] vision tools were not attached")
        return []
    return [_OfficeImageTool(tool, office, image_scope=image_scope) for tool in built]


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

    def __init__(self, base: Tool, office: Path, *, image_scope=None) -> None:
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
        self._image_scope = image_scope
        if image_scope is not None:
            self.card.description = (
                "理解当前执行已授权的图片。image_path_or_url 使用消息给出的 media:image:N（或 feishu-image:N）编号，"
                "或当前会话 output/ 中生成的截图路径。禁止其他会话文件或任意 URL。question 填用户的问题。"
                if card.name == "visual_question_answering" else
                "提取当前执行图片文字，使用 media:image:N 编号或当前会话 output/ 截图路径。"
            )

    async def invoke(self, inputs: dict[str, Any] | None, **kwargs: Any):
        data = dict(inputs or {})
        raw = data.get("image_path_or_url")
        if self._image_scope is not None:
            data["image_path_or_url"] = await self._image_scope.resolve(raw)
            if self.card.name == "visual_question_answering":
                data.setdefault("include_ocr", False)
            timeout = float(os.getenv("VISION_REQUEST_TIMEOUT_SECONDS", "60"))
            if timeout <= 0:
                raise ValueError("VISION_REQUEST_TIMEOUT_SECONDS 必须大于 0")
            try:
                async with asyncio.timeout(timeout):
                    return await self._base.invoke(data, **kwargs)
            except TimeoutError as exc:
                raise ValueError("视觉服务响应超时，请稍后重试。") from exc
        elif isinstance(raw, str):
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
