import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from .app import Store
from .execution import BudgetSettings, run_stage
from .pilot import run_pilot, preflight_pilot, answer_metrics


def setup(root):
    store=Store(root)
    text='Synthetic test fixture; contains no actual research evidence.'
    data={'kind':'paraphrased_development_pilot','review_status':'fixture',
          'evidence':[{'id':'X1','title':'Fixture','version':'v1','locator':'fixture','text':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}],
          'cases':[{'case_id':'DEV001','question':'Fixture question?','gold_answer_points_draft':['SECRET_GOLD_MUST_NOT_LEAK']}]}
    (Path(root)/'data').mkdir()
    (Path(root)/'data/pilot-corpus.json').write_text(json.dumps(data),encoding='utf-8')
    store.change('test only',lambda s:s.update(budget=BudgetSettings(enabled=True,total_cny=30,task_cny=2,
                     input_cny_per_million=2,output_cny_per_million=8,pricing_checked=True).model_dump()))
    return store


class PilotTest(unittest.TestCase):
    def test_real_orchestration_with_mock_model_no_gold_leak(self):
        with tempfile.TemporaryDirectory() as root:
            store=setup(root)
            self.assertEqual(preflight_pilot(store)['live_calls'],0)
            calls=[]
            async def fake(key,messages,cfg):
                calls.append(messages)
                self.assertNotIn('SECRET_GOLD_MUST_NOT_LEAK',json.dumps(messages))
                instruction=messages[0]['content']
                content=('[X1] Fixture memory.' if 'Compress' in instruction else
                         '{"open_ids":["X1"]}' if 'Choose' in instruction else
                         '{"answer":"Fixture only","citations":["X1"],"abstain":false}')
                return SimpleNamespace(content=content,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
            result=asyncio.run(run_pilot(store,'fixture-key','DEV001',fake))
            self.assertEqual(result['status'],'completed')
            self.assertEqual(len(calls),8)
            self.assertEqual(len(result['results']),3)
            self.assertTrue(any(t.get('tool')=='open_evidence' for t in result['trace']))
            self.assertTrue(all(r['metrics']['answer_correct'] is None for r in result['results']))
            folder=Path(root)/'pilots'/result['id']
            for name,digest in result['files'].items():
                self.assertEqual(hashlib.sha256((folder/name).read_bytes()).hexdigest(),digest)
            self.assertNotIn('fixture-key',store.file.read_text(encoding='utf-8'))
            self.assertEqual(len(json.loads((folder/'review-blind.json').read_text(encoding='utf-8'))),3)
            # The analysis stage consumes real pipeline records, without fake
            # manual experiment entries. Tampering must block the next request.
            task_id='a'*32
            task_folder=Path(root)/'executions'/task_id
            task_folder.mkdir(parents=True)
            (task_folder/'lit.md').write_text('Fixture literature')
            (task_folder/'plan.md').write_text('Fixture plan')
            task={'id':task_id,'project':store.read()['project'],'papers':[],
                  'stages':[{'stage':'literature','status':'completed','file':'lit.md'},
                            {'stage':'plan','status':'completed','file':'plan.md'}]}
            store.change('fixture parent task',lambda s:s.update(executions=[task]))
            original=(folder/'M-result.json').read_bytes()
            (folder/'M-result.json').write_text('tampered')
            with self.assertRaises(ValueError):
                asyncio.run(run_stage(store,'fixture-key','analysis',task_id,invoke=fake))
            self.assertEqual(len(calls),8)
            (folder/'M-result.json').write_bytes(original)
            reviewed=result['results'][0]
            review={'run_id':result['id'],'method':reviewed['method'],'answer_sha256':'wrong','answer_correct':True}
            store.change('fixture review',lambda s:s.update(independent_reviews=[review]))
            with self.assertRaises(ValueError):
                asyncio.run(run_stage(store,'fixture-key','analysis',task_id,invoke=fake))
            review['answer_sha256']=hashlib.sha256(json.dumps(reviewed['answer'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            store.change('bind fixture review',lambda s:s.update(independent_reviews=[review]))
            analysis=asyncio.run(run_stage(store,'fixture-key','analysis',task_id,invoke=fake))
            self.assertEqual(analysis['status'],'completed')
            self.assertIn('hash_checked_pilot_results',calls[-1][1]['content'])

    def test_failure_stops_all_remaining_calls_and_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            store=setup(root)
            calls=[]
            async def fail(*args):
                calls.append(1)
                raise RuntimeError('DO_NOT_LOG_SECRET')
            result=asyncio.run(run_pilot(store,'fixture-key','DEV001',fail))
            self.assertEqual(len(calls),1)
            self.assertEqual(result['status'],'stopped')
            self.assertGreater(result['held_cny'],0)
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertNotIn('DO_NOT_LOG_SECRET',store.file.read_text(encoding='utf-8'))

    def test_invalid_tools_and_missing_citations_are_not_success_metrics(self):
        self.assertIsNone(answer_metrics({'answer':'No evidence','citations':[],'abstain':True},set())['citation_id_validity'])
        self.assertEqual(answer_metrics({'answer':'x','citations':['fake'],'abstain':False},{'X1'})['invalid_citations'],['fake'])
        with tempfile.TemporaryDirectory() as root:
            store=setup(root)
            calls=[]
            async def fake(key,messages,cfg):
                calls.append(messages)
                content='{"open_ids":["../../secret"]}' if 'Choose' in messages[0]['content'] else ('[X1] Fixture' if 'Compress' in messages[0]['content'] else '{"answer":"fixture","citations":["X1"],"abstain":false}')
                return SimpleNamespace(content=content,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
            result=asyncio.run(run_pilot(store,'fixture-key','DEV001',fake))
            self.assertEqual(result['status'],'stopped')
            self.assertFalse(any(t.get('tool')=='open_evidence' for t in result['trace']))
            self.assertLess(len(calls),8)


if __name__=='__main__':unittest.main()
