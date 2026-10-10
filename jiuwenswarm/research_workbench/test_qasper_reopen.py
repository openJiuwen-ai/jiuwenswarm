import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from .app import Store
from .execution import BudgetSettings
from .qasper_experiment import adapt,write
from .qasper_reopen import prepare,jobs,make_plan,preflight,run,score
from .test_qasper_experiment import fixture


def setup(root):
    root=Path(root);raw=fixture();paper=raw['doc'];qa=paper['qas'][0]
    qa['question']='Repeated evidence?';qa['answers']=qa['answers'][:1]
    other=json.loads(json.dumps(paper));other['qas'][0]['question_id']='q2'
    other['qas'][0]['answers'][0]['answer']['unanswerable']=True
    raw['doc2']=other
    papers,qs,gold,_=adapt(raw,'train');base=root/'base';(base/'train').mkdir(parents=True)
    for name,value in [('public-papers',papers),('public-questions',qs),('gold-references',gold)]:write(base/'train'/(name+'.json'),value)
    bundle=root/'cohort';prepare(base,bundle,set(),n_each=1)
    store=Store(root/'research')
    cfg=BudgetSettings(enabled=True,total_cny=15,task_cny=.26,calls_per_task=8,max_output_tokens=512,
        input_cny_per_million=2,output_cny_per_million=8,pricing_checked=True).model_dump()
    store.change('fixture config',lambda s:s.update(budget=cfg))
    return store,bundle,make_plan(bundle,cfg)


def response(messages,abstain):
    payload=json.loads(messages[1]['content'])
    answer={'answer':'Unanswerable' if abstain else 'No','unanswerable':abstain,
        'evidence_ids':[] if abstain else [payload['paragraphs'][0]['id']]}
    return SimpleNamespace(content=json.dumps(answer),usage_metadata=SimpleNamespace(input_tokens=100,output_tokens=20),
        response_model='fixture',finish_reason='stop')


class ReopenTests(unittest.TestCase):
    def test_conditional_routing_with_no_gold_and_paired_controls(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root);calls=[]
            async def fake(key,messages,cfg):
                calls.append(messages)
                return response(messages,'Re-evaluate the same question' not in messages[0]['content'])
            result=asyncio.run(run(store,bundle,plan,'fixture',fake))
            self.assertEqual(result['status'],'completed');self.assertEqual(len(calls),10)
            self.assertTrue(all('labels_for_analysis_only' not in json.dumps(m) for m in calls))
            report=score(bundle,store.root/'reopen-runs'/plan['id'])
            self.assertEqual(len(report['transitions']),2)
            self.assertEqual(report['policies']['static2']['logical_calls'],2)
            self.assertEqual(report['policies']['adaptive2to8']['logical_calls'],4)
            self.assertEqual(report['policies']['adaptive2to8']['groups']['unanswerable']['abstentions'],0)
            self.assertFalse(store.read()['budget']['enabled'])
            with self.assertRaises(ValueError):asyncio.run(run(store,bundle,plan,'fixture',fake))

    def test_early_answer_skips_second_passes(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root)
            async def fake(key,messages,cfg):return response(messages,False)
            result=asyncio.run(run(store,bundle,plan,'fixture',fake))
            self.assertEqual(len(result['records']),6)
            report=score(bundle,store.root/'reopen-runs'/plan['id'])
            self.assertEqual(report['policies']['static2']['metrics'],report['policies']['adaptive2to8']['metrics'])
            self.assertEqual(report['transitions'],[])

    def test_failure_budget_and_tampered_plan(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root)
            with self.assertRaises(ValueError):preflight(store,bundle,plan|{'max_calls':999})
            store.change('budget fixture',lambda s:s.setdefault('api_requests',[]).append({'status':'failed','held_units':15000000,'id':'x','task_id':'old'}))
            with self.assertRaises(ValueError):preflight(store,bundle,plan)
            store.change('clear fixture',lambda s:s.update(api_requests=[]))
            calls=[]
            async def fail(*args):calls.append(1);raise RuntimeError('do-not-log-secret')
            result=asyncio.run(run(store,bundle,plan,'fixture',fail))
            self.assertEqual(len(calls),1);self.assertEqual(result['status'],'stopped')
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertNotIn('do-not-log-secret',store.file.read_text())


if __name__=='__main__':unittest.main()
