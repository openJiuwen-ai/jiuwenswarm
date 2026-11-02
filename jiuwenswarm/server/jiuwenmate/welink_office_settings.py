"""User-facing, employee-scoped WeLink mailbox connection settings."""
from __future__ import annotations

from contextlib import closing
import json
import re

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_cli import WelinkCli
from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import parse_auth_status
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

MAILBOX_PATTERN = re.compile(r'[^\s@,<>;]+@[^\s@,<>;]+\.[^\s@,<>;]+')


def discover_mailbox(cli, email: str) -> None:
    try:
        result = cli.run(['mail', 'autodiscover', '--email', email], timeout=30)
        data = json.loads(result.stdout)
        if (not result.ok or not isinstance(data, dict) or data.get('success') is False
                or data.get('error') or ('code' in data and data['code'] not in (0, '0'))
                or ('resultCode' in data and data['resultCode'] not in (0, '0'))):
            raise ValueError('Mailbox discovery rejected')
    except Exception as exc:
        raise PersistentAgentError('邮箱连接验证失败，请检查邮箱地址、登录状态和邮件服务权限。',
                                   code='WELINK_MAIL_DISCOVERY_FAILED') from exc


class WelinkOfficeSettings(SQLiteStore):
    _schema = '''CREATE TABLE IF NOT EXISTS welink_office_settings (
        agent_id TEXT PRIMARY KEY, account TEXT NOT NULL, email TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0
    );'''

    def status(self, binding):
        if binding is None:
            return {'status': 'unavailable', 'reason': 'WELINK_NOT_BOUND'}
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT * FROM welink_office_settings WHERE agent_id=?', (binding.agent_id,)).fetchone()
        if row is not None and row['account'].casefold() == binding.account.casefold():
            return {'status': 'connected' if row['enabled'] else 'not_connected',
                    'account': binding.account, 'email': row['email']}
        return {'status': 'not_connected', 'account': binding.account}

    def connect(self, binding, email, *, cli_factory=None):
        from pathlib import Path
        if binding is None:
            raise PersistentAgentError('请先在该同事的 WeLink 渠道完成登录和助手绑定。', code='WELINK_NOT_BOUND')
        if (not isinstance(email, str) or len(email) > 320 or not MAILBOX_PATTERN.fullmatch(email.strip())
                or email.strip().startswith('-')):
            raise PersistentAgentError('请输入有效的本人邮箱地址。', code='BAD_REQUEST')
        email = email.strip()
        cli_factory = cli_factory or WelinkCli
        cli = cli_factory(binding.cli_path, Path(binding.reader_credential_dir), env_name=binding.env_name)
        try:
            auth = cli.auth_status()
            logged_in, account = parse_auth_status(auth.stdout, auth.stderr)
            if not auth.ok or not logged_in or account.casefold() != binding.account.casefold():
                raise ValueError('Account not verified')
            for command in (['mail', 'send', '--help'], ['calendar', 'list', '--help'], ['calendar', 'create', '--help']):
                if not cli.run(command, timeout=15).ok:
                    raise ValueError('CLI capability unavailable')
        except Exception as exc:
            raise PersistentAgentError('WeLink 登录或工具版本验证失败，请重新登录并确认支持邮件和日历。',
                                       code='WELINK_OFFICE_UNAVAILABLE') from exc
        discover_mailbox(cli, email)
        with self._transaction() as conn:
            conn.execute('INSERT INTO welink_office_settings VALUES (?,?,?,1) ON CONFLICT(agent_id) '
                         'DO UPDATE SET account=excluded.account,email=excluded.email,enabled=1',
                         (binding.agent_id, binding.account, email))
        return self.status(binding)

    def disconnect(self, binding):
        if binding is not None:
            with self._transaction() as conn:
                conn.execute('UPDATE welink_office_settings SET enabled=0 WHERE agent_id=?', (binding.agent_id,))
        return self.status(binding)
