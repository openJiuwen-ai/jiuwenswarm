# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""网页通道出站脱敏。个人版和企业版共用这条路径，不按版本分叉。

开关和正则在 config.yaml 的 sensitive_mask。开关关闭时原文直出。
只改发给网页的副本，不改历史文件，也不改送给模型的上下文。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from jiuwenswarm.common.config import (
    dump_yaml_round_trip,
    get_config_file,
    get_merged_config_dict,
    load_yaml_round_trip,
)

logger = logging.getLogger(__name__)

_MAX_RULES = 32
_MAX_TITLE = 64
_MAX_PATTERN = 256
_TEXT_KEYS = ("content", "reasoning_content", "result", "raw_output", "summary", "error")
_TAIL_FIELDS = ("stream_source_id", "member_name", "member_action", "source_channel", "role")
_MAX_WALK_DEPTH = 8
_USER_ROLES = frozenset({"user", "human"})
_STREAM_EVENTS = frozenset({"chat.delta", "chat.reasoning"})
_COMPLETE_EVENTS = frozenset({"chat.final", "chat.tool_result", "chat.tool_update"})
_UNSAFE_PATTERN = re.compile(r"\(\.[*+]\)[*+{]")
_STREAMS: dict[tuple[str, str], Any] = {}
_COMPILED: tuple[tuple[str, ...], list[str]] | None = None
_IMPORT_WARNED = False


def _load_mask_types() -> tuple[Any, Any] | None:
    global _IMPORT_WARNED
    try:
        from security_mask.stream_mask import StreamSensitiveMask, mask_complete
    except ImportError:
        if not _IMPORT_WARNED:
            logger.warning("[sensitive_mask] security_mask 未安装，网页脱敏不生效")
            _IMPORT_WARNED = True
        return None
    return StreamSensitiveMask, mask_complete


def _section() -> dict[str, Any]:
    raw = get_merged_config_dict()
    section = raw.get("sensitive_mask") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return {"enabled": False, "rules": []}
    return section


def _rules_from_section(section: dict[str, Any]) -> list[dict[str, str]]:
    rules = section.get("rules")
    if not isinstance(rules, list):
        return []
    out: list[dict[str, str]] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        out.append({"title": title, "pattern": pattern})
    return out


def get_sensitive_mask_config() -> dict[str, Any]:
    section = _section()
    enabled = bool(section.get("enabled"))
    return {"enabled": enabled, "rules": _rules_from_section(section)}


def validate_sensitive_mask_rules(rules: list[Any]) -> list[dict[str, str]]:
    if len(rules) > _MAX_RULES:
        raise ValueError(f"规则不能超过 {_MAX_RULES} 条")
    cleaned: list[dict[str, str]] = []
    for item in rules:
        if not isinstance(item, dict):
            raise TypeError("每条规则必须包含 title 和 pattern")
        title = str(item.get("title") or "").strip()
        pattern = str(item.get("pattern") or "").strip()
        if not title:
            raise ValueError("规则名称不能为空")
        if len(title) > _MAX_TITLE:
            raise ValueError(f"规则名称不能超过 {_MAX_TITLE} 个字符")
        if not pattern or len(pattern) > _MAX_PATTERN:
            raise ValueError(f"正则长度必须在 1 到 {_MAX_PATTERN} 之间")
        if _UNSAFE_PATTERN.search(pattern):
            raise ValueError(f"正则可能卡死，已拒绝: {title}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"正则无法编译（{title}）: {exc}") from exc
        cleaned.append({"title": title, "pattern": pattern})
    return cleaned


def set_sensitive_mask_config(enabled: bool, rules: list[Any]) -> dict[str, Any]:
    cleaned = validate_sensitive_mask_rules(rules)
    path = get_config_file()
    data = load_yaml_round_trip(path)
    if not isinstance(data, dict):
        raise TypeError("config.yaml 不是对象")
    data["sensitive_mask"] = {"enabled": bool(enabled), "rules": cleaned}
    dump_yaml_round_trip(path, data)
    reset_sensitive_mask_state()
    return {"enabled": bool(enabled), "rules": cleaned}


def reset_sensitive_mask_state() -> None:
    global _COMPILED
    _STREAMS.clear()
    _COMPILED = None


def _active_patterns() -> list[str] | None:
    """开关关闭时返回 None，调用方原文直出。"""
    global _COMPILED
    section = _section()
    if not section.get("enabled"):
        if _STREAMS or _COMPILED is not None:
            _STREAMS.clear()
            _COMPILED = None
        return None
    rules = _rules_from_section(section)
    fingerprint = tuple(item["pattern"] for item in rules)
    if _COMPILED is not None and _COMPILED[0] == fingerprint:
        return _COMPILED[1]
    usable: list[str] = []
    for item in rules:
        pattern = item["pattern"]
        if len(pattern) > _MAX_PATTERN or _UNSAFE_PATTERN.search(pattern):
            logger.warning("[sensitive_mask] 跳过非法正则 title=%s", item["title"])
            continue
        try:
            re.compile(pattern)
        except re.error:
            logger.warning("[sensitive_mask] 跳过无法编译的正则 title=%s", item["title"])
            continue
        usable.append(pattern)
    _STREAMS.clear()
    _COMPILED = (fingerprint, usable)
    return usable


def _mask_string(text: str, patterns: list[str], mask_complete: Any) -> str:
    try:
        return mask_complete(text, patterns)
    except re.error:
        logger.warning("[sensitive_mask] 替换失败，本段原文直出", exc_info=True)
        return text


def _mask_value(value: Any, patterns: list[str], mask_complete: Any, depth: int = 0) -> Any:
    """替换字符串，并走进字典和列表。返回新对象，不改调用方手里的原对象。"""
    if isinstance(value, str):
        if not value:
            return value
        return _mask_string(value, patterns, mask_complete)
    if depth >= _MAX_WALK_DEPTH:
        return value
    if isinstance(value, list):
        return [_mask_value(item, patterns, mask_complete, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            key: _mask_value(item, patterns, mask_complete, depth + 1)
            for key, item in value.items()
        }
    return value


def _mask_known_fields(payload: dict[str, Any], patterns: list[str], mask_complete: Any) -> None:
    for key in _TEXT_KEYS:
        if key not in payload:
            continue
        payload[key] = _mask_value(payload[key], patterns, mask_complete)


def mask_history_message(message: dict[str, Any], patterns: list[str] | None = None) -> dict[str, Any]:
    """返回脱敏后的副本。用户自己发的内容不改。"""
    if patterns is None:
        patterns = _active_patterns()
    if not patterns:
        return message
    role = str(message.get("role") or "").strip().lower()
    if role in _USER_ROLES:
        return message
    loaded = _load_mask_types()
    if loaded is None:
        return message
    _, mask_complete = loaded
    copied = dict(message)
    _mask_known_fields(copied, patterns, mask_complete)
    return copied


def mask_history_records(records: list[Any]) -> list[Any]:
    patterns = _active_patterns()
    if not patterns:
        return records
    out: list[Any] = []
    for item in records:
        if isinstance(item, dict):
            out.append(mask_history_message(item, patterns))
        else:
            out.append(item)
    return out


def _stream_key(payload: dict[str, Any], event_name: str, request_id: str) -> tuple[str, str]:
    rid = str(payload.get("request_id") or request_id or payload.get("session_id") or "")
    return rid, event_name


def _drop_streams(request_id: str) -> None:
    if not request_id:
        stale = [key for key in _STREAMS if key[0] == ""]
    else:
        stale = [key for key in _STREAMS if key[0] == request_id]
    for key in stale:
        _STREAMS.pop(key, None)


def pop_final_tail_frames(payload: dict[str, Any], request_id: str = "") -> list[dict[str, Any]]:
    """流式收尾时把待匹配段吐成先于 chat.final 发送的帧。

    已经流过正文时，chat.final 的 content 经常是空的，页面沿用前面的 delta。
    若不在这里把尾巴发出去，末尾没有分割字符的敏感内容会一直留在缓冲里。
    """
    if not _active_patterns():
        return []
    session_id = str(payload.get("session_id") or "")
    rid = str(payload.get("request_id") or request_id or session_id)
    frames: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event_name in ("chat.delta", "chat.reasoning"):
        for key_id in (rid, session_id):
            if not key_id:
                continue
            key = (key_id, event_name)
            if key in seen:
                continue
            seen.add(key)
            masker = _STREAMS.pop(key, None)
            if masker is None:
                continue
            try:
                text = masker.finish()
            except re.error:
                logger.warning("[sensitive_mask] 收尾替换失败", exc_info=True)
                continue
            if not text:
                continue
            tail_payload: dict[str, Any] = {"content": text, "request_id": rid or key_id}
            if session_id:
                tail_payload["session_id"] = session_id
            for field in _TAIL_FIELDS:
                field_value = payload.get(field)
                if isinstance(field_value, str) and field_value:
                    tail_payload[field] = field_value
            frames.append({"type": "event", "event": event_name, "payload": tail_payload})
    return frames


def apply_web_event_mask(
    event_name: str,
    payload: dict[str, Any],
    request_id: str = "",
) -> None:
    """就地替换网页帧里要展示的文本。开关关闭时什么都不做。"""
    watched = _STREAM_EVENTS | _COMPLETE_EVENTS | {"history.message"}
    if event_name not in watched:
        return
    patterns = _active_patterns()
    if not patterns:
        return
    loaded = _load_mask_types()
    if loaded is None:
        return
    stream_cls, mask_complete = loaded

    if event_name in _STREAM_EVENTS:
        text = payload.get("content")
        if not isinstance(text, str) or text == "":
            return
        key = _stream_key(payload, event_name, request_id)
        masker = _STREAMS.get(key)
        if masker is None:
            if len(_STREAMS) > 128:
                _STREAMS.pop(next(iter(_STREAMS)))
            masker = stream_cls(patterns)
            _STREAMS[key] = masker
        try:
            payload["content"] = masker.push(text)
        except re.error:
            logger.warning("[sensitive_mask] 流式替换失败，本段原文直出", exc_info=True)
        return

    if event_name == "chat.final":
        _mask_known_fields(payload, patterns, mask_complete)
        _drop_streams(str(payload.get("request_id") or request_id or payload.get("session_id") or ""))
        return

    if event_name in _COMPLETE_EVENTS:
        _mask_known_fields(payload, patterns, mask_complete)
        nested = payload.get("tool_result")
        if isinstance(nested, dict):
            payload["tool_result"] = _mask_value(nested, patterns, mask_complete)
        return

    message = payload.get("message")
    if isinstance(message, dict):
        payload["message"] = mask_history_message(message, patterns)
