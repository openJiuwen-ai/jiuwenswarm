"""Offline boundary and persistence regression checks; no model/network calls."""
import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from jiuwenswarm.research_workbench.app import create_app


class WorkbenchTest(unittest.TestCase):
    def test_budget_and_key_settings_do_not_execute_or_persist_key(self):
        with tempfile.TemporaryDirectory() as temp:
            app=create_app(temp)
            with TestClient(app) as client:
                token=re.search(r'name="research-token" content="([^"]+)"',client.get('/').text)[1]
                headers={'X-Research-Token':token}
                self.assertEqual(client.put('/api/session-key',json={'api_key':'fixture-secret'}).status_code,403)
                self.assertEqual(client.put('/api/session-key',json={'api_key':'fixture-secret'},headers=headers).status_code,200)
                state=client.get('/api/state').json()
                self.assertTrue(state['key_configured'])
                self.assertNotIn('fixture-secret',json.dumps(state))
                self.assertNotIn('fixture-secret',(Path(temp)/'workspace.json').read_text(encoding='utf-8'))
                self.assertEqual(state['budget_status']['calls'],0)
                self.assertEqual(client.put('/api/budget',json={'enabled':True},headers=headers).status_code,422)
                self.assertEqual(client.post('/api/stages',json={'stage':'literature'},headers=headers).status_code,409)
                self.assertEqual(client.get('/api/state').json()['budget_status']['calls'],0)
            self.assertFalse(create_app(temp).state.api_key)

    def test_evidence_export_and_write_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(temp)
            with TestClient(app) as client:
                token = re.search(r'name="research-token" content="([^"]+)"',client.get('/').text)[1]
                headers = {'X-Research-Token': token}
                self.assertEqual(client.post('/api/prepare').status_code,403)
                self.assertEqual(client.get('/',headers={'host':'attacker.example'}).status_code,400)
                data=client.get('/api/state').json()
                self.assertEqual(len(data['papers']),3)
                self.assertFalse(any(p['verified'] for p in data['papers']))
                paper=data['papers'][0]
                paper['verified']=True
                self.assertEqual(client.put('/api/papers/'+paper['id'],json=paper,headers=headers).status_code,422)
                paper.update(note='Test fixture note, not a research finding.',locator='Fixture section')
                self.assertEqual(client.put('/api/papers/'+paper['id'],json=paper,headers=headers).status_code,200)
                self.assertEqual(client.post('/api/papers',json={**paper,'url':'javascript:alert(1)'},headers=headers).status_code,422)
                self.assertEqual(client.post('/api/papers',json=paper,headers=headers).status_code,409)
                run=client.post('/api/prepare',json={},headers=headers).json()
                self.assertFalse(run['ready_for_planning'])
                self.assertEqual(run['api_calls'],0)
                response=client.get('/api/runs/'+run['id']+'/download')
                self.assertEqual(response.status_code,200)
                with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
                    manifest=json.loads(bundle.read('manifest.json'))
                    for filename,digest in manifest['files'].items():
                        self.assertEqual(hashlib.sha256(bundle.read(filename)).hexdigest(),digest)
                    evidence=json.loads(bundle.read('evidence.json'))
                    self.assertEqual(len(evidence),1)
                    self.assertEqual(evidence[0]['id'],paper['id'])
                    self.assertIn('NOT A SUBMISSION',bundle.read('paper_outline.md').decode())
                persisted=json.loads((Path(temp)/'workspace.json').read_text(encoding='utf-8'))
                self.assertEqual(len(persisted['runs']),1)
                self.assertEqual(client.get('/api/runs/not-a-run/download').status_code,404)


if __name__=='__main__':
    unittest.main()
