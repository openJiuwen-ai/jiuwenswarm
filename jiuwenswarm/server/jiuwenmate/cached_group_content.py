"""Reauthorize private local copies using immutable host approval receipts."""

import asyncio
import hashlib
import json
import re

from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness import persistent_agent as core

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.output_files import approved_import_path


def normal(value):
    return re.sub(r'[/\\]+', '/', str(value)).casefold()


def blocked(reason='缓存文件的来源或跨群分享授权无法核验，请重新进行跨群搜索并审批。'):
    return ToolOutput(success=False, data={'status': 'forbidden', 'content': reason}, error=reason)


def mentioned(path, text, session_id):
    path = normal(path)
    local = f'sessions/{session_id}/'.casefold()
    suffix = r'(?=$|[\s\"\';,)&|<>])'
    candidates = [path, path[len(local):]] if path.startswith(local) else [path]
    return any(re.search(re.escape(candidate) + suffix, text) for candidate in candidates)


class CachedGroupContent:
    """An imported blob is private until its exact bytes have a confirmed public delivery."""

    def __init__(self, sharing, port):
        self.sharing, self.port = sharing, port

    async def prepare(self, execution, text, rows, requested='', command=''):
        if not await asyncio.to_thread(
            self.sharing.repository.request_window_is_current, execution.namespace_id, execution.request_id,
        ):
            return True, blocked('群访问或分享授权已失效，请重新发起请求。')
        receipts = await asyncio.to_thread(
            self.sharing.repository.approvals_for_session,
            execution.namespace_id, execution.agent_id, execution.session_id,
        )
        local_rows = [row for row in rows if row['session_id'] == execution.session_id]
        named = [row for row in local_rows if mentioned(row['file_path'], text, execution.session_id)]
        inspector = getattr(self.sharing.files, 'current_file_fact', None)
        if inspector is not None:
            checked = []
            for row in named:
                actual = await asyncio.to_thread(inspector, execution, row['file_path'])
                if actual is None:
                    return True, blocked()
                same = (actual['file_ref'], actual['file_size']) == (row['file_ref'], row['file_size'])
                checked.append({**row, **actual, 'published': row.get('published', True) and same})
            named = checked
        # Only confirmed platform ACKs count; queued candidates cannot launder a cache.
        if named and 'imports/' not in text and all(row.get('published', True) for row in named):
            return False, None
        if not named and requested and inspector is not None:
            fact = await asyncio.to_thread(inspector, execution, requested)
            if fact is not None:
                named.append(fact)
        if not named and not requested and inspector is not None:
            raw = re.sub(r'[/\\]+', '/', command)
            for match in re.finditer(r'(?:sessions/[A-Za-z0-9_-]+/)?(?:imports|output)/[^\s\"\';&|()<>]+', raw):
                fact = await asyncio.to_thread(inspector, execution, match.group())
                if fact is not None:
                    named.append(fact)
        selected = []
        public_import = False
        known_imports = set()
        for receipt in receipts:
            snapshot = receipt['result']
            for artifact in snapshot.get('artifacts', ()):
                if artifact.get('artifact_kind') != 'file':
                    continue
                if snapshot.get('kind') == 'cli_group_read':
                    name = artifact['artifact_id'][:25] + '-' + artifact['file_name']
                    path = f'sessions/{execution.session_id}/imports/{name}'
                else:
                    path = approved_import_path(execution.session_id, artifact['artifact_id'], artifact['file_name'])
                known_imports.add(normal(path).split('/imports/', 1)[1])
                if mentioned(path, text, execution.session_id) and inspector is not None:
                    actual = await asyncio.to_thread(inspector, execution, path)
                    if actual is None or (actual['file_ref'], actual['file_size']) != (
                        artifact['file_digest'], artifact['file_size'],
                    ):
                        return True, blocked()
                if any(row.get('published', True) and row['file_ref'] == artifact['file_digest']
                       and row['file_size'] == artifact['file_size'] for row in local_rows):
                    public_import |= mentioned(path, text, execution.session_id)
                    continue
                blob = (artifact['file_digest'], artifact['file_size'])
                same_blob = any((row['file_ref'], row['file_size']) == blob for row in named)
                derived = [row for row in named if row.get('request_id') == receipt['request_id']]
                if mentioned(path, text, execution.session_id) or same_blob or derived:
                    selected.append((receipt, artifact))
            if any(row.get('request_id') == receipt['request_id'] for row in named):
                selected.append((receipt, None))
        for match in re.finditer(r'imports/([^\s\"\';&|()<>]+)', text):
            if match[1] not in known_imports and not any(
                name.startswith(match[1]) and mentioned('imports/' + name, text, execution.session_id)
                for name in known_imports
            ):
                return True, blocked()
        if not selected:
            if public_import:
                return True, None
            return (True, blocked()) if 'imports/' in text else (False, None)
        # Even with no explicit path (e.g. `cat imports/*`), do not release unknown imports.
        if 'imports/' in text and re.search(r'imports/(?:\*|\.|["\s]|$)', text):
            return True, blocked()
        by_source = {}
        for receipt, artifact in selected:
            group_id = receipt['source_groups'][0]['group_id']
            key = (group_id, receipt['result'].get('kind', 'search'))
            entry = by_source.setdefault(key, [receipt, {}, False])
            if receipt['request_id'] == execution.request_id:
                entry[0] = receipt
            if artifact is None:
                entry[2] = True
            else:
                entry[1][artifact['artifact_id']] = artifact
        snapshots = []
        approvals = []
        if len({key[0] for key in by_source}) != len(by_source):
            return True, blocked()
        for (group_id, kind), (receipt, artifacts, derived) in by_source.items():
            snapshot = dict(receipt['result'])
            if kind == 'cli_group_read':
                snapshot['preview_request_id'] = snapshot.get('preview_request_id') or snapshot['request_id']
            snapshot.update(core.InvocationRef.from_execution(execution).model_dump(mode='json'))
            if not derived:
                snapshot['artifacts'] = list(artifacts.values())
                message_ids = {item.get('message_id') for item in artifacts.values()}
                snapshot['hits'] = [hit for hit in snapshot.get('hits', ())
                                    if kind == 'cli_group_read' and hit['message']['message_id'] in message_ids]
            from jiuwenswarm.server.jiuwenmate.group_share_repository import _result_content_types

            required_types = _result_content_types(snapshot)
            query = core.GroupMessageQuery(
                query='cached-file:' + hashlib.sha256(json.dumps(
                    {key: snapshot.get(key) for key in ('kind', 'artifacts', 'hits')}, sort_keys=True,
                ).encode()).hexdigest(),
                group_ids=(group_id,), content_types=tuple(required_types), limit=500,
            )
            scope, _ = await asyncio.to_thread(self.sharing.scope, execution, query)
            if any(scope['target'][key] != receipt['scope']['target'][key]
                   for key in ('channel', 'app_id', 'kind', 'id')):
                return True, blocked()
            sources = await self.port.sharing_sources(execution, query)
            source = next((item for item in sources if item['group_id'] == group_id), None)
            if source is None:
                return True, blocked()
            saved = receipt['scope'].get('source_authorizations', {}).get(group_id, {})
            current = {key: source[key] for key in ('app_id', 'visible_from') if key in source}
            if saved and saved != current:
                return True, blocked()
            scope['source_authorizations'] = {group_id: current}
            digest = hashlib.sha256((query.query + ':' + kind).encode()).hexdigest()
            if receipt['request_id'] == execution.request_id:
                approvals.append(receipt)
            else:
                snapshots.append((scope, digest, snapshot, [dict(group_id=group_id, name=source['name'])]))
        denied = await asyncio.to_thread(self.sharing.repository.denied_groups,
                                         execution.namespace_id, execution.agent_id, execution.request_id)
        if any(key[0] in denied for key in by_source):
            return True, blocked('所有者已拒绝或审批已过期，本次请求不再读取或分享该来源文件，请直接告知提问者。')
        if snapshots:
            try:
                approvals.extend(await asyncio.to_thread(
                    self.sharing.repository.create_or_get_batch, snapshots, now=self.sharing.now(),
                ))
            except PersistentAgentError:
                return True, blocked('群访问或分享授权已失效，请重新发起请求。')
        for row in approvals:
            if row['status'] == 'pending':
                try:
                    row = await asyncio.to_thread(
                        self.sharing.repository.authorize_from_grants, row['approval_id'], now=self.sharing.now(),
                    )
                except PersistentAgentError:
                    return True, blocked('群访问或分享授权已失效，请重新发起请求。')
                row = await asyncio.to_thread(self.sharing.repository.get, row['approval_id'])
            if row['status'] == 'pending':
                await asyncio.to_thread(self.sharing.notify_owner, row['scope'], row['approval_id'])
                raise core.GroupContentApprovalRequired(approval_id=row['approval_id'])
            if row['status'] == 'owner_read' and not await asyncio.to_thread(
                self.sharing.is_owner_direct, execution,
            ):
                return True, blocked()
            if row['status'] not in {'approved_once', 'approved_permanent', 'owner_read'}:
                return True, blocked('所有者已拒绝或审批已过期，本次请求不再读取或分享该来源文件，请直接告知提问者。')
            if row['status'] == 'approved_permanent' and not await asyncio.to_thread(
                self.sharing.repository.has_grant, row['scope'], row['source_groups'][0]['group_id'],
                _result_content_types(row['result']),
            ):
                return True, blocked()
            frozen = await self.sharing.verified_snapshot(execution.namespace_id, row['result'])
            if frozen is None:
                return True, blocked()
        if not await asyncio.to_thread(
            self.sharing.repository.request_window_is_current, execution.namespace_id, execution.request_id,
        ):
            return True, blocked('群访问或分享授权已失效，请重新发起请求。')
        return True, None
