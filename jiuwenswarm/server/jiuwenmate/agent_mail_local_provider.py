"""Local, employee-scoped runtime for the official Tencent Agent Mail CLI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import AgentMailScope

_CLI_VERSION = '1.0.18'
_CLI_PACKAGE = f'@tencent-qqmail/agently-cli@{_CLI_VERSION}'
# The CLI otherwise names its credential directory after the host process
# (for example Cursor selects "codex"). A later start without that environment
# looks in "default" and reports the saved login as gone.
_WORKSPACE = 'jiuwenmate'
_AUTH_URL = re.compile(r'https://[^\s<>"\'\x1b]+')
_OPERATION_ID = re.compile(r'^[a-f0-9]{16}\.[a-f0-9]{32}$')
_MAX_OUTPUT_BYTES = 240 * 1024
_MAX_ATTACHMENT_BYTES = 128 * 1024
_FLAG_OPTIONS = {
    'limit': '--limit',
    'cursor': '--cursor',
    'dir': '--dir',
    'after': '--after',
    'before': '--before',
    'q': '--q',
    'search_in': '--search-in',
    'from': '--from',
    'to': '--to',
    'id': '--id',
    'msg': '--msg',
    'att': '--att',
}
_WRITE_OPERATIONS = frozenset({'mail.send', 'mail.reply', 'mail.forward', 'mail.delete'})


def _find_npx() -> Path | None:
    """Find npx even when the server was launched without an interactive shell PATH."""
    configured = os.environ.get('JIUWENMATE_AGENT_MAIL_NPX')
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        located_configured = shutil.which(configured)
        candidates.append(Path(located_configured) if located_configured else configured_path)
    located = shutil.which('npx')
    if located:
        candidates.append(Path(located))

    home = Path.home()
    for variable in ('NVM_BIN',):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value).expanduser() / 'npx')
    candidates.extend([
        home / '.nvm' / 'versions' / 'node' / 'current' / 'bin' / 'npx',
        home / '.volta' / 'bin' / 'npx',
        home / '.asdf' / 'shims' / 'npx',
        home / '.npm-global' / 'bin' / 'npx',
        Path('/opt/homebrew/bin/npx'),
        Path('/usr/local/bin/npx'),
        Path('/usr/bin/npx'),
    ])

    nvm_dir = Path(os.environ.get('NVM_DIR', home / '.nvm')).expanduser()
    versions_dir = nvm_dir / 'versions' / 'node'
    try:
        versions = sorted(
            (path for path in versions_dir.iterdir() if path.is_dir()),
            key=lambda path: tuple(int(part) for part in re.findall(r'\d+', path.name)),
            reverse=True,
        )
    except OSError:
        versions = []
    candidates.extend(path / 'bin' / 'npx' for path in versions)

    executable_name = 'npx.cmd' if os.name == 'nt' else 'npx'
    for candidate in candidates:
        if candidate.is_dir():
            candidate = candidate / executable_name
        if candidate.is_file() and (os.name == 'nt' or os.access(candidate, os.X_OK)):
            # Keep the npx symlink path: resolving it points into npm's
            # package directory, which is not where the sibling `node` lives.
            return candidate.absolute()
    return None


def _default_runner(
    args: list[str], *, env: dict[str, str], input_text: str | None, timeout: float | None, cwd: str
):
    return subprocess.run(
        args, input=input_text, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout, check=False
    )


class LocalAgentMailRuntime:
    """Runs a version-pinned CLI in a private directory for each employee.

    The official CLI is fetched through npm's cache when needed. It is never
    installed globally and every invocation receives the same employee-private
    HOME/XDG directories.
    """

    def __init__(
        self,
        *,
        root: Path,
        npx_path: Path,
        runner: Callable[..., subprocess.CompletedProcess] = _default_runner,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._root = Path(root)
        self._npx_path = Path(npx_path)
        self._runner = runner
        self._popen = popen

    @staticmethod
    def _scope_digest(scope: AgentMailScope) -> str:
        return hashlib.sha256(f'{scope.namespace_id}\0{scope.agent_id}'.encode()).hexdigest()[:16]

    def _scope_root(self, scope: AgentMailScope) -> Path:
        return self._root / self._scope_digest(scope)

    def _environment(self, scope: AgentMailScope) -> dict[str, str]:
        home = self._scope_root(scope) / 'home'
        config = home / '.config'
        data = home / '.local' / 'share'
        cache = home / '.cache'
        for path in (home, config, data, cache):
            path.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        self._pin_saved_workspace(home)
        environment.update({
            'HOME': str(home),
            'XDG_CONFIG_HOME': str(config),
            'XDG_DATA_HOME': str(data),
            'XDG_CACHE_HOME': str(cache),
            'npm_config_cache': str(cache / 'npm'),
            'AGENTLY_WORKSPACE': _WORKSPACE,
            'NO_COLOR': '1',
        })
        # npx is a node script on macOS/Linux. Its sibling node binary must be
        # visible to its /usr/bin/env shebang even for GUI-launched services.
        environment['PATH'] = f"{self._npx_path.parent}{os.pathsep}{environment.get('PATH', '')}"
        return environment

    @staticmethod
    def _credential_agent_roots(home: Path) -> tuple[Path, ...]:
        return (
            home / '.agently-cli' / 'agents',
            home / 'Library' / 'Application Support' / 'agently-cli' / 'agents',
            home / '.local' / 'share' / 'agently-cli' / 'agents',
        )

    @classmethod
    def _pin_saved_workspace(cls, home: Path) -> None:
        """Keep one credential directory across restarts that change the host environment."""
        roots = [root for root in cls._credential_agent_roots(home) if root.is_dir()]
        if any((root / _WORKSPACE).exists() for root in roots):
            return
        slugs = [
            name for name in dict.fromkeys(
                child.name
                for root in roots
                for child in root.iterdir()
                if child.is_dir() and child.name != _WORKSPACE
            )
        ]
        if len(slugs) != 1:
            return
        slug = slugs[0]
        for root in roots:
            source = root / slug
            target = root / _WORKSPACE
            if source.is_dir() and not target.exists():
                source.rename(target)

    def _command(self, scope: AgentMailScope, *args: str) -> list[str]:
        """Use the employee's installed package directly so routine calls do not hit npm."""
        npx_root = self._scope_root(scope) / 'home' / '.cache' / 'npm' / '_npx'
        try:
            packages = sorted(npx_root.glob('*/node_modules/@tencent-qqmail/agently-cli'))
        except OSError:
            packages = []
        for package in packages:
            package_json = package / 'package.json'
            script = package / 'scripts' / 'run.js'
            try:
                metadata = json.loads(package_json.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if metadata.get('version') != _CLI_VERSION or not script.is_file():
                continue
            node_name = 'node.exe' if os.name == 'nt' else 'node'
            node_path = self._npx_path.parent / node_name
            if not node_path.is_file():
                located_node = shutil.which(node_name)
                if located_node is None:
                    break
                node_path = Path(located_node)
            return [str(node_path), str(script), *args]
        return [str(self._npx_path), '--yes', f'--package={_CLI_PACKAGE}', 'agently-cli', *args]

    def _run(
        self,
        scope: AgentMailScope,
        command: list[str],
        *,
        input_text: str | None = None,
        timeout: float = 30,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess:
        try:
            result = self._runner(
                command,
                env=self._environment(scope),
                input_text=input_text,
                timeout=timeout,
                cwd=str(self._scope_root(scope)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('Agent Mail CLI could not be started') from exc
        if len(result.stdout.encode('utf-8', errors='replace')) > _MAX_OUTPUT_BYTES:
            raise RuntimeError('Agent Mail CLI output is too large')
        if result.returncode and not allow_failure:
            raise RuntimeError('Agent Mail CLI command failed')
        return result

    @staticmethod
    def _json_response(stdout: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        for index, character in enumerate(stdout):
            if character != '{':
                continue
            try:
                value, _ = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get('ok') is True:
                return value
        raise RuntimeError('Agent Mail CLI returned an invalid response')

    @staticmethod
    def _is_unauthorized(stdout: str) -> bool:
        decoder = json.JSONDecoder()
        for index, character in enumerate(stdout):
            if character != '{':
                continue
            try:
                value, _ = decoder.raw_decode(stdout[index:])
            except json.JSONDecodeError:
                continue
            error = value.get('error') if isinstance(value, dict) else None
            if isinstance(error, dict):
                return error.get('type') == 'unauthorized' or error.get('code') == 401
        return False

    def _auth_paths(self, scope: AgentMailScope, operation_id: str) -> tuple[Path, Path, Path]:
        root = self._scope_root(scope) / 'auth'
        return root / f'{operation_id}.log', root / f'{operation_id}.pid', root / f'{operation_id}.exit'

    def _current_auth_path(self, scope: AgentMailScope) -> Path:
        return self._scope_root(scope) / 'auth' / 'current'

    def _current_operation(self, scope: AgentMailScope) -> str | None:
        try:
            operation_id = self._current_auth_path(scope).read_text(encoding='utf-8').strip()
        except OSError:
            return None
        if _OPERATION_ID.fullmatch(operation_id) and operation_id.startswith(f'{self._scope_digest(scope)}.'):
            return operation_id
        return None

    def _mailbox_status(self, scope: AgentMailScope) -> dict[str, Any]:
        # npx may need to fetch the pinned CLI on the first status check.
        auth = self._run(scope, self._command(scope, 'auth', 'status'), timeout=120, allow_failure=True)
        if auth.returncode:
            return {'status': 'not_connected', 'cli_version': _CLI_VERSION}
        data = self._json_response(auth.stdout).get('data')
        if not isinstance(data, dict) or data.get('logged_in') is not True:
            return {'status': 'not_connected', 'cli_version': _CLI_VERSION}
        mailbox = self._run(scope, self._command(scope, '+me'), timeout=120, allow_failure=True)
        if mailbox.returncode:
            # Tencent revokes the old token when the mailbox is authorized for another employee.
            if self._is_unauthorized(mailbox.stdout):
                return {'status': 'not_connected', 'cli_version': _CLI_VERSION}
            raise RuntimeError('Agent Mail mailbox identity could not be verified')
        mailbox_data = self._json_response(mailbox.stdout).get('data')
        aliases = mailbox_data.get('aliases') if isinstance(mailbox_data, dict) else None
        emails = [
            alias['email'].strip()
            for alias in aliases or []
            if isinstance(alias, dict) and isinstance(alias.get('email'), str) and '@' in alias['email']
        ] if isinstance(aliases, list) else []
        if not emails:
            raise RuntimeError('Agent Mail mailbox identity could not be verified')
        return {'status': 'connected', 'cli_version': _CLI_VERSION, 'email': emails[0]}

    async def status(self, scope: AgentMailScope) -> dict[str, Any]:
        return await asyncio.to_thread(self._status, scope)

    def _status(self, scope: AgentMailScope) -> dict[str, Any]:
        try:
            result = self._mailbox_status(scope)
        except RuntimeError as exc:
            cause = exc.__cause__
            reason = type(cause).__name__ if cause is not None else type(exc).__name__
            logging.getLogger(__name__).warning('Agent Mail CLI status check failed (%s)', reason)
            return {'status': 'error', 'cli_version': _CLI_VERSION}
        operation_id = self._current_operation(scope)
        if result['status'] == 'connected':
            if operation_id:
                self._clear_auth(scope, operation_id)
            return result
        if operation_id:
            _, _, exit_path = self._auth_paths(scope, operation_id)
            if not exit_path.exists():
                return {'status': 'authorizing', 'cli_version': _CLI_VERSION, 'operation_id': operation_id}
            self._clear_auth(scope, operation_id)
        return result

    async def begin_auth(self, scope: AgentMailScope) -> dict[str, Any]:
        return await asyncio.to_thread(self._begin_auth, scope)

    def _begin_auth(self, scope: AgentMailScope) -> dict[str, Any]:
        current = self._status(scope)
        if current['status'] == 'connected':
            return current
        if current['status'] == 'authorizing':
            return self._authorization_from_log(scope, current['operation_id'])
        if current['status'] == 'unavailable':
            raise RuntimeError('Agent Mail CLI is unavailable')
        operation_id = f'{self._scope_digest(scope)}.{uuid.uuid4().hex}'
        log_path, pid_path, exit_path = self._auth_paths(scope, operation_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._current_auth_path(scope).write_text(operation_id, encoding='utf-8')
        with log_path.open('wb') as log:
            try:
                process = self._popen(
                    self._command(scope, 'auth', 'login'),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=self._environment(scope),
                    start_new_session=True,
                )
            except OSError as exc:
                self._clear_auth(scope, operation_id)
                raise RuntimeError('Agent Mail authorization could not be started') from exc
        pid_path.write_text(str(process.pid), encoding='utf-8')
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                completed = process.poll()
            except AttributeError:
                completed = None
            if completed is not None:
                exit_path.write_text(str(completed), encoding='utf-8')
            try:
                return self._authorization_from_log(scope, operation_id)
            except RuntimeError:
                if completed is not None:
                    self._clear_auth(scope, operation_id)
                    raise RuntimeError('Agent Mail CLI did not return an authorization URL')
                time.sleep(0.2)
        self._cancel_auth(scope, operation_id)
        raise RuntimeError('Agent Mail CLI did not return an authorization URL')

    def _authorization_from_log(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        log_path, _, _ = self._auth_paths(scope, operation_id)
        try:
            output = log_path.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            raise RuntimeError('Agent Mail authorization is not available') from exc
        match = _AUTH_URL.search(output)
        if match is None:
            raise RuntimeError('Agent Mail authorization URL is not available')
        return {
            'status': 'authorizing',
            'cli_version': _CLI_VERSION,
            'operation_id': operation_id,
            'auth_url': match.group(0),
        }

    def _validate_operation(self, scope: AgentMailScope, operation_id: str) -> str:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise RuntimeError('invalid Agent Mail authorization operation')
        if not operation_id.startswith(f'{self._scope_digest(scope)}.'):
            raise RuntimeError('invalid Agent Mail authorization operation')
        return operation_id

    async def poll_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._poll_auth, scope, operation_id)

    def _poll_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        operation_id = self._validate_operation(scope, operation_id)
        current = self._current_operation(scope)
        if current != operation_id:
            raise RuntimeError('invalid Agent Mail authorization operation')
        return self._status(scope)

    async def cancel_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._cancel_auth, scope, operation_id)

    def _cancel_auth(self, scope: AgentMailScope, operation_id: str) -> dict[str, Any]:
        operation_id = self._validate_operation(scope, operation_id)
        _, pid_path, _ = self._auth_paths(scope, operation_id)
        try:
            process_id = int(pid_path.read_text(encoding='utf-8'))
            if os.name == 'nt':
                os.kill(process_id, signal.SIGTERM)
            else:
                os.killpg(process_id, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        self._clear_auth(scope, operation_id)
        return {'status': 'not_connected', 'cli_version': _CLI_VERSION}

    def _clear_auth(self, scope: AgentMailScope, operation_id: str) -> None:
        log_path, pid_path, exit_path = self._auth_paths(scope, operation_id)
        for path in (log_path, pid_path, exit_path):
            try:
                path.unlink()
            except OSError:
                pass
        current = self._current_auth_path(scope)
        try:
            if current.read_text(encoding='utf-8').strip() == operation_id:
                current.unlink()
        except OSError:
            pass

    async def disconnect(self, scope: AgentMailScope) -> dict[str, Any]:
        return await asyncio.to_thread(self._disconnect, scope)

    def _disconnect(self, scope: AgentMailScope) -> dict[str, Any]:
        operation = self._current_operation(scope)
        if operation:
            self._cancel_auth(scope, operation)
        self._run(scope, self._command(scope, 'auth', 'logout'), timeout=30, allow_failure=True)
        return {'status': 'not_connected', 'cli_version': _CLI_VERSION}

    async def execute(self, scope: AgentMailScope, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._execute, scope, operation, payload)

    def _execute(self, scope: AgentMailScope, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._status(scope)['status'] != 'connected':
            raise RuntimeError('Agent Mail is not connected')
        args = self._operation_args(operation, payload)
        result = self._run(scope, self._command(scope, *args), timeout=60)
        response = self._json_response(result.stdout)
        if operation not in _WRITE_OPERATIONS:
            return response
        data = response.get('data')
        confirmation_token = data.get('confirmation_token') if isinstance(data, dict) else None
        if not isinstance(confirmation_token, str) or not confirmation_token or len(confirmation_token) > 4096:
            raise RuntimeError('Agent Mail CLI did not return a write confirmation token')
        confirmed = self._run(
            scope,
            self._command(scope, *args, '--confirmation-token', confirmation_token),
            timeout=60,
        )
        return self._json_response(confirmed.stdout)

    @staticmethod
    def _operation_args(operation: str, payload: dict[str, Any]) -> list[str]:
        commands = {
            'mail.list': ['message', '+list'],
            'mail.search': ['message', '+search'],
            'mail.read': ['message', '+read'],
            'mail.attachment.read': ['attachment', '+download'],
            'mail.send': ['message', '+send'],
            'mail.reply': ['message', '+reply'],
            'mail.forward': ['message', '+forward'],
            'mail.delete': ['message', '+trash'],
        }
        if operation not in commands:
            raise RuntimeError('unsupported Agent Mail operation')
        allowed = {
            'mail.list': {'limit', 'cursor', 'dir', 'after', 'before', 'has_attachments', 'is_unread'},
            'mail.search': {'q', 'search_in', 'from', 'to', 'limit', 'cursor', 'dir', 'after', 'before', 'has_attachments', 'is_unread'},
            'mail.read': {'id'},
            'mail.attachment.read': {'msg', 'att'},
            'mail.send': {'to', 'cc', 'bcc', 'subject', 'body'},
            'mail.reply': {'id', 'cc', 'bcc', 'body'},
            'mail.forward': {'id', 'to', 'cc', 'bcc', 'body'},
            'mail.delete': {'id'},
        }[operation]
        if set(payload) - allowed:
            raise RuntimeError('invalid Agent Mail operation payload')
        args = list(commands[operation])
        if operation in _WRITE_OPERATIONS:
            required = {
                'mail.send': ('to', 'subject', 'body'),
                'mail.reply': ('id', 'body'),
                'mail.forward': ('id', 'to'),
                'mail.delete': ('id',),
            }[operation]
            if any(key not in payload for key in required):
                raise RuntimeError('invalid Agent Mail operation payload')
            if 'id' in payload:
                value = payload['id']
                if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                    raise RuntimeError('invalid Agent Mail operation payload')
                args.extend(['--id', value])
            for key in ('to', 'cc', 'bcc'):
                if key not in payload:
                    continue
                values = payload[key] if isinstance(payload[key], list) else [payload[key]]
                if not values or any(
                    not isinstance(value, str)
                    or len(value) > 320
                    or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value)
                    for value in values
                ):
                    raise RuntimeError('invalid Agent Mail operation payload')
                args.extend(option for value in values for option in (f'--{key}', value))
            for key in ('subject', 'body'):
                value = payload.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value.strip() or len(value) > 16_384:
                        raise RuntimeError('invalid Agent Mail operation payload')
                    args.extend([f'--{key}', value])
            return args
        for key, flag in _FLAG_OPTIONS.items():
            if key == 'limit':
                continue
            value = payload.get(key)
            if value is None:
                continue
            if key not in allowed or not isinstance(value, str) or not value or len(value) > 2048:
                raise RuntimeError('invalid Agent Mail operation payload')
            args.extend([flag, value])
        limit = payload.get('limit')
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                raise RuntimeError('invalid Agent Mail operation payload')
            args.extend(['--limit', str(limit)])
        for key, flag in (('has_attachments', '--has-attachments'), ('is_unread', '--is-unread')):
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise RuntimeError('invalid Agent Mail operation payload')
                if payload[key]:
                    args.append(flag)
        return args

    async def remove_runtime(self, scope: AgentMailScope) -> None:
        await asyncio.to_thread(self._remove_runtime, scope)

    def _remove_runtime(self, scope: AgentMailScope) -> None:
        root = self._scope_root(scope)
        if root.exists():
            shutil.rmtree(root)


def create_provider() -> LocalAgentMailRuntime | None:
    """Create the built-in local provider when Node's npx is available."""
    npx_path = _find_npx()
    if npx_path is None:
        return None
    from jiuwenswarm.server.jiuwenmate.service import default_data_dir

    root = Path(os.environ.get('JIUWENMATE_AGENT_MAIL_HOME') or default_data_dir() / 'agent_mail')
    return LocalAgentMailRuntime(root=root, npx_path=npx_path)
