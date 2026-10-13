import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from .app import Store, create_app
from .execution import BudgetSettings, reserve
from .fulltext import sha
from .fulltext_runner import POLICY, FOLDER, prepare, run_fulltext


def setup(root):
    store=Store(root)
    cfg=BudgetSettings(enabled=True,total_cny=15,task_cny=2,calls_per_task=10,max_output_tokens=1000,
        pricing_checked=True,input_cny_per_million=2,output_cny_per_million=8).model_dump()
    store.change('fixture',lambda s:s.update(budget=cfg))
    folder=store.root/FOLDER;folder.mkdir(parents=True)
    plan={'repetitions':2,'max_output_tokens':1000,'authorization_cap_cny':15,
          'input_cny_per_million':2,'output_cny_per_million':8,'model':'deepseek-flash'}
    cases=[{'case_id':'TST001','question':'What does synthetic alpha evidence say?',
            'gold_answer':'SECRET_GOLD_MUST_NOT_LEAK','evidence_quotes':['SECRET_GOLD_MUST_NOT_LEAK']}]
    index={'documents':[{'id':'D1','title':'Fixture'}], 'chunks':[
        {'id':'D1-P001-C001','text':'Synthetic alpha evidence supports a fixture answer.',
         'document_id':'D1','page_1based':1,'sha256':sha(b'Synthetic alpha evidence supports a fixture answer.')}]}
    for name,value in [('plan.json',plan),('test-cases.pending.json',{'cases':cases}),('corpus.json',index)]:
        (folder/name).write_text(json.dumps(value),encoding='utf-8')
    return store,index,cases


class FulltextRunnerTest(unittest.TestCase):
    def test_complete_pairing_no_labels_and_no_real_calls(self):
        with tempfile.TemporaryDirectory() as root:
            store,index,cases=setup(root);seen=[]
            async def fake(key,messages,cfg):
                seen.append(messages);self.assertNotIn('SECRET_GOLD',json.dumps(messages))
                ins=messages[0]['content']
                if 'Compress' in ins:text='Fixture memory.'
                elif 'Choose' in ins:text='{"open_ids":["D1-P001-C001"]}'
                else:text=json.dumps({'answer':'Fixture answer','citations':['D1-P001-C001'],'abstain':False,
                    'supports':[{'citation':'D1-P001-C001','quote':'Synthetic alpha evidence'}]})
                return SimpleNamespace(content=text,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
            result=asyncio.run(run_fulltext(store,'fixture','TST001',invoke=fake,offline=True))
            self.assertEqual(result['status'],'completed');self.assertEqual(len(seen),8)
            self.assertEqual(result['api_calls'],0);self.assertTrue(all(r['status']=='completed' for r in result['results']))
            self.assertNotIn(cases[0]['question'],json.dumps(seen[:2]))
            selections=[json.loads(m[1]['content']) for m in seen if 'Choose' in m[0]['content']]
            self.assertEqual(len(selections),2)
            self.assertEqual(selections[0]['memory'],selections[1]['memory'])
            self.assertEqual({('excerpt' in s['catalogue'][0]) for s in selections},{False,True})
            self.assertEqual(sum('output_gate' in r for r in result['results']),2)
            with self.assertRaises(ValueError):asyncio.run(run_fulltext(store,'fixture','TST001',invoke=fake,offline=True))
            self.assertEqual(len(store.read()['api_requests']),8)

    def test_hash_tampering_and_gold_invariance(self):
        with tempfile.TemporaryDirectory() as root:
            store,index,cases=setup(root)
            a=prepare(index,cases[0]);cases[0]['gold_answer']='DIFFERENT';cases[0]['evidence_quotes']=[]
            self.assertEqual(a,prepare(index,cases[0]))
            index['chunks'][0]['text']='tampered'
            with self.assertRaises(ValueError):prepare(index,cases[0])

    def test_network_failure_stops_and_preserves_four_denominators(self):
        with tempfile.TemporaryDirectory() as root:
            store,_,_=setup(root)
            async def fail(*args):raise RuntimeError('DO_NOT_LOG_CREDENTIALS')
            r=asyncio.run(run_fulltext(store,'fixture','TST001',invoke=fail,offline=True))
            self.assertEqual(r['status'],'stopped');self.assertEqual(len(r['results']),4)
            self.assertEqual(r['model_invocations'],1)
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertNotIn('DO_NOT_LOG_CREDENTIALS',json.dumps(store.read()))
            self.assertGreater(r['held_cny'],0)

    def test_bound_is_checked_before_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            store,_,_=setup(root)
            limits={'max_input_upper':12000,'max_output_tokens':1000,'total_cny':15,
                    'input_cny_per_million':2,'output_cny_per_million':8,'model':'deepseek-flash'}
            with self.assertRaises(ValueError):reserve(store,'x',[{'role':'user','content':'x'*12000}],limits=limits)
            self.assertFalse(store.read().get('api_requests'))
            store.change('wrong output cap',lambda s:s['budget'].update(max_output_tokens=1500))
            with self.assertRaises(ValueError):reserve(store,'x',[],limits=limits)
            self.assertFalse(store.read().get('api_requests'))

    def test_no_unfrozen_live_or_implicit_offline_transport(self):
        with tempfile.TemporaryDirectory() as root:
            store,_,_=setup(root)
            with self.assertRaises((ValueError,KeyError,OSError)):asyncio.run(run_fulltext(store,'fixture','TST001'))
            with self.assertRaises(ValueError):asyncio.run(run_fulltext(store,'fixture','TST001',offline=True))
            self.assertFalse(store.read().get('api_requests'))

    def test_invalid_tool_selection_never_opens_external_paths(self):
        with tempfile.TemporaryDirectory() as root:
            store,_,_=setup(root)
            async def fake(key,messages,cfg):
                text='memory' if 'Compress' in messages[0]['content'] else '{"open_ids":["../../secret"]}'
                return SimpleNamespace(content=text,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
            r=asyncio.run(run_fulltext(store,'fixture','TST001',invoke=fake,offline=True))
            self.assertEqual(r['status'],'stopped');self.assertEqual(r['model_invocations'],3)
            self.assertTrue(all('answer' not in result for result in r['results']))

    def test_freeze_passes_only_with_bound_inputs_and_fails_on_mutation(self):
        from . import fulltext_runner as runner
        with tempfile.TemporaryDirectory() as root:
            store,index,cases=setup(root);folder=store.root/FOLDER
            pdf=Path(root)/'fixture.pdf';pdf.write_bytes(b'fixture_pdf_not_real_research')
            text=pdf.with_suffix('.extracted.txt');text.write_bytes(b'fixture text')
            index['documents'][0].update(pdf=str(pdf),pdf_sha256=sha(pdf.read_bytes()),extracted_text_sha256=sha(text.read_bytes()))
            c=cases[0];c.update(source_group='new',source_sha256=sha(pdf.read_bytes()),split='test',answerable=True,
                 adjudication='agreement',reviews=[{'reviewer_id':who,'kind':'human','confirmed':True,
                 'checked_same_pdf_hash':sha(pdf.read_bytes()),'independent':True,'source_record':'fixture only'} for who in ['A','B']])
            plan=json.loads((folder/'plan.json').read_text());plan.update(status='frozen',runner_status='implemented_and_tested',
                 model_and_price_checked=True,expected_cases=1,expected_source_groups=1,max_calls_per_case=8,max_input_tokens=12000,
                 development_reserve_cny=0,previous_reserved_cny=0,input_policy=POLICY)
            for name,val in [('plan.json',plan),('test-cases.pending.json',{'cases':cases}),('corpus.json',index)]:
                (folder/name).write_text(json.dumps(val),encoding='utf-8')
            (store.root/'data').mkdir();(store.root/'data/source-groups-dev-v1.json').write_text('{"groups":[]}')
            paths={'plan':folder/'plan.json','cases':folder/'test-cases.pending.json','corpus':folder/'corpus.json',
                   **{key:Path(runner.__file__).with_name(name) for key,name in [('runner','fulltext_runner.py'),
                   ('execution','execution.py'),('readiness','experiment_readiness.py'),('fulltext','fulltext.py'),('pilot','pilot.py')]}}
            (folder/'freeze.json').write_text(json.dumps({k:sha(v.read_bytes()) for k,v in paths.items()}))
            self.assertTrue(runner.preflight(store)['ready'])
            text.write_bytes(b'tampered')
            self.assertFalse(runner.preflight(store)['ready'])


if __name__=='__main__':unittest.main()
