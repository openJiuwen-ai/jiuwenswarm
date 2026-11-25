"""Atomic per-session telemetry storage. Message bodies and UI resources are excluded."""
from collections import OrderedDict
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from .state import SessionState


class HistoryStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, sid):
        return self.root / (sha256(sid.encode()).hexdigest() + '.json')

    def load(self, sid):
        state = SessionState()
        path = self.path(sid)
        if not path.exists():
            return state
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('history too large')
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema') != 1 or type(data.get('count')) is not int or data['count'] < 0:
                raise ValueError('invalid history schema')
            rows = data['history']
            if not isinstance(rows, list) or len(rows) > 12:
                raise ValueError('invalid history rows')
            for row in rows:
                if not isinstance(row, dict) or not all(type(row.get(key)) is int and row[key] >= 0 for key in ('number','input','output','cache')):
                    raise ValueError('invalid history measurement')
                if row.get('context') is not None and (type(row['context']) not in (int,float) or row['context'] < 0):
                    raise ValueError('invalid context')
            seen = data['seen']
            if not isinstance(seen, list) or len(seen)>4096 or any(not isinstance(item,str) for item in seen):
                raise ValueError('invalid request identities')
            state.history, state.count, state.seen = rows, data['count'], OrderedDict.fromkeys(seen)
            # Current usage/model are re-queried on mount; only history survives.
            state.model = data.get('model') if isinstance(data.get('model'),str) else None
            state.observed = data.get('observed') if isinstance(data.get('observed'),str) else state.observed
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            state = SessionState(history_error=True)
        return state

    def save(self, sid, state):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {'schema':1, 'history':state.history, 'seen':list(state.seen), 'count':state.count,
                   'model':state.model, 'observed':state.observed}
        fd, name = tempfile.mkstemp(prefix='.pending-',dir=self.root)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(payload,stream,ensure_ascii=False,allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name,self.path(sid))
        finally:
            if os.path.exists(name):
                os.unlink(name)
