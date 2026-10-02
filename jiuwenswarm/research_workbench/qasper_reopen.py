"""Development-only paired abstention-triggered evidence reopening; no gold in routing."""
import json
from pathlib import Path

from .execution import BudgetSettings,budget_view,cost_units,reserve,settle,units
from .qasper_experiment import make_request,ParagraphRanker,parse_prediction,evaluate,sha,write
from .qasper_runner import invoke,short_answer_request,read,digest,code_hashes

SLOTS=('initial2','static8','overlap8','reopen8','recheck2')
SECOND=' Re-evaluate the same question once using only the supplied evidence. The previous pass abstained; this does not imply that an answer exists. Keep abstaining if unsupported.'
FILES=('cases-public.json','papers-public.json','gold-private.json','selection.json')


def prepare(base,out,excluded_papers,n_each=6):
    """Label-stratified TRAIN selection is explicit; independent of prior performance."""
    out=Path(out)
    if out.exists():raise ValueError('Never overwrite a cohort')
    questions=read(Path(base)/'train/public-questions.json');papers=read(Path(base)/'train/public-papers.json')
    gold=read(Path(base)/'train/gold-references.json');selected=[];groups={True:[],False:[]};mixed=0
    for q in questions:
        kinds={r['type']=='none' for r in gold[q['question_id']]}
        if len(kinds)!=1:mixed+=1;continue
        if q['paper_id'] not in excluded_papers:groups[True in kinds].append(q)
    seen=set(excluded_papers);labels={}
    for unanswerable in (True,False):
        picked=0
        for q in sorted(groups[unanswerable],key=lambda q:digest(['reopen-20261002',q['question_id']])):
            if q['paper_id'] in seen:continue
            seen.add(q['paper_id']);selected.append(q);labels[q['question_id']]=unanswerable;picked+=1
            if picked==n_each:break
        if picked!=n_each:raise ValueError('Insufficient distinct sources')
    selected.sort(key=lambda q:digest(['execution-order',q['question_id']]))
    out.mkdir(parents=True)
    write(out/'cases-public.json',selected)
    write(out/'papers-public.json',{q['paper_id']:papers[q['paper_id']] for q in selected})
    write(out/'gold-private.json',{q['question_id']:gold[q['question_id']] for q in selected})
    write(out/'selection.json',{'kind':'balanced_train_diagnostic_not_population_estimate','seed':'reopen-20261002',
        'labels_for_analysis_only':labels,'excluded_previous_papers':sorted(excluded_papers),
        'mixed_answerability_excluded':mixed,'eligible_questions':{str(k):len(v) for k,v in groups.items()},
        'source_hashes':{name:sha((Path(base)/'train'/name).read_bytes()) for name in ('public-questions.json','public-papers.json','gold-references.json')}})


def jobs(bundle):
    cases=read(Path(bundle)/'cases-public.json');papers=read(Path(bundle)/'papers-public.json');all_jobs=[]
    if not cases or len(cases)>12 or len({q['paper_id'] for q in cases})!=len(cases):raise ValueError('Invalid cohort')
    if len({q['question_id'] for q in cases})!=len(cases):raise ValueError('Duplicate case')
    for q in cases:
        if set(q)!={'question_id','paper_id','question','split'} or q['split']!='train':raise ValueError('Only public train question fields allowed')
        paper=papers[q['paper_id']];ranker=ParagraphRanker(paper['paragraphs']);slots={}
        for slot,method,k in [('initial2','bm25',2),('static8','bm25',8),('overlap8','overlap',8),('reopen8','bm25',8),('recheck2','bm25',2)]:
            r=short_answer_request(make_request(q,paper,ranker.rank(q['question'],method),k))
            if slot in ('reopen8','recheck2'):
                r['messages'][0]['content']+=SECOND
                r['content_utf8_bytes']=sum(len(m['content'].encode()) for m in r['messages'])
            slots[slot]=r
        all_jobs.append({'question_id':q['question_id'],'paper_id':q['paper_id'],'slots':slots})
    return all_jobs,papers


def make_plan(bundle,cfg):
    cfg=BudgetSettings(**cfg).model_dump();work,_=jobs(bundle)
    if cfg['max_output_tokens']!=512 or not 0<cfg['total_cny']<=15 or not cfg['pricing_checked']:
        raise ValueError('Invalid budget/model settings')
    if min(cfg['input_cny_per_million'],cfg['output_cny_per_million'])<=0:raise ValueError('Invalid prices')
    uppers=[];total=0
    for job in work:
        bounds=[len(json.dumps(r['messages'],ensure_ascii=False).encode())+1024 for r in job['slots'].values()]
        per=sum(cost_units(b,512,cfg) for b in bounds)
        if per>units(cfg['task_cny']) or cfg['calls_per_task']<5:raise ValueError('Task budget too small')
        uppers.extend(bounds);total+=per
    if max(uppers)>24000 or total>units(2):raise ValueError('Development batch cap exceeded')
    code=code_hashes()|{'qasper_reopen.py':sha(Path(__file__).read_bytes())}
    plan={'kind':'paired_train_reopening','files':{name:sha((Path(bundle)/name).read_bytes()) for name in FILES},
        'code':code,'jobs_hash':digest(work),'model':cfg['model'],'input_cny_per_million':cfg['input_cny_per_million'],
        'output_cny_per_million':cfg['output_cny_per_million'],'total_cny':cfg['total_cny'],
        'max_input_upper':max(uppers),'max_output_tokens':512,'max_calls':len(work)*5,'reserve_upper_units':total,
        'seed':'reopen-20261002','routing':'Only initial2 valid Unanswerable triggers reopen8 and recheck2',
        'cost_note':'Initial2 draw shared across three policies; logical policy costs count it once per policy. Policy costs cannot be summed as actual bill.',
        'control_note':'Same per-call ceilings and schema; actual context/cost differ, no exact spending-match claim.'}
    return plan|{'id':digest(plan)}


def preflight(store,bundle,plan):
    if make_plan(bundle,store.read()['budget'])!=plan:raise ValueError('Frozen plan changed')
    s=store.read();view=budget_view(s)
    if any(r['status']=='reserved' for r in s.get('api_requests',[])):raise ValueError('Unresolved active reservation')
    if view['held_cny']+plan['reserve_upper_units']/1e6>plan['total_cny']:raise ValueError('Total budget exceeded')
    if any(b['plan_id']==plan['id'] for b in s.get('reopen_batches',[])):raise ValueError('Already executed/claimed')
    return {'historical_held_cny':view['held_cny'],'batch_upper_cny':plan['reserve_upper_units']/1e6,
            'combined_upper_cny':view['held_cny']+plan['reserve_upper_units']/1e6,'max_calls':plan['max_calls']}


async def call_one(store,key,plan,folder,job,slot,paper,transport):
    r=job['slots'][slot];stem=f'{job["question_id"]}-{slot}';res=None
    rec={'question_id':job['question_id'],'slot':slot,'request_sha256':digest(r),'status':'failed'}
    write(folder/(stem+'-request.json'),r)
    try:
        res=reserve(store,'reopen:'+plan['id']+':'+job['question_id'],r['messages'],limits=plan)
        rec['request_id']=res['id'];response=await transport(key,r['messages'],res['settings'])
        settled=settle(store,res,response)
        raw=folder/(stem+'-raw.txt');raw.write_text(response.content,encoding='utf-8')
        rec.update(raw_sha256=sha(raw.read_bytes()),held_units=settled['held_units'],estimated_units=settled.get('estimated_units'),
            input_tokens=settled.get('input_tokens'),output_tokens=settled.get('output_tokens'),
            response_model=getattr(response,'response_model',None),finish_reason=getattr(response,'finish_reason',None))
        if settled['status']!='completed' or rec['finish_reason']!='stop':raise ValueError('Incomplete response/usage')
        rec['prediction']=parse_prediction(response.content,r,paper);rec['status']='completed'
    except Exception:
        if res and next(x for x in store.read()['api_requests'] if x['id']==res['id'])['status']=='reserved':settle(store,res)
        rec['error']='Stopped on request/output failure; no automatic retry.'
    write(folder/(stem+'-record.json'),rec)
    return rec


async def run(store,bundle,plan,key,transport=None):
    if not key:raise ValueError('Session key required')
    preflight(store,bundle,plan)
    if not store.read()['budget']['enabled']:raise ValueError('Execution disabled')
    lock=store.root/'.qasper-active'
    with lock.open('x') as f:f.write(plan['id'])
    try:
        # Recheck after exclusive claim; no CLI workstation writer may run concurrently.
        preflight(store,bundle,plan);work,papers=jobs(bundle)
        folder=store.root/'reopen-runs'/plan['id'];folder.mkdir(parents=True,exist_ok=False)
        write(folder/'plan.json',plan);(folder/'source-snapshot').mkdir()
        for name in plan['code']:(folder/'source-snapshot'/name).write_bytes(Path(__file__).with_name(name).read_bytes())
        store.change('Claim reopening batch',lambda s:s.setdefault('reopen_batches',[]).append({'plan_id':plan['id'],'status':'running','records':[]}))
        failed=False
        for job in work:
            initial=None
            for slot in SLOTS:
                if slot in ('reopen8','recheck2') and initial['prediction']['answer']!='Unanswerable':continue
                rec=await call_one(store,key,plan,folder,job,slot,papers[job['paper_id']],transport or invoke)
                store.change('Record reopening step',lambda s:next(b for b in s['reopen_batches'] if b['plan_id']==plan['id'])['records'].append(rec))
                if slot=='initial2':initial=rec
                if rec['status']!='completed':failed=True;break
            if failed:break
        def finish(s):
            b=next(b for b in s['reopen_batches'] if b['plan_id']==plan['id']);b['status']='stopped' if failed else 'completed'
            s['budget']['enabled']=False;return b
        result=store.change('Finish reopening batch',finish);write(folder/'result.json',result)
        return result
    finally:
        store.change('Disable paid execution',lambda s:s['budget'].update(enabled=False))
        lock.unlink()


def score(bundle,folder):
    folder=Path(folder);plan=read(folder/'plan.json');result=read(folder/'result.json')
    if any(sha((Path(bundle)/name).read_bytes())!=v for name,v in plan['files'].items()):raise ValueError('Data changed')
    work,papers=jobs(bundle)
    if digest(work)!=plan['jobs_hash']:raise ValueError('Requests changed')
    gold=read(Path(bundle)/'gold-private.json');labels=read(Path(bundle)/'selection.json')['labels_for_analysis_only']
    records={(r['question_id'],r['slot']):r for r in result['records']}
    if len(records)!=len(result['records']):raise ValueError('Duplicate run output')
    for job in work:
        for slot,r in job['slots'].items():
            rec=records.get((job['question_id'],slot))
            if not rec or rec['status']!='completed':continue
            raw=folder/f'{job["question_id"]}-{slot}-raw.txt'
            if digest(r)!=rec['request_sha256'] or sha(raw.read_bytes())!=rec['raw_sha256']:raise ValueError('Output binding changed')
            if parse_prediction(raw.read_text(encoding='utf-8'),r,papers[job['paper_id']])!=rec['prediction']:raise ValueError('Parsed answer changed')
    output={'kind':'balanced_train_diagnostic','policies':{},'transitions':[],'actual_calls':len(result['records']),
        'actual_estimated_cny':sum(r.get('estimated_units') or 0 for r in result['records'])/1e6,
        'note':plan['cost_note']+' '+plan['control_note']}
    for policy in ('static2','static8','overlap8','adaptive2to8','recheck2'):
        pred={};cost=0;calls=0
        for job in work:
            q=job['question_id'];first=records.get((q,'initial2'));chosen={'static2':'initial2','static8':'static8','overlap8':'overlap8'}.get(policy,'initial2')
            route=[chosen]
            if policy in ('adaptive2to8','recheck2') and first and first.get('prediction',{}).get('answer')=='Unanswerable':
                chosen='reopen8' if policy=='adaptive2to8' else 'recheck2';route=['initial2',chosen]
            rec=records.get((q,chosen))
            if rec and rec['status']=='completed':pred[q]=rec['prediction']
            cost+=sum(records[(q,s)].get('estimated_units') or 0 for s in route if (q,s) in records)
            calls+=sum((q,s) in records for s in route)
        sub={}
        for label in (False,True):
            ids={q for q in gold if labels[q]==label}
            sub['unanswerable' if label else 'answerable']={'questions':len(ids),
                'abstentions':sum(pred[q]['answer']=='Unanswerable' for q in ids if q in pred),
                'metrics':evaluate({q:gold[q] for q in ids},{q:pred[q] for q in ids if q in pred})}
        output['policies'][policy]={'metrics':evaluate(gold,pred),'groups':sub,'logical_calls':calls,'logical_estimated_cny':cost/1e6}
    for q in gold:
        first=records.get((q,'initial2'),{}).get('prediction')
        if first and first['answer']=='Unanswerable':
            output['transitions'].append({'question_id':q,'reference_unanswerable':labels[q],
                'reopened_answer':records.get((q,'reopen8'),{}).get('prediction',{}).get('answer'),
                'rechecked_answer':records.get((q,'recheck2'),{}).get('prediction',{}).get('answer')})
    write(folder/'scores.json',output);return output
