# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Helpers to extract A2A variables from Xiaoyi message parts."""

from __future__ import annotations

from typing import Any


def extract_model_name(parts: list[Any] | None) -> str | None:
    """Extract modelName from A2A data parts (variables.clientVariables.modelName).

    Mirrors OpenClaw xy_channel ``extractModelName``: ignore empty / ``none``.
    """
    for part in parts or []:
        if not isinstance(part, dict) or part.get("kind") != "data":
            continue
        data = part.get("data")
        if not isinstance(data, dict):
            continue
        variables = data.get("variables")
        if not isinstance(variables, dict):
            continue
        client_vars = variables.get("clientVariables")
        if not isinstance(client_vars, dict):
            continue
        model_name = client_vars.get("modelName")
        if not isinstance(model_name, str):
            continue
        model_name = model_name.strip()
        if model_name and model_name.lower() != "none":
            return model_name
    return None


def normalize_model_input(raw: Any) -> list[str] | None:
    """Normalize modelInput to a lowercase, deduped list; empty/invalid → None.

    Accepts a list/tuple of strings or a single string. A single string may carry
    comma-separated modes (clients that flatten the array, e.g. ``"text,image"``).
    """
    values = raw if isinstance(raw, (list, tuple)) else [raw] if isinstance(raw, str) else []
    modes: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for token in value.split(","):
            mode = token.strip().lower()
            if mode and mode not in modes:
                modes.append(mode)
    return modes or None


def extract_model_input(parts: list[Any] | None) -> list[str] | None:
    """Extract modelInput from A2A data parts (variables.clientVariables.modelInput).

    Declares the input modalities of the model selected for this turn
    (e.g. ``["text", "image"]``); empty / missing → ``None`` = no declaration.
    """
    for part in parts or []:
        if not isinstance(part, dict) or part.get("kind") != "data":
            continue
        data = part.get("data")
        if not isinstance(data, dict):
            continue
        variables = data.get("variables")
        if not isinstance(variables, dict):
            continue
        client_vars = variables.get("clientVariables")
        if not isinstance(client_vars, dict):
            continue
        modes = normalize_model_input(client_vars.get("modelInput"))
        if modes:
            return modes
    return None


def has_model_name_key(parts: list[Any] | None) -> bool:
    """判断端侧消息是否显式携带 clientVariables.modelName 键（含空值）。

    ``extract_model_name`` 按契约忽略空值/``none``，无法区分"未携带"与
    "显式置空（端侧回到默认模型）"——会话级模型记录依赖该区分：显式
    置空应清除记录，未携带应保持上次选择不动。
    """
    for part in parts or []:
        if not isinstance(part, dict) or part.get("kind") != "data":
            continue
        data = part.get("data")
        if not isinstance(data, dict):
            continue
        variables = data.get("variables")
        if not isinstance(variables, dict):
            continue
        client_vars = variables.get("clientVariables")
        if not isinstance(client_vars, dict):
            continue
        if "modelName" in client_vars:
            return True
    return False
