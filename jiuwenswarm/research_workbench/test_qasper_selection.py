import json
import unittest

from .qasper_experiment import sha
from .qasper_selection import select, checked_request


def sample():
    paragraphs = [{'id': str(i), 'text': t, 'sha256': sha(t.encode())} for i,t in enumerate([
        'alpha '*100, 'alpha beta', 'alpha gamma', 'beta delta', 'beta delta'])]
    return {'question_id':'q', 'paper_id':'p', 'question':'alpha beta gamma', 'split':'train'}, {
        'paper_id':'p', 'title':'Fixture', 'paragraphs':paragraphs}


class SelectionTests(unittest.TestCase):
    def test_budgets_hashes_and_determinism(self):
        q,p = sample()
        for mode in ('prefix','pack','coverage'):
            a=select(q,p,mode,k=2,evidence_bytes=25)
            self.assertLessEqual(a['evidence_utf8_bytes'],25)
            self.assertLessEqual(len(a['visible_ids']),2)
            self.assertEqual(a,select(q,p,mode,k=2,evidence_bytes=25))
        with self.assertRaises(ValueError): select(q,p,'coverage',evidence_bytes=0)
        p['paragraphs'][0]['text']+='tampered'
        with self.assertRaises(ValueError): select(q,p,'coverage')

    def test_scan_skips_oversized_and_uses_later_evidence(self):
        q,p = sample();q['question']='alpha'
        # Force a long high-score paragraph with many repetitions.
        prefix=select(q,p,'prefix',evidence_bytes=20)
        packed=select(q,p,'pack',evidence_bytes=20)
        self.assertEqual(prefix['visible_ids'],[])
        self.assertGreater(len(packed['visible_ids']),0)
        self.assertNotIn('0',packed['visible_ids'])

    def test_no_gold_in_requests_or_semantic_claim(self):
        q,p=sample()
        original=select(q,p,'coverage')
        self.assertEqual(original,select(q|{'gold':'PRIVATE ANSWER'},p|{'gold':'PRIVATE'},'coverage'))
        checked=checked_request(original)
        self.assertEqual(original['messages'][1],checked['messages'][1])
        self.assertEqual(original['visible_ids'],checked['visible_ids'])
        self.assertNotIn('PRIVATE',json.dumps(checked))
        self.assertNotEqual(original['messages'][0],checked['messages'][0])


if __name__=='__main__': unittest.main()
