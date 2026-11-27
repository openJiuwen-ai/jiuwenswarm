"""临时真实技能、真实 SDK 状态；只替换文件与模型等外部边界。"""

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from segment_state_fixtures import storage
from test_accelerator_catalog import isolated
from test_accelerator_manifest import declaration, make_skill
from jiuwenswarm.server.runtime.skill_turbo.agent import SkillTurbo
from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment

SOURCE = '''from skill_turbo_runtime import PlanNode

class Leaf(PlanNode):
    async def _execute(self, inputs):
        return await self.call_tool('segment_probe', stage=self.plan_name, inputs=inputs)

class Root(PlanNode):
    async def _execute(self, inputs):
        for child in self.sub_plans:
            await self.execute_subplan(child, inputs)
        if inputs.get('pause'):
            await self.call_tool('ask_user', questions=[{'question': '确认继续？', 'options': ['继续', '修改']}])
            if inputs.get('pause_twice'):
                await self.call_tool('ask_user', questions=[{'question': '再次确认？', 'options': ['继续', '修改']}])
        return await self.call_tool('segment_probe', stage='root', inputs=inputs)

root = Root(plan_name='root', instruction='segment root', sub_plans=[Leaf(plan_name='first', instruction='first stage')])
'''


@pytest.fixture
def segment(tmp_path, monkeypatch, isolated, storage):
    spec = declaration()
    spec['input_schema']['properties'] = {'pause': {'type': 'boolean'}, 'pause_twice': {'type': 'boolean'}}
    skill, meta, raw = make_skill(tmp_path / 'skills', items=[spec])
    import jiuwenswarm.common.utils as utils
    monkeypatch.setattr(utils, 'resolve_agent_registered_skill_dirs', lambda: [skill.parent])
    code = skill / 'turbo/turbo_codes/demo/accel_prep.py'
    code.write_text(SOURCE, encoding='utf-8')
    (code.parent / 'demo_gen_root.py').write_text('from .accel_prep import root\n', encoding='utf-8')
    calls = Counter()
    control = {'status': 'ok', 'child_status': 'ok', 'artifact': True, 'handoff': True}
    workspace = tmp_path / 'work'
    workspace.mkdir()
    session = storage.session()

    async def probe(stage, inputs):
        calls[stage] += 1
        if control.get('wait') and stage == 'root':
            await control['wait']()
        if stage != 'root':
            return {'status': control['child_status'], **control.get('child_output', {})}
        if control.get('exception'):
            raise control['exception']
        path = Path(inputs['run_dir']) / 'prep.json'
        if control['artifact']:
            path.write_text(json.dumps({'query': inputs['query']}), encoding='utf-8')
        result = {'facts': {'prep_fact': 'ready'}}
        if control['status'] is not None:
            result['status'] = control['status']
        if control['handoff']:
            result['__segment_handoff__'] = {'facts': {'prep_fact': 'ready'}, 'next_steps': ['outline']}
        result.update(control.get('root_output', {}))
        return result

    def new():
        value = SkillTurbo({'card': session._card, 'enabled': True, 'skill_codes_dir': ''})
        monkeypatch.setattr(value._env, 'register_tools', AsyncMock())
        monkeypatch.setattr(value._env, 'get_tool_function', lambda name: probe if name == 'segment_probe' else None)
        monkeypatch.setattr(value._planner, 'plan', AsyncMock(side_effect=AssertionError('planner forbidden')))
        return value

    inputs = {'query': 'original', 'session_id': session.get_session_id(),
              'metadata': {'output_dir': str(workspace), '__skill_turbo_model_identity__': {'model_ref': 'test'}}}

    async def run(*, instance=None, params=None, resume_ctx=None, answers=None, sid='segment-session', request_id='req', **changes):
        value = instance or new()
        events = []
        async for event in value.run_accelerator_stream(
            skill='demo-craft', accelerator='prep', params=params, inputs={**inputs, 'session_id': sid, **changes},
            session=storage.session(sid), request_id=request_id, channel_id='test',
            resume_ctx=resume_ctx, user_input=answers,
        ):
            events.append(event)
        return value.segment_result(), events

    return SimpleNamespace(new=new, run=run, calls=calls, control=control, inputs=inputs,
                           skill=skill, raw=raw, code=code, storage=storage, workspace=workspace)
