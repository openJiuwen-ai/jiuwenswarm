import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from .pilot import run_pilot
from .test_pilot import setup

class AblationTest(unittest.TestCase):
    def test_shared_memory_and_selections_with_eight_calls(self):
        with tempfile.TemporaryDirectory() as root:
            store=setup(root);calls=[];selections=[]
            async def fake(key,messages,cfg):
                calls.append(messages)
                self.assertNotIn('SECRET_GOLD_MUST_NOT_LEAK',json.dumps(messages))
                instruction=messages[0]['content']
                if 'Compress' in instruction: text='[X1] Fixture memory.'
                elif 'Choose' in instruction:
                    selections.append(json.loads(messages[1]['content']))
                    text='{"open_ids":["X1"]}'
                else:text='{"answer":"Fixture only","citations":["X1"],"abstain":false,"supports":[{"citation":"X1","quote":"Synthetic test fixture"}]}'
                return SimpleNamespace(content=text,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
            record=asyncio.run(run_pilot(store,'fixture-key','DEV001',fake,protocol_version='ablation-0.1'))
            self.assertEqual(record['status'],'completed')
            self.assertEqual(len(calls),8)
            self.assertEqual(len(selections),2)
            self.assertEqual(selections[0]['memory'],selections[1]['memory'])
            self.assertEqual({('excerpt' in s['catalogue'][0]) for s in selections},{True,False})
            self.assertEqual(sum('output_gate' in r for r in record['results']),2)
            self.assertTrue(all(r['status']=='completed' for r in record['results']))
            self.assertEqual(sum(t.get('tool')=='reuse_paired_selection' for t in record['trace']),2)
            self.assertEqual(len(store.read()['api_requests']),8)
    def test_unknown_protocol_fails_before_spend(self):
        with tempfile.TemporaryDirectory() as root:
            store=setup(root)
            with self.assertRaises(ValueError): asyncio.run(run_pilot(store,'fixture-key','DEV001',protocol_version='unknown'))
            self.assertFalse(store.read().get('api_requests'))

if __name__=='__main__':unittest.main()
