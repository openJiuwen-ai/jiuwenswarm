"""Immutable UTF-16 text pages, shared by legacy and native read contracts."""
from .primitives import record, safeInteger, utf16Length


def textPage(text, offset=0, length=4096):
    """Page an immutable local value using the same UTF-16 contract as drivers."""
    if not isinstance(text, str) or not safeInteger(offset) or offset < 0 or not safeInteger(length) or not 2 <= length <= 16384:
        raise ValueError("Invalid captured text range")
    raw = text.encode("utf-16-le")
    total = len(raw) // 2
    if offset > total or offset < total and 0xDC00 <= int.from_bytes(raw[offset * 2:offset * 2 + 2], "little") <= 0xDFFF:
        raise ValueError("Text offset is outside the capture or inside a surrogate pair")
    end = min(total, offset + length)
    if end < total and 0xDC00 <= int.from_bytes(raw[end * 2:end * 2 + 2], "little") <= 0xDFFF:
        end -= 1
    return dict(text=raw[offset * 2:end * 2].decode("utf-16-le"), offset=offset, total=total,
                next=end if end < total else None)


def decodeTextPage(value):
    page = record(value, ["text", "offset", "total", "next"])
    text, offset, total, next_offset = (page[k] for k in ("text", "offset", "total", "next"))
    valid = isinstance(text, str) and safeInteger(offset) and offset >= 0 and safeInteger(total) and total >= offset
    if valid:
        length = utf16Length(text)
        valid = length <= 16_384 and offset + length <= total
        if next_offset is None:
            valid = valid and offset + length == total
        else:
            valid = valid and safeInteger(next_offset) and next_offset == offset + length and offset < next_offset < total
    if not valid:
        raise ValueError("protocol: invalid text page")
    return page
