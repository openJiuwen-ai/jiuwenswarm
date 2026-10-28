"""Wire primitives, including JavaScript/UTF-16 numeric and string boundaries.

These helpers preserve the existing protocol; they are not a JS evaluator.
"""
from __future__ import annotations

import math
import re
from types import MappingProxyType
from typing import Any, Mapping

MAX_SAFE_INTEGER = 9_007_199_254_740_991
REFERENCE = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)
ROLE = re.compile(r"[a-z][a-zA-Z0-9]*\Z", re.ASCII)
# ECMAScript WhiteSpace + LineTerminator; Python str.strip() also strips U+0085
# and U+001C..U+001F, but does not strip BOM. Those differences are observable.
_SPACE = ('\t\x0b\x0c \xa0\ufeff\n\r\u2028\u2029\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006'
          '\u2007\u2008\u2009\u200a\u202f\u205f\u3000')


def trim(value: str) -> str:
    return value.strip(_SPACE)


def trim_start(value: str) -> str:
    return value.lstrip(_SPACE)


def trim_end(value: str) -> str:
    return value.rstrip(_SPACE)


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def utf16_slice(value: str, start: int, end: int | None = None) -> str:
    data = value.encode("utf-16-le", errors="surrogatepass")
    return data[start * 2:end * 2 if end is not None else None].decode("utf-16-le", errors="surrogatepass")


def finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def safe_integer(value: Any) -> bool:
    return finite(value) and -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER and int(value) == value


def reference(value: Any) -> bool:
    return isinstance(value, str) and REFERENCE.fullmatch(value) is not None


def nonempty(value: Any, name: str) -> None:
    if not isinstance(value, str) or not trim(value):
        raise ValueError(f"{name} must be a nonempty string")


def record(value: Any, fields: list[str] | tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, (dict, MappingProxyType)) or set(value) != set(fields):
        raise ValueError("protocol: invalid object fields")
    return dict(value)


def frozen(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))
