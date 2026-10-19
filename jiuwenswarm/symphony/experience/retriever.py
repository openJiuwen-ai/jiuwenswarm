from __future__ import annotations

from .bank import ExperienceBank


class ExperienceRetriever:
    """Fast-path retriever that queries the experience knowledge base.

    On hit: returns skill_ids from the matched experience item.
    On miss: returns None so the caller falls through to tree retrieval.
    """

    def __init__(
        self,
        kb: ExperienceBank,
        *,
        threshold: float = 0.80,
        top_k: int = 1,
    ) -> None:
        self._kb = kb
        self._threshold = threshold
        self._top_k = top_k

    def search(self, query: str) -> list[str]:
        """Search the experience KB."""
        results = self._kb.search_by_embedding(
            query, top_k=self._top_k, threshold=self._threshold
        )
        if not results:
            return []
        return list(dict.fromkeys(
            skill_id for result in results for skill_id in result[1].skill_ids
        ))


__all__ = ["ExperienceRetriever"]
