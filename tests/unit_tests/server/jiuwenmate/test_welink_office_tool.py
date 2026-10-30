import subprocess
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.jiuwenmate.welink_office_tool import OfficeRuntime, operation_args


def test_mail_arguments_are_fixed_and_recipients_validated():
    args = operation_args('mail.send', {'to': ['a@example.com'], 'subject': 'Hello', 'body': 'line1\nline2'})
    assert args == ['mail', 'send', '--to', 'a@example.com', '--subject', 'Hello', '--body', 'line1\nline2']
    with pytest.raises(ValueError):
        operation_args('mail.send', {'to': ['-x'], 'subject': 'Hello', 'body': 'Hi'})
    with pytest.raises(ValueError):
        operation_args('mail.send', {'to': ['a@example.com'], 'subject': 'Hello', 'body': 'Hi', 'shell': 'x'})


def test_calendar_dates_and_invites():
    args = operation_args('calendar.create', {'subject': 'review', 'start': '2026-11-01T14:00:00+08:00', 'end': '2026-11-01T15:00:00+08:00', 'attendee': ['a@example.com']})
    assert args[-2:] == ['--attendee', 'a@example.com']
    with pytest.raises(ValueError):
        operation_args('calendar.create', {'subject': 'x', 'start': '2026-11-01T14:00:00', 'end': '2026-11-01T15:00:00'})
    with pytest.raises(ValueError):
        operation_args('calendar.list', {'start': '2026-11-02', 'end': '2026-11-01'})


class Cli:
    def __init__(self, timeout=False):
        self.calls = []
        self.timeout = timeout

    def run(self, args, timeout=30):
        self.calls.append(args)
        if args[:2] == ['mail', 'send'] and self.timeout:
            raise subprocess.TimeoutExpired('welink-cli', 30)
        return SimpleNamespace(ok=True, stdout='{"success":true,"ItemId":"test-id"}')


def test_write_discovery_and_replay_survive_new_instance(tmp_path):
    cli = Cli()
    payload = {'to': ['a@example.com'], 'subject': 'Hi', 'body': 'hello'}
    first = OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account')
    result = first.execute('request1', 'mail.send', payload)
    assert result['status'] == 'submitted'
    assert cli.calls[0] == ['mail', 'autodiscover', '--email', 'owner@example.com']
    second = OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account')
    assert second.execute('request1', 'mail.send', payload) == result
    assert sum(c[:2] == ['mail', 'send'] for c in cli.calls) == 1


def test_timeout_does_not_resend(tmp_path):
    cli = Cli(timeout=True)
    runtime = OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account')
    payload = {'to': ['a@example.com'], 'subject': 'Hi', 'body': 'hello'}
    assert runtime.execute('request1', 'mail.send', payload)['status'] == 'unknown'
    assert runtime.execute('request1', 'mail.send', payload)['status'] == 'unknown'
    assert sum(c[:2] == ['mail', 'send'] for c in cli.calls) == 1


def test_recovery_cannot_bypass_unknown_with_changed_payload(tmp_path):
    cli = Cli(timeout=True)
    runtime = OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account')
    payload = {'to': ['a@example.com'], 'subject': 'Hi', 'body': 'hello'}
    runtime.execute('request1', 'mail.send', payload)
    other = OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account')
    assert other.execute('request1', 'mail.send', {**payload, 'body': 'changed'})['status'] == 'unknown'
    assert sum(c[:2] == ['mail', 'send'] for c in cli.calls) == 1


def test_invocation_only_injects_for_bound_account_owner(tmp_path, monkeypatch):
    from dataclasses import dataclass
    from jiuwenswarm.server.jiuwenmate import welink_inbound as inbound
    from jiuwenswarm.server.jiuwenmate.welink_office_tool import prepare_welink_office_invocation, PROVIDER

    @dataclass
    class Execution:
        namespace_id: str = 'local'
        agent_id: str = 'agent1'
        request_id: str = 'request1'
        config_fingerprint: str = 'old'

    class Spec:
        tools = []
        def model_copy(self, update):
            clone = Spec()
            clone.tools = update['tools']
            return clone
        def fingerprint(self):
            return 'new'

    from jiuwenswarm.server.jiuwenmate.welink_office_settings import WelinkOfficeSettings
    store = WelinkOfficeSettings(tmp_path / 'agents.db')
    with store._transaction() as conn:
        conn.execute('INSERT INTO welink_office_settings VALUES (?,?,?,1)', ('agent1', 'owner', 'owner@example.com'))
    monkeypatch.setattr(inbound, 'welink_binding_for_agent', lambda agent: SimpleNamespace(agent_id='agent1', account='owner', cli_path='welink-cli', reader_credential_dir=str(tmp_path), env_name='pro'))
    spec = Spec()
    context = SimpleNamespace(execution=Execution(), extras={})
    route = SimpleNamespace(channel='welink')
    outsider = SimpleNamespace(source=SimpleNamespace(sender_id='outsider'))
    assert prepare_welink_office_invocation(spec, context, request_input=outsider, route_snapshot=route, settings_path=tmp_path / 'agents.db') is spec
    assert not context.extras
    owner = SimpleNamespace(source=SimpleNamespace(sender_id='owner'))
    result = prepare_welink_office_invocation(spec, context, request_input=owner, route_snapshot=route, settings_path=tmp_path / 'agents.db')
    assert result.tools[0].type == PROVIDER
    assert context.execution.config_fingerprint == 'new'
    assert context.extras[PROVIDER][1] == 'request1'


@pytest.mark.asyncio
async def test_tool_stops_after_unknown(tmp_path):
    from jiuwenswarm.server.jiuwenmate.welink_office_tool import WelinkOfficeTool
    cli = Cli(timeout=True)
    tool = WelinkOfficeTool(OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account'), 'req')
    inputs = {'operation': 'mail.send', 'payload': {'to': ['a@example.com'], 'subject': 'Hi', 'body': 'hello'}}
    assert (await tool.invoke(inputs))['status'] == 'unknown'
    assert (await tool.invoke(inputs))['stop_retrying']
    assert sum(c[:2] == ['mail', 'send'] for c in cli.calls) == 1


def test_discovery_business_failure_never_sends(tmp_path):
    cli = Cli()
    cli.run = lambda args, timeout=30: SimpleNamespace(ok=True, stdout='{"success":false,"code":403}')
    runtime = OfficeRuntime(cli, 'owner@example.com', tmp_path / 'ops.db', 'account')
    result = runtime.execute('req', 'mail.send', {'to': ['a@example.com'], 'subject': 'x', 'body': 'y'})
    assert result['code'] == 'WELINK_MAIL_DISCOVERY_FAILED'


@pytest.mark.asyncio
async def test_unconfigured_tool_explains_setup_instead_of_silent_fallback(tmp_path):
    from jiuwenswarm.server.jiuwenmate.welink_office_tool import ConfiguredOfficeRuntime, WelinkOfficeTool, welink_execution_guidance
    binding = SimpleNamespace(agent_id='agent1', account='owner')
    runtime = ConfiguredOfficeRuntime(tmp_path / 'agents.db', binding)
    result = await WelinkOfficeTool(runtime, 'req').invoke({'operation': 'calendar.list', 'payload': {'start': '2026-11-02', 'end': '2026-11-03'}})
    assert result['code'] == 'WELINK_OFFICE_NOT_CONNECTED'
    assert '邮箱' in result['message']
    assert 'Bash' in welink_execution_guidance(SimpleNamespace(channel='welink'))
    assert welink_execution_guidance(SimpleNamespace(channel='feishu')) == ''


def test_video_meeting_uses_meeting_command_and_millisecond_times(tmp_path):
    payload = {'subject': '评审', 'start': '2026-11-02T14:00:00+08:00',
               'end': '2026-11-02T15:00:00+08:00', 'meeting_user_list': ['h001', 'c002']}
    args = operation_args('meeting.create', payload)
    from datetime import datetime
    assert args == ['meeting', 'create', '--subject', '评审',
                    '--start-time', str(int(datetime.fromisoformat(payload['start']).timestamp() * 1000)),
                    '--end-time', str(int(datetime.fromisoformat(payload['end']).timestamp() * 1000)),
                    '--meeting-user-list', 'h001', 'c002']
    cli = Cli()
    runtime = OfficeRuntime(cli, '', tmp_path / 'ops.db', 'owner')
    assert runtime.execute('video', 'meeting.create', payload)['status'] == 'submitted'
    assert runtime.execute('video', 'meeting.create', payload)['status'] == 'submitted'
    assert cli.calls == [args]
    for accounts in ([], ['a@example.com'], ['--help'], ['h001 c002']):
        with pytest.raises(ValueError):
            operation_args('meeting.create', {**payload, 'meeting_user_list': accounts})


def test_video_meeting_does_not_require_mailbox_connection(tmp_path, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.welink_office_tool import ConfiguredOfficeRuntime
    from jiuwenswarm.server.jiuwenmate import welink_inbound
    binding = SimpleNamespace(agent_id='a', account='h001', reader_credential_dir=str(tmp_path),
                              cli_path='welink-cli', env_name='pro')
    monkeypatch.setattr(welink_inbound, 'welink_binding_for_agent', lambda _: binding)
    calls = []
    def run(self, args, timeout=60):
        calls.append(args)
        return SimpleNamespace(ok=True, stdout='{"resultCode":0,"meetingId":"primary-key"}')
    monkeypatch.setattr('jiuwenswarm.server.jiuwenmate.welink_office_tool.WelinkCli.run', run)
    result = ConfiguredOfficeRuntime(tmp_path / 'settings.db', binding).execute('r', 'meeting.create', {
        'subject': 'review', 'start': '2026-11-02T14:00:00+08:00',
        'end': '2026-11-02T15:00:00+08:00', 'meeting_user_list': ['h001']})
    assert result['status'] == 'submitted'
    assert len(calls) == 1 and calls[0][:2] == ['meeting', 'create']


def test_video_meeting_timeout_is_not_retried(tmp_path):
    class TimeoutCli(Cli):
        def run(self, args, timeout=60):
            self.calls.append(args)
            raise subprocess.TimeoutExpired('welink-cli', timeout)
    cli = TimeoutCli()
    payload = {'subject': 'review', 'start': '2026-11-02T14:00:00+08:00',
               'end': '2026-11-02T15:00:00+08:00', 'meeting_user_list': ['h001']}
    for _ in range(2):
        result = OfficeRuntime(cli, '', tmp_path / 'ops.db', 'h001').execute('r', 'meeting.create', payload)
        assert result['status'] == 'unknown' and result['stop_retrying']
    assert len(cli.calls) == 1
