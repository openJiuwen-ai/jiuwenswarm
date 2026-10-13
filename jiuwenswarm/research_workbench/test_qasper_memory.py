import copy
import json
import tempfile
import unittest
from .qasper_memory import fragments,compress,memory_inputs,METHODS
from .qasper_dense_bundle import reconstructed
from .qasper_experiment import write
from .qasper_runner import make_plan
from .test_qasper_dense_bundle import fixture

class MemoryTests(unittest.TestCase):
    def test_offsets_unicode_and_budget(self):
        text='The model uses data. Additional data includes 42 examples. Only 中文 is retained.'
        paragraphs=[{'id':'p','text':text}]
        req={'messages':[{'role':'system','content':'JSON'},{'role':'user','content':json.dumps({'question':'What data is used?','paragraphs':paragraphs})}]}
        for row in fragments(paragraphs):self.assertEqual(row['text'],text[row['start']:row['end']])
        original=copy.deepcopy(req)
        for mode in ('plain','qualifiers'):
            result=compress(req,mode,65)
            self.assertLessEqual(result['evidence_utf8_bytes'],65)
            for row in result['memory_provenance']:
                self.assertEqual(row['text'],text[row['start']:row['end']])
            self.assertEqual(result,compress(req,mode,65))
        self.assertEqual(req,original)
        self.assertEqual(compress(req,'plain',1)['visible_ids'],[])

    def test_limit_is_bounded_and_parent_ids_are_unique(self):
        req={'messages':[{'content':'system'},{'content':json.dumps({'question':'Data?',
            'paragraphs':[{'id':'p','text':'Data one. Data two. Data three.'}]})}]}
        out=compress(req,'qualifiers',2400)
        self.assertEqual(out['visible_ids'],['p'])
        self.assertEqual(out['evidence_utf8_bytes'],len(json.loads(out['messages'][1]['content'])['paragraphs'][0]['text'].encode()))
        with self.assertRaises(ValueError):compress(req,'unknown')

    def test_three_way_reconstruction_and_provenance_tamper(self):
        with tempfile.TemporaryDirectory() as root:
            store,bundle,_=fixture(root);expected,_=reconstructed(bundle)
            base=next(v for (q,m),v in expected.items() if m=='hybrid-check@8')
            spec={'question_ids':[base['question_id']],'byte_limit':2400,'version':'extractive-v2'}
            write(bundle/'memory-mode.json',spec);requests=[]
            for m in METHODS:
                r=copy.deepcopy(base) if m=='memory-full' else compress(base,m.split('-')[1])
                r['method']=m;requests.append(r)
            write(bundle/'pilot-requests.json',requests)
            self.assertEqual(len(memory_inputs(bundle)[0]),3)
            plan=make_plan(bundle,store.read()['budget'])
            self.assertIn('memory-mode.json',plan['files']);self.assertIn('qasper_memory.py',plan['code'])
            requests[-1]['memory_provenance']=[{'text':'injected'}];write(bundle/'pilot-requests.json',requests)
            with self.assertRaises(ValueError):memory_inputs(bundle)

    def test_adjacent_qualifier_without_query_word_is_preserved(self):
        req={'messages':[{'content':'system'},{'content':json.dumps({'question':'Which dataset?',
            'paragraphs':[{'id':'p','text':'The dataset is Alpha. Only English was included.'},
                          {'id':'unrelated','text':'Only random animals occur.'}]})}]}
        plain=compress(req,'plain');qualified=compress(req,'qualifiers')
        self.assertNotIn('Only English',plain['messages'][1]['content'])
        self.assertIn('Only English',qualified['messages'][1]['content'])
        self.assertNotIn('random animals',qualified['messages'][1]['content'])

if __name__=='__main__':unittest.main()
