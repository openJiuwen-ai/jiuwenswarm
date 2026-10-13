"""Public-only bounded paragraph selection. Conventional heuristic, not novel RAG."""
import math

from .qasper_experiment import ParagraphRanker, make_request
from .retrieval_diagnostics import terms


def select(question, paper, mode, k=8, evidence_bytes=12000, ranker=None):
    """Compare prefix, scan-to-fit, and lexical coverage under identical ceilings.

    Coverage is query-token coverage, NOT verified semantic fact coverage. Fixed
    weights .65 relevance/.35 new-term coverage; no gold or parameter fitting.
    """
    if mode not in ('prefix', 'pack', 'coverage'):
        raise ValueError('Unknown selector')
    if type(k) is not int or k < 1 or type(evidence_bytes) is not int or evidence_bytes < 1:
        raise ValueError('Invalid budget')
    ranker = ranker or ParagraphRanker(paper['paragraphs'])
    ranking = ranker.rank(question['question'], 'bm25')
    if mode == 'prefix':
        return make_request(question, paper, ranking, k, evidence_bytes)
    byid = {p['id']: p for p in paper['paragraphs']}
    remaining = list(ranking)
    query = set(terms(question['question']))
    weights = {t: math.log1p((len(ranker.rows)-ranker.df[t]+.5)/(ranker.df[t]+.5)) for t in query}
    total_weight = sum(weights.values()) or 1
    matched = {r['id']: query & set(terms(byid[r['id']]['text'])) for r in ranking}
    maximum = ranking[0]['score'] if ranking else 1
    covered, chosen, used = set(), [], 0
    while remaining and len(chosen) < k:
        fitting = [r for r in remaining if used+len(byid[r['id']]['text'].encode()) <= evidence_bytes]
        if not fitting:
            break
        if mode == 'pack':
            hit = fitting[0]
        else:
            # ponytail: lexical diversity only; semantic fact extraction requires a separately controlled study.
            def utility(r):
                gain = sum(weights[t] for t in sorted(matched[r['id']]-covered))/total_weight
                return .65*r['score']/maximum + .35*gain
            hit = min(fitting, key=lambda r: (-utility(r), -r['score'], r['id']))
        chosen.append(hit)
        covered.update(matched[hit['id']])
        used += len(byid[hit['id']]['text'].encode())
        remaining.remove(hit)
    return make_request(question, paper, chosen, k, evidence_bytes)


COMPLETENESS = (
    ' Before answering, identify exactly what the question asks: the entities, '
    'qualifiers, comparisons and number of items requested. Check all supplied '
    'paragraphs for each requested part. When evidence lists multiple relevant '
    'items, include all supported requested items, not just the first one. '
    'Do not infer that evidence exists simply because a question asks for it. '
    'If a necessary part cannot be supported, keep the same Unanswerable rule. '
    'Return only the existing JSON schema, without your checking notes.'
)


def checked_request(request):
    import copy
    result = copy.deepcopy(request)
    result['messages'][0]['content'] += COMPLETENESS
    result['content_utf8_bytes'] = sum(len(m['content'].encode()) for m in result['messages'])
    result['prompt_version'] = 'completeness-v1'
    return result
