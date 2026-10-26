# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from jiuwenswarm.common.config import get_config


def response_language_line() -> str:
    """根据 config.yaml ``preferred_language`` 生成 Agent 回复语言指令。"""
    lang = str(get_config().get("preferred_language") or "zh").strip().lower()
    if lang != "en":
        lang = "zh"
    return "Respond in Chinese (simplified)." if lang == "zh" else "Respond in English."
