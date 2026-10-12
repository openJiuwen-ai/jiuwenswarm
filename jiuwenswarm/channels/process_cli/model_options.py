# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ordinary command model selection and ephemeral connection configuration."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import argparse


class ModelOptionsError(ValueError):
    """A secret-free configuration error shown before starting Runtime."""


def _validate_connection(entry: dict[str, Any]) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    try:
        build_model_from_entry(
            dict(entry.get("model_client_config") or {}),
            dict(entry.get("model_config_obj") or {}),
        )
    except Exception:  # noqa: BLE001 - configuration errors may contain credentials
        raise ModelOptionsError(
            "所选模型连接配置无效；请检查 API 地址、密钥来源、提供方和模型配置。"
        ) from None


def validate_model_options(args: argparse.Namespace) -> str | None:
    """Validate flag combinations and capture the key before dotenv loading.

    Return the key only to the caller; do not attach it to argparse Namespace.
    """
    model = args.model
    connection = (args.api_base, args.api_key_env, args.model_provider)
    if model is None and not any(value is not None for value in connection):
        return None
    if args.prompt is None or getattr(args, "_interactive_worker", False):
        raise ModelOptionsError("模型连接参数仅支持带任务文本的非交互式命令。")
    if not model or not model.strip():
        raise ModelOptionsError("模型连接参数需要同时指定非空的 --model。")
    if args.api_base is not None:
        try:
            url = urlsplit(args.api_base)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.port != 0
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not re.search(r"\s", args.api_base)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ModelOptionsError(
                "--api-base 必须是 HTTP(S) 服务基础地址，不能包含认证信息、查询参数或片段。"
            )
        if args.api_key_env is None:
            raise ModelOptionsError("指定 --api-base 时必须同时指定 --api-key-env。")
    if args.model_provider is not None and args.api_base is None:
        raise ModelOptionsError("指定 --model-provider 时必须同时指定 --api-base。")
    if args.api_key_env is None:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        raise ModelOptionsError("--api-key-env 应填写环境变量名称，而不是密钥内容。")
    key = os.environ.get(args.api_key_env)
    if key is None or not key.strip():
        raise ModelOptionsError("--api-key-env 指定的环境变量未设置或为空。")
    if "\r" in key or "\n" in key:
        raise ModelOptionsError("--api-key-env 指定的密钥不能包含换行。")
    return key.strip()


def prepare_model_options(
    args: argparse.Namespace, api_key: str | None
) -> dict[str, Any] | None:
    """Resolve a configured selector, or build a new connection in memory.

    Imports configuration only after CLI diagnostics have been captured.
    Only the credential-free selector is added to the execution request.
    """
    if args.model is None:
        return None
    from jiuwenswarm.runtime.model_catalog import (
        ModelCatalogError,
        build_model_catalog,
        resolve_model_selection,
    )
    from jiuwenswarm.common.config import get_default_models

    entries = get_default_models()
    requested = args.model.strip()
    selected = None
    try:
        selected = resolve_model_selection(build_model_catalog(entries), requested)
    except ModelCatalogError as exc:
        if args.api_base is None or exc.code != "NOT_FOUND":
            raise ModelOptionsError(
                "--model 未找到唯一的已配置模型；请使用模型名称、唯一别名或 name#索引。"
                "新模型需要同时指定 --api-base 和 --api-key-env。"
            ) from None
        if "#" in requested:
            raise ModelOptionsError("新的模型名称不能使用 name#索引 选择器。") from None

    if args.api_base is None and api_key is None:
        index = int(selected.selection_key.rsplit("#", 1)[1])
        _validate_connection(entries[index])
        args.model_selection = selected.selection_key
        return None

    index = (
        int(selected.selection_key.rsplit("#", 1)[1]) if selected is not None else None
    )
    entry = deepcopy(entries[index]) if index is not None else {}
    client = dict(entry.get("model_client_config") or {})
    model_name = selected.model_name if selected is not None else requested
    if args.api_base is not None:
        # Changing destination must not forward old credentials, extra headers,
        # account tokens or endpoint dialect settings to the new service.
        client = {
            "client_provider": args.model_provider
            or client.get("client_provider")
            or "OpenAI",
            "api_base": args.api_base,
            "verify_ssl": True,
        }
    else:
        headers = client.get("custom_headers")
        if isinstance(headers, dict):
            authentication_headers = {
                "authorization",
                "api-key",
                "x-api-key",
                "proxy-authorization",
            }
            filtered_headers = {}
            for name, value in headers.items():
                if name.lower() not in authentication_headers:
                    filtered_headers[name] = value
            client["custom_headers"] = filtered_headers
    client["model_name"] = model_name
    client["api_key"] = api_key
    entry["model_client_config"] = client
    entry["is_default"] = True
    _validate_connection(entry)
    args.model_selection = model_name
    return entry
