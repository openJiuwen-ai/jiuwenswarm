from pathlib import Path
import tempfile
import unittest
from .research_figures import make_results_figure
from .review_scoring import reconcile, statistics_report
from . import test_review_scoring as scoring_tests


class FigureTests(unittest.TestCase):
    def test_rejected_raw_success_is_separate_from_primary_count(self):
        fixture=scoring_tests.ReviewTests();fixture.setUp()
        fixture.records[3]['delivery_status']='rejected'
        review=reconcile(fixture.records,fixture.a,fixture.b)
        stats=statistics_report(fixture.records,fixture.cases,review,bootstrap_samples=20)
        with tempfile.TemporaryDirectory() as tmp:
            data=make_results_figure(stats,review,fixture.records,fixture.cases,Path(tmp))
            last=data['bars'][3]
            self.assertEqual((last['delivered'],last['semantic_success_rejected'],last['other_failures']),(7,1,0))
            self.assertEqual(stats['primary_effect'],-1/8)
            self.assertTrue((Path(tmp)/'results-figure.pdf').read_bytes().startswith(b'%PDF'))
            self.assertEqual(data['source_group_differences']['P0'],-.25)
        with self.assertRaises(ValueError):
            make_results_figure({'status':'waiting_for_human_reviews'},review,[],[],Path('.'))


if __name__=='__main__':unittest.main()
