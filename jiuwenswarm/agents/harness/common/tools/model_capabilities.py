# coding: utf-8
"""模型原生能力（modalities）的配置归一。

纯函数模块：不引入 I/O，不读取历史，不处理 base64。

每个模型条目的原生能力由 ``modalities`` 列表声明（OpenAI 风格），例如
``modalities: [text, image]``。是否支持原生图片输入只由该列表是否含 ``image``
决定：

- 列表含 ``image`` → 支持原生看图（原生图片输入）；
- 缺失 / 未声明 / 其它不可识别值 → 不支持（走 VQA / 视觉工具链）。

不再内置模型能力表，也不按模型名前缀猜测。
"""

from __future__ import annotations


def coerce_modalities(value: object) -> list[str] | None:
    """把配置里的 ``modalities`` 规范成小写去重列表；无法识别 → None（未声明）。

    YAML 里 ``modalities: [text, image]`` 会被解析成字符串列表；也可能被写成
    单个字符串或逗号/空白分隔字符串（``"text,image"``）。这里统一成规范模态列表，
    去空白、去重、保序；空列表 / 空串 / 其它非字符串容器一律归 None。
    """

    if value is None:
        return None
    if isinstance(value, str):
        tokens: list[str] = [p for p in value.replace(",", " ").split() if p]
    elif isinstance(value, (list, tuple, set, frozenset)):
        tokens = [item.strip() for item in value if isinstance(item, str)]
    else:
        return None
    if not tokens:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        canonical = token.strip().lower()
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized or None


def supports_native_image(value: object) -> bool:
    """给定模型条目的 ``modalities`` 声明，判定是否支持原生图片输入。"""

    return "image" in (coerce_modalities(value) or ())
