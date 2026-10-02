import hashlib
import json
import unittest

from .qasper_experiment import adapt, answer_f1, evidence_f1, evaluate, make_request, parse_prediction, ParagraphRanker


def fixture():
    def ann(answer):return {'annotation_id':'r','answer':answer}
    base={'unanswerable':False,'extractive_spans':[],'free_form_answer':'','yes_no':False,
          'evidence':['Repeated evidence.','FLOAT SELECTED: table','Not in corpus.']}
    return {'doc':{'title':'Example','full_text':[{'section_name':'Result','paragraphs':['Repeated evidence.','Repeated evidence.']}],
        'qas':[{'question_id':'q','question':'Does it work?', 'answers':[ann(base),ann(base|{'unanswerable':True})]}]}}


class QasperTests(unittest.TestCase):
    def test_multireference_figures_missing_and_boolean(self):
        papers,questions,gold,diag=adapt(fixture(),'dev')
        self.assertEqual(gold['q'][0]['answer'],'No')
        self.assertEqual(diag['mixed_answerability_questions'],1)
        self.assertEqual(diag['unmapped_text_evidence_items'],1)
        self.assertEqual(diag['float_evidence_items'],1)
        self.assertNotIn('answers',questions[0]);self.assertNotIn('qas',papers['doc'])
        self.assertEqual(evaluate(gold,{})['Missing predictions'],1)
        self.assertEqual(evaluate(gold,{'q':{'answer':'Unanswerable','evidence':[]}})['Answer F1'],1)
        with self.assertRaises(ValueError):adapt(fixture(),'test')

    def test_official_empty_duplicates_and_tokens(self):
        self.assertEqual(answer_f1('',''),0)
        self.assertEqual(answer_f1('The CAT!','cat'),1)
        self.assertAlmostEqual(answer_f1('cat cat dog','cat dog'),.8)
        self.assertEqual(evidence_f1([],[]),1)
        self.assertAlmostEqual(evidence_f1(['x','x'],['x']),2/3)

    def test_public_request_cannot_leak_gold_or_invisible_citations(self):
        papers,qs,_,_=adapt(fixture(),'train');paper=papers['doc'];q=qs[0]|{'gold':'DO NOT LEAK'}
        ranking=ParagraphRanker(paper['paragraphs']).rank('repeated','bm25')
        request=make_request(q,paper,ranking,k=8,evidence_bytes=20)
        self.assertEqual(len(request['visible_ids']),1)
        self.assertNotIn('DO NOT LEAK',json.dumps(request))
        visible=request['visible_ids'][0]
        value={'answer':'No','unanswerable':False,'evidence_ids':[visible]}
        self.assertEqual(parse_prediction(json.dumps(value),request,paper)['evidence'],['Repeated evidence.'])
        for change in [{'evidence_ids':['unknown']},{'evidence_ids':[visible,visible]},
                       {'unanswerable':True},{'answer':''},{'unanswerable':1}]:
            with self.assertRaises(ValueError):parse_prediction(json.dumps(value|change),request,paper)
        self.assertEqual(make_request(q,paper,ranking,evidence_bytes=1)['visible_ids'],[])
        with self.assertRaises(ValueError):
            parse_prediction('{"answer":"Yes","answer":"No","unanswerable":true,"evidence_ids":[]}',request,paper)

    def test_rank_numeric_and_tampering(self):
        rows=[{'id':i,'text':t,'sha256':hashlib.sha256(t.encode()).hexdigest()} for i,t in [('a','cat'),('b','dog')]]
        ranker=ParagraphRanker(rows)
        self.assertAlmostEqual(ranker.rank('cat','bm25')[0]['score'],.6931471805599453)
        self.assertEqual(ranker.rank('absent','bm25'),[])
        rows[0]['text']='changed'
        with self.assertRaises(ValueError):ParagraphRanker(rows)


if __name__=='__main__':unittest.main()
