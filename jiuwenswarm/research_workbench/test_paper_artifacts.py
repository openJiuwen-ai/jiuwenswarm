import copy
from pathlib import Path
import tempfile
import unittest
from .paper_artifacts import render_results, verify_inputs
from .pipeline import put,sha


class PaperArtifactTests(unittest.TestCase):
    def test_pending_never_renders_effect_or_table(self):
        result=render_results({'status':'waiting_for_human_reviews','primary_effect':.99})
        self.assertNotIn('99',result['summary'])
        self.assertNotIn('tabular',result['table'])
        self.assertIn('not reported',result['summary'])

    def test_results_keep_negative_direction_and_fixed_denominator(self):
        stats={'status':'scored','planned_outputs':32,'resolved_outputs':32,
               'methods':{m:{'denominator':8,'grounded_success':v,'answerable':v,'unanswerable':v}
                          for m,v in [('A00',.75),('A10',.5),('A01',.5),('A11',.25)]},
               'primary_effect':-.5,'bootstrap_95_percentile':[-.75,-.25]}
        result=render_results(stats)
        self.assertIn('-50.00',result['summary'])
        self.assertIn('A11 & 8 & 25.00',result['table'])
        for mutation in ('missing','nan','effect'):
            bad=copy.deepcopy(stats)
            if mutation=='missing':bad['resolved_outputs']=31
            if mutation=='nan':bad['methods']['A00']['grounded_success']=float('nan')
            if mutation=='effect':bad['primary_effect']=.5
            with self.assertRaises(ValueError):render_results(bad)

    def fixture(self,base):
        paper=base/'paper';research=base/'research'
        paper.mkdir();research.mkdir()
        put(paper/'paper.tex','Synthetic test document only')
        put(research/'workspace.json',{})
        put(paper/'paper-inputs.json',{'files':{'paper.tex':sha(paper/'paper.tex')},
            'live_inputs':{'workspace.json':sha(research/'workspace.json')},'review_paths':[],
            'code':{},'semantic_status':'waiting_for_human_reviews'})
        return paper,research

    def test_source_and_new_review_make_manuscript_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper,research=self.fixture(Path(tmp))
            self.assertTrue(verify_inputs(paper,research)['valid'])
            put(research/'evaluations/formal-final/reviewer-A.json',{'answers':[]})
            self.assertIn('review_file_set_changed',verify_inputs(paper,research)['blockers'])
            put(paper/'paper.tex','Changed draft')
            self.assertIn('changed_or_missing:paper.tex',verify_inputs(paper)['blockers'])

    def test_build_must_bind_pdf_and_input_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper,research=self.fixture(Path(tmp))
            self.assertFalse(verify_inputs(paper,require_pdf=True)['valid'])
            (paper/'paper.pdf').write_bytes(b'%PDF-synthetic-test')
            put(paper/'build-result.json',{'pdf_sha256':sha(paper/'paper.pdf'),
                'paper_inputs_sha256':sha(paper/'paper-inputs.json'),'sources':{'paper.tex':sha(paper/'paper.tex')}})
            self.assertTrue(verify_inputs(paper,research,True)['valid'])
            (paper/'paper.pdf').write_bytes(b'%PDF-replaced')
            self.assertIn('pdf_not_bound_to_build',verify_inputs(paper,require_pdf=True)['blockers'])


if __name__=='__main__':unittest.main()
