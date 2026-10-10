import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from fastapi.testclient import TestClient
from .app import create_app
from .submission import check, REQUIRED


class PipelineTests(unittest.TestCase):
    def test_api_is_offline_and_requires_local_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=create_app(tmp)
            with TestClient(app) as client:
                token=re.search(r'name="research-token" content="([^"]+)"',client.get('/').text)[1]
                self.assertEqual(client.post('/api/pipeline/refresh').status_code,403)
                response=client.post('/api/pipeline/refresh',json={},headers={'X-Research-Token':token})
                self.assertEqual(response.status_code,409)  # No frozen materials in this fixture.
                self.assertEqual(client.get('/api/state').json()['budget_status']['calls'],0)
                self.assertEqual(client.get('/api/pipeline/files/workspace.json').status_code,404)
                self.assertEqual(client.get('/api/pipeline/status').json()['stages'],[])

    def test_submission_requires_final_pdf_and_matching_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'TestTeam';root.mkdir()
            self.assertFalse(check(root)['ready'])
            (root/'release-metadata.json').write_text(json.dumps({'team_name':None,'contribution_pr_url':None}))
            self.assertIn('contribution_pr_link_missing',check(root)['blockers'])
            for name in REQUIRED:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture',encoding='utf-8')
            (root/'paper/paper.pdf').write_bytes(b'%PDF-1.7\nsynthetic test header only\n%%EOF')
            (root/'code').mkdir();(root/'code/example.py').write_text('# harmless fixture')
            (root/'evidence').mkdir()
            (root/'evidence/statistics.json').write_text(json.dumps({'status':'scored','resolved_outputs':192}))
            (root/'release-metadata.json').write_text(json.dumps({'team_name':'TestTeam','final_paper_approved':True,
                'contribution_pr_url':'https://github.com/example/repo/pull/1'}))
            receipt={'paper_sha256':hashlib.sha256((root/'paper/paper.pdf').read_bytes()).hexdigest(),
                'token_sha256':hashlib.sha256(b'fixture').hexdigest(),'result_verified':True}
            (root/'AgenticReviewer/receipt.json').write_text(json.dumps(receipt))
            self.assertTrue(check(root)['ready'])
            (root/'paper/paper.pdf').write_text('changed')
            self.assertIn('reviewer_result_not_bound_to_final_pdf',check(root)['blockers'])


if __name__=='__main__':unittest.main()
