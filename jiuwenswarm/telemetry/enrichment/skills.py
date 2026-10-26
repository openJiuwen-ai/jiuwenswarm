from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .messages import _get_value


@dataclass(frozen=True)
class SkillObservation:
    name: str
    skill_id: str
    version: str = ""
    path: str = ""
    loaded: bool = False
    released: bool = False


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        attributes = vars(value)
    except (TypeError, ValueError):
        return {}
    return attributes if isinstance(attributes, Mapping) else {}


def _arguments(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return _mapping(parsed)
    return {}


def _wrapped_tool_inputs(inputs: Any) -> tuple[Any, ...]:
    """Return payload candidates from AgentCore's ``inputs=(args, kwargs)``."""
    try:
        if not isinstance(inputs, (tuple, list)) or len(inputs) != 2:
            return ()
        positional, keyword = inputs
    except Exception:
        return ()

    candidates: list[Any] = []
    keyword_inputs = _get_value(keyword, "inputs")
    if keyword_inputs is not None:
        candidates.append(keyword_inputs)
    if isinstance(keyword, Mapping):
        candidates.append(keyword)
    append_positional = False
    first_positional = None
    try:
        if isinstance(positional, (tuple, list)) and positional:
            first_positional = positional[0]
            append_positional = True
    except Exception:
        append_positional = False
    if append_positional:
        candidates.append(first_positional)
    return tuple(candidates)


def _tool_arguments(inputs: Any) -> Mapping[str, Any]:
    tool_call = _get_value(inputs, "tool_call")
    for candidate in (
        _get_value(tool_call, "arguments"),
        _get_value(inputs, "arguments"),
        _get_value(inputs, "tool_args"),
        *_wrapped_tool_inputs(inputs),
        inputs,
    ):
        parsed = _arguments(candidate)
        if any(
            _get_value(parsed, key) is not None
            for key in ("skill_name", "skill_id", "relative_file_path", "version")
        ):
            return parsed
    return {}


def _result_sources(result: Any) -> tuple[Mapping[str, Any], ...]:
    root = _mapping(result)
    data = _mapping(_get_value(result, "data"))
    skill = _mapping(_get_value(result, "skill"))
    data_skill = _mapping(_get_value(data, "skill"))
    return skill, data_skill, data, root


def _first_text(sources: tuple[Mapping[str, Any], ...], *names: str) -> str:
    for source in sources:
        for name in names:
            value = _get_value(source, name)
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:
                text = ""
            if text:
                return text
    return ""


def _skill_kind(tool_name: Any) -> str:
    try:
        normalized = str(tool_name or "").strip().lower()
    except Exception:
        return ""
    for separator in ("/", ":", "."):
        normalized = normalized.rsplit(separator, 1)[-1]
    return normalized if normalized in {"skill_tool", "skill_complete"} else ""


def _generated_skill_id(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"skill_{digest}"


def _truthy(value: Any) -> bool:
    try:
        return bool(value)
    except Exception:
        return False


def _has_error(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        return bool(value)
    except Exception:
        return True


def extract_skill(tool_name: Any, inputs: Any, result: Any) -> SkillObservation | None:
    kind = _skill_kind(tool_name)
    if not kind:
        return None

    tool_message = _get_value(inputs, "tool_msg")
    metadata = _mapping(_get_value(tool_message, "metadata"))
    if not metadata:
        metadata = _mapping(_get_value(inputs, "metadata"))
    arguments = _tool_arguments(inputs)
    result_sources = _result_sources(result)
    sources = (metadata, arguments, *result_sources)

    name = _first_text(sources, "skill_name", "name")
    if not name:
        return None
    skill_id = _first_text(sources, "skill_id", "id") or _generated_skill_id(name)
    version = _first_text(sources, "skill_version", "version")
    path = _first_text(sources, "relative_file_path", "file_path", "path")
    loaded = False
    if kind == "skill_tool":
        success = _get_value(result, "success")
        failed = success is False or _has_error(_get_value(result, "error"))
        metadata_loaded = _truthy(_get_value(metadata, "is_skill_body")) or _truthy(
            _get_value(metadata, "original_is_skill_body")
        )
        loaded = not failed and (metadata_loaded or success is True)
    return SkillObservation(
        name=name,
        skill_id=skill_id,
        version=version,
        path=path,
        loaded=loaded,
        released=kind == "skill_complete",
    )


__all__ = ["SkillObservation", "extract_skill"]
