"""Train-only local retrieval comparison; gold is opened after all rankings exist."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import time

import numpy as np
import onnxruntime
import tokenizers

from .qasper_dense import LocalBGE, dense_rank, fuse_rankings, PREFIX
from .qasper_experiment import ParagraphRanker, make_request, evidence_f1, sha, write

METHODS = ('bm25', 'bge', 'hybrid_rrf60')


def summarize(rows):
    result = {}
    for method in METHODS:
        result[method] = {}
        for metric in ('evidence_f1', 'positive_reference_recall', 'complete_reference_coverage',
                       'selected_paragraphs', 'evidence_bytes'):
            values = [r[method][metric] for r in rows if r[method][metric] is not None]
            result[method][metric] = {'mean': float(np.mean(values)) if values else None, 'n': len(values)}
    return result


def paired_cluster_interval(rows, method, metric, draws=10000):
    groups = defaultdict(list)
    for row in rows:
        a, b = row['bm25'][metric], row[method][metric]
        if a is not None and b is not None:
            groups[row['paper_id']].append(b-a)
    if not groups:
        return {'delta': None, 'interval95': None, 'papers': 0, 'questions': 0}
    sums = np.array([sum(v) for _, v in sorted(groups.items())])
    counts = np.array([len(v) for _, v in sorted(groups.items())])
    sampled = np.random.default_rng(134).integers(0, len(sums), size=(draws, len(sums)))
    deltas = sums[sampled].sum(axis=1)/counts[sampled].sum(axis=1)
    return {'delta': float(sums.sum()/counts.sum()), 'interval95': np.quantile(deltas, [.025,.975]).tolist(),
            'papers': len(groups), 'questions': int(counts.sum()), 'draws': draws, 'seed': 134,
            'unit': 'paper clusters; question-weighted estimator; descriptive exploratory interval'}


def run(data, model_dir, output):
    data, model_dir, output = Path(data), Path(model_dir), Path(output)
    if output.exists():
        raise ValueError('Use a new output directory; do not silently overwrite results')
    papers = json.loads((data/'public-papers.json').read_text(encoding='utf-8'))
    questions = json.loads((data/'public-questions.json').read_text(encoding='utf-8'))
    if any(q['split'] != 'train' for q in questions):
        raise ValueError('Train-only evaluation')
    selected = sorted(papers, key=lambda pid: sha(('dense-20261004:'+pid).encode()))[:64]
    qs = [q for q in questions if q['paper_id'] in selected]
    output.mkdir(parents=True)
    (output/'embeddings').mkdir()
    (output/'retrieval').mkdir()
    source_files = [Path(__file__), Path(__file__).with_name('qasper_dense.py'),
                    Path(__file__).with_name('qasper_experiment.py'), Path(__file__).with_name('retrieval_diagnostics.py')]
    (output/'source-snapshot').mkdir()
    for p in source_files:
        (output/'source-snapshot'/p.name).write_bytes(p.read_bytes())
    plan = {'sample_seed': 'dense-20261004', 'selection': '64 paper IDs sorted by SHA256(seed+colon+ID); all their questions',
            'paper_ids': selected, 'question_ids': [q['question_id'] for q in qs], 'methods': METHODS,
            'split': 'train', 'k': 8, 'evidence_byte_limit': 12000, 'selection_policy': 'whole-paragraph prefix',
            'model': json.loads((model_dir/'manifest.json').read_text(encoding='utf-8')),
            'query_prefix': PREFIX, 'max_tokens': 512, 'pooling': 'CLS L2 normalized',
            'sources': {p.name: sha(p.read_bytes()) for p in source_files},
            'inputs': {name: sha((data/name).read_bytes()) for name in ('public-papers.json','public-questions.json','gold-references.json')},
            'gold_use': 'scoring only after retrieval; no tuning based on this run',
            'new_api_calls': 0, 'new_api_cost_cny': 0}
    write(output/'plan.json', plan)
    started = time.perf_counter()
    encoder = LocalBGE(model_dir)
    smoke = encoder.encode(['Paris is the capital of France.', 'Dogs bark and chase balls.'])
    smoke_query = encoder.encode(['What is the capital of France?'], query=True)
    similarity = (smoke@smoke_query[0]).tolist()
    if similarity[0] <= similarity[1]:
        raise ValueError('Semantic smoke test failed')
    encoder.truncated = 0
    total_paragraphs = 0
    retrievals = {}
    for index, pid in enumerate(selected):
        paper = papers[pid]
        subset = [q for q in qs if q['paper_id'] == pid]
        paragraphs = paper['paragraphs']
        ranker = ParagraphRanker(paragraphs)
        before = encoder.truncated
        vectors = encoder.encode([p['text'] for p in paragraphs])
        paragraph_truncation = encoder.truncated-before
        query_vectors = encoder.encode([q['question'] for q in subset], query=True)
        stem = sha(pid.encode())[:16]
        np.save(output/'embeddings'/f'{stem}-passages.npy', vectors, allow_pickle=False)
        np.save(output/'embeddings'/f'{stem}-queries.npy', query_vectors, allow_pickle=False)
        record = {'paper_id': pid, 'paragraph_ids': [p['id'] for p in paragraphs],
                  'question_ids': [q['question_id'] for q in subset], 'paragraphs_truncated': paragraph_truncation,
                  'queries_truncated': encoder.truncated-before-paragraph_truncation, 'questions': []}
        for q, vector in zip(subset, query_vectors):
            bm25 = ranker.rank(q['question'], 'bm25')
            dense = dense_rank(paragraphs, vectors, vector)
            rankings = {'bm25': bm25, 'bge': dense, 'hybrid_rrf60': fuse_rankings(bm25,dense)}
            item = {'question_id': q['question_id'], 'paper_id': pid, 'methods': {}}
            for method, ranking in rankings.items():
                request = make_request(q, paper, ranking, k=8, evidence_bytes=12000)
                item['methods'][method] = {'ranking': ranking, 'selected_ids': request['visible_ids'],
                                           'evidence_bytes': request['evidence_utf8_bytes']}
            record['questions'].append(item)
            retrievals[q['question_id']] = item
        write(output/'retrieval'/f'{stem}.json', record)
        total_paragraphs += len(paragraphs)
        print(f'{index+1}/64 papers; {len(retrievals)} questions; {time.perf_counter()-started:.1f}s', flush=True)
    # Gold annotations are loaded only now, after every method has completed retrieval.
    gold = json.loads((data/'gold-references.json').read_text(encoding='utf-8'))
    rows = []
    for q in qs:
        qid = q['question_id']; refs = gold[qid]
        positives = [set(r['evidence']) for r in refs if r['type'] != 'none' and r['evidence']]
        byid = {p['id']: p['text'] for p in papers[q['paper_id']]['paragraphs']}
        row = {'question_id': qid, 'paper_id': q['paper_id'], 'question': q['question'],
               'has_positive_reference': bool(positives), 'all_unanswerable': all(r['type']=='none' for r in refs),
               'has_float': any(r['float_evidence'] for r in refs),
               'has_unmapped': any(r['unmapped_text_evidence'] for r in refs)}
        for method, item in retrievals[qid]['methods'].items():
            evidence = [byid[pid] for pid in item['selected_ids']]
            seen = set(evidence)
            row[method] = {'evidence_f1': max(evidence_f1(evidence,r['evidence']) for r in refs),
                'positive_reference_recall': max(len(seen&r)/len(r) for r in positives) if positives else None,
                'complete_reference_coverage': float(any(r <= seen for r in positives)) if positives else None,
                'selected_paragraphs': len(evidence), 'evidence_bytes': item['evidence_bytes'],
                'selected_ids': item['selected_ids']}
        rows.append(row)
    result = {'kind': 'offline_train_retrieval_exploratory_not_answer_quality', 'papers': len(selected),
              'questions': len(qs), 'paragraphs': total_paragraphs, 'truncated_encodings': encoder.truncated,
              'seconds': time.perf_counter()-started, 'new_api_calls': 0, 'new_api_cost_cny': 0,
              'all_unanswerable': sum(r['all_unanswerable'] for r in rows),
              'questions_with_float': sum(r['has_float'] for r in rows),
              'questions_with_unmapped': sum(r['has_unmapped'] for r in rows),
              'scores': summarize(rows),
              'paired_vs_bm25': {method: {metric: paired_cluster_interval(rows,method,metric)
                for metric in ('evidence_f1','positive_reference_recall','complete_reference_coverage')}
                for method in METHODS[1:]},
              'runtime': {'numpy': np.__version__, 'onnxruntime': onnxruntime.__version__,
                          'tokenizers': tokenizers.__version__, 'providers': encoder.session.get_providers(),
                          'intra_threads': 4, 'inter_threads': 1, 'batch_size': 16},
              'smoke_cosines': similarity,
              'limits': ['Training-only exploratory sample; not independent confirmation or unseen evaluation.',
                         'Public pretrained model may have seen these papers; pretraining overlap is unknown.',
                         'Full evidence denominator retains figures/unmapped reference text.',
                         'Nonempty answerable reference recall uses the best-matching reference, not their union.',
                         '512-token right truncation for embeddings; retrieval passes whole paragraphs.',
                         'Same top8/12000-byte caps, different actual bytes and CPU costs.',
                         'Retrieval coverage is not generated answer F1 or human factual accuracy.']}
    write(output/'rows.json', rows)
    write(output/'summary.json', result)
    paths = sorted(p for p in output.rglob('*') if p.is_file())
    write(output/'artifact-hashes.json', {p.relative_to(output).as_posix(): sha(p.read_bytes()) for p in paths})
    print(json.dumps({'papers':len(selected),'questions':len(qs),'seconds':result['seconds'],'scores':result['scores']}),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.data, args.model, args.output)
