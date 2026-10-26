# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Python script Unicode escape readability: normalize + artifact hook."""

from __future__ import annotations

import ast
import io
import logging
import re
import tokenize
from pathlib import Path
from tokenize import TokenInfo
from typing import Any

logger = logging.getLogger(__name__)

_ESCAPE_MARKERS = re.compile(r"\\(?:u|U|x)")
_PYTHON_SCRIPT_SUFFIXES = (".py", ".pyw")
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", ""})
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
_HOOK_REGISTERED = False


def is_code_unicode_readable_enabled(config: dict[str, Any] | None = None) -> bool:
    if config is None:
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
        except Exception:
            return True
    code_gen = config.get("code_generation") if isinstance(config, dict) else None
    if not isinstance(code_gen, dict):
        return True
    value = code_gen.get("unicode_readable", True)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_STRINGS
    return bool(value)


def _string_prefix(token_str: str) -> str:
    idx = 0
    while idx < len(token_str) and token_str[idx] in "fFbBrRuU":
        idx += 1
    return token_str[:idx]


def _should_skip_string_token(token_str: str) -> bool:
    prefix = _string_prefix(token_str).lower()
    return "f" in prefix or "b" in prefix or "r" in prefix


def _contains_lone_surrogate(value: str) -> bool:
    """Return True when *value* has UTF-16 surrogate code units (not valid in UTF-8)."""
    return any(_SURROGATE_MIN <= ord(ch) <= _SURROGATE_MAX for ch in value)


def _quote_python_string(value: str) -> str:
    parts = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\n":
            parts.append("\\n")
        elif ch == "\t":
            parts.append("\\t")
        elif ch == "\\":
            parts.append("\\\\")
        elif ch == '"':
            parts.append('\\"')
        elif ch == "\r":
            parts.append("\\r")
        elif code < 32 or (127 <= code <= 159):
            parts.append(f"\\x{code:02x}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def normalize_python_source_unicode_literals(source: str) -> tuple[str, int]:
    """Decode \\u/\\U/\\x in Python string literals to readable Unicode text."""
    if not source:
        return source, 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        # IndentationError/TabError (SyntaxError subclasses) also escape generate_tokens
        # on LLM-generated code with mixed tabs/spaces or inconsistent indent.
        return source, 0

    modified: list[TokenInfo] = []
    replacements = 0
    for tok in tokens:
        if tok.type != tokenize.STRING or _should_skip_string_token(tok.string):
            modified.append(tok)
            continue
        if not _ESCAPE_MARKERS.search(tok.string):
            modified.append(tok)
            continue
        try:
            value = ast.literal_eval(tok.string)
        except (ValueError, SyntaxError):
            modified.append(tok)
            continue
        if not isinstance(value, str) or not any(ord(ch) > 127 for ch in value):
            modified.append(tok)
            continue
        if _contains_lone_surrogate(value):
            modified.append(tok)
            continue
        new_token = _quote_python_string(value)
        if new_token == tok.string:
            modified.append(tok)
            continue
        replacements += 1
        modified.append(TokenInfo(tok.type, new_token, tok.start, tok.end, tok.line))

    if replacements == 0:
        return source, 0
    try:
        return tokenize.untokenize(modified), replacements
    except (tokenize.TokenError, TypeError) as exc:
        logger.debug("[code_source_unicode] untokenize failed: %s", exc)
        return source, 0


def normalize_python_script_file(path: str | Path) -> int:
    file_path = Path(path)
    if file_path.suffix.lower() not in _PYTHON_SCRIPT_SUFFIXES:
        return 0
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("[code_source_unicode] skip read %s: %s", file_path, exc)
        return 0
    normalized, count = normalize_python_source_unicode_literals(source)
    if count <= 0 or normalized == source:
        return 0
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        logger.warning(
            "[code_source_unicode] skip write (not utf-8 encodable) path=%s error=%s",
            file_path,
            exc,
        )
        return 0
    try:
        file_path.write_text(normalized, encoding="utf-8", newline="\n")
    except (OSError, UnicodeEncodeError) as exc:
        logger.warning("[code_source_unicode] write failed path=%s error=%s", file_path, exc)
        return 0
    return count


async def _normalize_code_artifact_hook(ctx: Any) -> None:
    if not is_code_unicode_readable_enabled():
        return
    for raw_path in getattr(ctx, "artifact_paths", []) or []:
        path = str(raw_path or "").strip()
        if not path:
            continue
        count = normalize_python_script_file(path)
        if count > 0:
            logger.info(
                "[code_source_unicode] normalized path=%s count=%d",
                path,
                count,
            )


def register_code_source_unicode_hook() -> None:
    global _HOOK_REGISTERED
    if _HOOK_REGISTERED:
        return
    try:
        from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
        from jiuwenswarm.extensions.registry import ExtensionRegistry
    except ImportError as exc:
        logger.warning("[code_source_unicode] skip hook registration: %s", exc)
        return
    try:
        registry = ExtensionRegistry.get_instance()
    except RuntimeError as exc:
        logger.warning("[code_source_unicode] skip hook registration: %s", exc)
        return
    registry.register(
        AgentServerHookEvents.ARTIFACT_POST_PROCESS,
        _normalize_code_artifact_hook,
        priority=50,
    )
    _HOOK_REGISTERED = True
    logger.info("[code_source_unicode] registered ARTIFACT_POST_PROCESS hook")
