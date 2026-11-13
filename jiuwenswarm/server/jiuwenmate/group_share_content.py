"""Exact content identities shared by search and private CLI approval snapshots."""

from datetime import datetime, timezone
import hashlib
import json


def _timestamp(value: str | None) -> str | None:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    return value


def _content_key(item: dict, kind: str) -> str:
    if kind == 'message':
        message = item['message']
        fields = ('message_id', 'sender_id', 'sender_kind', 'text', 'reply_to', 'status')
        content = {key: message.get(key) for key in fields}
        content['received_at'] = _timestamp(message.get('received_at'))
        for key in ('mention_ids', 'attachment_refs'):
            content[key] = message.get(key, [])
    else:
        # Import paths, preview cache names and execution references are not file contents.
        fields = (
            'artifact_id', 'artifact_kind', 'title', 'file_name', 'file_size', 'file_digest',
            'url', 'snippet', 'message_id', 'file_key', 'resource_type', 'source_session_id',
        )
        content = {key: item.get(key) for key in fields}
        content['created_at'] = _timestamp(item.get('created_at'))
    payload = [kind, item['group_id'], content]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def content_fingerprints(result: dict) -> set[str]:
    """Only fingerprint content actually released by the snapshot's content types."""
    messages = result.get('kind') != 'cli_group_read' or 'messages' in result['content_types']
    keys = {_content_key(hit, 'message') for hit in result.get('hits', ())} if messages else set()
    return keys | {_content_key(item, 'artifact') for item in result.get('artifacts', ())}


def unreviewed_content(result: dict, covered: set[str]) -> dict:
    """Build an owner-only delta preview without changing the frozen release snapshot."""
    return {
        **result,
        'hits': [hit for hit in result.get('hits', ()) if _content_key(hit, 'message') not in covered],
        'artifacts': [item for item in result.get('artifacts', ()) if _content_key(item, 'artifact') not in covered],
    }
