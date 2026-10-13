import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import DeepAgent, DeepAgentConfig
from openjiuwen.harness.rails import SkillUseRail
from .native_research import ResearchEvidenceTool
from .review_scoring import digest, import_markdown, import_text_review, validate_ratings


class NativeResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_plugin_load_dispatch_unload(self):
        agent = DeepAgent(AgentCard(name='research-offline-test')).configure(
            DeepAgentConfig(enable_task_loop=False, rails=[SkillUseRail(skills_dir=[], include_tools=False)]))
        package = Path(__file__).parent/'native_plugin'
        record = await agent.load_plugin(str(package))
        try:
            refs = [ref for ref in record.refs if ref.kind.value == 'tool']
            self.assertEqual(len(refs), 1)
            tool_id = refs[0].identity
            tool = Runner.resource_mgr.get_tool(tool_id)
            self.assertIsNotNone(tool)
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RESEARCH_WORKSPACE': tmp}):
                (Path(tmp)/'workspace.json').write_text('{}')
                with patch('jiuwenswarm.research_workbench.native_research.pipeline.audit',
                           return_value={'totals': {'requests':384}, 'protocol_hash':'synthetic'}) as audit:
                    result = await tool.invoke({'action':'audit'})
                    self.assertEqual(result['resources']['requests'],384)
                    self.assertEqual(result['new_api_calls'],0)
                    audit.assert_called_once_with(Path(tmp).resolve())
        finally:
            await agent.unload_extension(record)
        self.assertIsNone(Runner.resource_mgr.get_tool(tool_id))

    async def test_rejects_paid_actions_and_caller_paths(self):
        tool = ResearchEvidenceTool()
        for value in ({'action':'run'}, {'action':'prepare','path':'elsewhere'}, {}, None):
            with self.assertRaises(ValueError): await tool.invoke(value)
        with patch.dict(os.environ, {'RESEARCH_WORKSPACE':''}):
            with self.assertRaises(ValueError): await tool.invoke({'action':'prepare'})


class CombinedReviewTests(unittest.TestCase):
    def test_text_ids_are_not_guessed_and_punctuation_is_supported(self):
        template={'answers':[{'response_id':'aabb','answer_sha256':'hash'}]}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'text.txt'
            p.write_text('01 · aabb\n- 答案正确：是。\n02 · abb\n- 答案正确：否。\n',encoding='utf-8')
            result=import_text_review(p,template)
        self.assertIs(result['answers'][0]['answer_correct'],True)
        self.assertIsNone(result['answers'][0]['human_confirmed'])
        self.assertEqual(result['unmatched_records'][0]['received_id'],'abb')
        self.assertEqual(len(result['answers']),1)

    def test_per_paper_declarations_and_ai_draft_stay_bound(self):
        answer={'answer':'x','abstain':False,'citations':[]}
        checksum=digest(answer)
        template={'answers':[{'response_id':rid,'answer_sha256':checksum,'answer':answer} for rid in ('aa','bb')]}
        def section(rid, name, date, human):
            return f'''# 队友B · T01 · Paper
姓名：{name} 日期：{date} AI辅助范围：AI起草 本人实际核对范围：{human}
是否已看过其他人的评分或这些模型输出：未看
## 01 · {rid}
模型原始输出（不改写）：
```json
{json.dumps(answer)}
```
回答SHA256：`{checksum}`
### 本人评分（请填写）
- 答案正确：是
- 全部实质性主张有证据支持：是
- 相对指定完整论文是否完成问题：是
- 原文页码、短证据与理由：第1页测试证据。
- 我已本人核对上述评分：{human}
'''
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'combined.md'
            p.write_text(section('aa','Alice','2026-09-28','已本人核对')+
                         section('bb','Bob','____','待本人核对'),encoding='utf-8')
            value=import_markdown([p],template)
        self.assertEqual([r['reviewer'] for r in value['answers']],['Alice','Bob'])
        self.assertEqual(value['answers'][1]['ai_assistance'],'AI起草')
        accepted,pending=validate_ratings(value,template['answers'])
        self.assertEqual(list(accepted),['aa'])
        self.assertEqual(pending,['bb'])


if __name__ == '__main__': unittest.main()
