from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jiuwenswarm.server.jiuwenmate.agent_mail_local_provider import LocalAgentMailRuntime
from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import AgentMailScope


@pytest.mark.parametrize(
    ('operation', 'payload', 'expected'),
    [
        ('mail.send', {'to': ['a@example.com', 'b@example.com'], 'subject': 'Hello', 'body': 'World'},
         ['message', '+send', '--to', 'a@example.com', '--to', 'b@example.com', '--subject', 'Hello', '--body', 'World']),
        ('mail.reply', {'id': 'msg_1', 'cc': ['c@example.com'], 'body': 'Thanks'},
         ['message', '+reply', '--id', 'msg_1', '--cc', 'c@example.com', '--body', 'Thanks']),
        ('mail.forward', {'id': 'msg_1', 'to': 'a@example.com', 'body': 'FYI'},
         ['message', '+forward', '--id', 'msg_1', '--to', 'a@example.com', '--body', 'FYI']),
        ('mail.delete', {'id': 'msg_1'}, ['message', '+trash', '--id', 'msg_1']),
    ],
)
def test_local_runtime_builds_allowlisted_write_commands(operation, payload, expected):
    assert LocalAgentMailRuntime._operation_args(operation, payload) == expected


def test_local_runtime_rejects_unknown_write_fields():
    with pytest.raises(RuntimeError, match='invalid Agent Mail operation payload'):
        LocalAgentMailRuntime._operation_args(
            'mail.send', {'to': 'a@example.com', 'subject': 'Hi', 'body': 'Hello', 'command': 'rm -rf'}
        )


@pytest.mark.asyncio
async def test_local_runtime_completes_cli_two_step_confirmation_for_send(tmp_path):
    calls = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append(args)
        if args[-2:] == ['auth', 'status']:
            output = '{"ok":true,"data":{"logged_in":true}}'
        elif args[-1:] == ['+me']:
            output = '{"ok":true,"data":{"aliases":[{"email":"employee@example.com"}]}}'
        elif '--confirmation-token' in args:
            output = '{"ok":true,"data":{"message_id":"sent-1"}}'
        else:
            output = '{"ok":true,"data":{"confirmation_token":"one-time-token"}}'
        return subprocess.CompletedProcess(args, 0, output, '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)
    result = await runtime.execute(
        AgentMailScope(namespace_id='local', agent_id='employee-1'),
        'mail.send',
        {'to': 'recipient@example.com', 'subject': 'Hello', 'body': 'World'},
    )

    assert result == {'ok': True, 'data': {'message_id': 'sent-1'}}
    first, second = calls[-2:]
    assert first[-8:] == [
        'message', '+send', '--to', 'recipient@example.com', '--subject', 'Hello', '--body', 'World'
    ]
    assert second == [*first, '--confirmation-token', 'one-time-token']


def test_local_runtime_reuses_employee_cached_cli_without_npx_registry_access(tmp_path):
    scope = AgentMailScope(namespace_id='local', agent_id='employee-1')
    bin_dir = tmp_path / 'node-bin'
    bin_dir.mkdir()
    (bin_dir / 'node').touch()
    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=bin_dir / 'npx')
    package = runtime._scope_root(scope) / 'home/.cache/npm/_npx/cache-key/node_modules/@tencent-qqmail/agently-cli'
    (package / 'scripts').mkdir(parents=True)
    (package / 'package.json').write_text('{"version":"1.0.18"}', encoding='utf-8')
    script = package / 'scripts/run.js'
    script.write_text('', encoding='utf-8')

    assert runtime._command(scope, 'message', '+list') == [
        str(bin_dir / 'node'), str(script), 'message', '+list'
    ]


@pytest.mark.asyncio
async def test_local_runtime_scopes_cli_environment_per_employee(tmp_path):
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        calls.append((args, env))
        return subprocess.CompletedProcess(
            args,
            0,
            'tip: Authorization required; follow the agently mail skill OAuth login flow.\\n'
            '{"ok":true,"data":{"logged_in":false}}',
            '',
        )

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)
    first = AgentMailScope(namespace_id='local', agent_id='employee-1')
    second = AgentMailScope(namespace_id='local', agent_id='employee-2')

    assert await runtime.status(first) == {'status': 'not_connected', 'cli_version': '1.0.18'}
    assert await runtime.status(second) == {'status': 'not_connected', 'cli_version': '1.0.18'}

    assert calls[0][0] == ['npx', '--yes', '--package=@tencent-qqmail/agently-cli@1.0.18', 'agently-cli', 'auth', 'status']
    assert calls[0][1]['HOME'] != calls[1][1]['HOME']
    assert calls[0][1]['XDG_CONFIG_HOME'].startswith(calls[0][1]['HOME'])
    assert calls[0][1]['AGENTLY_WORKSPACE'] == 'jiuwenmate'
    assert calls[1][1]['AGENTLY_WORKSPACE'] == 'jiuwenmate'


@pytest.mark.asyncio
async def test_local_runtime_keeps_a_saved_login_when_the_host_workspace_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTLY_WORKSPACE', 'codex')
    monkeypatch.setenv('CURSOR_AGENT', '1')
    recorded = {}

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        recorded['env'] = env
        return subprocess.CompletedProcess(args, 0, '{"ok":true,"data":{"logged_in":false}}', '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)
    scope = AgentMailScope(namespace_id='local', agent_id='employee-1')
    home = runtime._scope_root(scope) / 'home'
    config = home / '.agently-cli' / 'agents' / 'codex'
    secret = home / 'Library' / 'Application Support' / 'agently-cli' / 'agents' / 'codex'
    config.mkdir(parents=True)
    secret.mkdir(parents=True)
    (config / 'config.json').write_text('{"app_id":"saved"}', encoding='utf-8')
    (secret / 'bootstrap_token.enc').write_text('saved', encoding='utf-8')

    await runtime.status(scope)

    assert recorded['env']['AGENTLY_WORKSPACE'] == 'jiuwenmate'
    assert (home / '.agently-cli' / 'agents' / 'jiuwenmate' / 'config.json').is_file()
    assert (home / 'Library' / 'Application Support' / 'agently-cli' / 'agents' / 'jiuwenmate' / 'bootstrap_token.enc').is_file()
    assert not config.exists()
    assert not secret.exists()


@pytest.mark.asyncio
async def test_local_runtime_status_returns_verified_mailbox_address(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[-2:] == ['auth', 'status']:
            output = '{"ok":true,"data":{"logged_in":true}}'
        else:
            output = '{"ok":true,"data":{"aliases":[{"email":"employee@example.com"}]}}'
        return subprocess.CompletedProcess(args, 0, output, '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)

    result = await runtime.status(AgentMailScope(namespace_id='local', agent_id='employee-1'))

    assert result == {'status': 'connected', 'cli_version': '1.0.18', 'email': 'employee@example.com'}


@pytest.mark.asyncio
async def test_local_runtime_treats_a_revoked_token_as_disconnected(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[-2:] == ['auth', 'status']:
            return subprocess.CompletedProcess(args, 0, '{"ok":true,"data":{"logged_in":true}}', '')
        output = '{"ok":false,"error":{"type":"unauthorized","code":401,"message":"Invalid or expired access token"}}'
        return subprocess.CompletedProcess(args, 3, output, '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)

    result = await runtime.status(AgentMailScope(namespace_id='local', agent_id='employee-1'))

    assert result == {'status': 'not_connected', 'cli_version': '1.0.18'}


@pytest.mark.asyncio
async def test_local_runtime_reports_other_identity_failures_as_errors(tmp_path):
    def runner(args, *, env, input_text=None, timeout=None, cwd):
        if args[-2:] == ['auth', 'status']:
            return subprocess.CompletedProcess(args, 0, '{"ok":true,"data":{"logged_in":true}}', '')
        return subprocess.CompletedProcess(args, 1, '{"ok":false,"error":{"type":"internal","code":500}}', '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)

    result = await runtime.status(AgentMailScope(namespace_id='local', agent_id='employee-1'))

    assert result == {'status': 'error', 'cli_version': '1.0.18'}


@pytest.mark.asyncio
async def test_local_runtime_starts_official_cli_and_returns_its_oauth_url(tmp_path):
    class Process:
        pid = 4321

        @staticmethod
        def poll():
            return None

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 0, '{"ok":true,"data":{"logged_in":false}}', '')

    def popen(args, *, stdout, env, **kwargs):
        assert args == ['npx', '--yes', '--package=@tencent-qqmail/agently-cli@1.0.18', 'agently-cli', 'auth', 'login']
        assert '.agent-mail-bin' in env['PATH']
        opener = Path(env['PATH'].split(':', 1)[0]) / 'open'
        assert opener.read_text(encoding='utf-8') == '#!/bin/sh\nexit 0\n'
        assert len(list((tmp_path / 'd29deebbd3878c9a' / 'auth').glob('*.frontend'))) == 1
        stdout.write(('请访问 https://agent.qq.com/oauth?ticket=opaque-token' + chr(10)).encode())
        stdout.flush()
        return Process()

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner, popen=popen)
    result = await runtime.begin_auth(AgentMailScope(namespace_id='local', agent_id='employee-1'))

    assert result['status'] == 'authorizing'
    assert result['auth_url'] == 'https://agent.qq.com/oauth?ticket=opaque-token'
    assert result['frontend_opens_auth'] is True
    assert result['operation_id'].startswith('d29deebbd3878c9a.')


@pytest.mark.asyncio
async def test_local_runtime_expires_abandoned_authorization(tmp_path, monkeypatch):
    import os
    import time

    scope = AgentMailScope(namespace_id='local', agent_id='employee-1')

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 0, '{"ok":true,"data":{"logged_in":false}}', '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)
    operation_id = f'{runtime._scope_digest(scope)}.0123456789abcdef0123456789abcdef'
    log_path, pid_path, exit_path = runtime._auth_paths(scope, operation_id)
    log_path.parent.mkdir(parents=True)
    log_path.write_text('auth url', encoding='utf-8')
    pid_path.write_text('4321', encoding='utf-8')
    runtime._frontend_auth_marker(scope, operation_id).write_text('managed', encoding='utf-8')
    runtime._current_auth_path(scope).write_text(operation_id, encoding='utf-8')
    old_time = time.time() - 301
    os.utime(pid_path, (old_time, old_time))
    killed = []
    monkeypatch.setattr(os, 'killpg', lambda process_id, sig: killed.append((process_id, sig)))

    result = await runtime.status(scope)

    assert result == {'status': 'not_connected', 'cli_version': '1.0.18'}
    assert killed and killed[0][0] == 4321
    assert not runtime._current_auth_path(scope).exists()
    assert not exit_path.exists()


@pytest.mark.asyncio
async def test_local_runtime_cleans_up_legacy_untracked_authorization(tmp_path, monkeypatch):
    import os

    scope = AgentMailScope(namespace_id='local', agent_id='employee-1')

    def runner(args, *, env, input_text=None, timeout=None, cwd):
        return subprocess.CompletedProcess(args, 0, '{"ok":true,"data":{"logged_in":false}}', '')

    runtime = LocalAgentMailRuntime(root=tmp_path, npx_path=Path('npx'), runner=runner)
    operation_id = f'{runtime._scope_digest(scope)}.0123456789abcdef0123456789abcdef'
    log_path, pid_path, _ = runtime._auth_paths(scope, operation_id)
    log_path.parent.mkdir(parents=True)
    log_path.write_text('https://agent.qq.com/oauth', encoding='utf-8')
    pid_path.write_text('4321', encoding='utf-8')
    runtime._current_auth_path(scope).write_text(operation_id, encoding='utf-8')
    killed = []
    monkeypatch.setattr(os, 'killpg', lambda process_id, sig: killed.append((process_id, sig)))

    result = await runtime.status(scope)

    assert result == {'status': 'not_connected', 'cli_version': '1.0.18'}
    assert killed
    assert runtime._current_operation(scope) is None

