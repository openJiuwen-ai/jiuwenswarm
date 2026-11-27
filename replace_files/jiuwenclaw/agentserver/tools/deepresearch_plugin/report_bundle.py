# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

from jiuwenclaw.agentserver.tools.deepresearch_plugin.conversion_utils import (
    strip_internal_citation_markers,
)

INFERENCE_LINK_RE = re.compile(r"\[([^\]]+)\]\(#inference:(\d+)\)")
CHART_PLACEHOLDER_RE = re.compile(r"\(#insertChart:([^)]+)\)")
SAFE_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class ReportBundle:
    markdown_text: str
    infer_dir: str | None
    chart_dir: str | None


def _decode_base64_payload(payload: str, field_name: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ValueError(f"invalid base64 payload for {field_name}") from exc


def _validate_bundle_id(raw_value: object, field_name: str) -> str:
    value = "" if raw_value is None else str(raw_value).strip()
    if not value:
        return ""
    if not SAFE_BUNDLE_ID_RE.fullmatch(value):
        raise ValueError(
            f"invalid {field_name}: only letters, numbers, underscores, and hyphens are allowed"
        )
    return value


def _validate_message_list(final_result: dict, field_name: str) -> list[dict]:
    messages = final_result.get(field_name, [])
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ValueError(f"{field_name} must be a list")
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise ValueError(f"{field_name}[{index}] must be a dictionary")
    return messages


def _validate_final_result(final_result: dict) -> tuple[str, list[dict], list[dict]]:
    if not isinstance(final_result, dict):
        raise ValueError("final_result must be a dictionary")

    response_content = final_result.get("response_content", "") or ""
    if not isinstance(response_content, str):
        raise ValueError("response_content must be a string")
    if not response_content:
        raise ValueError("response_content is empty")

    infer_messages = _validate_message_list(final_result, "infer_messages")
    chart_messages = _validate_message_list(final_result, "chart_messages")
    return response_content, infer_messages, chart_messages


def _write_inference_html_files(
    report_base: Path,
    infer_messages: list[dict],
    *,
    max_infer_messages: int,
    max_single_html_base64_bytes: int,
) -> tuple[str | None, set[str]]:
    if not infer_messages:
        return None, set()

    infer_dir = report_base.parent / f"{report_base.name}_infer"
    infer_dir.mkdir(parents=True, exist_ok=True)
    infer_ids: set[str] = set()

    for item in infer_messages:
        if len(infer_ids) >= max_infer_messages:
            break

        infer_id = _validate_bundle_id(item.get("id", ""), "infer_messages[].id")
        html_base64 = item.get("html_base64", "")
        if not infer_id or not html_base64:
            continue
        if len(html_base64) > max_single_html_base64_bytes:
            continue

        html_bytes = _decode_base64_payload(
            html_base64,
            f"infer_messages[{infer_id}].html_base64",
        )
        (infer_dir / f"inference_{infer_id}.html").write_bytes(html_bytes)
        infer_ids.add(infer_id)

    return (str(infer_dir).replace("\\", "/") if infer_ids else None), infer_ids


def _write_chart_files(report_base: Path, chart_messages: list[dict]) -> tuple[str | None, set[str]]:
    if not chart_messages:
        return None, set()

    chart_dir = report_base.parent / f"{report_base.name}_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    chart_ids: set[str] = set()

    for item in chart_messages:
        chart_id = _validate_bundle_id(item.get("chart_id", ""), "chart_messages[].chart_id")
        chart_base64 = item.get("base64", "")
        if not chart_id or not chart_base64:
            continue

        chart_bytes = _decode_base64_payload(
            chart_base64,
            f"chart_messages[{chart_id}].base64",
        )
        (chart_dir / f"{chart_id}.png").write_bytes(chart_bytes)
        chart_ids.add(chart_id)

    return (str(chart_dir).replace("\\", "/") if chart_ids else None), chart_ids


def _rewrite_inference_links(report_content: str, infer_dir_name: str | None, infer_ids: set[str]) -> str:
    if not infer_dir_name or not infer_ids:
        return report_content

    def _replace(match: re.Match[str]) -> str:
        label = match.group(1)
        infer_id = match.group(2)
        if infer_id not in infer_ids:
            return label
        return f"[{label}]({infer_dir_name}/inference_{infer_id}.html)"

    return INFERENCE_LINK_RE.sub(_replace, report_content)


def _rewrite_chart_placeholders(
    report_content: str,
    chart_dir_name: str | None,
    chart_messages: list[dict],
    chart_ids: set[str],
) -> str:
    if not chart_dir_name or not chart_ids:
        return report_content

    chart_index: dict[str, dict] = {}
    for item in chart_messages:
        chart_id = _validate_bundle_id(item.get("chart_id", ""), "chart_messages[].chart_id")
        if chart_id:
            chart_index[chart_id] = item

    def _replace(match: re.Match[str]) -> str:
        chart_id = _validate_bundle_id(match.group(1), "chart placeholder")
        if chart_id not in chart_ids:
            return match.group(0)
        chart_info = chart_index.get(chart_id, {})
        label = (chart_info.get("chart_title", "") or "").strip() or chart_id
        return f"![{label}]({chart_dir_name}/{chart_id}.png)"

    return CHART_PLACEHOLDER_RE.sub(_replace, report_content)


def build_report_bundle(
    final_result: dict,
    report_base: str | Path,
    *,
    max_infer_messages: int = 20,
    max_single_html_base64_bytes: int = 5 * 1024 * 1024,
) -> ReportBundle:
    response_content, infer_messages, chart_messages = _validate_final_result(final_result)

    report_base_path = Path(report_base)
    infer_dir, infer_ids = _write_inference_html_files(
        report_base_path,
        infer_messages,
        max_infer_messages=max_infer_messages,
        max_single_html_base64_bytes=max_single_html_base64_bytes,
    )
    chart_dir, chart_ids = _write_chart_files(report_base_path, chart_messages)

    markdown_text = strip_internal_citation_markers(response_content)
    markdown_text = _rewrite_inference_links(
        markdown_text,
        Path(infer_dir).name if infer_dir else None,
        infer_ids,
    )
    markdown_text = _rewrite_chart_placeholders(
        markdown_text,
        Path(chart_dir).name if chart_dir else None,
        chart_messages,
        chart_ids,
    )
    return ReportBundle(
        markdown_text=markdown_text,
        infer_dir=infer_dir,
        chart_dir=chart_dir,
    )
