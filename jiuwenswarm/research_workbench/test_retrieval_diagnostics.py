import unittest
from .fulltext import sha
from .retrieval_diagnostics import BM25, coverage, agreement

def index(texts):
    return {'documents':[{'id':'D'}], 'chunks':[{'id':f'C{i}','document_id':'D','page_1based':i+1,'text':t,'sha256':sha(t.encode())} for i,t in enumerate(texts)]}

class DiagnosticTests(unittest.TestCase):
    def test_rank_ties_and_no_labels(self):
        self.assertAlmostEqual(BM25(index(['alpha','beta'])).search('alpha')[0]['score'],.6931471805599453)
        retriever=BM25(index(['rare phrase','common common common','common text','rare phrase']))
        self.assertEqual([h['id'] for h in retriever.search('rare')],['C0','C3'])
        self.assertEqual(retriever.search('unseen'),[])
        with self.assertRaises(ValueError):retriever.search({'question':'rare','gold_answer':'C1'})
        with self.assertRaises(ValueError):retriever.search('rare',0)
        bad=index(['changed']);bad['chunks'][0]['sha256']='bad'
        with self.assertRaises(ValueError):BM25(bad)

    def test_document_page_quote_and_unknown(self):
        case={'answerable':True,'evidence_quotes':[{'page_1based':1,'quote':'rare\nphrase'}]}
        rows=index(['rare phrase'])['chunks']
        self.assertTrue(coverage(case,rows,'D')['all_quotes'])
        self.assertFalse(coverage(case,rows,'other')['any_quote'])
        rows[0]['page_1based']=2
        self.assertFalse(coverage(case,rows,'D')['any_quote'])
        rows[0]['page_1based']=1;rows[0]['text']='rare'
        result=coverage(case,rows,'D')
        self.assertTrue(result['page_hit']);self.assertFalse(result['any_quote'])
        self.assertIsNone(coverage({'answerable':False},rows,'D')['any_quote'])

    def test_agreement_preserves_missing_and_degeneracy(self):
        r=agreement([True,False,None],[True,False,True])
        self.assertEqual((r['n'],r['excluded'],r['kappa']),(2,1,1.))
        self.assertIsNone(agreement([True],[True])['kappa'])
        self.assertEqual(agreement([True,False],[False,True])['kappa'],-1.)
        with self.assertRaises(ValueError):agreement([True],[])

if __name__=='__main__':unittest.main()
