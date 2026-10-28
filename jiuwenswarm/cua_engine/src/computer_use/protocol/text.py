"""Immutable UTF-16 text pages, shared by legacy and native read contracts."""
from .primitives import record, safe_integer, utf16_length


def text_page(text, offset=0, length=4096):
    """Page an immutable local value using the same UTF-16 contract as drivers."""
    invalid_text_range = not isinstance(
        text, str) or not safe_integer(offset) or offset < 0 or not safe_integer(length) or not 2 <= length <= 16384
    if invalid_text_range:
        raise ValueError("Invalid captured text range")
    raw = text.encode("utf-16-le")
    total = len(raw) // 2
    if offset > total or offset < total and 0xDC00 <= int.from_bytes(raw[offset * 2:offset * 2 + 2],
                                                                     "little") <= 0xDFFF:
        raise ValueError("Text offset is outside the capture or inside a surrogate pair")
    end = min(total, offset + length)
    if end < total and 0xDC00 <= int.from_bytes(raw[end * 2:end * 2 + 2], "little") <= 0xDFFF:
        end -= 1
    return dict(text=raw[offset * 2:end * 2].decode("utf-16-le"), offset=offset, total=total,
                next=end if end < total else None)


def decode_text_page(value):
    page = record(value, ["text", "offset", "total", "next"])
    text, offset, total, next_offset = (page[k] for k in ("text", "offset", "total", "next"))
    valid = isinstance(text, str) and safe_integer(offset) and offset >= 0 and safe_integer(total) and total >= offset
    if valid:
        length = utf16_length(text)
        valid = length <= 16_384 and offset + length <= total
        if next_offset is None:
            valid = valid and offset + length == total
        else:
            valid = valid and safe_integer(next_offset) and next_offset == offset + \
                length and offset < next_offset < total
    if not valid:
        raise ValueError("protocol: invalid text page")
    return page
