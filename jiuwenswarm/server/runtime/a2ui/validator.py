# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A2UI schema and runtime semantic validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from jiuwenswarm.server.runtime.a2ui.parser import (
    coerce_message_list,
    iter_tagged_block_bodies,
)
from jiuwenswarm.server.runtime.a2ui.types import A2UIResponsePart, A2UIValidationResult


def _normalize_data_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip() or path == "/":
        return "/"
    return "/" + path.strip("/")


def _join_data_path(base_path: str, key: str) -> str:
    normalized_base = _normalize_data_path(base_path)
    if key == ".":
        return normalized_base
    if normalized_base == "/":
        return f"/{key}"
    return f"{normalized_base}/{key}"


def _validate_value_map_keys(entries: Any, path: str) -> None:
    if not isinstance(entries, list):
        return
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        if key in seen:
            raise ValueError(f"Duplicate dataModelUpdate key at {path}: {key}")
        seen.add(key)
        nested = entry.get("valueMap")
        if isinstance(nested, list):
            _validate_value_map_keys(nested, _join_data_path(path, key))


def _index_data_model_entries(
    entries: Any,
    path: str,
    index: dict[str, list[dict[str, Any]]],
) -> None:
    if not isinstance(entries, list):
        return
    index[_normalize_data_path(path)] = entries
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        nested = entry.get("valueMap")
        if isinstance(key, str) and isinstance(nested, list):
            _index_data_model_entries(nested, _join_data_path(path, key), index)


def _build_data_model_index(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        update = message.get("dataModelUpdate")
        if not isinstance(update, dict):
            continue
        path = _normalize_data_path(update.get("path", "/"))
        _index_data_model_entries(update.get("contents"), path, index)
    return index


def _iter_templates(value: Any) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        template = value.get("template")
        if isinstance(template, dict):
            templates.append(template)
        for nested in value.values():
            templates.extend(_iter_templates(nested))
    elif isinstance(value, list):
        for nested in value:
            templates.extend(_iter_templates(nested))
    return templates


def _iter_component_references(value: Any, component_ids: set[str]) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value in component_ids:
            refs.append(value)
    elif isinstance(value, dict):
        explicit_list = value.get("explicitList")
        if isinstance(explicit_list, list):
            refs.extend(item for item in explicit_list if isinstance(item, str) and item in component_ids)
        template = value.get("template")
        if isinstance(template, dict):
            component_id = template.get("componentId")
            if isinstance(component_id, str) and component_id in component_ids:
                refs.append(component_id)
        for nested in value.values():
            refs.extend(_iter_component_references(nested, component_ids))
    elif isinstance(value, list):
        for nested in value:
            refs.extend(_iter_component_references(nested, component_ids))
    return refs


def _component_subtree_ids(
    root_component_id: str,
    components_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    visited: set[str] = set()
    ordered: list[str] = []
    stack = [root_component_id]
    component_ids = set(components_by_id)
    while stack:
        component_id = stack.pop()
        if component_id in visited or component_id not in components_by_id:
            continue
        visited.add(component_id)
        ordered.append(component_id)
        refs = _iter_component_references(components_by_id[component_id], component_ids)
        stack.extend(reversed(refs))
    return ordered


def _iter_binding_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "dataBinding":
                continue
            if key == "path" and isinstance(nested, str):
                paths.append(nested)
                continue
            paths.extend(_iter_binding_paths(nested))
    elif isinstance(value, list):
        for nested in value:
            paths.extend(_iter_binding_paths(nested))
    return paths


def _component_has_template(value: Any) -> bool:
    return bool(_iter_templates(value))


def _is_valid_template_item_path(path: str) -> bool:
    if not path.startswith("/"):
        return True
    return path in {"/item", "/text", "/label"} or path.startswith(
        ("/item/", "/text/", "/label/")
    )


def _template_path_requires_object_item(path: str) -> bool:
    if path in {"", "."}:
        return False
    normalized = path
    for prefix in ("/item/", "./", "/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return bool(normalized and normalized not in {".", "item", "text", "label"})


def _entry_is_object_like(entry: dict[str, Any]) -> bool:
    if isinstance(entry.get("valueMap"), list):
        return True
    value = entry.get("valueString")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict)
    return False


def _iter_image_url_literals(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        image = value.get("Image")
        if isinstance(image, dict):
            url = image.get("url")
            if isinstance(url, dict) and isinstance(url.get("literalString"), str):
                urls.append(url["literalString"])
        for nested in value.values():
            urls.extend(_iter_image_url_literals(nested))
    elif isinstance(value, list):
        for nested in value:
            urls.extend(_iter_image_url_literals(nested))
    return urls


def _validate_image_runtime_semantics(messages: list[dict[str, Any]]) -> None:
    for url in _iter_image_url_literals(messages):
        if url.startswith("https://upload.wikimedia.org/wikipedia/commons/thumb/"):
            raise ValueError(
                "A2UI Image.url must use a stable image URL. Hard-coded Wikimedia "
                "thumbnail URLs are often guessed and can return 404; use a verified "
                "HTTPS image URL or a Wikimedia Commons Special:FilePath URL instead."
            )


def _validate_template_runtime_semantics(messages: list[dict[str, Any]]) -> None:
    data_model_index = _build_data_model_index(messages)
    for message in messages:
        update = message.get("dataModelUpdate")
        if isinstance(update, dict):
            _validate_value_map_keys(update.get("contents"), _normalize_data_path(update.get("path", "/")))

    for message in messages:
        surface_update = message.get("surfaceUpdate")
        if not isinstance(surface_update, dict):
            continue
        components = surface_update.get("components")
        if not isinstance(components, list):
            continue
        components_by_id = {
            component.get("id"): component
            for component in components
            if isinstance(component, dict) and isinstance(component.get("id"), str)
        }
        for component in components_by_id.values():
            for template in _iter_templates(component):
                template_component_id = template.get("componentId")
                data_binding = template.get("dataBinding")
                if not isinstance(template_component_id, str) or not isinstance(data_binding, str):
                    continue
                binding_path = _normalize_data_path(data_binding)
                paths: list[str] = []
                for component_id in _component_subtree_ids(template_component_id, components_by_id):
                    if _component_has_template(components_by_id[component_id]):
                        raise ValueError(
                            "A2UI v0.8 nested templates are not supported by the Web renderer; "
                            f"found a template inside template {template_component_id!r}. "
                            "Flatten repeated item content into fields on the outer item, or use "
                            "explicit child components inside the template."
                        )
                    component_paths = _iter_binding_paths(components_by_id[component_id])
                    for path in component_paths:
                        if not _is_valid_template_item_path(path):
                            raise ValueError(
                                "A2UI template component paths must be item-relative; "
                                f"found {path!r} under template {template_component_id!r}. "
                                "Use paths like 'name', 'price', or '/item/name' so the "
                                "React renderer can resolve the clicked item's data."
                            )
                    paths.extend(component_paths)

                entries = data_model_index.get(binding_path)
                if not entries or not any(_template_path_requires_object_item(path) for path in paths):
                    continue
                primitive_keys = [
                    entry.get("key")
                    for entry in entries
                    if isinstance(entry, dict) and not _entry_is_object_like(entry)
                ]
                if primitive_keys:
                    raise ValueError(
                        "A2UI template dataBinding must resolve to a collection of item objects; "
                        f"{binding_path} contains primitive entries {primitive_keys!r}. "
                        'Encode repeated items as one collection key with indexed valueMap entries, '
                        'for example key "0", key "1", each containing a nested valueMap.'
                    )


def validate_a2ui_messages(catalog: Any, messages: list[dict[str, Any]]) -> None:
    if not messages:
        raise ValueError("A2UI message list is empty")
    catalog.validator.validate(messages)
    _validate_template_runtime_semantics(messages)
    _validate_image_runtime_semantics(messages)


def validate_a2ui_response(
    content: str,
    *,
    parse_response: Callable[[str], list[A2UIResponsePart]],
    validate_messages: Callable[[list[dict[str, Any]]], None],
) -> A2UIValidationResult:
    try:
        tagged_blocks = iter_tagged_block_bodies(content or "")
        if tagged_blocks:
            for block_index, body in tagged_blocks:
                try:
                    parsed = json.loads(body.strip())
                except json.JSONDecodeError as exc:
                    return A2UIValidationResult(
                        valid=False,
                        error=(
                            f"A2UI block {block_index} at $: invalid JSON "
                            f"({exc.msg} at line {exc.lineno} column {exc.colno})"
                        ),
                    )
                messages = coerce_message_list(parsed)
                if messages is None:
                    return A2UIValidationResult(
                        valid=False,
                        error=(
                            f"A2UI block {block_index} at $: expected an A2UI "
                            "0.8 server-to-client message list"
                        ),
                    )
                try:
                    validate_messages(messages)
                except Exception as exc:  # noqa: BLE001
                    return A2UIValidationResult(
                        valid=False,
                        error=f"A2UI block {block_index}: {exc}",
                    )
            return A2UIValidationResult(valid=True)

        for part in parse_response(content):
            if part.kind == "a2ui":
                validate_messages(part.messages or [])
        return A2UIValidationResult(valid=True)
    except Exception as exc:  # noqa: BLE001
        return A2UIValidationResult(valid=False, error=str(exc))


__all__ = [
    "validate_a2ui_messages",
    "validate_a2ui_response",
]
