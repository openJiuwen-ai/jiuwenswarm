import asyncio
import tempfile
from pathlib import Path
import unittest
import numpy as np
from .qasper_experiment import write,sha
from .qasper_runner import inputs,make_plan,run_batch,preflight
from .qasper_dense_bundle import reconstructed,MODEL_SHA,TOKENIZER_SHA
from .test_qasper_runner import setup,response

def fixture(root):
    store,bundle,_=setup(root)
    import json
    papers=json.loads((bundle/'train/public-papers.json').read_text());qs=json.loads((bundle/'train/public-questions.json').read_text())
    (bundle/'cache').mkdir();dv=np.zeros((2,384),dtype=np.float32);dv[:,0]=1
    np.save(bundle/'cache/p.npy',dv);np.save(bundle/'cache/q.npy',dv[:1])
    cache={'model_sha256':MODEL_SHA,'tokenizer_sha256':TOKENIZER_SHA,
           'inputs':{n:sha((bundle/'train'/n).read_bytes()) for n in ('public-papers.json','public-questions.json')},
           'files':{n:sha((bundle/n).read_bytes()) for n in ('cache/p.npy','cache/q.npy')},
           'papers':{'doc':{'paragraph_ids':[p['id'] for p in papers['doc']['paragraphs']],
                            'question_ids':[q['question_id'] for q in qs],'passages':'cache/p.npy','queries':'cache/q.npy'}}}
    write(bundle/'dense-cache.json',cache)
    expected,_=reconstructed(bundle);write(bundle/'pilot-requests.json',list(expected.values()))
    return store,bundle,make_plan(bundle,store.read()['budget'])

class DenseBundleTests(unittest.TestCase):
    def test_three_conditions_reuse_budget_execution_and_reject_replay(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=fixture(root);calls=[]
            async def fake(*args):calls.append(1);return response()
            self.assertEqual(len(inputs(bundle)[0]),3)
            self.assertIn('cache/p.npy',plan['files'])
            self.assertIn('qasper_dense_bundle.py',plan['code'])
            result=asyncio.run(run_batch(store,bundle,plan,'fixture-only',transport=fake))
            self.assertEqual(result['status'],'completed');self.assertEqual(len(calls),3)
            with self.assertRaises(ValueError):preflight(store,bundle,plan)
            self.assertFalse(store.read()['budget']['enabled'])

    def test_cache_tampering_and_incomplete_or_gold_modified_prompt_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=fixture(root)
            requests,_=inputs(bundle);original=(bundle/'pilot-requests.json').read_bytes()
            requests[0]['messages'][1]['content']+=' reference answer injection'
            write(bundle/'pilot-requests.json',requests)
            with self.assertRaises(ValueError):inputs(bundle)
            (bundle/'pilot-requests.json').write_bytes(original)
            write(bundle/'pilot-requests.json',inputs(bundle)[0][:-1])
            with self.assertRaises(ValueError):inputs(bundle)
            (bundle/'pilot-requests.json').write_bytes(original)
            with (bundle/'cache/p.npy').open('ab') as f:f.write(b'changed')
            with self.assertRaises(ValueError):preflight(store,bundle,plan)

    def test_wrong_model_paths_and_row_mapping_rejected(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=fixture(root);p=bundle/'dense-cache.json';original=p.read_bytes()
            for mode in ('model','path','rows'):
                cache=json.loads(original)
                if mode=='model':cache['model_sha256']='different'
                if mode=='path':cache['files']['cache/../../escape.npy']='x'
                if mode=='rows':cache['papers']['doc']['paragraph_ids'].reverse()
                write(p,cache)
                with self.assertRaises(ValueError):inputs(bundle)
            p.write_bytes(original)

if __name__=='__main__':unittest.main()
