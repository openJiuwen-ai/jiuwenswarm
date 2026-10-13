import copy
import unittest

from .review_scoring import digest, reconcile, statistics_report, validate_ratings


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.cases = [{'case_id': f'C{i}', 'source_group': f'P{i//2}', 'answerable': i % 2 == 0}
                      for i in range(4)]
        self.records = []
        for case in self.cases:
            for rep in range(2):
                for method in ('A00','A10','A01','A11'):
                    answer = {'answer': 'x' if case['answerable'] else '',
                              'abstain': not case['answerable'], 'citations': ['source'] if case['answerable'] else []}
                    self.records.append({'response_id': f"{case['case_id']}-{rep}-{method}",
                        'case_id': case['case_id'], 'repetition': rep, 'method': method,
                        'answer': answer, 'answer_sha256': digest(answer), 'delivery_status': 'completed'})
        self.a = self.form('Alice')
        self.b = self.form('Bob')

    def form(self, name):
        return {'answers': [{'response_id': r['response_id'], 'answer_sha256': r['answer_sha256'],
            'reviewer': name, 'date': '2026-09-28', 'human_confirmed': True, 'prior_exposure': 'none',
            'rationale': 'Synthetic test only: all evidence checked.', 'answer_correct': True,
            'all_substantive_claims_supported': True, 'all_emitted_citations_support_claims': True,
            'input_relative_abstention_appropriate': True, 'corpus_relative_task_success': True}
            for r in self.records]}

    def test_user_acceptance_is_distinct_and_hash_bound(self):
        self.a['answers'][0]['human_confirmed'] = None
        rows = copy.deepcopy(self.b['answers'])
        for i, row in enumerate(rows):
            row['confirmation_role'] = 'user_acceptance_not_reviewer_attestation'
            row['review_hashes'] = [digest(self.a['answers'][i]), digest(self.b['answers'][i])]
        doc = {'approval_mode':'user_accepted_scoring', 'answers':rows,
               'user_confirmation':{'confirmed':True, 'statement':'Synthetic user acceptance',
                                    'date':'2026-09-29', 'ratings_sha256':digest(rows)}}
        review = reconcile(self.records, self.a, self.b, doc)
        self.assertFalse(review['independent_review_verified'])
        self.assertEqual(review['first_complete'],31)
        self.assertEqual(review['resolved'],32)
        stats = statistics_report(self.records, self.cases, review, bootstrap_samples=20)
        self.assertEqual(stats['review_basis'],'user_accepted_scoring')
        stale = copy.deepcopy(doc)
        stale['answers'][0]['answer_correct'] = False
        with self.assertRaises(ValueError): reconcile(self.records, self.a, self.b, stale)
        self.a['answers'][1]['rationale'] = 'changed after approval'
        with self.assertRaises(ValueError): reconcile(self.records, self.a, self.b, doc)

    def test_user_acceptance_requires_explicit_role_and_confirmation(self):
        rows = copy.deepcopy(self.b['answers'])
        for i, row in enumerate(rows):
            row['review_hashes'] = [digest(self.a['answers'][i]), digest(self.b['answers'][i])]
        doc = {'approval_mode':'user_accepted_scoring', 'answers':rows,
               'user_confirmation':{'confirmed':True, 'statement':'Synthetic',
                   'date':'2026-09-29', 'ratings_sha256':digest(rows)}}
        with self.assertRaises(ValueError): reconcile(self.records,self.a,self.b,doc)
        doc['user_confirmation']['confirmed'] = False
        with self.assertRaises(ValueError): reconcile(self.records,self.a,self.b,doc)

    def test_blank_and_partial_stay_pending(self):
        review = reconcile(self.records)
        self.assertIsNone(statistics_report(self.records, self.cases, review)['primary_effect'])
        self.a['answers'][0]['human_confirmed'] = None
        self.assertEqual(reconcile(self.records, self.a, self.b)['resolved'], 31)

    def test_tamper_and_duplicates_fail(self):
        self.a['answers'][0]['answer_sha256'] = 'bad'
        with self.assertRaises(ValueError): validate_ratings(self.a, self.records)
        self.a = self.form('Alice')
        self.a['answers'].append(self.a['answers'][0])
        with self.assertRaises(ValueError): validate_ratings(self.a, self.records)

    def test_no_coercion_or_single_reviewer_consensus(self):
        self.a['answers'][0]['answer_correct'] = 1
        with self.assertRaises(ValueError): validate_ratings(self.a, self.records)
        with self.assertRaises(ValueError): reconcile(self.records, self.b, self.b)

    def test_dispute_requires_bound_adjudication(self):
        self.a['answers'][0]['answer_correct'] = False
        review = reconcile(self.records, self.a, self.b)
        self.assertEqual(len(review['disputes']), 1)
        resolved = {**self.b['answers'][0], 'reviewer': 'Adjudicator',
                    'review_hashes': [digest(self.a['answers'][0]), digest(self.b['answers'][0])]}
        review = reconcile(self.records, self.a, self.b, {'answers':[resolved]})
        self.assertEqual(review['resolved'], 32)
        resolved['review_hashes'] = ['stale','stale']
        with self.assertRaises(ValueError): reconcile(self.records,self.a,self.b,{'answers':[resolved]})

    def test_rejections_remain_in_fixed_denominator(self):
        self.records[3]['delivery_status'] = 'rejected'
        review = reconcile(self.records, self.a, self.b)
        result = statistics_report(self.records, self.cases, review, bootstrap_samples=200)
        self.assertEqual(result['methods']['A11']['denominator'], 8)
        self.assertEqual(result['methods']['A11']['grounded_success'], 7/8)
        self.assertEqual(result['primary_effect'], -1/8)
        self.assertEqual(len(result['case_means']), 4)
        self.assertEqual(result, statistics_report(self.records,self.cases,review,bootstrap_samples=200))

    def test_unanswerable_positive_answer_does_not_succeed(self):
        for row in self.records:
            if row['case_id']=='C1':
                row['answer']['abstain'] = False
        review = reconcile(self.records,self.a,self.b)
        result = statistics_report(self.records,self.cases,review,bootstrap_samples=100)
        self.assertEqual(result['methods']['A00']['unanswerable'], .5)

    def test_missing_run_is_not_reported_as_complete(self):
        review = reconcile(self.records,self.a,self.b)
        result = statistics_report(self.records[:-1],self.cases,review)
        self.assertIsNone(result['methods'])


if __name__ == '__main__': unittest.main()
