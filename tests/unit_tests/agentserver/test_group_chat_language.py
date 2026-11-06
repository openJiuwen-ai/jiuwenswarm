"""Group mentions must render context using a supported core language."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict


def team_config(language):
    return {
        'preferred_language': language,
        'modes': {'team': {'test': {'agents': {'leader': {}, 'teammate': {}}}}},
    }


@pytest.mark.parametrize('language, expected', [('zh', 'cn'), (' ZH ', 'cn'), ('cn', 'cn'), ('en', 'en')])
def test_team_config_uses_core_language(language, expected, monkeypatch):
    monkeypatch.setenv('AGENT_PROMPT_LANGUAGE', 'en')
    config = team_config(language)
    spec = load_team_spec_dict(config_base=config)
    assert spec['language'] == expected
    assert config['preferred_language'] == language


@pytest.mark.asyncio
async def test_chinese_group_mention_builds_real_core_context(monkeypatch):
    from openjiuwen.agent_teams.group_chat import handler

    spec = load_team_spec_dict(config_base=team_config('zh'))
    row = SimpleNamespace(
        message_id='trigger', team_name='group', from_member_name='user', content='@expert hello', timestamp=10,
        meta=json.dumps({
            'type': 'group_chat', 'client_message_id': 'message', 'sender_name': 'User',
            'mentions': ['expert'], 'attachments': [],
        }),
    )
    conversation = SimpleNamespace(session_id='group', history_path=Path('/tmp/history.jsonl'))
    backend = SimpleNamespace(
        group_chat_spec=SimpleNamespace(language=spec['language']), team_name='group', message_manager=None,
        group_conversation=AsyncMock(return_value=conversation),
        db=SimpleNamespace(message=SimpleNamespace(get_broadcast_read_at=AsyncMock(return_value=0))),
    )
    monkeypatch.setattr(handler, 'sync_history', AsyncMock(return_value=[]))
    context = await handler.context_for(backend, 'expert', row)
    assert '@expert hello' in context
    assert 'trigger' in context
