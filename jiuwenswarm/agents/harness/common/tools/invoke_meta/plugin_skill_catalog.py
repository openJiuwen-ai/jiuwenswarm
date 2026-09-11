# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Invoke ToolCard zone hint and leftover Seedance result parsers.

functionName / bundleName live in desktop SKILL.md. invoke does not rewrite them.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

_PROD_MCP_HOST_MARKERS = ("hag-drcn", "dbankcloud.com", "huawei.com")


def is_prod_plugin_runtime(url: str | None = None) -> bool:
    """True when mcp/run points at 现网 (hag-drcn / dbankcloud / huawei.com).

    Desktop spawn points AGENT_RUNTIME_MCP_RUN at the loopback inject proxy;
    prefer AGENT_RUNTIME_MCP_UPSTREAM (real 现网/蓝绿 URL) when set.
    """
    raw = (
        url
        if url is not None
        else (
            os.environ.get("AGENT_RUNTIME_MCP_UPSTREAM")
            or os.environ.get("AGENT_RUNTIME_MCP_RUN")
            or ""
        )
    ).strip()
    if not raw:
        return False
    host = ""
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        host = ""
    haystack = f"{host} {raw.lower()}"
    return any(marker in haystack for marker in _PROD_MCP_HOST_MARKERS)


def parse_plugin_json_payload(raw: Any) -> dict[str, Any]:
    """Parse cloud plugin content that may be a JSON string or dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _looks_like_http_url(value: str) -> bool:
    lower = value.strip().lower()
    return lower.startswith("http://") or lower.startswith("https://")


def _payload_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bags: list[dict[str, Any]] = [payload]
    nested = payload.get("content")
    if isinstance(nested, dict):
        bags.append(nested)
    for bag in list(bags):
        items = bag.get("items")
        if isinstance(items, list):
            bags.extend(item for item in items if isinstance(item, dict))
    return bags


def _payload_item_strings(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for bag in (payload, payload.get("content") if isinstance(payload.get("content"), dict) else {}):
        if not isinstance(bag, dict):
            continue
        items = bag.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and item.strip():
                texts.append(item.strip())
    return texts


def extract_seedance_task_id(result: dict[str, Any]) -> str:
    """Read task_id from seedanceMiniTask invoke result."""
    payload = parse_plugin_json_payload(result.get("content"))
    if not payload and isinstance(result, dict):
        payload = {k: v for k, v in result.items() if k != "frames"}
    for candidate in _payload_dicts(payload):
        for key in ("task_id", "id", "taskId"):
            value = str(candidate.get(key) or "").strip()
            if value and not _looks_like_http_url(value):
                return value
    for text in _payload_item_strings(payload):
        if not _looks_like_http_url(text):
            return text
    return ""


def extract_seedance_query_state(result: dict[str, Any]) -> tuple[str, str]:
    """Return (status, video_url) from seedanceMiniTaskQuery result."""
    payload = parse_plugin_json_payload(result.get("content"))
    if not payload and isinstance(result, dict):
        payload = {k: v for k, v in result.items() if k != "frames"}
    status = ""
    video_url = ""
    for candidate in _payload_dicts(payload):
        if not status:
            status = str(candidate.get("status") or "").strip().lower()
        if not video_url:
            video_url = str(
                candidate.get("video_url") or candidate.get("videoUrl") or ""
            ).strip()
        nested = candidate.get("content")
        if isinstance(nested, dict):
            if not status:
                status = str(nested.get("status") or "").strip().lower()
            if not video_url:
                video_url = str(
                    nested.get("video_url") or nested.get("videoUrl") or ""
                ).strip()
        if status and video_url:
            break
    if not video_url:
        for text in _payload_item_strings(payload):
            if _looks_like_http_url(text):
                video_url = text
                break
    return status, video_url


def plugin_runtime_zone_label() -> str:
    """LLM-facing zone name for ToolCard: 现网 vs 蓝绿."""
    return "现网" if is_prod_plugin_runtime() else "蓝绿"


def invoke_arguments_description() -> str:
    """ToolCard arguments.description — passthrough; names come from SKILL.md."""
    return (
        "Must include bundleName (the package name from the current-zone table) "
        "plus the capability's business fields, passed through as-is. "
        "Put functionName at the top level; do not wrap business parameters in "
        "content unless the SKILL.md requires an array. Do not put timeout_s "
        "inside arguments (it is an optional top-level field)."
    )


def invoke_timeout_s_description() -> str:
    """ToolCard timeout_s.description — optional local wait budget."""
    return (
        "Optional. Max local wait in seconds for a single cloud plugin call. "
        "Can be raised for group image generation or song generation. Defaults "
        "when omitted: group image 60×max_images, song 600s, others 300s. "
        "Hard cap 3600. Applies to the local wall clock only; never sent to "
        "the cloud plugin."
    )


def invoke_function_name_description() -> str:
    """ToolCard functionName.description — flattened capability name."""
    return (
        "Cloud capability: the real functionName from the current-zone table "
        "of the loaded skill. Remote Agent: agent_as_a_tool."
    )


def invoke_tool_description() -> str:
    """Short ToolCard: invoke cloud tools from loaded skill tables."""
    return "Invoke the relevant cloud tools according to the loaded skills."


__all__ = [
    "extract_seedance_query_state",
    "extract_seedance_task_id",
    "invoke_arguments_description",
    "invoke_function_name_description",
    "invoke_timeout_s_description",
    "invoke_tool_description",
    "is_prod_plugin_runtime",
    "plugin_runtime_zone_label",
    "parse_plugin_json_payload",
]
