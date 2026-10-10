"""Native openJiuwen tool bridge over the frozen, offline research pipeline."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from openjiuwen.core.foundation.tool import Tool, ToolCard
from . import pipeline


class ResearchEvidenceTool(Tool):
    def __init__(self):
        super().__init__(ToolCard(
            id='research_evidence', name='research_evidence',
            description=('Audit the frozen study, prepare verified writing inputs, or assemble an English development draft. '
                         'Offline only; cannot run new experiments, score answers, or submit papers.'),
            input_params={'type': 'object', 'properties': {
                'action': {'type': 'string', 'enum': ['audit', 'prepare', 'draft']}},
                'required': ['action'], 'additionalProperties': False}))

    async def invoke(self, inputs, **kwargs):
        if not isinstance(inputs, dict) or set(inputs) != {'action'} or inputs['action'] not in ('audit', 'prepare', 'draft'):
            raise ValueError('Only action=audit, prepare or draft is supported; no path, command or API input.')
        value = os.environ.get('RESEARCH_WORKSPACE')
        if not value or not Path(value).is_absolute():
            raise ValueError('RESEARCH_WORKSPACE must explicitly select an absolute local research directory.')
        root = Path(value).resolve(strict=True)
        if not (root/'workspace.json').is_file():
            raise ValueError('No research workspace ledger found.')
        return await asyncio.to_thread(self._execute, root, inputs['action'])

    @staticmethod
    def _execute(root, action):
        if action == 'audit':
            result = pipeline.audit(root)
            return {'action': action, 'new_api_calls': 0, 'resources': result['totals'],
                    'protocol_hash': result['protocol_hash'],
                    'interpretation': 'Execution integrity only; this does not establish answer quality.'}
        if action == 'draft':
            from .paper_artifacts import assemble
            out = root/('paper-native-' + uuid4().hex)
            result = assemble(root, Path(__file__).with_name('paper_template'), out)
            return {'action': action, **result, 'paper_source': str(out/'paper.tex'),
                    'input_manifest': str(out/'paper-inputs.json'),
                    'interpretation': 'English sources assembled from verified evidence. PDF compilation and author review are still required.'}
        # Each native invocation owns its outputs; concurrent agents cannot mix manifests.
        out = root/'pipeline/native'/uuid4().hex
        if not out.resolve().is_relative_to(root):
            raise ValueError('Native output directory escapes the selected research workspace.')
        report = pipeline.run(root, out=out)
        return {'action': action, 'new_api_calls': 0, 'stages': report['stages'],
                'resources': report['resources'], 'statistics': report['statistics'],
                'human_reviews': {k: report['review'][k] for k in ('first_complete','second_complete','resolved')},
                'review_basis': report['statistics']['review_basis'],
                'review_limitation': report['statistics']['review_limitation'],
                'writing_materials': (out/'writing-materials.en.md').read_text(encoding='utf-8'),
                'report_path': str(out/'report.json'), 'manifest_path': str(out/'manifest.json'),
                'limitation': report['limitation'], 'submission_ready': False}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
