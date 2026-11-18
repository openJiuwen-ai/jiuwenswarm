# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render bounded exception diagnostics without source-file or linecache IO."""

from collections import deque
from types import TracebackType


_MAX_EXCEPTIONS = 32
_MAX_DEPTH = 8
_MAX_FRAMES = 64
_MAX_FRAME_SCAN = 4096
_MAX_GROUP_WIDTH = 15
_MAX_NOTES = 8
_MAX_TEXT_BYTES = 64 * 1024


def _exception_text(value: object) -> str:
    try:
        text = str(value)
    except Exception:
        return "<exception str() failed>"
    if len(text) > _MAX_TEXT_BYTES or (
        not text.isascii() and len(text.encode("utf-8", errors="replace")) > _MAX_TEXT_BYTES
    ):
        # Do not keep a prefix that could contain a partially masked secret.
        return "[exception text omitted: exceeds UTF-8 byte limit]"
    return text


def _exception_stack(tb: TracebackType | None) -> list[str]:
    """Keep innermost locations without retaining frames or scanning forever."""
    locations: deque[tuple[str, int, str]] = deque(maxlen=_MAX_FRAMES)
    scanned = 0
    while tb is not None and scanned < _MAX_FRAME_SCAN:
        code = tb.tb_frame.f_code
        locations.append((code.co_filename, tb.tb_lineno, code.co_name))
        tb = tb.tb_next
        scanned += 1
    lines = []
    if scanned > _MAX_FRAMES:
        lines.append("  [additional frames omitted: showing innermost scanned locations]\n")
    lines.extend(f'  File "{filename}", line {lineno}, in {name}\n' for filename, lineno, name in locations)
    if tb is not None:
        lines.append("  [traceback scan limit reached; inner frames unavailable]\n")
    return lines


def _exception_notes(exc: BaseException) -> list[str]:
    """Treat broken metadata as unavailable, without rendering its failure."""
    try:
        notes = getattr(exc, "__notes__", ())
        if not isinstance(notes, (list, tuple)):
            return []
        lines = [f"{_exception_text(note)}\n" for note in notes[:_MAX_NOTES]]
        if len(notes) > _MAX_NOTES:
            lines.append("[additional exception notes omitted]\n")
        return lines
    except Exception:
        return ["[exception notes omitted: metadata unavailable]\n"]


def format_log_exception(
    exc_info: tuple[type[BaseException] | None, BaseException | None, TracebackType | None],
) -> str:
    """Keep locations, chains, groups and notes without looking up source lines.

    Even ``TracebackException(lookup_lines=False)`` checks linecache entries and
    can stat files. Walk only the supplied traceback and exception metadata;
    never capture locals or retain exception/frame references in the result.
    Callers must apply their log redaction policy to the returned text.
    """
    parts: list[str] = []
    active: set[int] = set()
    rendered = 0
    bytes_used = 0
    omitted = False

    def append(text: str) -> None:
        nonlocal bytes_used, omitted
        if omitted:
            return
        remaining = _MAX_TEXT_BYTES - bytes_used
        size = len(text)
        # Reject oversized text before allocating its UTF-8 encoding.
        if size <= remaining and not text.isascii():
            size = len(text.encode("utf-8", errors="replace"))
        if size > remaining:
            parts.clear()
            parts.append("[exception diagnostics omitted: exceeds UTF-8 byte limit]\n")
            omitted = True
        else:
            bytes_used += size
            parts.append(text)

    def render(exc: BaseException | None, tb: TracebackType | None, depth: int) -> None:
        nonlocal rendered
        if omitted:
            return
        if exc is None:
            append("NoneType: None\n")
            return
        if id(exc) in active:
            return
        if depth >= _MAX_DEPTH or rendered >= _MAX_EXCEPTIONS:
            append("[additional exceptions omitted]\n")
            return
        active.add(id(exc))
        rendered += 1
        prior = exc.__cause__
        separator = "\nThe above exception was the direct cause of the following exception:\n\n"
        if prior is None and not exc.__suppress_context__:
            prior = exc.__context__
            separator = "\nDuring handling of the above exception, another exception occurred:\n\n"
        if prior is not None and id(prior) not in active:
            render(prior, prior.__traceback__, depth + 1)
            append(separator)

        if tb is not None and not omitted:
            append("Traceback (most recent call last):\n")
            for line in _exception_stack(tb):
                append(line)

        cls = type(exc)
        name = cls.__qualname__
        if cls.__module__ not in ("builtins", "__main__"):
            name = f"{cls.__module__}.{name}"
        append(f"{name}: {_exception_text(exc)}\n")
        for line in _exception_notes(exc):
            append(line)
        if isinstance(exc, BaseExceptionGroup):
            for index, child in enumerate(exc.exceptions[:_MAX_GROUP_WIDTH], 1):
                if omitted or rendered >= _MAX_EXCEPTIONS:
                    append("[additional exceptions omitted]\n")
                    break
                append(f"  [group exception {index}/{len(exc.exceptions)}]\n")
                render(child, child.__traceback__, depth + 1)
            if len(exc.exceptions) > _MAX_GROUP_WIDTH:
                append("[additional group exceptions omitted]\n")
        active.remove(id(exc))

    render(exc_info[1], exc_info[2], 0)
    return "".join(parts)
