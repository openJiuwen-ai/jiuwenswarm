"""Per-session note persistence for the Notes Vault plugin.

Storage is one JSON document per *scope* (normally a chat session id) under a
plugin-owned directory; standard library only, so it is testable without the
Jiuwen runtime.  Safety properties enforced here, not in the handlers: scope
names are whitelisted and cannot escape the storage directory, every write is
atomic (temp file + ``os.replace``), every field is type- and length-checked,
and a corrupt store raises instead of dropping data silently.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Scope used when the caller has no session (e.g. the TUI before a session).
DEFAULT_SCOPE = "default"

#: Scope names must start alphanumeric and stay within this character set.
_SAFE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MAX_SCOPE_LENGTH = 64
MAX_NOTES_PER_SCOPE = 200
MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 20000
MAX_TAGS = 16
MAX_TAG_LENGTH = 32
MAX_QUERY_LENGTH = 200
STORAGE_VERSION = 1


class NoteStoreError(ValueError):
    """Raised for invalid input or an unreadable store."""


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string.

    ``datetime.utcnow()`` is avoided on purpose: it is deprecated since
    Python 3.12 and the project runs pytest with ``filterwarnings = error``.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(value: Any, name: str, *, max_length: int, required: bool) -> str:
    """Validate a string field, optionally requiring non-blank content."""
    if value is None:
        if required:
            raise NoteStoreError(f"{name} must be a string")
        return ""
    if not isinstance(value, str):
        raise NoteStoreError(f"{name} must be a string")
    if len(value) > max_length:
        raise NoteStoreError(f"{name} must be at most {max_length} characters")
    if not required:
        return value
    text = value.strip()
    if not text:
        raise NoteStoreError(f"{name} must not be empty")
    return text


def _boolean(value: Any, name: str) -> bool:
    """Validate a boolean field."""
    if not isinstance(value, bool):
        raise NoteStoreError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str, *, low: int, high: int) -> int:
    """Validate an integer field inside an inclusive range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise NoteStoreError(f"{name} must be an integer")
    if not low <= value <= high:
        raise NoteStoreError(f"{name} must be between {low} and {high}")
    return value


def _normalize_tags(value: Any) -> list[str]:
    """Validate and de-duplicate a tag list, preserving input order."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise NoteStoreError("tags must be a list of strings")

    tags: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise NoteStoreError("tags must be a list of strings")
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise NoteStoreError(
                f"each tag must be at most {MAX_TAG_LENGTH} characters"
            )
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise NoteStoreError(f"at most {MAX_TAGS} tags are allowed")
    return tags


def _normalize_scope(scope: Any) -> str:
    """Validate a scope name and return the canonical form."""
    text = DEFAULT_SCOPE if scope is None else str(scope).strip()
    if not text:
        text = DEFAULT_SCOPE
    if len(text) > MAX_SCOPE_LENGTH:
        raise NoteStoreError(f"scope must be at most {MAX_SCOPE_LENGTH} characters")
    if not _SAFE_SCOPE.match(text) or ".." in text:
        raise NoteStoreError(
            "scope must start alphanumeric and contain only letters, digits, "
            "dot, underscore or dash"
        )
    return text


@dataclass
class Note:
    """A single note. Timestamps are ISO-8601 UTC strings."""

    id: str
    title: str
    body: str
    created_at: str
    updated_at: str
    tags: list[str] = field(default_factory=list)
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Note:
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})


class NotesStore:
    """JSON-backed note store, one file per scope.

    Args:
        base_dir: Directory holding the per-scope JSON files (created lazily).
        now: Clock override returning an ISO-8601 string (test seam).
        max_notes: Upper bound on notes per scope.
    """

    def __init__(
        self,
        base_dir: str | os.PathLike[str],
        *,
        now: Callable[[], str] | None = None,
        max_notes: int = MAX_NOTES_PER_SCOPE,
    ) -> None:
        self._max_notes = _integer(
            max_notes, "max_notes", low=1, high=MAX_NOTES_PER_SCOPE
        )
        self._base = Path(base_dir)
        self._now = now or _utc_now

    def scope_path(self, scope: Any) -> Path:
        """Return the on-disk path for ``scope``, verifying containment."""
        target = (self._base / f"{_normalize_scope(scope)}.json").resolve()
        base = self._base.resolve()
        if target != base and base not in target.parents:
            # Defensive: _normalize_scope should already prevent this.
            raise NoteStoreError("scope escapes the storage directory")
        return target

    def _read(self, scope: Any) -> list[Note]:
        """Load one scope's notes; missing file means an empty scope."""
        path = self.scope_path(scope)
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NoteStoreError(f"note store is unreadable: {path.name}") from exc
        if not isinstance(raw, dict):
            raise NoteStoreError(f"note store has an invalid shape: {path.name}")
        items = raw.get("notes")
        if items is None:
            return []
        if not isinstance(items, list):
            raise NoteStoreError(f"note store has an invalid shape: {path.name}")
        return [Note.from_dict(item) for item in items if isinstance(item, Mapping)]

    def _write(self, scope: Any, notes: list[Note]) -> None:
        """Atomically replace one scope's file."""
        path = self.scope_path(scope)
        payload = {
            "version": STORAGE_VERSION,
            "scope": _normalize_scope(scope),
            "notes": [note.to_dict() for note in notes],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    def _find(self, notes: list[Note], note_id: Any) -> Note:
        wanted = _text(note_id, "note_id", max_length=64, required=True)
        for note in notes:
            if note.id == wanted:
                return note
        raise NoteStoreError(f"note not found: {wanted}")

    def list_notes(
        self,
        scope: Any = None,
        *,
        query: Any = None,
        tag: Any = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return notes for ``scope``, pinned first then newest-updated."""
        notes = self._read(scope)

        if tag is not None:
            wanted = _text(tag, "tag", max_length=MAX_TAG_LENGTH, required=True)
            notes = [n for n in notes if wanted.casefold() in _folded(n.tags)]

        if query is not None:
            needle = _text(query, "query", max_length=MAX_QUERY_LENGTH, required=False)
            if needle:
                notes = [n for n in notes if _matches(n, needle.casefold())]

        # Pinned first, then most recently updated.  ISO-8601 UTC strings are
        # fixed-width, so reversing the code points reverses chronological order.
        notes.sort(key=lambda note: (0 if note.pinned else 1, _invert(note.updated_at)))

        if limit is not None:
            notes = notes[: _integer(limit, "limit", low=0, high=MAX_NOTES_PER_SCOPE)]
        return [note.to_dict() for note in notes]

    def get_note(self, scope: Any, note_id: Any) -> dict[str, Any]:
        """Return one note by id."""
        return self._find(self._read(scope), note_id).to_dict()

    def stats(self, scope: Any = None) -> dict[str, Any]:
        """Return counts for the workspace header."""
        notes = self._read(scope)
        tags = {tag.casefold() for note in notes for tag in note.tags}
        return {
            "scope": _normalize_scope(scope),
            "notes": len(notes),
            "pinned": sum(1 for note in notes if note.pinned),
            "tags": len(tags),
            "max_notes": self._max_notes,
        }

    def create_note(
        self,
        scope: Any = None,
        *,
        title: Any = None,
        body: Any = None,
        tags: Any = None,
        pinned: Any = False,
    ) -> dict[str, Any]:
        """Create a note and return it."""
        clean_title = _text(title, "title", max_length=MAX_TITLE_LENGTH, required=True)
        clean_body = _text(body, "body", max_length=MAX_BODY_LENGTH, required=False)
        clean_tags = _normalize_tags(tags)
        clean_pinned = _boolean(pinned, "pinned")

        notes = self._read(scope)
        if len(notes) >= self._max_notes:
            raise NoteStoreError(
                f"scope already holds the maximum of {self._max_notes} notes"
            )

        moment = self._now()
        note = Note(
            id=uuid.uuid4().hex[:12],
            title=clean_title,
            body=clean_body,
            created_at=moment,
            updated_at=moment,
            tags=clean_tags,
            pinned=clean_pinned,
        )
        notes.append(note)
        self._write(scope, notes)
        return note.to_dict()

    def update_note(
        self,
        scope: Any = None,
        note_id: Any = None,
        *,
        title: Any = None,
        body: Any = None,
        tags: Any = None,
        pinned: Any = None,
    ) -> dict[str, Any]:
        """Apply a partial update to one note and return the new value."""
        notes = self._read(scope)
        note = self._find(notes, note_id)

        if title is not None:
            note.title = _text(
                title, "title", max_length=MAX_TITLE_LENGTH, required=True
            )
        if body is not None:
            note.body = _text(body, "body", max_length=MAX_BODY_LENGTH, required=False)
        if tags is not None:
            note.tags = _normalize_tags(tags)
        if pinned is not None:
            note.pinned = _boolean(pinned, "pinned")

        note.updated_at = self._now()
        self._write(scope, notes)
        return note.to_dict()

    def delete_note(self, scope: Any = None, note_id: Any = None) -> dict[str, Any]:
        """Delete one note and return the removed value."""
        notes = self._read(scope)
        note = self._find(notes, note_id)
        self._write(scope, [item for item in notes if item.id != note.id])
        return note.to_dict()

    def export_markdown(self, scope: Any = None) -> str:
        """Render the scope as a Markdown document."""
        canonical = _normalize_scope(scope)
        notes = [Note.from_dict(item) for item in self.list_notes(scope)]
        lines = [f"# Notes: {canonical}", ""]
        if not notes:
            lines.append("_No notes yet._")
            return "\n".join(lines) + "\n"

        for note in notes:
            marker = " (pinned)" if note.pinned else ""
            lines += [f"## {note.title}{marker}", "", f"- id: `{note.id}`"]
            lines.append(f"- updated: {note.updated_at}")
            if note.tags:
                lines.append(f"- tags: {', '.join(note.tags)}")
            lines += ["", note.body if note.body else "_(empty)_", ""]
        return "\n".join(lines)


def _folded(tags: Iterable[str]) -> set[str]:
    """Return the case-folded tag set of a note."""
    return {tag.casefold() for tag in tags}


def _invert(value: str) -> str:
    """Return a sort key that orders ISO-8601 strings newest-first."""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in value)


def _matches(note: Note, needle: str) -> bool:
    """Case-insensitive substring match across title, body and tags."""
    return (
        needle in note.title.casefold()
        or needle in note.body.casefold()
        or any(needle in tag.casefold() for tag in note.tags)
    )
