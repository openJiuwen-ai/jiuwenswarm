"""Public-only reconstruction of three-condition dense QA requests from pinned caches."""
import json
from pathlib import Path
import numpy as np
from .qasper_dense import dense_rank,fuse_rankings
from .qasper_experiment import ParagraphRanker,make_request,sha
from .qasper_selection import checked_request
from .qasper_validation import data_split

METHODS=('bm25-check@8','bge-check@8','hybrid-check@8')
MODEL_SHA='828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35'
TOKENIZER_SHA='d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66'

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def cache_files(bundle):
    cache=read(Path(bundle)/'dense-cache.json')
    names=list(cache['files'])
    # No path traversal or model downloads during execution.
    if any(Path(n).as_posix()!=n or not n.startswith('cache/') or '..' in Path(n).parts
           or Path(n).suffix!='.npy' for n in names):raise ValueError('Invalid cache paths')
    return ['dense-cache.json',*names]

def reconstructed(bundle):
    from .qasper_runner import short_answer_request
    bundle=Path(bundle);split=data_split(bundle);cache=read(bundle/'dense-cache.json')
    if cache['model_sha256']!=MODEL_SHA or cache['tokenizer_sha256']!=TOKENIZER_SHA:
        raise ValueError('Wrong pinned model')
    for name in cache_files(bundle)[1:]:
        if sha((bundle/name).read_bytes())!=cache['files'][name]:raise ValueError('Embedding cache changed')
    for name in ('public-papers.json','public-questions.json'):
        if sha((bundle/split/name).read_bytes())!=cache['inputs'][name]:raise ValueError('Public input changed')
    papers=read(bundle/split/'public-papers.json');qs=read(bundle/split/'public-questions.json')
    if not 1<=len(qs)<=32 or len({q['question_id'] for q in qs})!=len(qs):raise ValueError('Invalid cohort')
    if {q['paper_id'] for q in qs}!=set(papers) or set(cache['papers'])!=set(papers):raise ValueError('Paper identity mismatch')
    if any(set(q)!={'question_id','paper_id','question','split'} or q['split']!=split for q in qs):
        raise ValueError('Public questions must retain the allowed split')
    result={}
    for pid,paper in papers.items():
        info=cache['papers'][pid];sub=[q for q in qs if q['paper_id']==pid]
        if info['paragraph_ids']!=[p['id'] for p in paper['paragraphs']] or info['question_ids']!=[q['question_id'] for q in sub]:
            raise ValueError('Embedding row identities changed')
        if info['passages'] not in cache['files'] or info['queries'] not in cache['files']:raise ValueError('Unbound cache')
        dv=np.load(bundle/info['passages'],allow_pickle=False);qv=np.load(bundle/info['queries'],allow_pickle=False)
        if dv.shape!=(len(paper['paragraphs']),384) or qv.shape!=(len(sub),384):raise ValueError('Embedding shape mismatch')
        for values in (dv,qv):
            if not np.isfinite(values).all() or not np.allclose(np.linalg.norm(values,axis=1),1,atol=1e-5):
                raise ValueError('Invalid normalized embeddings')
        ranker=ParagraphRanker(paper['paragraphs'])
        for q,v in zip(sub,qv):
            bm25=ranker.rank(q['question'],'bm25');dense=dense_rank(paper['paragraphs'],dv,v)
            for method,ranking in zip(METHODS,(bm25,dense,fuse_rankings(bm25,dense))):
                request={'method':method,**make_request(q,paper,ranking)}
                result[(q['question_id'],method)]=checked_request(short_answer_request(request))
    return result,papers

def validated_inputs(bundle):
    expected,papers=reconstructed(bundle)
    requests=read(Path(bundle)/'pilot-requests.json')
    keys=[(r['question_id'],r['method']) for r in requests]
    if len(keys)!=len(set(keys)) or set(keys)!=set(expected):raise ValueError('Incomplete or duplicated dense comparison')
    for key,r in zip(keys,requests):
        if r!=expected[key]:raise ValueError('Dense request differs from public reconstruction')
    return requests,papers
