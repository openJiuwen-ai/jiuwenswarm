import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from .pilot import run_pilot
from .reporting import audit
from .test_pilot import setup


class ReportingTest(unittest.TestCase):
    def make_run(self, root):
        store=setup(root)
        async def fake(key,messages,cfg):
            s=messages[0]['content']
            value='[X1] Synthetic test fixture' if 'Compress' in s else ('{"open_ids":["X1"]}' if 'Choose' in s else '{"answer":"Fixture","citations":["X1"],"abstain":false}')
            return SimpleNamespace(content=value,usage_metadata=SimpleNamespace(input_tokens=10,output_tokens=5))
        record=asyncio.run(run_pilot(store,'fixture', 'DEV001',fake))
        self.assertEqual(record['status'],'completed')
        return store,record

    def test_missing_reviews_remain_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            store,run=self.make_run(root)
            summary=audit(root)
            self.assertEqual(summary['total_requests'],8)
            self.assertEqual(summary['reviewed_outputs'],0)
            self.assertTrue(all(a['answer_correct'] is None for a in summary['answers']))

    def test_modified_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store,run=self.make_run(root)
            (Path(root)/'pilots'/run['id']/'B1-result.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Artifact mismatch'): audit(root)

    def test_wrong_review_binding_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store,run=self.make_run(root)
            store.change('test',lambda s:s.update(independent_reviews=[{'run_id':run['id'],'method':'M','answer_sha256':'wrong'}]))
            with self.assertRaisesRegex(ValueError,'Review does not match'): audit(root)

if __name__=='__main__':unittest.main()
