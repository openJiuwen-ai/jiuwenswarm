from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .disclosure import build_disclosure_messages

from ..contracts import SubtreeRenderer
from ..types import CurrentSubtree, PromptBundle, SelectionProtocol


@dataclass(frozen=True)
class DefaultSubtreeRenderer(SubtreeRenderer):
    render_messages: bool = True

    def render_subtree(
        self,
        *,
        subtree: CurrentSubtree,
        query_messages: Sequence[dict[str, str]],
        protocol: SelectionProtocol,
    ) -> PromptBundle:
        messages = ()
        if self.render_messages:
            disclosure_messages = build_disclosure_messages(
                fragment=subtree.fragment,
                query_messages=query_messages,
                top_k=subtree.cursor.top_k,
            )
            messages = tuple(dict(message) for message in disclosure_messages)
        return PromptBundle(
            fragment=subtree.fragment,
            protocol=protocol,
            messages=messages,
        )
