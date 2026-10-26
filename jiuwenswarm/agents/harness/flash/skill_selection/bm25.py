"""Bilingual lexical ranking; translations come from the main LLM's keywords."""
import re
import unicodedata
import math
from collections import Counter

from .types import Candidate


class BM25Okapi:
    """Local lexical index; no dependency on another mode's retrieval package."""

    def __init__(self, corpus_tokens, k1=0.9, b=0.4):
        self.k1, self.b = k1, b
        self.tf = [Counter(document) for document in corpus_tokens]
        self.lengths = [sum(counts.values()) for counts in self.tf]
        self.avgdl = sum(self.lengths) / len(self.lengths) if self.lengths else 0
        frequencies = Counter(term for counts in self.tf for term in counts)
        self.idf = {term: math.log(1 + (len(self.tf) - count + 0.5) / (count + 0.5))
                    for term, count in frequencies.items()}
        self.postings = {}
        self.normalizers = [self.k1 * (1 - self.b + self.b * (length / self.avgdl if self.avgdl else 0))
                            for length in self.lengths]
        for i, counts in enumerate(self.tf):
            for term, frequency in counts.items():
                self.postings.setdefault(term, []).append((i, frequency))

    def get_scores(self, tokens):
        scores = [0.0] * len(self.tf)
        for term in tokens:
            if term not in self.idf:
                continue
            for i, frequency in self.postings[term]:
                denominator = frequency + self.normalizers[i]
                scores[i] += self.idf[term] * frequency * (self.k1 + 1) / denominator
        return scores


_STOP = frozenset(
    'a an the to of for in on and or with from by as is are be this that it its '
    'use using can your you our when needs need please help me my'.split()
)


def tokenize(text):
    result = []
    for word in re.findall(r'[a-z0-9]+|[一-龥]+', unicodedata.normalize('NFKC', text).lower()):
        if re.fullmatch(r'[一-龥]+', word):
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        elif word not in _STOP:
            if len(word) > 4 and word.endswith('ies'):
                word = word[:-3] + 'y'
            elif len(word) > 4 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
                word = word[:-1]
            result.append(word)
    return result


class BM25Retriever:
    def __init__(self, *, k1=0.9, b=0.4, name_weight=0.5):
        self.k1, self.b, self.name_weight = k1, b, name_weight
        self.documents = ()
        self.index = None
        self.names = None

    def prepare(self, documents):
        self.documents = tuple(documents)
        self.index = BM25Okapi([tokenize(d.text) for d in documents], k1=self.k1, b=self.b)
        self.names = BM25Okapi([tokenize(d.name) for d in documents], k1=self.k1, b=self.b)

    def search(self, query, keywords=(), *, limit, allowed_ids):
        terms = list(dict.fromkeys(tokenize(query + ' ' + ' '.join(keywords))))
        chinese = [t for t in terms if re.search(r'[一-龥]', t)]
        english = [t for t in terms if not re.search(r'[一-龥]', t)]
        scores = [0.0] * len(self.documents)
        for index, group, weight in ((self.index, chinese, 1), (self.index, english, 1),
                                     (self.names, terms, self.name_weight)):
            denominator = sum(index.idf.get(t, 0) for t in group)
            if denominator and weight:
                for i, score in enumerate(index.get_scores(group)):
                    scores[i] += weight * score / denominator
        ranked = [Candidate(d.id, score) for d, score in zip(self.documents, scores)
                  if d.id in allowed_ids and score > 0]
        return sorted(ranked, key=lambda item: (-item.score, item.skill_id))[:limit]
