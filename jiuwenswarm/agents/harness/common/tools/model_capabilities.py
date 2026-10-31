# coding: utf-8
"""模型图像能力判定。

纯函数/常量模块：不引入 I/O，不读取历史，不处理 base64。

两段式判定：

1. ``resolve_model_entry_support``：只看模型条目自己的声明。
   ``supports_image: true/false`` 优先；否则看 ``modalities`` 是否含 ``image``；
   两处都没配置 → ``None``（未声明）。
2. 调用方在未声明时回退到内置模型表（``builtin_model_supports_image``），
   显式配置 ``react.enable_read_image_multimodal`` 只在这一层兜底。

``resolve_model_supports_image`` 是「条目声明 > 内置表」的便捷组合，不处理显式配置。
"""

from __future__ import annotations

# 内置模型能力表：模型名前缀 → 是否支持原生图片输入。
# 只收录明确支持视觉输入的模型族；未命中的模型按「不支持」处理，
# 避免把纯文本模型误判成可以收图。用户可在模型条目里用
# ``supports_image`` / ``modalities`` 覆盖这里的结论。
BUILTIN_IMAGE_CAPABLE_MODEL_PREFIXES: tuple[str, ...] = (
    # OpenAI（o1 与 o3-mini / o3-pro 不收图，见下方 text-only 表）
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.5",
    "gpt-4-turbo",
    "gpt-4-vision",
    "chatgpt-4o",
    "o3",
    "o4-mini",
    # Anthropic
    "claude-3",
    "claude-4",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    # Google（只列支持视觉输入的主力族；gemini-1.0-pro 纯文本不在内）
    "gemini-1.5",
    "gemini-2",
    "gemini-3",
    "gemini-exp",
    # 通义千问 / Qwen
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "qvq",
    # 智谱 GLM
    "glm-4v",
    "glm-4.1v",
    "glm-4.5v",
    # 豆包 / Doubao
    "doubao-vision",
    "doubao-1.5-vision",
    "doubao-seed-1.5-vision",
    "doubao-seed-1.6",
    # 华为
    "hunyuan-vision",
    "ernie-4.5-vl",
    # 月之暗面 / MiniMax / 零一万物
    "kimi-vl",
    "moonshot-v1-vision",
    "minimax-vl",
    "yi-vision",
    # DeepSeek 视觉 / 阶跃
    "deepseek-vl",
    "step-1v",
    "step-3",
    # 开源视觉模型族（只列视觉变体；llama-3.2-1b/3b、mistral-small-3 纯文本不在内）
    "internvl",
    "llava",
    "pixtral",
    "llama-3.2-11b",
    "llama-3.2-90b",
    "llama-4",
    "minicpm-v",
    "phi-4-multimodal",
    "phi-4-reasoning-vision",
    "mistral-small-3.1",
    "mistral-small-3.2",
    "mistral-medium-3",
    # xAI
    "grok-2-vision",
    "grok-4",
)

# 命中 capable 前缀但实际是纯文本的变体：先于 capable 表判定。
# 例如 ``o3-mini`` / ``o3-pro`` 以 ``o3`` 开头但不接收图片。
BUILTIN_IMAGE_TEXT_ONLY_MODEL_PREFIXES: tuple[str, ...] = (
    "o3-mini",
    "o3-pro",
)


def normalize_model_name(model_name: str | None) -> str:
    """模型名归一化：去空白、转小写，便于前缀匹配。"""

    return str(model_name or "").strip().lower()


def builtin_model_supports_image(model_name: str | None) -> bool:
    """按内置能力表判断模型是否支持原生图片输入（表仅作兜底启发式）。"""

    name = normalize_model_name(model_name)
    if not name:
        return False
    if any(name.startswith(prefix) for prefix in BUILTIN_IMAGE_TEXT_ONLY_MODEL_PREFIXES):
        return False
    return any(name.startswith(prefix) for prefix in BUILTIN_IMAGE_CAPABLE_MODEL_PREFIXES)


def resolve_model_entry_support(
    modalities: list[str] | None = None,
    supports_image: bool | None = None,
) -> bool | None:
    """只看模型条目显式声明的图像能力。

    - ``supports_image`` 为 bool → 直接采信；
    - 否则 ``modalities`` 非 ``None`` → ``"image" in modalities``（空列表 = 明确纯文本）；
    - 都未配置 → ``None``，交给调用方回退内置表 / 显式配置。
    """

    if isinstance(supports_image, bool):
        return supports_image
    if modalities is not None:
        return "image" in modalities
    return None


def resolve_model_supports_image(
    modalities: list[str] | None = None,
    model_name: str | None = None,
    supports_image: bool | None = None,
) -> bool:
    """判定模型是否支持图像输入（不含 ``enable_read_image_multimodal`` 显式覆盖）。

    - 模型条目显式声明 → 采信；
    - 未声明 → 回退内置模型能力表；未命中视为不支持。
    """

    entry_support = resolve_model_entry_support(modalities, supports_image)
    if entry_support is not None:
        return entry_support
    return builtin_model_supports_image(model_name)
