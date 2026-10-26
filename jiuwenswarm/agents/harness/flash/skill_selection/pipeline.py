"""Return candidates only. No score threshold or semantic auto-selection."""
from dataclasses import dataclass, field
import time
from .bm25 import BM25Retriever


@dataclass(frozen=True)
class SelectionResult:
    candidates: tuple = ()
    elapsed_ms: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    generation: int = 0


class SelectionPipeline:
    def __init__(self, settings, retriever=None):
        self.settings = settings
        self.documents = {}
        self.retriever = retriever or BM25Retriever(k1=settings.k1, b=settings.b,
                                                   name_weight=settings.name_weight)

    def prepare(self, documents):
        self.documents = {d.id: d for d in documents}
        self.retriever.prepare(documents)

    def search(self, query, keywords, *, allowed_ids):
        start = time.perf_counter()
        ranked = self.retriever.search(query, keywords, limit=self.settings.candidate_k,
                                       allowed_ids=allowed_ids)
        return SelectionResult(
            candidates=tuple((self.documents[c.skill_id], c.score) for c in ranked),
            elapsed_ms={'bm25': (time.perf_counter() - start) * 1000},
            diagnostics={'catalog_count': len(self.documents), 'allowed_count': len(allowed_ids),
                         'reason': 'candidates' if ranked else 'no_overlap'},
        )
