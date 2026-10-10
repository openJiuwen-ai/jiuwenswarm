"""Offline, post-hoc evidence coverage. Never changes frozen runs or calls models."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re

from .fulltext import search
from .fulltext_runner import FOLDER, validate_index
from .pipeline import audit, read, sha, put
from .review_scoring import FIELDS, reconcile

STOP = {'the','a','an','is','are','of','in','to','and','does','what','with'}

def terms(text):
    return [t for t in re.findall(r'[a-z0-9]+', text.lower()) if t not in STOP]

class BM25:
    """Fixed k1=1.2, b=.75; same tokenizer/stop words as frozen overlap baseline.

    API accepts question text only. Labels and gold locators are never search inputs.
    BM25 is a conventional baseline, not a proposed novel method.
    """
    def __init__(self, index, k1=1.2, b=.75):
        validate_index(index)
        if not math.isfinite(k1) or k1 <= 0 or not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError('Invalid BM25 parameters')
        self.rows = sorted((dict(c) for c in index['chunks']), key=lambda c:c['id'])
        self.counts = [Counter(terms(c['text'])) for c in self.rows]
        self.df = Counter(t for c in self.counts for t in c)
        self.lengths = [sum(c.values()) for c in self.counts]
        self.avg = sum(self.lengths)/len(self.rows) or 1
        self.k1, self.b = k1, b

    def search(self, question, top_k=8):
        if not isinstance(question, str) or type(top_k) is not int or top_k < 1:
            raise ValueError('Search takes a question string and positive integer k')
        query = set(terms(question)); hits=[]; n=len(self.rows)
        for row, count, length in zip(self.rows,self.counts,self.lengths):
            score=0.
            for term in sorted(query & count.keys()):
                freq=count[term]
                idf=math.log1p((n-self.df[term]+.5)/(self.df[term]+.5))
                score+=idf*freq*(self.k1+1)/(freq+self.k1*(1-self.b+self.b*length/self.avg))
            if score>0:
                hits.append({k:row[k] for k in ('id','document_id','page_1based')} | {'score':score})
        return sorted(hits,key=lambda h:(-h['score'],h['id']))[:top_k]

def normalized(text):
    # Only whitespace; no fuzzy matching or inference that a quote entails an answer.
    return ' '.join(text.split())

def coverage(case, rows, document_id):
    if not case['answerable']:
        return {'page_hit':None,'any_quote':None,'all_quotes':None,'quote_fraction':None}
    gold=case['evidence_quotes']
    if not gold or any(not normalized(q['quote']) for q in gold):
        raise ValueError('Answerable case has empty reference quotes')
    scoped=[r for r in rows if r['document_id']==document_id]
    found=[any(r['page_1based']==q['page_1based'] and normalized(q['quote']) in normalized(r['text'])
               for r in scoped) for q in gold]
    return {'page_hit':any(r['page_1based']==q['page_1based'] for r in scoped for q in gold),
            'any_quote':any(found),'all_quotes':all(found),'quote_fraction':sum(found)/len(found)}

def aggregate(rows):
    answerable=[r for r in rows if r['any_quote'] is not None]
    return {'all_cases':len(rows),'answerable_denominator':len(answerable),
            'page_hit_count':sum(r['page_hit'] for r in answerable),
            'any_quote_count':sum(r['any_quote'] for r in answerable),
            'all_quotes_count':sum(r['all_quotes'] for r in answerable),
            'mean_quote_fraction':sum(r['quote_fraction'] for r in answerable)/len(answerable) if answerable else None}

def agreement(a,b):
    pairs=[(x,y) for x,y in zip(a,b) if type(x) is bool and type(y) is bool]
    if len(a)!=len(b): raise ValueError('Unpaired ratings')
    if not pairs:return {'n':0,'excluded':len(a),'agreement':None,'kappa':None}
    n=len(pairs); observed=sum(x==y for x,y in pairs)/n
    p=sum(x for x,y in pairs)/n; q=sum(y for x,y in pairs)/n
    expected=p*q+(1-p)*(1-q)
    return {'n':n,'excluded':len(a)-n,'agreement':observed,
            'kappa':(observed-expected)/(1-expected) if expected<1 else None,
            'both_true':sum(x and y for x,y in pairs),'both_false':sum(not x and not y for x,y in pairs)}

def run(research,out):
    research=Path(research).resolve(); out=Path(out).resolve()
    if out.exists():raise ValueError('Use a new output folder; never overwrite diagnostics')
    verified=audit(research)  # Reuse frozen protocol, PDF and 1440 artifact checks.
    index=read(research/FOLDER/'corpus.json'); bm25=BM25(index)
    cases={c['case_id']:c for c in verified['cases']}
    docids={d['pdf_sha256']:d['id'] for d in index['documents']}
    byid={r['id']:r for r in index['chunks']}
    retrieval=[]; corpus_coverage=[]
    for case in cases.values():
        docid=docids[case['source_sha256']]
        corpus_coverage.append({'case_id':case['case_id'],**coverage(case,index['chunks'],docid)})
        for name,hits in [('frozen_overlap',search(index,case['question'],8)),('bm25',bm25.search(case['question'],8))]:
            for k in (2,8):
                rows=[byid[h['id']] for h in hits[:k]]
                retrieval.append({'case_id':case['case_id'],'retriever':name,'k':k,'ids':[r['id'] for r in rows],
                    **coverage(case,rows,docid)})
    openings=[]; gates=Counter()
    for record in verified['records']:
        case=cases[record['case_id']]
        rows=[byid[i] for i in record['opened_ids']]
        openings.append({k:record[k] for k in ('response_id','case_id','method','repetition')} |
                        coverage(case,rows,docids[case['source_sha256']]))
        gates.update(record['gate_issues'])
    forms=[read(research/'evaluations/formal-final'/name) for name in ('reviewer-A.json','reviewer-B.json','adjudications.json')]
    if any(len(form['answers'])!=len(verified['records']) for form in forms[:2]):
        raise ValueError('Missing or duplicate rating rows')
    reconciled=reconcile(verified['records'],*forms)
    rater_maps=[{r['response_id']:r for r in form['answers']} for form in forms[:2]]
    ids=[r['response_id'] for r in verified['records']]
    for mapping in rater_maps:
        if set(mapping)!=set(ids):raise ValueError('Missing/duplicate paired scores')
        for record in verified['records']:
            row=mapping[record['response_id']]
            if row['answer_sha256']!=record['answer_sha256']:raise ValueError('Unbound rating')
    agreements={field:agreement(*[[mapping[rid].get(field) for rid in ids] for mapping in rater_maps]) for field in FIELDS}
    summary={'kind':'posthoc_diagnostic_not_new_confirmatory_experiment','new_api_calls':0,
        'frozen_protocol_hash':verified['protocol_hash'],'verified_artifacts':verified['totals']['verified_artifacts'],
        'query_scope':'Same full 8-paper corpus and unchanged question for both retrievers; no gold labels passed to search.',
        'bm25_parameters':{'k1':1.2,'b':.75,'tokenizer':'frozen overlap ASCII words + same stoplist'},
        'mapping_definition':'Exact quote substring after whitespace collapse within matching PDF document/page; page hit alone is not supporting evidence.',
        'corpus_reference_coverage':aggregate(corpus_coverage),
        'retrieval':{f'{name}@{k}':aggregate([r for r in retrieval if r['retriever']==name and r['k']==k]) for name in ('frozen_overlap','bm25') for k in (2,8)},
        'historical_openings':{m:aggregate([r for r in openings if r['method']==m]) for m in ('A00','A10','A01','A11')},
        'gate_issue_counts':dict(gates),'paired_received_rating_agreement':agreements,
        'rating_basis':reconciled['basis'],
        'limits':['Historical test is now development evidence, not untouched validation.',
                  'Gold quotes are partial annotations; coverage is neither complete semantic recall nor QA accuracy.',
                  'Two repeats share question/source, not independent samples.',
                  'Agreement on received AI-assisted forms does not establish human independence or correctness.',
                  'BM25 results are retrieval only; no new model answers were generated.']}
    out.mkdir(parents=True)
    put(out/'summary.json',summary); put(out/'retrieval-cases.json',retrieval)
    put(out/'historical-openings.json',openings);put(out/'corpus-reference-mapping.json',corpus_coverage)
    inputs=[research/FOLDER/'corpus.json',research/FOLDER/'test-cases.pending.json',research/'workspace.json',Path(__file__)]
    inputs += list((research/'evaluations/formal-final').glob('*.json'))
    put(out/'manifest.json',{'inputs':{str(p):sha(p) for p in inputs},'outputs':{p.name:sha(p) for p in out.glob('*.json')}})
    return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(run(args.research,args.out),ensure_ascii=False,indent=2))
