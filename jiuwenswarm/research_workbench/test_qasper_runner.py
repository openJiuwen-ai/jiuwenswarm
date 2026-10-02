import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from .app import Store
from .execution import BudgetSettings, budget_view
from .qasper_experiment import adapt,write,make_request,ParagraphRanker
from .qasper_runner import make_plan,preflight,run_batch,score_batch,invoke
from .test_qasper_experiment import fixture


def setup(root):
    root=Path(root);bundle=root/'bundle';(bundle/'train').mkdir(parents=True)
    papers,qs,gold,_=adapt(fixture(),'train')
    write(bundle/'train/public-papers.json',papers);write(bundle/'train/public-questions.json',qs)
    write(bundle/'train/gold-references.json',gold)
    requests=[]
    for method,k in [('overlap',8),('bm25',8),('bm25',2)]:
        q=qs[0];paper=papers[q['paper_id']]
        requests.append({'method':f'{method}@{k}',**make_request(q,paper,ParagraphRanker(paper['paragraphs']).rank(q['question'],method),k)})
    write(bundle/'pilot-requests.json',requests)
    store=Store(root/'research')
    cfg=BudgetSettings(enabled=True,total_cny=15,task_cny=.6,calls_per_task=3,max_output_tokens=512,
        input_cny_per_million=2,output_cny_per_million=8,pricing_checked=True).model_dump()
    store.change('fixture config',lambda s:s.update(budget=cfg))
    return store,bundle,make_plan(bundle,cfg)


def response(content=None,**change):
    return SimpleNamespace(content=content or json.dumps({'answer':'Unanswerable','unanswerable':True,'evidence_ids':[]},indent=2),
        usage_metadata=SimpleNamespace(input_tokens=100,output_tokens=30),response_model='fixture-model',
        response_id='fixture-id',finish_reason=change.get('finish_reason','stop'))


class RunnerTests(unittest.TestCase):
    def test_batch_once_hash_bound_and_scored_without_dropping_failures(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root);calls=[]
            async def fake(*args):calls.append(1);return response()
            result=asyncio.run(run_batch(store,bundle,plan,'fixture-only',transport=fake))
            self.assertEqual(len(calls),3);self.assertEqual(result['status'],'completed')
            self.assertFalse(store.read()['budget']['enabled'])
            with self.assertRaises(ValueError):asyncio.run(run_batch(store,bundle,plan,'fixture',transport=fake))
            folder=store.root/'qasper-runs'/plan['id']
            scores=score_batch(bundle,folder)
            self.assertEqual(scores['methods']['bm25@2']['metrics']['Answer F1'],1)
            raw=folder/'000-raw.txt';raw.write_text('modified',encoding='utf-8')
            with self.assertRaises(ValueError):score_batch(bundle,folder)

    def test_failure_no_retry_no_exception_secret_and_holds_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root);calls=[]
            async def fail(*args):calls.append(1);raise RuntimeError('private-secret-marker')
            result=asyncio.run(run_batch(store,bundle,plan,'fixture-only',transport=fail))
            self.assertEqual(len(calls),1);self.assertEqual(result['status'],'stopped')
            self.assertGreater(budget_view(store.read())['held_cny'],0)
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertNotIn('private-secret-marker',store.file.read_text())
            scores=score_batch(bundle,store.root/'qasper-runs'/plan['id'])
            self.assertEqual(scores['methods']['bm25@2']['metrics']['Missing predictions'],1)

    def test_limits_tampering_and_invalid_json_stop_before_next_call(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root)
            store.change('spent fixture',lambda s:s.setdefault('api_requests',[]).append({'id':'old','status':'failed','held_units':15000000,'task_id':'old'}))
            with self.assertRaises(ValueError):preflight(store,bundle,plan)
            store.change('reset fixture',lambda s:s.update(api_requests=[]))
            original=(bundle/'pilot-requests.json').read_bytes()
            (bundle/'pilot-requests.json').write_bytes(original+b' ')
            with self.assertRaises(ValueError):preflight(store,bundle,plan)
            (bundle/'pilot-requests.json').write_bytes(original)
            calls=[]
            async def invalid(*args):calls.append(1);return response('not json')
            result=asyncio.run(run_batch(store,bundle,plan,'fixture-only',transport=invalid))
            self.assertEqual(len(calls),1);self.assertEqual(result['records'][0]['status'],'invalid_output')

    def test_actual_sdk_serialization_intercepted_no_network(self):
        import httpx
        sends=[]
        async def fake_send(client,request,**kwargs):
            sends.append(json.loads(request.content))
            return httpx.Response(200,request=request,json={'id':'test','object':'chat.completion','created':1,
                'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{}'}}],
                'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
        with patch.object(httpx.AsyncClient,'send',fake_send):
            r=asyncio.run(invoke('fixture-only',[{'role':'user','content':'Return JSON fixture'}],{'model':'deepseek-flash'}))
        self.assertEqual(len(sends),1)
        for key,value in {'temperature':0,'max_tokens':512,'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}}.items():
            self.assertEqual(sends[0][key],value)
        self.assertEqual(r.usage_metadata.total_tokens,12)

    def test_actual_sdk_http_failure_zero_retry(self):
        import httpx
        sends=[]
        async def fail(client,request,**kwargs):
            sends.append(1);return httpx.Response(500,request=request,json={'error':{'message':'fixture failure','type':'server_error'}})
        with patch.object(httpx.AsyncClient,'send',fail):
            with self.assertRaises(Exception):asyncio.run(invoke('fixture-only',[{'role':'user','content':'fixture'}],{'model':'deepseek-flash'}))
        self.assertEqual(len(sends),1)


if __name__=='__main__':unittest.main()
