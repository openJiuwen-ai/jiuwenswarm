"""View-state store behind ``/ui-state``.

Only *view* state lives here (active tab, expanded members, canvas, theme…).
The whitelist is enforced on every write so no team data can ever be smuggled
into this store, which is what keeps the snapshot channel read-only (I3).

``PUT`` replaces the whole state (reset / first initialisation); ``PATCH``
merges only the keys present in the body. PATCH is serialised per ``sid`` so
two iframes writing different keys concurrently never lose each other's write.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping

from .snapshot import TASK_STATUS_LABELS

SCHEMA_VERSION = "1.1"
TTL_SECONDS = 1800
MAX_SCOPES = 128
MAX_BYTES = 16 * 1024
MAX_EXPANDED_MEMBERS = 200
ZOOM_MIN = 0.2
ZOOM_MAX = 4.0

TABS = ("team-info", "task-graph")
LAYOUT_MODES = ("horizontal", "vertical")
THEMES = ("system", "light", "dark")
TASK_STATUSES = frozenset(TASK_STATUS_LABELS)
DEEP_MERGE_KEYS = frozenset({"search", "canvas"})

#: Nested whitelists (§5.3.1 U7). Without them a deep merge could smuggle team
#: data in through an unknown sub-key, which the 9-key top-level whitelist alone
#: would not stop.
SEARCH_SUBKEYS = frozenset({"team_info", "task_graph"})
CANVAS_SUBKEYS = frozenset({"layout_mode", "pan", "zoom"})
PAN_SUBKEYS = frozenset({"x", "y"})

#: The nine keys §5.3 allows, in the order the contract lists them.
STATE_KEYS = (
    "active_tab",
    "expanded_member_ids",
    "member_page",
    "selected_task_id",
    "detail_open",
    "search",
    "status_filter",
    "canvas",
    "theme",
)

_ERROR_MESSAGES = {
    "STATE_KEY_NOT_ALLOWED": "视图状态只允许契约 §5.3 白名单内的键",
    "STATE_TOO_LARGE": "视图状态超过 16 KiB 上限",
    "STATE_INVALID": "视图状态结构不合法",
}


class UiStateError(ValueError):
    """Raised when a write breaks the whitelist or a size/shape limit."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        """Build an HTTP-ready error code plus a readable Chinese message."""
        message = _ERROR_MESSAGES.get(code, "视图状态不合法")
        super().__init__(f"{message}: {detail}" if detail else message)
        self.code = code
        self.message = message
        self.detail = detail


def default_state() -> dict[str, Any]:
    """The nine keys with their contract defaults (§5.3)."""
    return {
        "active_tab": "team-info",
        "expanded_member_ids": [],
        "member_page": 1,
        "selected_task_id": None,
        "detail_open": False,
        "search": {"team_info": "", "task_graph": ""},
        "status_filter": [],
        "canvas": {"layout_mode": "horizontal", "pan": {"x": 0.0, "y": 0.0}, "zoom": 1.0},
        "theme": "system",
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_list(value: Any, allowed: frozenset[str] | None = None) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    collected: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        if allowed is not None and item not in allowed:
            return None
        if item not in collected:
            collected.append(item)
    return collected


def _reject_unknown_subkeys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    """Raise ``STATE_KEY_NOT_ALLOWED`` for any sub-key outside ``allowed`` (§5.3.1 U7).

    Runs *before* type validation so ``{"search": {"evil": 1}}`` reports the
    whitelist code rather than a type error, and so nothing is written at all.
    """
    unknown = sorted(str(name) for name in set(value) - allowed)
    if unknown:
        raise UiStateError("STATE_KEY_NOT_ALLOWED", f"{label} 不允许的子键: " + ", ".join(unknown))


def _apply_key(state: dict[str, Any], key: str, value: Any) -> None:
    """Validate one whitelisted key and write it into ``state`` in place."""
    if key == "active_tab":
        if not isinstance(value, str) or value not in TABS:
            raise UiStateError("STATE_INVALID", "active_tab 取值非法")
        state["active_tab"] = value
    elif key == "expanded_member_ids":
        items = _string_list(value)
        if items is None:
            raise UiStateError("STATE_INVALID", "expanded_member_ids 必须是字符串数组")
        state["expanded_member_ids"] = items[:MAX_EXPANDED_MEMBERS]
    elif key == "member_page":
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise UiStateError("STATE_INVALID", "member_page 必须是不小于 1 的整数")
        state["member_page"] = value
    elif key == "selected_task_id":
        if value is not None and not isinstance(value, str):
            raise UiStateError("STATE_INVALID", "selected_task_id 必须是字符串或 null")
        state["selected_task_id"] = value
    elif key == "detail_open":
        if not isinstance(value, bool):
            raise UiStateError("STATE_INVALID", "detail_open 必须是布尔值")
        state["detail_open"] = value
    elif key == "search":
        if not isinstance(value, Mapping):
            raise UiStateError("STATE_INVALID", "search 必须是对象")
        _reject_unknown_subkeys(value, SEARCH_SUBKEYS, "search")
        for sub_key in ("team_info", "task_graph"):
            if sub_key in value:
                text = value[sub_key]
                if not isinstance(text, str):
                    # §5.3.1 U6: a nested ``null`` is invalid, never a reset.
                    raise UiStateError("STATE_INVALID", f"search.{sub_key} 必须是字符串")
                state["search"][sub_key] = text
    elif key == "status_filter":
        items = _string_list(value, TASK_STATUSES)
        if items is None:
            raise UiStateError("STATE_INVALID", "status_filter 只允许 7 个 TaskStatus 值")
        state["status_filter"] = items
    elif key == "canvas":
        if not isinstance(value, Mapping):
            raise UiStateError("STATE_INVALID", "canvas 必须是对象")
        _reject_unknown_subkeys(value, CANVAS_SUBKEYS, "canvas")
        canvas = state["canvas"]
        if "layout_mode" in value:
            mode = value["layout_mode"]
            if not isinstance(mode, str) or mode not in LAYOUT_MODES:
                raise UiStateError("STATE_INVALID", "canvas.layout_mode 取值非法")
            canvas["layout_mode"] = mode
        if "pan" in value:
            pan = value["pan"]
            if not isinstance(pan, Mapping):
                raise UiStateError("STATE_INVALID", "canvas.pan 必须是 {x:number, y:number}")
            _reject_unknown_subkeys(pan, PAN_SUBKEYS, "canvas.pan")
            if not all(_is_number(pan.get(axis)) for axis in ("x", "y")):
                raise UiStateError("STATE_INVALID", "canvas.pan 必须是 {x:number, y:number}")
            canvas["pan"] = {"x": float(pan["x"]), "y": float(pan["y"])}
        if "zoom" in value:
            zoom = value["zoom"]
            if not _is_number(zoom):
                raise UiStateError("STATE_INVALID", "canvas.zoom 必须是数字")
            canvas["zoom"] = round(min(max(float(zoom), ZOOM_MIN), ZOOM_MAX), 4)
    elif key == "theme":
        if not isinstance(value, str) or value not in THEMES:
            raise UiStateError("STATE_INVALID", "theme 取值非法")
        state["theme"] = value
    else:  # pragma: no cover - guarded by the whitelist check
        raise UiStateError("STATE_KEY_NOT_ALLOWED", str(key))


def _reject_unknown(raw: Mapping[str, Any]) -> None:
    unknown = [key for key in raw if key not in STATE_KEYS]
    if unknown:
        raise UiStateError("STATE_KEY_NOT_ALLOWED", "不允许的键: " + ", ".join(sorted(map(str, unknown))))


def _size_of(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise UiStateError("STATE_INVALID", f"state 无法序列化: {error}") from error


def _sanitize_state(raw: Any) -> tuple[dict[str, Any], int]:
    """Validate a full replacement body and return ``(state, size_bytes)``.

    ``PUT`` starts from the defaults and overwrites what it is given, so a
    ``null`` value simply means "keep the default for this key" (§5.3.1 U5) —
    the same meaning ``null`` has in ``PATCH``. Without this, the natural
    "GET the complete nine-key state, PUT it back" round trip would 400 on
    ``canvas``/``active_tab`` while ``selected_task_id: null`` passed.
    """
    if not isinstance(raw, Mapping):
        raise UiStateError("STATE_INVALID", "state 必须是对象")
    _reject_unknown(raw)
    size = _size_of(raw)
    if size > MAX_BYTES:
        raise UiStateError("STATE_TOO_LARGE", f"{size} > {MAX_BYTES}")
    state = default_state()
    for key in STATE_KEYS:
        if key not in raw:
            continue
        if raw[key] is None:  # U5: null = 取缺省值
            continue
        _apply_key(state, key, raw[key])
    size = _size_of(state)
    if size > MAX_BYTES:
        raise UiStateError("STATE_TOO_LARGE", f"{size} > {MAX_BYTES}")
    return state, size


def _merge_state(current: Mapping[str, Any], patch: Any) -> tuple[dict[str, Any], int]:
    """Merge a PATCH body into ``current``; ``null`` resets one key to default."""
    if not isinstance(patch, Mapping):
        raise UiStateError("STATE_INVALID", "state 必须是对象")
    _reject_unknown(patch)
    size = _size_of(patch)
    if size > MAX_BYTES:
        raise UiStateError("STATE_TOO_LARGE", f"{size} > {MAX_BYTES}")
    merged = copy.deepcopy(dict(current))
    defaults = default_state()
    for key in STATE_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if value is None:
            merged[key] = copy.deepcopy(defaults[key])
            continue
        if key in DEEP_MERGE_KEYS and isinstance(value, Mapping):
            base = merged.get(key)
            base = copy.deepcopy(base) if isinstance(base, Mapping) else {}
            for sub_key, sub_value in value.items():
                base[sub_key] = sub_value
            value = base
        _apply_key(merged, key, value)
    size = _size_of(merged)
    if size > MAX_BYTES:
        raise UiStateError("STATE_TOO_LARGE", f"{size} > {MAX_BYTES}")
    return merged, size


def validate_state(raw: Any) -> tuple[dict[str, Any], int]:
    """Validate a full ``PUT`` body; returns ``(normalised_state, size_bytes)``."""
    return _sanitize_state(raw)


class UiStateStore:
    """Bounded, TTL-and-LRU view-state store (§5.3)."""

    def __init__(self, ttl_seconds: int = TTL_SECONDS, max_scopes: int = MAX_SCOPES) -> None:
        """Create an empty store with the contract defaults."""
        self.ttl_seconds = ttl_seconds
        self.max_scopes = max_scopes
        self._lock = threading.Lock()
        self._scope_locks: dict[str, threading.Lock] = {}
        self._records: OrderedDict[str, tuple[dict[str, Any], int, int]] = OrderedDict()

    def _scope_lock(self, scope_key: str) -> threading.Lock:
        """One lock per ``sid`` so read-modify-write of that scope is serialised."""
        with self._lock:
            lock = self._scope_locks.get(scope_key)
            if lock is None:
                lock = threading.Lock()
                self._scope_locks[scope_key] = lock
            return lock

    def _expire(self, now: int) -> None:
        stale_before = now - self.ttl_seconds * 1000
        for key in [key for key, record in self._records.items() if record[1] <= stale_before]:
            self._records.pop(key, None)

    def _load(self, scope_key: str, now: int) -> tuple[dict[str, Any] | None, int | None]:
        self._expire(now)
        record = self._records.get(scope_key)
        if record is None:
            return None, None
        self._records.move_to_end(scope_key)
        state, updated_at, _ = record
        return state, updated_at

    def _store(self, scope_key: str, state: dict[str, Any], updated_at: int, size: int) -> None:
        self._records[scope_key] = (copy.deepcopy(state), updated_at, size)
        self._records.move_to_end(scope_key)
        while len(self._records) > self.max_scopes:
            self._records.popitem(last=False)

    def get(self, scope_key: str) -> tuple[dict[str, Any] | None, int | None]:
        """Return ``(state, updated_at)`` or ``(None, None)`` when absent/expired."""
        now = int(time.time() * 1000)
        with self._lock:
            state, updated_at = self._load(scope_key, now)
            return (None, None) if state is None else (copy.deepcopy(state), updated_at)

    def get_view(self, scope_key: str) -> tuple[dict[str, Any], int | None, bool]:
        """Return ``(complete_state, updated_at, empty)`` — contract §5.3.1 U1–U3.

        ``empty`` only reports "this scope was never written"; the state is
        *always* the complete nine-key object with the contract defaults filled
        in, so the page can tell "cleared to null" from "never set". A missing
        or sparse payload would make that distinction unobservable over HTTP.
        """
        now = int(time.time() * 1000)
        with self._lock:
            state, updated_at = self._load(scope_key, now)
            if state is None:
                return default_state(), None, True
            filled = default_state()
            filled.update(copy.deepcopy(state))
            return filled, updated_at, False

    def put(self, scope_key: str, raw_state: Any) -> tuple[dict[str, Any], int, int]:
        """Replace the whole state; returns ``(state, updated_at, bytes)``."""
        state, size = _sanitize_state(raw_state)
        now = int(time.time() * 1000)
        with self._scope_lock(scope_key):
            with self._lock:
                self._expire(now)
                self._store(scope_key, state, now, size)
        return copy.deepcopy(state), now, size

    def patch(self, scope_key: str, raw_patch: Any) -> tuple[dict[str, Any], int, int]:
        """Merge only the supplied keys; returns the complete ``(state, updated_at, bytes)``."""
        now = int(time.time() * 1000)
        with self._scope_lock(scope_key):
            with self._lock:
                current, _ = self._load(scope_key, now)
                current = copy.deepcopy(current) if current is not None else default_state()
            merged, size = _merge_state(current, raw_patch)
            with self._lock:
                self._store(scope_key, merged, now, size)
        return copy.deepcopy(merged), now, size

    def scope_count(self) -> int:
        """Number of live scopes (``/health.ui_state.scopes``)."""
        now = int(time.time() * 1000)
        with self._lock:
            self._expire(now)
            return len(self._records)

    def clear(self) -> None:
        """Drop every scope (tests and reload hygiene)."""
        with self._lock:
            self._records.clear()
