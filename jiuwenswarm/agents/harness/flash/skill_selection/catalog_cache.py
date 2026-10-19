"""Bounded content caches; authorization and request choices never live here."""

from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
import sys
import threading


def _size(value, seen=None):
    """Account for retained strings and records, including nested capability data."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_size(getattr(value, f.name), seen) for f in fields(value))
    if isinstance(value, dict):
        return size + sum(_size(k, seen) + _size(v, seen) for k, v in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return size + sum(_size(item, seen) for item in value)
    return size


@dataclass(frozen=True)
class CachedDetails:
    document: object
    observed_files: tuple


class CatalogCache:
    """Thread-safe LRU bounded by both entry count and retained memory.

    Validation stays outside the lock; immutable entries can safely outlive eviction.
    Versions include file identity, so same-mtime atomic replacement is not a hit.
    """

    def __init__(self, max_entries=1024, max_bytes=64 * 1024 * 1024):
        if max_entries < 1 or max_bytes < 1:
            raise ValueError('Catalog cache bounds must be positive')
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def _get(self, key, version):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] != version:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def _put(self, key, version, value):
        size = _size((key, version, value))
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
            if size > self.max_bytes:
                return
            self._entries[key] = (version, value, size)
            self._bytes += size
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                _, discarded = self._entries.popitem(last=False)
                self._bytes -= discarded[2]

    def get_document(self, key, version):
        return self._get(('document', key), version)

    def put_document(self, key, version, document):
        self._put(('document', key), version, document)

    def get_root(self, identity, digest):
        return self._get(('root', identity), digest)

    def put_root(self, identity, digest, raw):
        self._put(('root', identity), digest, raw)

    def get_details(self, key):
        return self._get(('details', key), None)

    def put_details(self, key, document, observed_files):
        self._put(('details', key), None, CachedDetails(document, tuple(observed_files.items())))

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0
