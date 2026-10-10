"""Extractive bounded evidence memory prototype; source offsets remain auditable.

Single-task compression only, not persistent/self-evolving memory or a novelty claim.
"""
import copy
import json
import re
from pathlib import Path
from .qasper_experiment import ParagraphRanker,sha
from .qasper_dense_bundle import reconstructed
from .retrieval_diagnostics import terms

METHODS=('memory-full','memory-plain','memory-qualifiers')
TAG_PATTERNS={
    'scope':r'\b(only|except|excluding|including|all|both|each|additional|also|respectively)\b',
    'quantity':r'\b\d+(?:\.\d+)?\b|%',
    'contrast':r'\b(but|however|whereas|rather|instead|unlike|compared)\b',
    'condition':r'\b(if|when|unless|under|without|provided)\b',
}

def fragments(paragraphs):
    rows=[]
    for p in paragraphs:
        # ponytail: conservative punctuation split; abbreviations can over-split.
        # Offsets preserve every selected character; semantic segmentation is unclaimed.
        for match in re.finditer(r'\S[\s\S]*?(?:[.!?](?=\s|$)|$)',p['text']):
            start,end=match.span();text=p['text'][start:end]
            if not text.strip():continue
            rows.append({'id':f'{p["id"]}@{start}:{end}','parent_id':p['id'],
                'start':start,'end':end,'text':text,'sha256':sha(text.encode()),
                'source_sha256':sha(p['text'].encode()),
                'tags':[key for key,pattern in TAG_PATTERNS.items() if re.search(pattern,text,re.I)]})
    return rows

def compress(request,mode,byte_limit=2400):
    if mode not in ('plain','qualifiers') or type(byte_limit) is not int or byte_limit<1:
        raise ValueError('Invalid memory configuration')
    payload=json.loads(request['messages'][1]['content']);rows=fragments(payload['paragraphs'])
    ranking=ParagraphRanker(rows).rank(payload['question'],'bm25') if rows else []
    scores={r['id']:r['score'] for r in ranking};maximum=max(scores.values(),default=1)
    eligible=set(scores)
    if mode=='qualifiers':
        # Keep one-hop adjacent qualifiers even if they repeat no query word.
        # This is contextual expansion, not an inferred factual requirement.
        for i,r in enumerate(rows):
            if not r['tags']:continue
            if any(0<=j<len(rows) and rows[j]['parent_id']==r['parent_id'] and rows[j]['id'] in scores
                   for j in (i-1,i+1)):eligible.add(r['id'])
    remaining=[r for r in rows if r['id'] in eligible];selected=[];covered=set();used=0;parents=set()
    while remaining:
        fitting=[r for r in remaining if used+len(r['text'].encode())+(r['parent_id'] in parents)<=byte_limit]
        if not fitting:break
        def score(r):
            relevance=scores.get(r['id'],0)/maximum
            return relevance if mode=='plain' else .8*relevance+.2*len(set(r['tags'])-covered)/len(TAG_PATTERNS)
        hit=min(fitting,key=lambda r:(-score(r),r['id']))
        used+=len(hit['text'].encode())+(hit['parent_id'] in parents);parents.add(hit['parent_id'])
        selected.append(hit);covered.update(hit['tags']);remaining.remove(hit)
    # Restore source order within each paragraph; never join fragments without a separator.
    selected.sort(key=lambda r:([p['id'] for p in payload['paragraphs']].index(r['parent_id']),r['start']))
    compact=[]
    for p in payload['paragraphs']:
        parts=[r for r in selected if r['parent_id']==p['id']]
        if parts:compact.append({'id':p['id'],'text':'\n'.join(r['text'] for r in parts)})
    output=copy.deepcopy(request);payload['paragraphs']=compact
    output['messages'][1]['content']=json.dumps(payload,ensure_ascii=False)
    output['visible_ids']=[p['id'] for p in compact]
    output['evidence_utf8_bytes']=sum(len(p['text'].encode()) for p in compact)
    output['content_utf8_bytes']=sum(len(m['content'].encode()) for m in output['messages'])
    output['memory_byte_limit']=byte_limit
    output['memory_provenance']=selected
    assert output['evidence_utf8_bytes']==used<=byte_limit
    return output

def memory_inputs(bundle):
    bundle=Path(bundle);spec=json.loads((bundle/'memory-mode.json').read_text(encoding='utf-8'))
    if set(spec)!={'question_ids','byte_limit','version'} or spec['version']!='extractive-v2' or spec['byte_limit']!=2400:
        raise ValueError('Unsupported memory protocol')
    qids=spec['question_ids']
    if not 1<=len(qids)<=12 or len(qids)!=len(set(qids)):raise ValueError('Invalid memory cohort')
    all_requests,papers=reconstructed(bundle);expected={}
    for qid in qids:
        base=all_requests[(qid,'hybrid-check@8')]
        for method in METHODS:
            value=copy.deepcopy(base) if method=='memory-full' else compress(base,method.split('-')[1])
            value['method']=method;expected[(qid,method)]=value
    requests=json.loads((bundle/'pilot-requests.json').read_text(encoding='utf-8'))
    keys=[(r['question_id'],r['method']) for r in requests]
    if len(keys)!=len(set(keys)) or set(keys)!=set(expected):raise ValueError('Incomplete memory comparison')
    if any(r!=expected[key] for key,r in zip(keys,requests)):raise ValueError('Memory request differs from public reconstruction')
    return requests,papers
