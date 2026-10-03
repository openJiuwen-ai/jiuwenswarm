import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from .test_qasper_reopen import setup
from .qasper_queries import parse_queries,planner_request,answer_request,public_inputs,make_plan,run,score,read,write,sha


class QueryTests(unittest.TestCase):
    def test_offline_metadata_recovery_never_replays_carried_call(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=setup(root);plan=make_plan(bundle,store.read()['budget']);calls=[]
            async def fake(key,messages,cfg):
                calls.append(messages)
                v={'requirements':['fact'],'queries':['repeated evidence']} if 'Produce search queries' in messages[0]['content'] else {'answer':'Unanswerable','unanswerable':True,'evidence_ids':[]}
                return SimpleNamespace(content=json.dumps(v),usage_metadata=SimpleNamespace(input_tokens=100,output_tokens=20),finish_reason='stop',response_model='fixture')
            result=asyncio.run(run(store,bundle,plan,'fixture',fake));parent=store.root/'query-runs'/plan['id']
            # Synthetic old strict-parser failure on the first response, not real data.
            old=result['records'][0];stem=old['question_id']+'-planner';raw=parent/(stem+'-raw.txt')
            raw.write_text(json.dumps(old['prediction']|{'type':'json_object'}),encoding='utf-8')
            old=old|{'status':'failed','raw_sha256':sha(raw.read_bytes())};old.pop('prediction')
            write(parent/'result.json',{'status':'stopped','records':[old]})
            resumed=make_plan(bundle,store.read()['budget'],parent)
            self.assertEqual(resumed['max_calls'],5);self.assertEqual(resumed['carried_steps'],1)
            store.change('fixture enable',lambda s:s['budget'].update(enabled=True));calls.clear()
            completed=asyncio.run(run(store,bundle,resumed,'fixture',fake))
            self.assertEqual(len(calls),5);self.assertEqual(len(completed['records']),6)
            report=score(bundle,store.root/'query-runs'/resumed['id'])
            self.assertEqual(report['offline_schema_recoveries'],1)
            self.assertEqual(read(parent/'result.json')['records'][0]['status'],'failed')

    def test_public_only_strict_planner_and_deduplicated_fusion(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=setup(root);cases,papers=public_inputs(bundle);q=cases[0];p=papers[q['paper_id']]
            req=planner_request(q|{'gold':'SECRET'},p|{'gold':'SECRET'})
            self.assertNotIn('SECRET',json.dumps(req));self.assertNotIn('paragraphs',json.loads(req['messages'][1]['content']))
            self.assertEqual(answer_request(q,p,[q['question']]),answer_request(q,p,[q['question'],q['question']]))
        for value in ['{"queries":[],"requirements":["x"]}','{"queries":["x","x"],"requirements":["x"]}', '{"queries":["x"],"requirements":["x"],"answer":"leak"}']:
            with self.assertRaises(ValueError):parse_queries(value)
        self.assertEqual(parse_queries('{"type":"json_object","queries":["x"],"requirements":["x"]}'),{'queries':['x'],'requirements':['x']})
        with self.assertRaises(ValueError):parse_queries('{"type":"other","queries":["x"],"requirements":["x"]}')

    def test_budget_bound_freeze_and_failure_stop(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=setup(root);plan=make_plan(bundle,store.read()['budget']);calls=[]
            async def fake(key,messages,cfg):
                calls.append(messages)
                content={'requirements':['requested evidence'],'queries':['repeated evidence']} if 'Produce search queries' in messages[0]['content'] else {'answer':'Unanswerable','unanswerable':True,'evidence_ids':[]}
                return SimpleNamespace(content=json.dumps(content),usage_metadata=SimpleNamespace(input_tokens=100,output_tokens=20),finish_reason='stop',response_model='fixture')
            result=asyncio.run(run(store,bundle,plan,'fixture',fake));self.assertEqual(len(calls),6)
            self.assertEqual(score(bundle,store.root/'query-runs'/plan['id'])['status'],'completed')
            self.assertFalse(store.read()['budget']['enabled'])
            with self.assertRaises(ValueError):asyncio.run(run(store,bundle,plan,'fixture',fake))
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=setup(root);plan=make_plan(bundle,store.read()['budget']);calls=[]
            async def bad(*args):
                calls.append(1)
                return SimpleNamespace(content='{}',usage_metadata=SimpleNamespace(input_tokens=100,output_tokens=1),finish_reason='stop',response_model='fixture')
            result=asyncio.run(run(store,bundle,plan,'fixture',bad))
            self.assertEqual(calls,[1]);self.assertEqual(result['status'],'stopped')
            self.assertFalse(store.read()['budget']['enabled'])


if __name__=='__main__':unittest.main()
