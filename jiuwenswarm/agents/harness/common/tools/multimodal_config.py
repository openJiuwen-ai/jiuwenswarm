# coding: utf-8
"""
多模态工具配置管理模块

配置优先级:
1. models.{audio/vision/video}.model_config
2. embed.{audio_model/video_model/vision_model} 和 embed.embed_api_key/embed_api_base
3. 环境变量 MODEL_NAME, API_KEY, API_BASE
"""
import os
from typing import Any


def _parse_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")


def _get_embed_config(config_base: dict[str, Any]) -> dict[str, Any]:
    embed = config_base.get("embed", {})
    return embed if isinstance(embed, dict) else {}


def _get_model_config(config_base: dict[str, Any], model_type: str) -> dict[str, Any]:
    """
    从 config.yaml 中读取指定类型的模型配置

    Args:
        config_base: 配置字典
        model_type: 模型类型，如 'audio', 'vision', 'video'

    Returns:
        模型配置字典
    """
    if not isinstance(config_base, dict):
        return {}

    raw_models = config_base.get("models")
    if isinstance(raw_models, dict):
        inner = raw_models.get(model_type)
        if isinstance(inner, dict):
            mc = inner.get("model_config") or inner.get("model_client_config")
            if isinstance(mc, dict):
                return mc
        return {}

    if not isinstance(raw_models, list):
        return {}

    for block in raw_models:
        if isinstance(block, dict) and model_type in block:
            inner = block.get(model_type)
            if isinstance(inner, dict):
                mc = inner.get("model_config") or inner.get("model_client_config")
                if isinstance(mc, dict):
                    return mc
    return {}


_EMBED_MODEL_KEY_MAP = {
    "audio": "audio_model",
    "vision": "vision_model",
    "video": "video_model",
    "image_gen": "image_gen_model",
}

_MULTIMODAL_MODEL_TYPES = ("audio", "vision", "video")


def _model_provider(model_config: dict[str, Any]) -> str:
    return str(
        model_config.get("client_provider")
        or model_config.get("model_provider")
        or ""
    ).strip()


def complete_multimodal_model_configured(
    config_base: dict[str, Any] | None, model_type: str
) -> bool:
    """Whether `models.{model_type}` has key, URL, model name, and provider."""
    if model_type not in _MULTIMODAL_MODEL_TYPES:
        return False
    if not isinstance(config_base, dict):
        return False
    mc = _get_model_config(config_base, model_type)
    api_key = str(mc.get("api_key") or "").strip()
    api_base = str(mc.get("api_base") or mc.get("base_url") or "").strip()
    model_name = str(mc.get("model_name") or mc.get("model") or "").strip()
    provider = _model_provider(mc)
    return bool(api_key and api_base and model_name and provider)


def multimodal_model_enabled(
    config_base: dict[str, Any] | None, model_type: str
) -> bool:
    """Return the explicit capability switch, preserving complete legacy configs."""
    if model_type not in _MULTIMODAL_MODEL_TYPES:
        return False

    enabled_env = os.getenv(f"{model_type.upper()}_ENABLED")
    if enabled_env is not None:
        return _parse_bool(enabled_env)

    if isinstance(config_base, dict):
        raw_models = config_base.get("models")
        if isinstance(raw_models, dict):
            model_block = raw_models.get(model_type)
            if isinstance(model_block, dict) and "enabled" in model_block:
                return _parse_bool(model_block.get("enabled"))

    # Deterministic compatibility for workspaces created before explicit flags.
    return complete_multimodal_model_configured(config_base, model_type)


def _get_embed_model_name(embed_cfg: dict[str, Any], model_type: str) -> str:
    """
    从 embed 配置中获取指定类型的模型名称

    Args:
        embed_cfg: embed 配置字典
        model_type: 模型类型，如 'audio', 'vision', 'video'

    Returns:
        模型名称字符串
    """
    key = _EMBED_MODEL_KEY_MAP.get(model_type)
    if key and isinstance(embed_cfg, dict):
        return str(embed_cfg.get(key) or "").strip()
    return ""


def apply_audio_model_config_from_yaml(config_base: dict[str, Any] | None) -> None:
    """
    从 config.yaml 读取音频模型配置并设置环境变量

    配置优先级:
    1. models.audio.model_config
    2. embed.audio_model + embed.embed_api_key/embed_api_base
    3. 环境变量 MODEL_NAME, API_KEY, API_BASE
    """
    if not isinstance(config_base, dict):
        return

    mc = _get_model_config(config_base, "audio")
    embed_cfg = _get_embed_config(config_base)

    api_key = str(mc.get("api_key") or "").strip()
    api_base = str(mc.get("api_base") or "").strip()
    model_name = str(mc.get("model_name") or mc.get("model") or "").strip()
    provider = _model_provider(mc)
    strict = _parse_bool(mc.get("strict"), default=False)

    if not strict:
        if not api_key:
            api_key = str(
                embed_cfg.get("embed_api_key") or os.getenv("API_KEY", "")
            ).strip()
        if not api_base:
            api_base = str(
                embed_cfg.get("embed_api_base") or os.getenv("API_BASE", "")
            ).strip()
        if not model_name:
            model_name = (
                _get_embed_model_name(embed_cfg, "audio")
                or os.getenv("MODEL_NAME", "").strip()
            )
        if not provider:
            provider = os.getenv("MODEL_PROVIDER", "").strip()

    if api_key:
        os.environ["AUDIO_API_KEY"] = api_key
    if api_base:
        os.environ["AUDIO_API_BASE"] = api_base
    if model_name:
        os.environ["AUDIO_MODEL_NAME"] = model_name
    if provider:
        os.environ["AUDIO_PROVIDER"] = provider


def apply_vision_model_config_from_yaml(config_base: dict[str, Any] | None) -> None:
    """
    从 config.yaml 读取图像模型配置并设置环境变量

    配置优先级:
    1. models.vision.model_config
    2. embed.vision_model + embed.embed_api_key/embed_api_base
    3. 环境变量 MODEL_NAME, API_KEY, API_BASE
    """
    if not isinstance(config_base, dict):
        return

    mc = _get_model_config(config_base, "vision")
    embed_cfg = _get_embed_config(config_base)

    api_key = str(mc.get("api_key") or "").strip()
    api_base = str(mc.get("api_base") or "").strip()
    model_name = str(mc.get("model_name") or mc.get("model") or "").strip()
    provider = _model_provider(mc)
    strict = _parse_bool(mc.get("strict"), default=False)

    if not strict:
        if not api_key:
            api_key = str(
                embed_cfg.get("embed_api_key") or os.getenv("API_KEY", "")
            ).strip()
        if not api_base:
            api_base = str(
                embed_cfg.get("embed_api_base") or os.getenv("API_BASE", "")
            ).strip()
        if not model_name:
            model_name = (
                _get_embed_model_name(embed_cfg, "vision")
                or os.getenv("MODEL_NAME", "").strip()
            )
        if not provider:
            provider = os.getenv("MODEL_PROVIDER", "").strip()

    if api_key:
        os.environ["VISION_API_KEY"] = api_key
    if api_base:
        os.environ["VISION_API_BASE"] = api_base
    if model_name:
        os.environ["VISION_MODEL_NAME"] = model_name
    if provider:
        os.environ["VISION_PROVIDER"] = provider


def apply_video_model_config_from_yaml(config_base: dict[str, Any] | None) -> None:
    """
    从 config.yaml 读取视频模型配置并设置环境变量

    配置优先级:
    1. models.video.model_config
    2. embed.video_model + embed.embed_api_key/embed_api_base
    3. 环境变量 MODEL_NAME, API_KEY, API_BASE
    """
    if not isinstance(config_base, dict):
        os.environ.pop("VIDEO_UNDERSTANDING_STRICT", None)
        return

    mc = _get_model_config(config_base, "video")
    embed_cfg = _get_embed_config(config_base)

    api_key = str(mc.get("api_key") or "").strip()
    api_base = str(mc.get("api_base") or "").strip()
    model_name = str(mc.get("model_name") or mc.get("model") or "").strip()
    provider = _model_provider(mc)
    strict = _parse_bool(mc.get("strict"), default=False)

    if strict:
        os.environ["VIDEO_UNDERSTANDING_STRICT"] = "1"
    else:
        os.environ.pop("VIDEO_UNDERSTANDING_STRICT", None)
        if not api_key:
            api_key = str(
                embed_cfg.get("embed_api_key") or os.getenv("API_KEY", "")
            ).strip()
        if not api_base:
            api_base = str(
                embed_cfg.get("embed_api_base") or os.getenv("API_BASE", "")
            ).strip()
        if not model_name:
            model_name = (
                _get_embed_model_name(embed_cfg, "video")
                or os.getenv("MODEL_NAME", "").strip()
            )
        if not provider:
            provider = os.getenv("MODEL_PROVIDER", "").strip()

    if api_key:
        os.environ["VIDEO_API_KEY"] = api_key
    if api_base:
        os.environ["VIDEO_API_BASE"] = api_base
    if model_name:
        os.environ["VIDEO_MODEL_NAME"] = model_name
    if provider:
        os.environ["VIDEO_PROVIDER"] = provider


def apply_image_gen_model_config_from_yaml(config_base: dict[str, Any] | None) -> None:
    """
    从 config.yaml 读取文生图模型配置并设置环境变量

    配置优先级:
    1. models.image_gen.model_config
    2. embed.image_gen_model + embed.embed_api_key/embed_api_base
    3. 环境变量 MODEL_NAME, API_KEY, API_BASE

    Default provider: DashScope
    Default api_base: https://dashscope.aliyuncs.com/api/v1
    """
    if not isinstance(config_base, dict):
        return

    mc = _get_model_config(config_base, "image_gen")

    api_key = str(mc.get("api_key") or "").strip()
    api_base = str(mc.get("api_base") or "").strip()
    model_name = str(mc.get("model_name") or mc.get("model") or "").strip()
    provider = str(
        mc.get("client_provider") or mc.get("model_provider") or ""
    ).strip()

    if api_key:
        os.environ["IMAGE_GEN_API_KEY"] = api_key
    if api_base:
        os.environ["IMAGE_GEN_API_BASE"] = api_base
    if model_name:
        os.environ["IMAGE_GEN_MODEL_NAME"] = model_name
    if provider:
        os.environ["IMAGE_GEN_PROVIDER"] = provider


def apply_video_gen_model_config_from_yaml(config_base: dict[str, Any] | None) -> None:
    """Apply text-to-video config; default to the same provider API as image_gen.

    Priority:
    1. models.video_gen.model_config / VIDEO_GEN_* env
    2. models.image_gen / IMAGE_GEN_* (shared DashScope / provider credentials)
    3. Built-in DashScope defaults (api_base + wan2.6-t2v)
    """
    if not isinstance(config_base, dict):
        config_base = {}

    # Ensure image_gen env is populated first so we can fall back to it.
    apply_image_gen_model_config_from_yaml(config_base)

    mc = _get_model_config(config_base, "video_gen")
    image_mc = _get_model_config(config_base, "image_gen")

    api_key = str(
        mc.get("api_key")
        or os.getenv("VIDEO_GEN_API_KEY")
        or image_mc.get("api_key")
        or os.getenv("IMAGE_GEN_API_KEY")
        or os.getenv("API_KEY")
        or ""
    ).strip()
    api_base = str(
        mc.get("api_base")
        or os.getenv("VIDEO_GEN_API_BASE")
        or image_mc.get("api_base")
        or os.getenv("IMAGE_GEN_API_BASE")
        or os.getenv("API_BASE")
        or "https://dashscope.aliyuncs.com/api/v1"
    ).strip()
    model_name = str(
        mc.get("model_name")
        or mc.get("model")
        or os.getenv("VIDEO_GEN_MODEL_NAME")
        or "wan2.6-t2v"
    ).strip()
    provider = str(
        mc.get("client_provider")
        or mc.get("model_provider")
        or os.getenv("VIDEO_GEN_PROVIDER")
        or image_mc.get("client_provider")
        or image_mc.get("model_provider")
        or os.getenv("IMAGE_GEN_PROVIDER")
        or "DashScope"
    ).strip()

    if api_key:
        os.environ["VIDEO_GEN_API_KEY"] = api_key
    if api_base:
        os.environ["VIDEO_GEN_API_BASE"] = api_base
    if model_name:
        os.environ["VIDEO_GEN_MODEL_NAME"] = model_name
    if provider:
        os.environ["VIDEO_GEN_PROVIDER"] = provider
