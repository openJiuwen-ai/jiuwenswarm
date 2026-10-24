# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Parse report citation destinations with CommonMark's balanced-link rules."""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from markdown_it.helpers import parseLinkDestination

_PREFIX = re.compile(r"\[\[(?P<index>\d+)\]\]\(")


@dataclass(frozen=True)
class CitationLink:
    index: str
    url: str
    start: int
    end: int


def parse_citation(
    source: str, start: int = 0, *, full: bool = False
) -> CitationLink | None:
    """Return a citation's source span without changing its original bytes."""
    prefix = _PREFIX.match(source, start)
    if prefix is None:
        return None
    destination = parseLinkDestination(source, prefix.end(), len(source))
    if not destination.ok or not destination.str.startswith(("http://", "https://")):
        return None
    if destination.pos >= len(source) or source[destination.pos] != ")":
        return None
    end = destination.pos + 1
    if full and end != len(source):
        return None
    return CitationLink(prefix.group("index"), destination.str, start, end)


def iter_citations(source: str) -> Iterator[CitationLink]:
    """Find citations while sharing the rewrite map's destination parser."""
    for prefix in _PREFIX.finditer(source):
        citation = parse_citation(source, prefix.start())
        if citation is not None:
            yield citation
