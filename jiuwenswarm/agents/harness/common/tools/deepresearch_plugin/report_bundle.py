# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.conversion_utils import (
    strip_internal_citation_markers,
)

INFERENCE_LINK_RE = re.compile(r"\[([^\]]+)\]\(#inference:(\d+)\)")
CHART_PLACEHOLDER_RE = re.compile(r"\(#insertChart:([^)]+)\)")
SAFE_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

RESPONSE_CONTENT_MAX_BYTES = 10 * 1024 * 1024
# Multi-section SDK reports can contain dozens of inference graphs and charts.
# Bound their counts separately from the per-field base64 and aggregate
# decoded-byte limits below; the byte limits are the real blast-containment.
INFER_MESSAGE_MAX = 512
CHART_MESSAGE_MAX = 512
BASE64_FIELD_MAX_BYTES = 5 * 1024 * 1024
DECODED_BINARY_TOTAL_MAX_BYTES = 100 * 1024 * 1024
CITATION_COUNT_MAX = 10_000
CITATION_FIELD_MAX_BYTES = 1024 * 1024
FINAL_RESULT_MAX_BYTES = 64 * 1024 * 1024
# Aggregate inference graphs JSON is written outside the snapshot byte budget,
# so bound it independently as well.
INFERENCE_GRAPH_MAX_BYTES = 64 * 1024 * 1024
MAX_NESTING_DEPTH = 64
MAX_CONTAINER_ITEMS = 100_000
MAX_TOTAL_NODES = 1_000_000


@dataclass(slots=True)
class ReportBundle:
    markdown_text: str
    infer_dir: str | None
    chart_dir: str | None
    citations: list[dict]
    inference_manifest: list[dict]
    chart_manifest: list[dict]
    final_result_snapshot: dict
    inference_graph_path: str | None = None
    inference_graph_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class _ResourcePayload:
    resource_id: str
    filename: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class _OwnedFile:
    path: Path
    device: int
    inode: int


@dataclass(slots=True)
class _PublishedGroup:
    directory: Path
    directory_device: int
    directory_inode: int
    owned_files: list[_OwnedFile]
    manifest: list[dict]


def _utf8_size(value: str, field_name: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc


def _decode_base64_payload(payload: object, field_name: str, input_limit: int) -> bytes:
    if not isinstance(payload, str) or not payload:
        raise ValueError(f"{field_name} must be a non-empty base64 string")
    if _utf8_size(payload, field_name) > input_limit:
        raise ValueError(f"{field_name} exceeds the base64 input limit")
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ValueError(f"invalid base64 payload for {field_name}") from exc


def _validate_bundle_id(raw_value: object, field_name: str) -> str:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int)):
        raise ValueError(f"{field_name} must be a string or integer")
    value = str(raw_value).strip()
    if not value or not SAFE_BUNDLE_ID_RE.fullmatch(value):
        raise ValueError(
            f"invalid {field_name}: only letters, numbers, underscores, and hyphens are allowed"
        )
    return value


def _validate_message_list(
    final_result: dict,
    field_name: str,
    hard_limit: int,
) -> list[dict]:
    messages = final_result.get(field_name, [])
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ValueError(f"{field_name} must be a list")
    if len(messages) > hard_limit:
        raise ValueError(f"{field_name} exceeds the message count limit")
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
    if _utf8_size(response_content, "response_content") > RESPONSE_CONTENT_MAX_BYTES:
        raise ValueError("response_content exceeds the UTF-8 byte limit")

    infer_messages = _validate_message_list(
        final_result, "infer_messages", INFER_MESSAGE_MAX
    )
    chart_messages = _validate_message_list(
        final_result, "chart_messages", CHART_MESSAGE_MAX
    )
    return response_content, infer_messages, chart_messages


def _extract_citations(final_result: dict) -> list[dict]:
    citation_messages = final_result.get("citation_messages") or {}
    if not isinstance(citation_messages, dict):
        raise ValueError("citation_messages must be a dictionary")
    citations = citation_messages.get("data") or []
    if (
        not isinstance(citations, list)
        or len(citations) > CITATION_COUNT_MAX
        or any(not isinstance(item, dict) for item in citations)
    ):
        raise ValueError("citation_messages.data must be a bounded list of dictionaries")
    return citations


def _json_scalar_size(value: object, label: str) -> int:
    if isinstance(value, str):
        _utf8_size(value, label)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} contains a non-finite float")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError(f"{label} contains a non-JSON value")
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _bounded_json_size(
    value: object,
    byte_limit: int,
    label: str,
    *,
    omitted_values: set[tuple[int, str]] | None = None,
) -> int:
    omitted_values = omitted_values or set()
    total_bytes = 0
    total_nodes = 0
    active_containers: set[int] = set()
    stack: list[tuple[object, int, bool, bool]] = [(value, 0, True, False)]
    while stack:
        current, depth, count_bytes, leaving = stack.pop()
        if leaving:
            active_containers.remove(id(current))
            continue
        total_nodes += 1
        if total_nodes > MAX_TOTAL_NODES:
            raise ValueError(f"{label} exceeds the node limit")
        if depth > MAX_NESTING_DEPTH:
            raise ValueError(f"{label} exceeds the nesting limit")
        if isinstance(current, dict):
            if id(current) in active_containers:
                raise ValueError(f"{label} contains a cyclic container")
            if len(current) > MAX_CONTAINER_ITEMS:
                raise ValueError(f"{label} exceeds the container item limit")
            active_containers.add(id(current))
            stack.append((current, depth, count_bytes, True))
            if count_bytes:
                total_bytes += 2 + max(0, len(current) - 1) + len(current)
            for key, item in reversed(tuple(current.items())):
                if not isinstance(key, str):
                    raise ValueError(f"{label} contains a non-string dictionary key")
                if count_bytes:
                    total_bytes += _json_scalar_size(key, label)
                omit = (id(current), key) in omitted_values
                stack.append((item, depth + 1, count_bytes and not omit, False))
        elif isinstance(current, list):
            if id(current) in active_containers:
                raise ValueError(f"{label} contains a cyclic container")
            if len(current) > MAX_CONTAINER_ITEMS:
                raise ValueError(f"{label} exceeds the container item limit")
            active_containers.add(id(current))
            stack.append((current, depth, count_bytes, True))
            if count_bytes:
                total_bytes += 2 + max(0, len(current) - 1)
            for item in reversed(current):
                stack.append((item, depth + 1, count_bytes, False))
        else:
            scalar_size = _json_scalar_size(current, label)
            if count_bytes:
                total_bytes += scalar_size
        if total_bytes > byte_limit:
            raise ValueError(f"{label} exceeds the UTF-8 byte limit")
    return total_bytes


def _validate_snapshot_input(
    final_result: dict,
    infer_messages: list[dict],
    chart_messages: list[dict],
    citations: list[dict],
) -> None:
    omitted_values = {
        *((id(item), "html_base64") for item in infer_messages),
        *((id(item), "base64") for item in chart_messages),
    }
    _bounded_json_size(
        final_result,
        FINAL_RESULT_MAX_BYTES,
        "final result snapshot",
        omitted_values=omitted_values,
    )
    for citation_index, citation in enumerate(citations):
        for field_name, field_value in citation.items():
            _bounded_json_size(
                field_value,
                CITATION_FIELD_MAX_BYTES,
                f"citation[{citation_index}].{field_name}",
            )


def _validate_compatibility_limit(value: object, hard_limit: int, field_name: str) -> int:
    if type(value) is not int or value < 0 or value > hard_limit:
        raise ValueError(f"{field_name} must be a bounded non-negative integer")
    return value


def _validated_resources(
    messages: list[dict],
    *,
    id_field: str,
    payload_field: str,
    filename: Callable[[str], str],
    message_limit: int,
    base64_limit: int,
) -> tuple[list[_ResourcePayload], int]:
    if len(messages) > message_limit:
        raise ValueError(f"{id_field} message count exceeds the configured limit")
    resources: list[_ResourcePayload] = []
    resource_ids: set[str] = set()
    decoded_total = 0
    for index, item in enumerate(messages):
        resource_id = _validate_bundle_id(
            item.get(id_field), f"{id_field}[{index}]"
        )
        if resource_id in resource_ids:
            raise ValueError(f"duplicate {id_field}: {resource_id}")
        payload = _decode_base64_payload(
            item.get(payload_field),
            f"{id_field}[{resource_id}].{payload_field}",
            base64_limit,
        )
        decoded_total += len(payload)
        if decoded_total > DECODED_BINARY_TOTAL_MAX_BYTES:
            raise ValueError("decoded binary payload exceeds the total limit")
        resource_ids.add(resource_id)
        resources.append(_ResourcePayload(resource_id, filename(resource_id), payload))
    return resources, decoded_total


def _validate_resources(
    infer_messages: list[dict],
    chart_messages: list[dict],
    *,
    max_infer_messages: int,
    max_single_html_base64_bytes: int,
) -> tuple[list[_ResourcePayload], list[_ResourcePayload]]:
    infer_resources, decoded_total = _validated_resources(
        infer_messages,
        id_field="id",
        payload_field="html_base64",
        filename=lambda resource_id: f"inference_{resource_id}.html",
        message_limit=max_infer_messages,
        base64_limit=max_single_html_base64_bytes,
    )
    chart_resources, chart_total = _validated_resources(
        chart_messages,
        id_field="chart_id",
        payload_field="base64",
        filename=lambda resource_id: f"{resource_id}.png",
        message_limit=CHART_MESSAGE_MAX,
        base64_limit=BASE64_FIELD_MAX_BYTES,
    )
    if decoded_total + chart_total > DECODED_BINARY_TOTAL_MAX_BYTES:
        raise ValueError("decoded binary payload exceeds the total limit")
    return infer_resources, chart_resources


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _unlink_if_owned(owned: _OwnedFile) -> None:
    quarantine = owned.path.with_name(
        f".{owned.path.name}.cleanup-{os.urandom(16).hex()}"
    )
    try:
        os.rename(owned.path, quarantine)
    except OSError:
        return
    try:
        metadata = os.lstat(quarantine)
    except OSError:
        return
    if _identity(metadata) != (owned.device, owned.inode):
        try:
            os.link(quarantine, owned.path, follow_symlinks=False)
        except OSError:
            return
        try:
            os.unlink(quarantine)
        except OSError:
            pass
        return
    try:
        os.unlink(quarantine)
    except OSError:
        pass


def _cleanup_group(group: _PublishedGroup) -> None:
    for owned in reversed(group.owned_files):
        _unlink_if_owned(owned)
    try:
        metadata = os.lstat(group.directory)
        if (
            _identity(metadata)
            != (group.directory_device, group.directory_inode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        ):
            return
        os.rmdir(group.directory)
    except OSError:
        pass


def _validate_owned_directory(
    group: _PublishedGroup, directory_fd: int | None
) -> None:
    named = os.lstat(group.directory)
    expected = (group.directory_device, group.directory_inode)
    valid = (
        _identity(named) == expected
        and stat.S_ISDIR(named.st_mode)
        and not stat.S_ISLNK(named.st_mode)
    )
    if directory_fd is not None:
        opened = os.fstat(directory_fd)
        valid = (
            valid
            and _identity(opened) == expected
            and stat.S_ISDIR(opened.st_mode)
        )
    if not valid:
        raise OSError("resource directory changed during publication")


def _open_directory_no_follow(directory: Path) -> int | None:
    if os.open not in getattr(os, "supports_dir_fd", set()):
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    return os.open(directory, flags, mode=0o700)


def _open_exclusive_resource(directory: Path, directory_fd: int | None, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        flags |= nofollow
    if directory_fd is not None:
        return os.open(name, flags, 0o600, dir_fd=directory_fd)

    path = directory / name
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(path)
    return os.open(path, flags, 0o600)


def _publish_resource_group(
    report_base: Path,
    suffix: str,
    resources: list[_ResourcePayload],
) -> _PublishedGroup | None:
    if not resources:
        return None
    directory = report_base.parent / f"{report_base.name}_{suffix}"
    os.mkdir(directory, 0o700)
    directory_metadata = os.lstat(directory)
    if not stat.S_ISDIR(directory_metadata.st_mode) or stat.S_ISLNK(
        directory_metadata.st_mode
    ):
        raise OSError("resource directory must be a direct directory")
    group = _PublishedGroup(
        directory,
        directory_metadata.st_dev,
        directory_metadata.st_ino,
        [],
        [],
    )
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory_no_follow(directory)
        _validate_owned_directory(group, directory_fd)
        for resource in resources:
            _validate_owned_directory(group, directory_fd)
            path = directory / resource.filename
            descriptor = _open_exclusive_resource(
                directory, directory_fd, resource.filename
            )
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise OSError("resource target must be an unlinked regular file")
                owned = _OwnedFile(path, opened.st_dev, opened.st_ino)
                group.owned_files.append(owned)
                view = memoryview(resource.payload)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("resource publication made no progress")
                    view = view[written:]
                os.fsync(descriptor)
                published = os.lstat(path)
                final = os.fstat(descriptor)
                invalid_resource = (
                    _identity(published) != _identity(final)
                    or not stat.S_ISREG(published.st_mode)
                    or published.st_nlink != 1
                    or final.st_nlink != 1
                    or published.st_size != len(resource.payload)
                    or final.st_size != len(resource.payload)
                )
                if invalid_resource:
                    raise OSError("resource target changed during publication")
                _validate_owned_directory(group, directory_fd)
            finally:
                os.close(descriptor)
            group.manifest.append(
                {
                    "id": resource.resource_id,
                    "path": f"{directory.name}/{resource.filename}",
                    "sha256": hashlib.sha256(resource.payload).hexdigest(),
                }
            )
        return group
    except BaseException:
        _cleanup_group(group)
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _externalize_binary_messages(
    final_result: dict,
    inference_manifest: list[dict],
    chart_manifest: list[dict],
) -> dict:
    snapshot = copy.deepcopy(final_result)
    inference_by_id = {str(item["id"]): item for item in inference_manifest}
    for item in snapshot.get("infer_messages") or []:
        item.pop("html_base64", None)
        artifact = inference_by_id.get(str(item.get("id", "")))
        if artifact:
            item["artifact_path"] = artifact["path"]
            item["artifact_sha256"] = artifact["sha256"]

    chart_by_id = {str(item["id"]): item for item in chart_manifest}
    for item in snapshot.get("chart_messages") or []:
        item.pop("base64", None)
        artifact = chart_by_id.get(str(item.get("chart_id", "")))
        if artifact:
            item["artifact_path"] = artifact["path"]
            item["artifact_sha256"] = artifact["sha256"]
    return snapshot


def serialize_final_result_snapshot(snapshot: dict) -> bytes:
    _bounded_json_size(snapshot, FINAL_RESULT_MAX_BYTES, "final result snapshot")
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > FINAL_RESULT_MAX_BYTES:
        raise ValueError("final result snapshot exceeds the UTF-8 byte limit")
    return payload


def _planned_manifest(
    report_base: Path,
    suffix: str,
    resources: list[_ResourcePayload],
) -> list[dict]:
    directory_name = f"{report_base.name}_{suffix}"
    return [
        {
            "id": resource.resource_id,
            "path": f"{directory_name}/{resource.filename}",
            "sha256": hashlib.sha256(resource.payload).hexdigest(),
        }
        for resource in resources
    ]


def _rewrite_inference_links(
    report_content: str, infer_dir_name: str | None, infer_ids: set[str]
) -> str:
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

    chart_index = {str(item["chart_id"]).strip(): item for item in chart_messages}

    def _replace(match: re.Match[str]) -> str:
        chart_id = _validate_bundle_id(match.group(1), "chart placeholder")
        if chart_id not in chart_ids:
            return match.group(0)
        chart_info = chart_index[chart_id]
        label = (chart_info.get("chart_title", "") or "").strip() or chart_id
        return f"![{label}]({chart_dir_name}/{chart_id}.png)"

    return CHART_PLACEHOLDER_RE.sub(_replace, report_content)


def build_report_bundle(
    final_result: dict,
    report_base: str | Path,
    *,
    max_infer_messages: int = INFER_MESSAGE_MAX,
    max_single_html_base64_bytes: int = BASE64_FIELD_MAX_BYTES,
) -> ReportBundle:
    max_infer_messages = _validate_compatibility_limit(
        max_infer_messages, INFER_MESSAGE_MAX, "max_infer_messages"
    )
    max_single_html_base64_bytes = _validate_compatibility_limit(
        max_single_html_base64_bytes,
        BASE64_FIELD_MAX_BYTES,
        "max_single_html_base64_bytes",
    )
    response_content, infer_messages, chart_messages = _validate_final_result(
        final_result
    )
    citations = _extract_citations(final_result)
    infer_resources, chart_resources = _validate_resources(
        infer_messages,
        chart_messages,
        max_infer_messages=max_infer_messages,
        max_single_html_base64_bytes=max_single_html_base64_bytes,
    )
    _validate_snapshot_input(
        final_result, infer_messages, chart_messages, citations
    )

    report_base_path = Path(report_base)
    planned_inference_manifest = _planned_manifest(
        report_base_path, "infer", infer_resources
    )
    planned_chart_manifest = _planned_manifest(
        report_base_path, "charts", chart_resources
    )
    final_result_snapshot = _externalize_binary_messages(
        final_result,
        planned_inference_manifest,
        planned_chart_manifest,
    )
    serialize_final_result_snapshot(final_result_snapshot)
    groups: list[_PublishedGroup] = []
    try:
        infer_group = _publish_resource_group(
            report_base_path, "infer", infer_resources
        )
        if infer_group is not None:
            groups.append(infer_group)
        chart_group = _publish_resource_group(
            report_base_path, "charts", chart_resources
        )
        if chart_group is not None:
            groups.append(chart_group)
    except BaseException:
        for group in reversed(groups):
            _cleanup_group(group)
        raise

    try:
        infer_dir = (
            str(groups[0].directory).replace("\\", "/")
            if infer_resources
            else None
        )
        chart_group = next(
            (group for group in groups if group.directory.name.endswith("_charts")),
            None,
        )
        chart_dir = (
            str(chart_group.directory).replace("\\", "/")
            if chart_group is not None
            else None
        )
        inference_manifest = groups[0].manifest if infer_resources else []
        chart_manifest = chart_group.manifest if chart_group is not None else []
        if (
            inference_manifest != planned_inference_manifest
            or chart_manifest != planned_chart_manifest
        ):
            raise OSError("published resource manifest changed")
        infer_ids = {resource.resource_id for resource in infer_resources}
        chart_ids = {resource.resource_id for resource in chart_resources}
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
        # Build inference graphs JSON — parse all inference HTMLs, merge, write
        graphs: list[dict] = []
        infer_msg_by_id = {str(im.get("id", "")): im for im in infer_messages}
        for resource in infer_resources:
            html_str = resource.payload.decode("utf-8", errors="replace")
            parsed = parse_inference_html(html_str)
            nodes = parsed.get("nodes", [])
            edges = parsed.get("edges", [])
            parse_error = parsed.get("parse_error")
            if not parse_error:
                nodes = _supplement_intermediate_nodes(nodes, edges)
                nodes = _enrich_citation_nodes(nodes, citations)
            inference_id = resource.resource_id
            msg = infer_msg_by_id.get(inference_id, {})
            graph: dict = {
                "inference_id": inference_id,
                "conclusion": msg.get("conclusion", ""),
                "inference": msg.get("inference", ""),
                "nodes": nodes,
                "edges": edges,
            }
            if parse_error:
                graph["parse_error"] = parse_error
            graphs.append(graph)
        if graphs:
            graph_data = {
                "schema_version": 1,
                "conversation_id": "",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "graphs": graphs,
            }
            graph_bytes = json.dumps(graph_data, ensure_ascii=False).encode("utf-8")
            if len(graph_bytes) > INFERENCE_GRAPH_MAX_BYTES:
                raise ValueError("inference_graphs exceeds the UTF-8 byte limit")
            graph_path = groups[0].directory / "inference_graphs.json"
            graph_path.write_bytes(graph_bytes)
            inference_graph_path = f"{groups[0].directory.name}/inference_graphs.json"
            inference_graph_bytes = graph_bytes
        else:
            inference_graph_path = None
            inference_graph_bytes = None
        return ReportBundle(
            markdown_text=markdown_text,
            infer_dir=infer_dir,
            chart_dir=chart_dir,
            citations=citations,
            inference_manifest=inference_manifest,
            chart_manifest=chart_manifest,
            final_result_snapshot=final_result_snapshot,
            inference_graph_path=inference_graph_path,
            inference_graph_bytes=inference_graph_bytes,
        )
    except BaseException:
        for group in reversed(groups):
            _cleanup_group(group)
        raise


# ---------------------------------------------------------------------------
# Node colour → type mapping (source: generate_html.py _select_show_info)
# ---------------------------------------------------------------------------
_NODE_COLOR_TO_TYPE: dict[str, str] = {
    "#e1cef0": "programmer_node",
    "#def0ce": "citation_node",
    "#d2e6f4": "conclusion_node",
    "#f6f6d2": "intermediate_node",
    "#f5c2c7": "final_conclusion_node",
}

# Edge label → type mapping (bilingual, case-insensitive)
_EDGE_LABEL_TO_TYPE: dict[str, str] = {
    "引用": "citation_edge",
    "refer": "citation_edge",
    "推理": "infer_edge",
    "infer": "infer_edge",
    "汇总": "combine_edge",
    "summ": "combine_edge",
}


def _extract_js_array(html: str, marker: str) -> str | None:
    """Locate ``marker`` in *html*, then extract the first ``[...]`` JSON
    array using string-aware bracket counting.  Returns the raw JSON string
    (including the outer brackets) or *None* if the marker or array cannot
    be found."""
    pos = html.find(marker)
    if pos == -1:
        return None
    pos = html.find("[", pos)
    if pos == -1:
        return None

    depth = 0
    in_string = False
    for i in range(pos, len(html)):
        ch = html[i]
        if in_string:
            if ch == '"' and (i == 0 or html[i - 1] != "\\"):
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in ("[", "{"):
            depth += 1
        elif ch in ("]", "}"):
            depth -= 1
            if depth == 0:
                return html[pos:i + 1]
    return None


def _map_node_type(node: dict) -> dict:
    """Add a ``type`` field to *node* based on its colour."""
    raw_color = node.get("color", "")
    if isinstance(raw_color, dict):
        colour = raw_color.get("background", "").lower()
    else:
        colour = str(raw_color).lower()
    node["type"] = _NODE_COLOR_TO_TYPE.get(colour, "unknown")
    return node


def _map_edge_type(edge: dict) -> dict:
    """Add a ``type`` field to *edge* based on its label."""
    label = str(edge.get("label", "")).lower().strip()
    edge["type"] = _EDGE_LABEL_TO_TYPE.get(label, "unknown")
    return edge


def parse_inference_html(html_content: str) -> dict:
    """Parse a pyvis-generated HTML string and return its nodes and edges.

    Returns a dictionary with the following keys:
    - ``nodes`` – list of node dicts, each with an added ``type`` field.
    - ``edges`` – list of edge dicts, each with an added ``type`` field.
    - ``parse_error`` – (only on failure) a human-readable error message.
    """
    try:
        nodes_raw = _extract_js_array(html_content, "nodes = new vis.DataSet(")
        edges_raw = _extract_js_array(html_content, "edges = new vis.DataSet(")

        if nodes_raw is None:
            return {"nodes": [], "edges": [], "parse_error": "nodes marker not found"}
        if edges_raw is None:
            return {"nodes": [], "edges": [], "parse_error": "edges marker not found"}

        nodes: list[dict] = json.loads(nodes_raw)
        edges: list[dict] = json.loads(edges_raw)

        return {
            "nodes": [_map_node_type(n) for n in nodes],
            "edges": [_map_edge_type(e) for e in edges],
        }
    except (ValueError, TypeError, IndexError) as exc:
        return {"nodes": [], "edges": [], "parse_error": str(exc)}


def _supplement_intermediate_nodes(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Enrich intermediate nodes with a human-readable label derived from
    their incoming *combine* edges.

    For each node where ``type == "intermediate_node"`` and ``label`` is
    empty or equal to the node's own ID (pyvis replaces empty labels with
    the integer node ID during JSON serialization), the function finds all
    incoming combine edges, collects the ``source_labels`` from the source
    nodes, and sets ``label`` to ``"汇总 N 条结论"``.

    The input *nodes* and *edges* lists are **not** modified — a deep copy
    is returned.
    """
    result = copy.deepcopy(nodes)
    node_by_id = {n["id"]: n for n in result}

    for node in result:
        if node.get("type") != "intermediate_node":
            continue
        # SDK creates intermediate nodes with label="" (empty string), but
        # pyvis replaces empty labels with the node's integer ID during
        # serialization.  Accept both forms as "no real label".
        label = node.get("label", "")
        node_id_str = str(node.get("id", ""))
        if label != "" and str(label) != node_id_str:
            continue

        from_ids = []
        for edge in edges:
            if edge.get("to") != node["id"]:
                continue
            if (
                edge.get("type") != "combine_edge"
                and edge.get("label") != "汇总"
            ):
                continue
            from_ids.append(edge["from"])
        source_labels = [
            node_by_id[from_id]["label"]
            for from_id in from_ids
            if from_id in node_by_id
        ]
        node["label"] = f"汇总 {len(source_labels)} 条结论"
        node["source_labels"] = source_labels

    return result


def _enrich_citation_nodes(nodes: list[dict], citations: list[dict]) -> list[dict]:
    """Enrich citation nodes with metadata from the citations list.

    For each node where ``type == "citation_node"`` and ``url`` is non-empty,
    the function looks up the URL in the citations list and sets the
    ``title``, ``source``, and ``publish_time`` fields. If the URL is not
    found, all three fields are set to empty string.

    The input *nodes* and *citations* lists are **not** modified — a deep
    copy is returned.
    """
    result = copy.deepcopy(nodes)
    url_to_meta = {
        c["url"]: {
            "title": c.get("title", ""),
            "source": c.get("source", ""),
            "publish_time": c.get("publish_time", ""),
        }
        for c in citations
        if c.get("url")
    }

    for node in result:
        if node.get("type") != "citation_node":
            continue
        url = node.get("url", "")
        if not url:
            continue

        meta = url_to_meta.get(url)
        if meta is not None:
            node["title"] = meta["title"]
            node["source"] = meta["source"]
            node["publish_time"] = meta["publish_time"]
        else:
            node["title"] = ""
            node["source"] = ""
            node["publish_time"] = ""

    return result
