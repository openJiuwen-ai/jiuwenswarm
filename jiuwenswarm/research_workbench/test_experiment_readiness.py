import unittest
from .experiment_readiness import inspect,public_questions


class AdmissionTest(unittest.TestCase):
    def fixture(self):
        plan=dict(status='frozen',runner_status='implemented_and_tested',model_and_price_checked=True,expected_cases=1,expected_source_groups=1,repetitions=2,max_calls_per_case=8,max_input_tokens=12000,max_output_tokens=1000,input_cny_per_million=2,output_cny_per_million=8,development_reserve_cny=.768,previous_reserved_cny=1.297178,authorization_cap_cny=15)
        c=dict(case_id='T001',source_group='new-paper',split='test',source_sha256='fixture-hash',question='Question only',gold_answer='SECRET',answerable=True,evidence_quotes=['SECRET_QUOTE'],adjudication='agreement',reviews=[dict(reviewer_id=n,kind='human',confirmed=True,checked_same_pdf_hash='fixture-hash',independent=True,source_record='local-record') for n in ['A','B']])
        return plan,c
    def test_assistant_and_duplicate_reviewer_do_not_pass(self):
        p,c=self.fixture();self.assertTrue(inspect(p,[c],set())['ready'])
        c['reviews'][1]['kind']='assistant';self.assertFalse(inspect(p,[c],set())['ready'])
        c['reviews'][1].update(kind='human',reviewer_id='A');self.assertFalse(inspect(p,[c],set())['ready'])
    def test_seen_source_and_excess_budget_block(self):
        p,c=self.fixture();self.assertFalse(inspect(p,[c],{'new-paper'})['ready'])
        p['authorization_cap_cny']=1;self.assertIn('authorization_budget_exceeded',inspect(p,[c],set())['blockers'])
    def test_gold_does_not_enter_question_packet(self):
        p,c=self.fixture();self.assertEqual(public_questions([c]),[{'case_id':'T001','question':'Question only'}])

if __name__=='__main__':unittest.main()
