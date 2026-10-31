# coding: utf-8
"""模型图像能力判定。

纯函数/常量模块：不引入 I/O，不读取历史，不处理 base64。

判定优先级（高 → 低）：
1. 模型条目显式 ``supports_image: true/false``（每个主模型独立配置）；
2. 模型条目显式 ``modalities``（含 ``image`` 即支持）；
3. 内置模型能力表（按模型名前缀匹配，提前标记好是否支持 Image）；
4. 未命中 → 视为不支持。
"""

from __future__ import annotations

# 内置模型能力表：模型名前缀 → 是否支持原生图片输入。
# 只收录明确支持视觉输入的模型族；未命中的模型按「不支持」处理，
# 避免把纯文本模型误判成可以收图。用户可在模型条目里用
# ``supports_image`` / ``modalities`` 覆盖这里的结论。
BUILTIN_IMAGE_CAPABLE_MODEL_PREFIXES: tuple[str, ...] = (
    # OpenAI
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.5",
    "gpt-4-turbo",
    "gpt-4-vision",
    "chatgpt-4o",
    "o1",
    "o3",
    "o4-mini",
    # Anthropic
    "claude-3",
    "claude-4",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    # Google
    "gemini-",
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
    # 开源视觉模型族
    "internvl",
    "llava",
    "pixtral",
    "llama-3.2",
    "llama-4",
    "minicpm-v",
    "phi-4-multimodal",
    "phi-4-reasoning-vision",
    "mistral-small-3",
    "mistral-medium-3",
    # xAI
    "grok-2-vision",
    "grok-4",
)


def normalize_model_name(model_name: str | None) -> str:
    """模型名归一化：去空白、转小写，便于前缀匹配。"""

    return str(model_name or "").strip().lower()


def builtin_model_supports_image(model_name: str | None) -> bool:
    """按内置能力表判断模型是否支持原生图片输入。"""

    name = normalize_model_name(model_name)
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in BUILTIN_IMAGE_CAPABLE_MODEL_PREFIXES)


def resolve_model_supports_image(
    modalities: list[str] | None = None,
    model_name: str | None = None,
    supports_image: bool | None = None,
) -> bool:
    """判定模型是否支持图像输入。

    - ``supports_image`` 为 bool 时直接采信（单模型显式配置）；
    - 否则看 ``modalities`` 是否显式声明 ``image``；
    - 都未配置时回退到内置模型能力表。
    """

    if isinstance(supports_image, bool):
        return supports_image
    if modalities is not None:
        return "image" in modalities
    return builtin_model_supports_image(model_name)
