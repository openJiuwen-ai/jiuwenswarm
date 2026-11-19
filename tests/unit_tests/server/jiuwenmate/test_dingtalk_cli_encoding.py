"""DWS uses UTF-8 regardless of the host's default subprocess encoding."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import LocalDingTalkCliRuntime, _default_runner
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_cli_chinese_json_is_decoded_as_utf8_on_gbk_hosts(tmp_path, monkeypatch, stream):
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'gbk')
    output = json.dumps({'account': '会议策划助手'}, ensure_ascii=False)
    script = f'import sys; sys.{stream}.buffer.write({output.encode("utf-8")!r})'

    result = _default_runner(
        [sys.executable, '-c', script], env=dict(os.environ), input_text=None, timeout=5, cwd=str(tmp_path),
    )

    assert result.returncode == 0
    assert json.loads(getattr(result, stream)) == {'account': '会议策划助手'}


def test_cli_chinese_input_is_encoded_as_utf8_on_gbk_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'gbk')
    script = 'import json,sys; print(json.dumps(sys.stdin.buffer.read().decode("utf-8")))'

    result = _default_runner(
        [sys.executable, '-c', script], env=dict(os.environ), input_text='会议纪要', timeout=5, cwd=str(tmp_path),
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == '会议纪要'


@pytest.mark.asyncio
async def test_authorized_chinese_account_is_connected_on_gbk_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'gbk')
    binary = tmp_path / 'dws.py'
    binary.write_text(
        'import json,sys\n'
        'data = {"success": True, "authenticated": True} if "auth" in sys.argv else '
        '{"success": True, "profiles": [{"profile": "corp:user", "isCurrent": True, '
        '"userId": "user", "userName": "测试账号", "corpName": "测试组织"}]}\n'
        'sys.stdout.buffer.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))\n',
        encoding='utf-8',
    )

    def runner(args, **kwargs):
        return _default_runner([sys.executable, str(binary), *args[1:]], **kwargs)

    runtime = LocalDingTalkCliRuntime(
        root=tmp_path / 'runtime', workspace_root=tmp_path / 'workspaces', dws_path=Path(sys.executable),
        config_loader=lambda: {'channels': {'dingtalk': {'apps': [{
            'client_id': 'ding-app', 'client_secret': 'test-secret', 'enabled': True,
            'persistent_agent_id': 'agent-1', 'allow_from': [],
        }]}}},
        runner=runner,
    )

    result = await runtime.dingtalk_auth_status(DingTalkCliScope('local', 'agent-1', app_id='ding-app'))

    assert result['status'] == 'connected'
    assert result['account'] == '测试账号'
    assert result['organization'] == '测试组织'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_cli_non_utf8_failure_output_preserves_exit_status(tmp_path, stream):
    output = '命令无法执行'.encode('gbk')
    script = f'import sys; sys.{stream}.buffer.write({output!r}); sys.exit(9)'

    result = _default_runner(
        [sys.executable, '-c', script], env=dict(os.environ), input_text=None, timeout=5, cwd=str(tmp_path),
    )

    assert result.returncode == 9
    assert getattr(result, stream) == output.decode('utf-8', errors='replace')
