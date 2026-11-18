# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render bounded exception diagnostics without source-file or linecache IO."""

from types import TracebackType


_MAX_EXCEPTIONS = 32
_MAX_DEPTH = 8
_MAX_FRAMES = 64
_MAX_GROUP_WIDTH = 15
_MAX_NOTES = 8
_MAX_TEXT_CHARACTERS = 64 * 1024


def _exception_text(value: object) -> str:
    try:
        text = str(value)
    except Exception:
        return "<exception str() failed>"
    if len(text) > _MAX_TEXT_CHARACTERS:
        # Do not keep a prefix that could contain a partially masked secret.
        return "[exception text omitted: exceeds character limit]"
    return text


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
    characters = 0
    omitted = False

    def append(text: str) -> None:
        nonlocal characters, omitted
        if omitted:
            return
        characters += len(text)
        if characters > _MAX_TEXT_CHARACTERS:
            parts.clear()
            parts.append("[exception diagnostics omitted: exceeds character limit]\n")
            omitted = True
        else:
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

        if tb is not None:
            append("Traceback (most recent call last):\n")
            for _ in range(_MAX_FRAMES):
                if tb is None or omitted:
                    break
                code = tb.tb_frame.f_code
                append(f'  File "{code.co_filename}", line {tb.tb_lineno}, in {code.co_name}\n')
                tb = tb.tb_next
            if tb is not None:
                append("  [additional frames omitted]\n")

        cls = type(exc)
        name = cls.__qualname__
        if cls.__module__ not in ("builtins", "__main__"):
            name = f"{cls.__module__}.{name}"
        append(f"{name}: {_exception_text(exc)}\n")
        notes = getattr(exc, "__notes__", ())
        if isinstance(notes, (list, tuple)):
            for note in notes[:_MAX_NOTES]:
                append(f"{_exception_text(note)}\n")
            if len(notes) > _MAX_NOTES:
                append("[additional exception notes omitted]\n")
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
