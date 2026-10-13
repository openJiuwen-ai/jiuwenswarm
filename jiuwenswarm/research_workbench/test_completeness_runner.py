import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from .qasper_experiment import write
from .qasper_runner import inputs,short_answer_request,make_plan,run_batch,score_batch,invoke
from .qasper_selection import checked_request
from .test_qasper_runner import setup,response


class CompletenessTests(unittest.TestCase):
    def test_total_deadline_cancels_without_retry_and_failure_retains_hold(self):
        from openjiuwen.core.foundation.llm import Model
        calls=[];cancelled=[]
        async def stuck(*args,**kwargs):
            calls.append(1)
            try:await asyncio.sleep(10)
            finally:cancelled.append(1)
        with patch.object(Model,'invoke',stuck),patch('jiuwenswarm.research_workbench.qasper_runner.TOTAL_TIMEOUT_SECONDS',.01):
            with self.assertRaises(TimeoutError):
                asyncio.run(invoke('fixture-only',[{'role':'user','content':'fixture'}],{'model':'deepseek-flash'}))
        self.assertEqual(calls,[1]);self.assertEqual(cancelled,[1])
        with tempfile.TemporaryDirectory() as root:
            store,bundle,plan=setup(root)
            async def timeout(*args):raise TimeoutError('do-not-log-private')
            result=asyncio.run(run_batch(store,bundle,plan,'fixture-only',transport=timeout))
            self.assertEqual(len(result['records']),1)
            self.assertEqual(result['records'][0]['status'],'request_failed')
            self.assertEqual(result['records'][0]['error_category'],'timeout')
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertGreater(store.read()['api_requests'][0]['held_units'],0)

    def test_batch_cap_cannot_be_expanded_unbounded(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=setup(root)
            with self.assertRaises(ValueError):make_plan(bundle,store.read()['budget'],batch_cny=2.01)
            rows=json.loads((bundle/'pilot-requests.json').read_text())
            write(bundle/'pilot-requests.json',[rows[0]]*97)
            with self.assertRaises(ValueError):inputs(bundle)

    def test_paired_same_evidence_hash_binding_and_execution(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=setup(root)
            original=json.loads((bundle/'pilot-requests.json').read_text())
            base=short_answer_request(next(r for r in original if r['method']=='bm25@8'))
            check=checked_request(base|{'method':'bm25-check@8'})
            write(bundle/'pilot-requests.json',[base,check])
            rows,_=inputs(bundle)
            self.assertEqual(rows[0]['messages'][1],rows[1]['messages'][1])
            plan=make_plan(bundle,store.read()['budget'])
            async def fake(*args):return response()
            result=asyncio.run(run_batch(store,bundle,plan,'fixture-only',transport=fake))
            self.assertEqual(result['status'],'completed')
            self.assertEqual(len(result['records']),2)
            self.assertEqual(set(score_batch(bundle,store.root/'qasper-runs'/plan['id'])['methods']),{'bm25@8','bm25-check@8'})
            write(bundle/'pilot-requests.json',[check])
            with self.assertRaises(ValueError):inputs(bundle)
            check['messages'][0]['content']+='changed'
            write(bundle/'pilot-requests.json',[base,check])
            with self.assertRaises(ValueError):inputs(bundle)


if __name__=='__main__':unittest.main()
