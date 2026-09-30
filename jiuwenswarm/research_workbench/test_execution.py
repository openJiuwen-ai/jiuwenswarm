"""Budget and pipeline checks use fixtures only, never live API credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from .app import Store
from .execution import BudgetSettings, budget_view, reserve, settle, run_stage, invoke_jiuwen


def configured(store, **changes):
    cfg = BudgetSettings(enabled=True, total_cny=30, task_cny=2, calls_per_task=10,
                         input_cny_per_million=1, output_cny_per_million=2, pricing_checked=True).model_dump()
    cfg.update(changes)
    store.change('test configuration', lambda s: s.update(budget=cfg))
    return cfg


class ExecutionTest(unittest.TestCase):
    def test_budget_persistence_concurrent_limit_and_uncertain_failure(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            with self.assertRaises(ValueError): reserve(store, 'task', [])
            configured(store, calls_per_task=2)
            def attempt(_):
                try: return reserve(store, 'task', [{'role':'user','content':'test fixture'}])
                except ValueError: return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = [x for x in pool.map(attempt, range(8)) if x]
            self.assertEqual(len(results), 2)
            self.assertEqual(budget_view(Store(root).read())['calls'], 2)
            before = budget_view(store.read())['held_cny']
            settle(store, results[0])
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertEqual(budget_view(store.read())['held_cny'], before)
            with self.assertRaises(ValueError): settle(store, results[0])
            configured(store, total_cny=0.000001, task_cny=0.000001)
            with self.assertRaises(ValueError): reserve(store, 'another_task', [])
            self.assertEqual(budget_view(store.read())['calls'], 2)

    def test_stage_chain_uses_snapshots_and_blocks_missing_experiments(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            configured(store)
            with self.assertRaises(ValueError): asyncio.run(run_stage(store, 'fixture-key', 'literature'))
            def setup(state):
                state['project'].update(question='fixture question',baseline='fixture baseline',metric='fixture metric')
                for paper in state['papers'][:2]:
                    paper.update(verified=True,note='Synthetic test fixture, not scientific evidence.',locator='fixture section')
            store.change('fixture evidence', setup)
            calls = []
            async def fake(key, messages, cfg):
                calls.append(messages)
                return SimpleNamespace(content='Fixture draft [L001]',usage_metadata=SimpleNamespace(input_tokens=100,output_tokens=20))
            first = asyncio.run(run_stage(store,'fixture-key','literature',invoke=fake))
            self.assertEqual(first['status'], 'completed')
            task_id = first['task_id']
            store.change('edit after snapshot',lambda s:s['project'].update(question='new question'))
            second = asyncio.run(run_stage(store,'fixture-key','plan',task_id,invoke=fake))
            self.assertEqual(second['status'], 'completed')
            self.assertIn('fixture question',calls[1][1]['content'])
            self.assertNotIn('new question',calls[1][1]['content'])
            with self.assertRaises(ValueError): asyncio.run(run_stage(store,'fixture-key','analysis',task_id,invoke=fake))
            with self.assertRaises(ValueError): asyncio.run(run_stage(store,'fixture-key','plan',task_id,invoke=fake))
            self.assertEqual(len(calls),2)
            self.assertNotIn('fixture-key',(Path(root)/'workspace.json').read_text(encoding='utf-8'))
            self.assertEqual(len(list((Path(root)/'executions'/task_id).glob('*.md'))),2)

    def test_failed_model_has_no_retry_or_secret_in_record(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            configured(store)
            def setup(s):
                s['project'].update(question='fixture',baseline='fixture',metric='fixture')
                for p in s['papers']: p.update(verified=True,note='fixture',locator='fixture')
            store.change('setup',setup)
            calls=[]
            async def fail(*args):
                calls.append(1)
                raise RuntimeError('sensitive-key-should-never-be-logged')
            result=asyncio.run(run_stage(store,'fixture-key','literature',invoke=fail))
            self.assertEqual(result['status'],'failed')
            self.assertEqual(len(calls),1)
            self.assertFalse(store.read()['budget']['enabled'])
            self.assertNotIn('sensitive-key',store.file.read_text(encoding='utf-8'))

    def test_installed_framework_adapter_with_mock_transport(self):
        # Exercise actual openJiuwen serialization and usage parsing; substitute
        # HTTP transport before any network send. This is not a live API test.
        import httpx
        sends=[]
        async def send(client, request, **kwargs):
            sends.append(json.loads(request.content))
            return httpx.Response(200,request=request,json={
                'id':'fixture','object':'chat.completion','created':1,'model':'deepseek-flash',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'Fixture only'}}],
                'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
        with patch.object(httpx.AsyncClient,'send',send):
            response=asyncio.run(invoke_jiuwen('fixture-key',[{'role':'user','content':'fixture'}],BudgetSettings().model_dump()))
        self.assertEqual(response.content,'Fixture only')
        self.assertEqual(response.usage_metadata.total_tokens,12)
        self.assertEqual(len(sends),1)
        self.assertEqual(sends[0]['max_tokens'],1500)
        self.assertEqual(sends[0]['thinking'],{'type':'disabled'})

    def test_framework_http_failure_is_not_retried(self):
        import httpx
        sends=[]
        async def fail(client, request, **kwargs):
            sends.append(1)
            return httpx.Response(500,request=request,json={'error':{'message':'fixture failure','type':'server_error'}})
        with patch.object(httpx.AsyncClient,'send',fail):
            with self.assertRaises(Exception):
                asyncio.run(invoke_jiuwen('fixture-key',[{'role':'user','content':'fixture'}],BudgetSettings().model_dump()))
        self.assertEqual(len(sends),1)


if __name__=='__main__': unittest.main()
