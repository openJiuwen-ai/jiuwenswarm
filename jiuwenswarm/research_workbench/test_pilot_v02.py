import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from .pilot import run_pilot, output_gate, evidence_catalogue, PROTOCOL_V02
from .test_pilot import setup


class PilotV02Test(unittest.TestCase):
    def test_reject_contradictory_abstention_and_forged_quote(self):
        contradictory={'answer':'Yes','citations':[],'abstain':True,'reason':'uncertain','supports':[]}
        self.assertFalse(output_gate(contradictory,{})['passed'])
        forged={'answer':'Claim','citations':['A'],'abstain':False,'supports':[{'citation':'A','quote':'invented'}]}
        self.assertFalse(output_gate(forged,{'A':'original'})['passed'])
        forged['supports'][0]['quote']='original'
        result=output_gate(forged,{'A':'original'})
        self.assertTrue(result['passed'])
        self.assertIsNone(result['semantic_entailment'])
        self.assertFalse(output_gate(forged,{'B':'original'})['passed'])
        self.assertTrue(output_gate({'answer':'','citations':[],'abstain':True,'reason':'No evidence','supports':[]},{})['passed'])

    def test_versioned_flow_retains_rejections_without_retry_or_label_leak(self):
        with tempfile.TemporaryDirectory() as root:
            store=setup(root);calls=[]
            async def fake(key,messages,settings):
                calls.append(messages)
                self.assertNotIn('SECRET_GOLD_MUST_NOT_LEAK',json.dumps(messages))
                instruction=messages[0]['content']
                if 'Compress' in instruction: content='[X1] Fixture memory.'
                elif 'Choose' in instruction:
                    payload=json.loads(messages[1]['content'])
                    self.assertIn('excerpt',payload['catalogue'][0])
                    content='{"open_ids":["X1"]}'
                else: content='{"answer":"Yes","citations":[],"abstain":true,"supports":[],"reason":"No evidence"}'
                return SimpleNamespace(content=content,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
            record=asyncio.run(run_pilot(store,'fixture-key','DEV001',fake,protocol_version='pilot-0.2'))
            self.assertEqual(len(calls),8)
            self.assertEqual(record['protocol']['version'],'pilot-0.2')
            self.assertEqual(record['status'],'completed')
            self.assertTrue(all(r['status']=='rejected' for r in record['results']))
            self.assertTrue(all(r['answer']['answer']=='Yes' for r in record['results']))

    def test_catalogue_budget_does_not_silently_drop_items(self):
        e={'id':'A','title':'x'*3000,'locator':'p1','text':'raw'}
        with self.assertRaises(ValueError): evidence_catalogue([e],PROTOCOL_V02)

if __name__=='__main__':unittest.main()
