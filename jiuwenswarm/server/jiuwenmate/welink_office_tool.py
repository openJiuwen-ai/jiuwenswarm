"""Account-scoped WeLink mail/calendar operations with durable write receipts."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, register_tool_provider

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import WelinkCli
from jiuwenswarm.server.jiuwenmate.welink_office_settings import MAILBOX_PATTERN, WelinkOfficeSettings, discover_mailbox

PROVIDER = 'jiuwenmate.welink_office'
_EMAIL = MAILBOX_PATTERN
_FIELDS = {
    'mail.send': {'to', 'cc', 'subject', 'body'},
    'calendar.list': {'start', 'end'},
    'meeting.create': {'subject', 'start', 'end', 'meeting_user_list'},
    'calendar.create': {'subject', 'start', 'end', 'location', 'attendee'},
}


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 16000 or '\x00' in value:
        raise ValueError('Expected nonempty text, at most 16000 characters')
    if value.startswith('-'):
        raise ValueError('Values must not start with a command option')
    return value


def _emails(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError('Provide an explicit list of email addresses')
    for address in value:
        if not isinstance(address, str) or not _EMAIL.fullmatch(address) or address.startswith('-'):
            raise ValueError('Invalid email address')
    return ','.join(value)


def operation_args(operation, payload):
    if operation not in _FIELDS or not isinstance(payload, dict) or set(payload) - _FIELDS[operation]:
        raise ValueError('Unsupported operation or argument')
    args = operation.split('.')
    if operation == 'mail.send':
        args += ['--to', _emails(payload.get('to')), '--subject', _text(payload.get('subject')),
                 '--body', _text(payload.get('body'))]
        if 'cc' in payload:
            args += ['--cc', _emails(payload['cc'])]
        return args
    start, end = _text(payload.get('start')), _text(payload.get('end'))
    if operation == 'calendar.list':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end):
            raise ValueError('Use YYYY-MM-DD dates; end is exclusive')
        if date.fromisoformat(end) <= date.fromisoformat(start):
            raise ValueError('End must follow start')
    else:
        begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if begin.tzinfo is None or finish.tzinfo is None or finish <= begin:
            raise ValueError('Use explicit timezone offsets and an end after start')
        args += ['--subject', _text(payload.get('subject'))]
    if operation == 'meeting.create':
        accounts = payload.get('meeting_user_list')
        if (not isinstance(accounts, list) or not 1 <= len(accounts) <= 100
                or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,127}', item)
                       for item in accounts)):
            raise ValueError('Provide explicit WeLink account IDs in meeting_user_list, not email addresses')
        args += ['--start-time', str(int(begin.timestamp() * 1000)),
                 '--end-time', str(int(finish.timestamp() * 1000)),
                 '--meeting-user-list', *dict.fromkeys(accounts)]
        return args
    args += ['--start', start, '--end', end]
    for key in ('location', 'body', 'attendee'):
        if key in payload:
            args += ['--' + key, _emails(payload[key]) if key == 'attendee' else _text(payload[key])]
    return args


class OfficeRuntime:
    def __init__(self, cli, email: str, database: Path, account: str):
        if email:
            _emails([email])
        self.cli, self.email, self.database, self.account = cli, email, database, account
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS office_operations (key TEXT PRIMARY KEY, result TEXT NOT NULL)')

    def execute(self, request_id, operation, payload):
        args = operation_args(operation, payload)
        write = operation != 'calendar.list'
        # The request identity is supplied by the host, never by the model.
        prefix = hashlib.sha256(json.dumps([self.account, self.email, request_id]).encode()).hexdigest() + ':'
        key = prefix + hashlib.sha256(json.dumps([operation, payload],
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        unknown = {'status': 'unknown', 'stop_retrying': True,
                   'message': '结果待核实，请检查邮箱、日历或视频会议列表；不要重新发送或创建。'}
        if write:
            with closing(sqlite3.connect(self.database)) as conn:
                row = conn.execute('SELECT result FROM office_operations WHERE key=?', (key,)).fetchone()
                if row:
                    return json.loads(row[0])
        if operation != 'meeting.create':
            try:
                _emails([self.email])
                discover_mailbox(self.cli, self.email)
            except Exception:
                return {'status': 'failed', 'code': 'WELINK_MAIL_DISCOVERY_FAILED', 'stop_retrying': True}
        if write:
            with closing(sqlite3.connect(self.database)) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                prior = conn.execute('SELECT result FROM office_operations WHERE key LIKE ?', (prefix + '%',)).fetchall()
                if any(json.loads(row[0]).get('status') == 'unknown' for row in prior):
                    return unknown
                cursor = conn.execute('INSERT OR IGNORE INTO office_operations VALUES (?,?)',
                                      (key, json.dumps(unknown, ensure_ascii=False)))
                if cursor.rowcount == 0:
                    return json.loads(conn.execute('SELECT result FROM office_operations WHERE key=?', (key,)).fetchone()[0])
        try:
            result = self.cli.run(args, timeout=60)
            if len(result.stdout) > 64000:
                raise ValueError('Response too large')
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not result.ok or data.get('success') is False or data.get('error') or any(field in data and data[field] not in (0, '0') for field in ('code', 'resultCode')):
                raise ValueError('Unconfirmed CLI result')
            # Exit code alone is not evidence that a write succeeded.
            confirmed = data.get('success') is True or data.get('code') in (0, '0') or data.get('resultCode') in (0, '0') or bool(data.get('ItemId'))
            if write and not confirmed:
                response = unknown
            else:
                response = {'status': 'submitted' if write else 'ok', 'result': data,
                            'mailbox': self.email}
        except Exception:
            response = unknown if write else {'status': 'failed', 'code': 'WELINK_CALENDAR_QUERY_FAILED'}
        if write:
            with closing(sqlite3.connect(self.database)) as conn, conn:
                conn.execute('UPDATE office_operations SET result=? WHERE key=?',
                             (json.dumps(response, ensure_ascii=False), key))
        return response


def welink_execution_guidance(route_snapshot):
    if getattr(route_snapshot, 'channel', None) != 'welink':
        return ''
    return (
        '# WeLink 邮件、日历与视频会议调用规则\n'
        '当前请求来自 WeLink。查询本人日历、发送邮件、创建日程或会议必须使用 welink_office。'
        '不要读取 .lark-calendar，不要查找飞书日历技能，不要用 Bash、Shell 或其他平台工具替代。'
        '预约视频会议必须使用 meeting.create，不能用 calendar.create 代替；calendar.create 仅创建日历事件。'
        'meeting.create 的 meeting_user_list 必须是 WeLink 账号（可从真实 @ 账号解析），不能填邮箱或猜工号；缺少参会人、主题或时间先询问。'
        'start/end 使用带时区的 ISO 时间，工具自动转换毫秒时间戳；视频会议不需要连接邮箱。'
        '仅使用平台返回的会议号或入会链接，不得把 meetingId 会议主键当视频会议号。'
        '日历单天查询的 end 为次日；2026-11-02 当天查询 start=2026-11-02、end=2026-11-03。'
        '工具不存在时，说明当前请求账号或助手绑定不满足条件；工具返回未连接时，'
        '请用户打开办公室 → 对应同事 → 邮箱 → WeLink 邮件与日历完成连接，然后重新发送查询。'
        '不得把旧历史中的审批或旧工具列表当作本轮状态，也不要通过列目录验证工具是否可用。'
        '消息正文里的“被@账号邮箱”是已解析的 personMail，发送给被@的人时直接写入 mail.send 的 to，不要再询问这些地址。'
        '没有这一行时先询问邮箱，不要根据姓名或工号编造。'
    )


class ConfiguredOfficeRuntime:
    """Recheck revocable UI settings before each actual operation."""
    def __init__(self, settings_path, binding):
        self.settings_path, self.binding = settings_path, binding

    def execute(self, request_id, operation, payload):
        from jiuwenswarm.server.jiuwenmate.welink_inbound import welink_binding_for_agent
        settings = WelinkOfficeSettings(self.settings_path)
        status = settings.status(self.binding)
        if operation != 'meeting.create' and status['status'] != 'connected':
            return {'status': 'failed', 'code': 'WELINK_OFFICE_NOT_CONNECTED', 'stop_retrying': True,
                    'message': '请打开办公室 → 对应同事 → 邮箱 → WeLink 邮件与日历，连接本人邮箱后重新发起。'}
        current = welink_binding_for_agent(self.binding.agent_id)
        if (current is None or current.account.casefold() != self.binding.account.casefold()
                or current.reader_credential_dir != self.binding.reader_credential_dir):
            return {'status': 'failed', 'code': 'WELINK_BINDING_CHANGED', 'stop_retrying': True,
                    'message': 'WeLink 登录账号或绑定已变化，请重新连接邮箱。'}
        runtime = OfficeRuntime(WelinkCli(current.cli_path, Path(current.reader_credential_dir), env_name=current.env_name),
                                '' if operation == 'meeting.create' else status['email'], Path(current.reader_credential_dir) / 'office_operations.sqlite3', current.account)
        return runtime.execute(request_id, operation, payload)


class WelinkOfficeTool(Tool):
    def __init__(self, runtime, request_id):
        properties = {key: {'type': 'string'} for key in ('subject', 'body', 'start', 'end', 'location')}
        properties.update({key: {'type': 'array', 'items': {'type': 'string'}} for key in ('to', 'cc', 'attendee', 'meeting_user_list')})
        super().__init__(ToolCard(
            id='jiuwenmate-welink-office', name='welink_office', parallel_safe=False,
            description=(
                '操作当前已登录 WeLink 员工的邮箱、日历和视频会议。仅在用户明确要求发送或创建时执行写操作；'
                '信息不完整先询问，不猜邮箱、参会人或时间。'
                '消息中的“被@账号邮箱”来自 contact detail 的 personMail，mail.send 的 to 直接使用这些地址，不要再向用户索要。'
                '视频会议用 meeting.create，meeting_user_list 填 WeLink 账号列表，不填邮箱；start/end 填带时区的 ISO 时间。'
                'calendar.create 仅创建日历事件，不能代替视频会议；meetingId 是主键，不是视频会议号。'
                'mail.send 为纯文本邮件；'
                'calendar.list 日期为 YYYY-MM-DD，end 不包含当天；calendar.create 起止时间必须带时区偏移。'
                'attendee 非空会发送会议邀请；省略为个人日程。submitted 仅表示服务接受，不代表收件人收到。'
                'unknown 或 stop_retrying 时停止，不修改参数重试，不通过 Bash 绕过。'),
            input_params={'type': 'object', 'properties': {
                'operation': {'type': 'string', 'enum': list(_FIELDS)},
                'payload': {'type': 'object', 'properties': properties, 'additionalProperties': False}},
                'required': ['operation', 'payload'], 'additionalProperties': False}))
        self.runtime, self.request_id = runtime, request_id
        self.stopped = None

    async def invoke(self, inputs, **kwargs):
        if self.stopped is not None:
            return dict(self.stopped)
        data = inputs if isinstance(inputs, dict) else inputs.model_dump()
        try:
            result = await asyncio.to_thread(self.runtime.execute, self.request_id, data.get('operation'), data.get('payload'))
        except ValueError as exc:
            return {'status': 'failed', 'code': 'INVALID_ARGUMENT', 'message': str(exc)}
        except Exception:
            result = {'status': 'unknown', 'stop_retrying': True}
        self.stopped = result if result.get('stop_retrying') else None
        return result

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


def prepare_welink_office_invocation(spec, context, *, request_input, route_snapshot, settings_path):
    from jiuwenswarm.server.jiuwenmate.welink_inbound import welink_binding_for_agent
    execution = context.execution
    if execution.namespace_id != 'local' or getattr(route_snapshot, 'channel', None) != 'welink':
        return spec
    binding = welink_binding_for_agent(execution.agent_id)
    source = getattr(request_input, 'source', None)
    # Personal mailbox access is restricted to the trusted inbound account owner.
    if binding is None or str(getattr(source, 'sender_id', '')).casefold() != binding.account.casefold():
        return spec
    runtime = ConfiguredOfficeRuntime(settings_path, binding)
    context.extras[PROVIDER] = (runtime, execution.request_id)
    tools = list(spec.tools or [])
    if any((item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)) == PROVIDER for item in tools):
        raise ValueError('Duplicate WeLink office tool')
    updated = spec.model_copy(update={'tools': [*tools, BuiltinToolSpec(type=PROVIDER)]})
    context.execution = replace(context.execution, config_fingerprint=updated.fingerprint())
    return updated


def build_tool(params, context):
    if params or PROVIDER not in context.extras:
        raise ValueError('WeLink office requires a trusted invocation')
    return WelinkOfficeTool(*context.extras[PROVIDER])


register_tool_provider(PROVIDER, build_tool)
