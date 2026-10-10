import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from . import qasper_validation as validation
from .qasper_experiment import write,sha
from .qasper_dense_bundle import reconstructed
from .qasper_memory import compress,METHODS
from .qasper_runner import inputs,make_plan,run_batch,preflight,score_batch
from .test_qasper_dense_bundle import fixture
from .test_qasper_runner import response

def dev_fixture(root):
    store,bundle,_=fixture(root)
    (bundle/'train').rename(bundle/'dev')
    questions=json.loads((bundle/'dev/public-questions.json').read_text())
    for q in questions:q['split']='dev'
    write(bundle/'dev/public-questions.json',questions)
    cache=json.loads((bundle/'dense-cache.json').read_text())
    cache['inputs']={n:sha((bundle/'dev'/n).read_bytes()) for n in ('public-papers.json','public-questions.json')}
    write(bundle/'dense-cache.json',cache)
    qid=questions[0]['question_id']
    registry=Path(root)/'registry.json'
    write(registry,{'protocol_sha256':validation.PROTOCOL,'batches':{'1':{
      'files':{f'dev/{n}':sha((bundle/'dev'/n).read_bytes()) for n in ('public-papers.json','public-questions.json','gold-references.json')},
      'order':[[qid,m] for m in METHODS]}}})
    write(bundle/'validation-batch.json',{'protocol_sha256':validation.PROTOCOL,'batch':1})
    write(bundle/'memory-mode.json',{'question_ids':[qid],'version':'extractive-v2','byte_limit':2400})
    with patch.object(validation,'REGISTRY',registry):
        values,_=reconstructed(bundle);base=values[(qid,'hybrid-check@8')];requests=[]
        for m in METHODS:
            r=copy.deepcopy(base) if m=='memory-full' else compress(base,m.split('-')[1])
            r['method']=m;requests.append(r)
        write(bundle/'pilot-requests.json',requests)
    return store,bundle,registry

class ValidationTests(unittest.TestCase):
    def test_dev_executes_scores_and_cannot_replay(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,registry=dev_fixture(root)
            with patch.object(validation,'REGISTRY',registry):
                plan=make_plan(bundle,store.read()['budget']);calls=[]
                async def fake(*args):calls.append(1);return response()
                result=asyncio.run(run_batch(store,bundle,plan,'test-only',transport=fake))
                self.assertEqual(result['status'],'completed');self.assertEqual(len(calls),3)
                self.assertEqual(plan['kind'],'qasper_dev_prospective_validation')
                self.assertIn('dev/gold-references.json',plan['files'])
                self.assertEqual(len(score_batch(bundle,store.root/'qasper-runs'/plan['id'])['methods']),3)
                with self.assertRaises(ValueError):preflight(store,bundle,plan)

    def test_dev_marker_identity_gold_and_order_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,registry=dev_fixture(root)
            with patch.object(validation,'REGISTRY',registry):
                self.assertEqual(len(inputs(bundle)[0]),3)
                for name in ('validation-batch.json','dev/public-questions.json','dev/public-papers.json','dev/gold-references.json'):
                    p=bundle/name;original=p.read_bytes()
                    p.write_text('{}',encoding='utf-8')
                    with self.assertRaises((ValueError,KeyError)):inputs(bundle)
                    p.write_bytes(original)
                requests=inputs(bundle)[0];write(bundle/'pilot-requests.json',list(reversed(requests)))
                with self.assertRaises(ValueError):inputs(bundle)
                write(bundle/'pilot-requests.json',requests)
                (bundle/'validation-batch.json').unlink()
                with self.assertRaises(ValueError):inputs(bundle)

    def test_proposed_cap_cannot_change_live_budget_or_enable_calls(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,registry=dev_fixture(root)
            with patch.object(validation,'REGISTRY',registry):
                before=store.read()
                plan=make_plan(bundle,before['budget']|{'total_cny':30})
                with self.assertRaises(ValueError):preflight(store,bundle,plan)
                self.assertEqual(store.read(),before)
                with self.assertRaises(ValueError):make_plan(bundle,before['budget']|{'total_cny':31})
                (bundle/'train').mkdir()
                with self.assertRaises(ValueError):inputs(bundle)

if __name__=='__main__':unittest.main()
