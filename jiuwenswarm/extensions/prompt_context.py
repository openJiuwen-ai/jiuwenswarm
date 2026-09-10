from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Iterable


@dataclass(frozen=True)
class ExtensionPromptContext:
    """Server-owned prompt fragments produced by trusted extensions."""

    system_prompt_blocks: tuple[str, ...] = ()
    reference_context_blocks: tuple[str, ...] = ()


_LOCK = RLock()
_CONTEXTS: dict[str, ExtensionPromptContext] = {}


def _normalized_blocks(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    )


def set_extension_prompt_context(
    session_id: str,
    *,
    system_prompt_blocks: Iterable[str] = (),
    reference_context_blocks: Iterable[str] = (),
) -> ExtensionPromptContext:
    """Replace one session's extension-owned prompt context atomically."""

    normalized_session_id = str(session_id or "default")
    context = ExtensionPromptContext(
        system_prompt_blocks=_normalized_blocks(system_prompt_blocks),
        reference_context_blocks=_normalized_blocks(reference_context_blocks),
    )
    with _LOCK:
        if context.system_prompt_blocks or context.reference_context_blocks:
            _CONTEXTS[normalized_session_id] = context
        else:
            _CONTEXTS.pop(normalized_session_id, None)
    return context


def get_extension_prompt_context(session_id: str) -> ExtensionPromptContext:
    """Return an immutable snapshot for one model-call session."""

    normalized_session_id = str(session_id or "default")
    with _LOCK:
        return _CONTEXTS.get(normalized_session_id, ExtensionPromptContext())


def clear_extension_prompt_context(session_id: str) -> None:
    """Drop extension prompt data when a new request has no such context."""

    normalized_session_id = str(session_id or "default")
    with _LOCK:
        _CONTEXTS.pop(normalized_session_id, None)


__all__ = [
    "ExtensionPromptContext",
    "clear_extension_prompt_context",
    "get_extension_prompt_context",
    "set_extension_prompt_context",
]
