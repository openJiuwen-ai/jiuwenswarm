# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The bundled icon set: ``search_icons`` finds an icon, ``insert_icon`` (a task pane tool) puts it on a slide.

The set is the 24 px regular and filled icons of Fluent UI System Icons (MIT, see ``fluent_icons/LICENSE``),
built by ``scripts/update_fluent_icons.py``. Icons are found by the words of their names and by the Chinese and
English aliases in ``fluent_icons/aliases.json``. Nothing is fetched from outside: the task pane gets the SVG of
the icon an ``insert_icon`` call names in the call's request.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure

ICON_DIR = Path(__file__).parent / "fluent_icons"
MAX_RESULTS = 20
_WORD = re.compile(r"[a-z0-9]+")
# An alias names its icon outright; a word of the query only has to appear in the icon's name.
_ALIAS_SCORE = 10
_NAME_WORD_SCORE = 3
_NAME_PREFIX_SCORE = 1


@dataclass(frozen=True)
class IconSet:
    """Each icon's SVG by name and style (``regular``, ``filled``), and the aliases of some icons."""

    icons: Mapping[str, Mapping[str, str]]
    aliases: Mapping[str, tuple[str, ...]]

    def search(self, query: str, top: int) -> list[dict[str, Any]]:
        """The ``top`` best icons for ``query``, best first; none when nothing in it names an icon."""
        ordered = _WORD.findall(query.lower())
        words = set(ordered)
        scores: dict[str, int] = {}
        for name, aliases in self.aliases.items():
            hits = sum(1 for alias in aliases if (alias in words if alias.isascii() else alias in query))
            if hits:
                scores[name] = _ALIAS_SCORE * hits
        exact = "_".join(ordered)
        if exact in self.icons:
            scores[exact] = scores.get(exact, 0) + _ALIAS_SCORE
        for name in self.icons:
            parts = name.split("_")
            score = sum(_NAME_WORD_SCORE if word in parts
                        else _NAME_PREFIX_SCORE if len(word) >= 3 and any(part.startswith(word) for part in parts)
                        else 0 for word in words)
            if score:
                scores[name] = scores.get(name, 0) + score
        ranked = sorted(scores, key=lambda name: (-scores[name], name.count("_"), name))
        return [{"icon": name, "styles": sorted(self.icons[name])} for name in ranked[:top]]

    def svg(self, name: str, style: str) -> str | None:
        return self.icons.get(name, {}).get(style)


@lru_cache(maxsize=1)
def icon_set() -> IconSet:
    with gzip.open(ICON_DIR / "fluent-24.json.gz", "rt", encoding="utf-8") as bundle:
        icons = json.load(bundle)["icons"]
    aliases = json.loads((ICON_DIR / "aliases.json").read_text("utf-8"))
    return IconSet(icons=icons, aliases={name: tuple(words) for name, words in aliases.items()})


class SearchIconsTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(
            name="search_icons",
            description=(
                "Find icons in the bundled line-art icon set (24 px, regular and filled styles) for insert_icon. "
                "Query with one short concept, in English or Chinese, such as \"team\", \"growth\", \"安全\" or "
                "\"物流\"; several unrelated words spread the matches thin. Returns the best matches first, each "
                "as {icon, styles}. An empty list means the set has nothing for that word: try a plainer synonym."
            ),
            input_params={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": "The concept the icon stands for."},
                    "top": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS,
                            "description": "How many matches to return; 5 when omitted."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            exposure=ToolExposure.DEFERRED,
        ))

    async def invoke(self, inputs: Any, **kwargs: Any) -> str:
        inputs = inputs or {}
        query = str(inputs.get("query") or "").strip()
        if not query:
            raise ValueError("query must name what the icon stands for")
        top = min(max(int(inputs.get("top") or 5), 1), MAX_RESULTS)
        return json.dumps({"icons": icon_set().search(query, top)}, ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield await self.invoke(inputs, **kwargs)
