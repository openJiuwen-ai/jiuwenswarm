"""Bounded QASPER development execution; original frozen execution code unchanged."""
import argparse
import asyncio
import getpass
import json
from pathlib import Path
import socket

from .execution import BudgetSettings, budget_view, cost_units, reserve, settle, units
from .qasper_experiment import make_request, ParagraphRanker, parse_prediction, evaluate, sha, write
from .qasper_selection import checked_request
from .qasper_validation import data_split,check_order


FILES=('pilot-requests.json','train/public-papers.json','train/public-questions.json','train/gold-references.json')
PARAMS={'temperature':0,'max_tokens':512,'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}}
TOTAL_TIMEOUT_SECONDS=90
SHORT_ANSWER=(' In the answer field, return only the minimal direct answer, not an explanation. '
    'For a yes/no question return exactly Yes or No when supported. '
    'For an entity, number or list question return only the requested entity, number or list. '
    'Prefer the wording used in the evidence. Put supporting paragraph IDs only in evidence_ids. '
    'Do not replace an unsupported answer with a guess.')


def short_answer_request(request):
    result=json.loads(json.dumps(request))
    result['messages'][0]['content']+=SHORT_ANSWER
    result['content_utf8_bytes']=sum(len(m['content'].encode()) for m in result['messages'])
    result['prompt_version']='short-answer-v1'
    return result


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def digest(value):return sha(json.dumps(value,sort_keys=True,ensure_ascii=False).encode())
def code_hashes(dense=False,memory=False):
    return {name:sha(Path(__file__).with_name(name).read_bytes()) for name in
            ('qasper_runner.py','qasper_experiment.py','qasper_selection.py','retrieval_diagnostics.py','execution.py','qasper_validation.py','qasper_validation_cohorts.json')+
            (('qasper_dense.py','qasper_dense_bundle.py') if dense else ())+
            (('qasper_memory.py',) if memory else ())}


def inputs(bundle):
    """Rebuild each prompt from public fields; never load answers into requests."""
    split=data_split(bundle)
    if (Path(bundle)/'memory-mode.json').exists():
        from .qasper_memory import memory_inputs
        requests,papers=memory_inputs(bundle)
        check_order(bundle,requests)
        return requests,papers
    if split=='dev':raise ValueError('Validation requires the frozen memory comparison')
    if (Path(bundle)/'dense-cache.json').exists():
        from .qasper_dense_bundle import validated_inputs
        return validated_inputs(bundle)
    bundle=Path(bundle);requests=read(bundle/'pilot-requests.json')
    papers=read(bundle/'train/public-papers.json')
    questions={q['question_id']:q for q in read(bundle/'train/public-questions.json')}
    keys=set()
    if not requests or len(requests)>96:raise ValueError('Invalid pilot size')
    versions={r.get('prompt_version','original') for r in requests}
    if not versions<={'original','short-answer-v1','completeness-v1'}:raise ValueError('Unknown prompt versions')
    if len(versions)>1 and not (versions=={'short-answer-v1','completeness-v1'} and {r['method'] for r in requests}=={'bm25@8','bm25-check@8'}):
        raise ValueError('Mixed versions require a paired completeness study')
    for r in requests:
        key=(r['question_id'],r['method'])
        if key in keys:raise ValueError('Duplicate request')
        keys.add(key)
        if r['method'] not in ('overlap@8','bm25@8','bm25@2','bm25-check@8'):raise ValueError('Unknown method')
        q=questions[r['question_id']];paper=papers[q['paper_id']]
        if q['split']!='train':raise ValueError('Pilot must use train only')
        method,k=r['method'].split('@')
        if method=='bm25-check':method='bm25'
        expected={'method':r['method'],**make_request(q,paper,ParagraphRanker(paper['paragraphs']).rank(q['question'],method),int(k))}
        if r.get('prompt_version') in ('short-answer-v1','completeness-v1'):expected=short_answer_request(expected)
        if r.get('prompt_version')=='completeness-v1':expected=checked_request(expected)
        if (r['method']=='bm25-check@8')!=(r.get('prompt_version')=='completeness-v1'):
            raise ValueError('Completeness method/version mismatch')
        if r!=expected:raise ValueError('Request differs from public reconstruction')
    if 'completeness-v1' in versions:
        base={r['question_id'] for r in requests if r['method']=='bm25@8'}
        check={r['question_id'] for r in requests if r['method']=='bm25-check@8'}
        if base!=check:raise ValueError('Incomplete paired control')
    return requests,papers


def make_plan(bundle,cfg,batch_cny=.6):
    cfg=BudgetSettings(**cfg).model_dump();split=data_split(bundle)
    ceiling=30 if split=='dev' else 15
    if not cfg['pricing_checked'] or cfg['max_output_tokens']!=512 or not 0<cfg['total_cny']<=ceiling:
        raise ValueError('Need checked pricing, 512 output limit and allowed total ceiling')
    if cfg['input_cny_per_million']<=0 or cfg['output_cny_per_million']<=0 or not 0<batch_cny<=2:
        raise ValueError('Invalid pricing/batch bound')
    requests,_=inputs(bundle)
    upper=[len(json.dumps(r['messages'],ensure_ascii=False).encode())+1024 for r in requests]
    holds=[cost_units(n,512,cfg) for n in upper]
    if max(upper)>24000 or sum(holds)>units(batch_cny):raise ValueError('Batch bound exceeded')
    for qid in {r['question_id'] for r in requests}:
        indices=[i for i,r in enumerate(requests) if r['question_id']==qid]
        if len(indices)>cfg['calls_per_task'] or sum(holds[i] for i in indices)>units(cfg['task_cny']):
            raise ValueError('Per-question task bound exceeded')
    dense=(Path(bundle)/'dense-cache.json').exists()
    memory=(Path(bundle)/'memory-mode.json').exists()
    files=[name.replace('train/',split+'/') for name in FILES]
    if split=='dev':files+=['validation-batch.json']
    if dense:
        from .qasper_dense_bundle import cache_files
        files+=cache_files(bundle)
    if memory:files+=['memory-mode.json']
    plan={'kind':('qasper_dev_prospective_validation' if split=='dev' else 'qasper_train_development'),'files':{name:sha((Path(bundle)/name).read_bytes()) for name in files},
          'code':code_hashes(dense,memory),'model':cfg['model'],'input_cny_per_million':cfg['input_cny_per_million'],
          'output_cny_per_million':cfg['output_cny_per_million'],'total_cny':cfg['total_cny'],
          'max_input_upper':max(upper),'max_output_tokens':512,'parameters':PARAMS,
          'max_calls':len(requests),'reserve_upper_units':sum(holds),'batch_cap_units':units(batch_cny),
          'client_total_timeout_seconds':TOTAL_TIMEOUT_SECONDS,
          'automatic_retries':0,'order':'exact frozen pilot-requests order; see study protocol',
          'note':('Frozen v137 dev-answer validation; prior offline dev use, not unseen test; no new approval implied.' if split=='dev' else 'Development smoke test, no randomization/confirmatory inference; billed cost may differ from peak-rate estimate.')}
    return plan|{'id':digest(plan)}


def preflight(store,bundle,plan):
    if digest({k:v for k,v in plan.items() if k!='id'})!=plan['id']:raise ValueError('Plan hash mismatch')
    current=make_plan(bundle,store.read().get('budget',{}),plan['batch_cap_units']/1e6)
    if current!=plan:raise ValueError('Input, code or settings changed')
    state=store.read();view=budget_view(state)
    if any(r['status']=='reserved' for r in state.get('api_requests',[])):raise ValueError('Outstanding API reservation')
    if view['held_cny']+plan['reserve_upper_units']/1e6>plan['total_cny']:raise ValueError('Cumulative budget exceeded')
    if any(r['plan_id']==plan['id'] for r in state.get('qasper_batches',[])):raise ValueError('Batch already claimed; no automatic replay')
    return {'historical_held_cny':view['held_cny'],'historical_calls':view['calls'],
            'batch_upper_cny':plan['reserve_upper_units']/1e6,
            'combined_upper_cny':view['held_cny']+plan['reserve_upper_units']/1e6,
            'historical_unresolved':view['unresolved'],'total_cap_cny':plan['total_cny']}


async def invoke(api_key,messages,cfg):
    from openjiuwen.core.foundation.llm import Model,ModelClientConfig,ModelRequestConfig
    model=Model(ModelClientConfig(client_provider='DeepSeek',api_key=api_key,api_base='https://api.deepseek.com',
        max_retries=0,timeout=60,verify_ssl=True,use_shared_llm_http_client=False),
        ModelRequestConfig(model=cfg['model'],temperature=0,max_tokens=512))
    # SDK inactivity timeout is not a total elapsed-time bound (e.g. keepalives).
    # Cooperative event-loop deadline; cannot run while the computer is suspended.
    return await asyncio.wait_for(model.invoke(messages,timeout=60,**PARAMS),timeout=TOTAL_TIMEOUT_SECONDS)


async def run_batch(store,bundle,plan,key,*,transport=None):
    lock=store.root/'.qasper-active'
    with lock.open('x',encoding='utf-8') as f:f.write(plan['id'])
    try:
        return await _run_batch(store,bundle,plan,key,transport=transport)
    finally:
        lock.unlink()


async def _run_batch(store,bundle,plan,key,*,transport=None):
    if not key:raise ValueError('Session API key required')
    preflight(store,bundle,plan)
    if not store.read()['budget']['enabled']:raise ValueError('Paid execution disabled')
    requests,papers=inputs(bundle);folder=store.root/'qasper-runs'/plan['id']
    # Atomic mkdir also prevents a second local process from claiming this batch.
    folder.mkdir(parents=True,exist_ok=False)
    write(folder/'plan.json',plan)
    (folder/'source-snapshot').mkdir()
    for name in plan['code']:
        (folder/'source-snapshot'/name).write_bytes(Path(__file__).with_name(name).read_bytes())
    batch={'plan_id':plan['id'],'status':'running','records':[]}
    store.change('Claim QASPER pilot once',lambda s:s.setdefault('qasper_batches',[]).append(batch))
    transport=transport or invoke
    for n,r in enumerate(requests):
        rec={'ordinal':n,'question_id':r['question_id'],'paper_id':r['paper_id'],'method':r['method'],
             'request_sha256':digest(r),'parameters':PARAMS,'status':'failed_before_request'}
        reservation=None
        try:
            write(folder/f'{n:03}-request.json',r)
            reservation=reserve(store,'qasper:'+plan['id']+':'+r['question_id'],r['messages'],limits=plan)
            rec['request_id']=reservation['id']
            response=await transport(key,r['messages'],reservation['settings'])
            settled=settle(store,reservation,response)
            content=response.content
            if not isinstance(content,str):raise ValueError('Non-text model output')
            (folder/f'{n:03}-raw.txt').write_text(content,encoding='utf-8')
            usage=getattr(response,'usage_metadata',None)
            rec.update(raw_sha256=sha((folder/f'{n:03}-raw.txt').read_bytes()),budget_status=settled['status'],
                input_tokens=getattr(usage,'input_tokens',None),output_tokens=getattr(usage,'output_tokens',None),
                response_model=getattr(response,'response_model',None),response_id=getattr(response,'response_id',None),
                finish_reason=getattr(response,'finish_reason',None),held_units=settled['held_units'],
                estimated_units=settled.get('estimated_units'),status='usage_unknown')
            if settled['status']!='completed':raise ValueError('Unverified usage')
            if rec['finish_reason']!='stop':
                rec['status']='truncated_or_unexpected_finish'
            else:
                try:
                    rec['prediction']=parse_prediction(content,r,papers[r['paper_id']])
                    rec['status']='completed'
                except (ValueError,KeyError,TypeError):rec['status']='invalid_output'
        except Exception as exc:
            # No raw SDK exception: it may contain credentials or headers.
            if reservation:
                saved=next(x for x in store.read()['api_requests'] if x['id']==reservation['id'])
                if saved['status']=='reserved':settle(store,reservation)
            rec['status']='request_failed' if reservation else 'failed_before_request'
            rec['error_category']='timeout' if isinstance(exc,TimeoutError) else 'request_or_artifact_failure'
            rec['error']='Request or artifact processing failed; no retry; budget held.'
        write(folder/f'{n:03}-record.json',rec)
        def append(state):
            saved=next(b for b in state['qasper_batches'] if b['plan_id']==plan['id'])
            saved['records'].append(rec)
            if rec['status']!='completed':
                saved['status']='stopped';state['budget']['enabled']=False
        store.change('Record QASPER pilot output',append)
        if rec['status']!='completed':break
    def finish(state):
        b=next(x for x in state['qasper_batches'] if x['plan_id']==plan['id'])
        if b['status']=='running':b['status']='completed'
        state['budget']['enabled']=False
        return b
    result=store.change('Finish QASPER batch; disable paid calls',finish)
    write(folder/'result.json',result)
    return result


def score_batch(bundle,folder):
    plan=read(Path(folder)/'plan.json');split=data_split(bundle);goldname=split+'/gold-references.json';goldfile=Path(bundle)/goldname
    if sha(goldfile.read_bytes())!=plan['files'][goldname]:raise ValueError('Gold changed')
    if any(sha((Path(bundle)/name).read_bytes())!=checksum for name,checksum in plan['files'].items()):
        raise ValueError('Scoring bundle changed')
    requests,papers=inputs(bundle);gold=read(goldfile);result=read(Path(folder)/'result.json')
    report={'kind':'development_only','batch_status':result['status'],'methods':{},
            'note':'Contract-valid delivered-answer metrics; failed/missing outputs score zero. Not independent human semantic review.'}
    for method in sorted({r['method'] for r in requests}):
        qids=[r['question_id'] for r in requests if r['method']==method]
        rows=[r for r in result['records'] if r['method']==method]
        predictions={}
        for row in rows:
            if row['status']!='completed':continue
            request=requests[row['ordinal']]
            if digest(request)!=row['request_sha256'] or request['question_id']!=row['question_id'] or request['method']!=method:
                raise ValueError('Response identity mismatch')
            raw=Path(folder)/f"{row['ordinal']:03}-raw.txt"
            if sha(raw.read_bytes())!=row['raw_sha256']:raise ValueError('Raw response modified')
            parsed=parse_prediction(raw.read_text(encoding='utf-8'),request,papers[request['paper_id']])
            if parsed!=row['prediction']:raise ValueError('Parsed answer modified')
            predictions[row['question_id']]=parsed
        report['methods'][method]={'planned_questions':len(qids),'attempted':len(rows),'valid_outputs':len(predictions),
            'metrics':evaluate({qid:gold[qid] for qid in qids},predictions),
            'estimated_cny':sum(r.get('estimated_units') or 0 for r in rows)/1e6}
    write(Path(folder)/'scores.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--research',required=True,type=Path);p.add_argument('--bundle',required=True,type=Path)
    p.add_argument('--plan',required=True,type=Path);p.add_argument('--execute',action='store_true')
    a=p.parse_args()
    from .app import Store
    store=Store(a.research);plan=read(a.plan)
    print(json.dumps(preflight(store,a.bundle,plan),ensure_ascii=False),flush=True)
    if a.execute:
        # Existing Store uses a process-local lock: refuse while the workbench server runs.
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',18765))==0:raise SystemExit('Stop the local workbench before CLI execution')
        key=getpass.getpass('DeepSeek session API key (not saved): ')
        result=asyncio.run(run_batch(store,a.bundle,plan,key));key=''
        print(json.dumps({'status':result['status'],'records':len(result['records'])}))
        print(json.dumps(score_batch(a.bundle,store.root/'qasper-runs'/plan['id']),ensure_ascii=False))
