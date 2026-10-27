from pathlib import Path


def test_workswarm_app_builder_skill_is_local_and_contract_is_complete():
    text = (Path(__file__).parents[2] / "jiuwenswarm/resources/agent/workspace/skills/workswarm-app-builder/SKILL.md").read_text()
    for method in ('session.create','session.get_metadata','session.rewind','session.delete','chat.send','chat.interrupt'):
        assert method in text
    assert 'persist_session:false' in text and 'state_delta' in text
    assert '数据面直接访问 Gateway' in text
    assert 'WorkSwarmApp.request' not in text
