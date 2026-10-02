"""GoogleDocsProvider: DocProvider implemented on the Docs and Drive APIs.

Every google dependency is **loaded lazily**, so this module imports cleanly without
the extras installed and only raises when instantiated.
"""

from __future__ import annotations

import asyncio
import html
import logging
import json
import re
import threading
from typing import Any

from . import google_formats
from .provider import (
    PRE_MUTATION_KINDS,
    PageAdded,
    UNSUPPORTED_KIND_DETAIL,
    AgentIdentity,
    DocSummary,
    DocComment,
    DocCapabilities,
    DocProvider,
    DocRef,
    DocReply,
    DocSnapshot,
    EditResult,
    ProviderError,
    ReplyRef,
    Segment,
    is_supported_kind,
)

logger = logging.getLogger(__name__)

_SCOPES = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    # Spreadsheets and slides are separate APIs with separate scopes. Granted here
    # rather than per format because a service account's key is authorised once, at
    # the point it is issued: discovering mid-run that the key lacks a scope is a
    # failure a person has to go and fix, and it would happen the first time someone
    # commented on a sheet rather than at configuration time.
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
)

# The shapes a share link comes in; the last one is a bare fileId.
_DOC_ID_PATTERNS = (
    re.compile(r"/document/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"^([A-Za-z0-9_-]{10,})$"),
)

# The two meanings of a 400 are told apart by the message text; the status code is
# identical in both.
_REVISION_CONFLICT_MARKER = "required revision id"

# Drive reports throttling as a 403 userRateLimitExceeded, and sometimes a 429.
# Classifying by status code alone would read a throttled document as a permission
# failure and freeze it permanently.
_RATE_LIMIT_REASONS = frozenset({"userRateLimitExceeded", "rateLimitExceeded"})


def _err_details(exc: Any) -> tuple[int | None, str, str]:
    """Dig (status, machine_reason, message) out of an HttpError.

    ``HttpError.reason`` is the human-readable message, not the machine reason. The
    latter hides in the response body, which comes in three shapes: a list of reason
    dictionaries, a details list, or a bare string.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    message = ""
    reason = ""
    try:
        body = json.loads(exc.content.decode())
        err = body.get("error") or {}
        message = err.get("message") or ""
        errors = err.get("errors")
        if isinstance(errors, list) and errors:
            reason = (errors[0] or {}).get("reason") or ""
        if not reason:
            details = err.get("details")
            if isinstance(details, list):
                for d in details:
                    if isinstance(d, dict) and d.get("reason"):
                        reason = d["reason"]
                        break
        if not reason:
            reason = err.get("status") or ""
    except Exception:  # noqa: BLE001 - the error body's shape is not ours to control,
        # and failing to parse it must not hide the original error
        message = str(exc)
    return status, reason, message


def _classify(exc: Any) -> ProviderError:
    status, reason, message = _err_details(exc)
    if reason in _RATE_LIMIT_REASONS or status == 429:
        return ProviderError("rate_limited", message, status=status)
    if status == 404:
        return ProviderError("not_found", message, status=status)
    if status == 403:
        return ProviderError("forbidden", message, status=status)
    if status == 400:
        if _REVISION_CONFLICT_MARKER in message.lower():
            return ProviderError("conflict", message, status=status)
        return ProviderError("invalid", message, status=status)
    return ProviderError("transport", message or str(exc), status=status)


def _utf16_len(s: str) -> int:
    """A string's length in UTF-16 code units, a surrogate pair counting as two. That is
    the unit document indices are measured in."""
    return len(s.encode("utf-16-le")) // 2


# Docs' own highlighter yellow, close enough to be read as "marked", light enough to
# leave the text legible.
_HIGHLIGHT_RGB = {"red": 1.0, "green": 0.898, "blue": 0.314}


def _trim_common_affixes(old: str, new: str) -> tuple[int, str, str]:
    """(prefix_len_utf16, old_middle, new_middle) with the shared prefix and suffix
    removed.

    Locating always uses the **full** old_string -- a trimmed one is shorter and can
    stop being unique -- so trimming happens at request construction, on the located
    span. The prefix length is returned in UTF-16 code units because it offsets a
    document index.

    Prefix first, then suffix on what remains, so the two never overlap when old and
    new share a run (e.g. "aaa" → "aa")."""
    a, b = old, new
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[-1 - suf] == b[-1 - suf]:
        suf += 1
    return (
        _utf16_len(a[:pre]),
        a[pre : len(a) - suf],
        b[pre : len(b) - suf],
    )


def _edit_requests(located: list[tuple[int, int, str, str]], *, highlight: bool) -> list[dict]:
    """batchUpdate requests for located edits, already sorted in descending order.

    A pure function on purpose: the wiring above it was mutation-tested once before and
    survived because only constants were asserted -- this shape lets a test drive the
    actual request construction.

    Each (old, new) pair is trimmed to the middle that actually differs before any
    request is built. The model habitually hands back whole rewritten paragraphs;
    replacing the whole span painted unchanged words too (measured: four edits turned
    four entire paragraphs yellow when the real changes were a few sentences), and the
    larger replacement also widened the revision-conflict surface. A pair whose middle
    is empty on both sides produces no request at all.

    With ``highlight``, each insertion gets an ``updateTextStyle`` over exactly the
    inserted middle, in the same batch. Spans are measured in UTF-16 code units like
    every other index here. Deletions get no style request -- there is nothing left to
    paint."""
    requests: list[dict] = []
    for i0, i1, old, new in located:
        pre16, old_mid, new_mid = _trim_common_affixes(old, new)
        if not old_mid and not new_mid:
            continue  # identical text: nothing to change, nothing to paint
        d0 = i0 + pre16
        d1 = d0 + _utf16_len(old_mid)
        if old_mid:
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": d0, "endIndex": d1}}}
            )
        if new_mid:
            requests.append({"insertText": {"location": {"index": d0}, "text": new_mid}})
            if highlight:
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": d0, "endIndex": d0 + _utf16_len(new_mid)},
                        "textStyle": {
                            "backgroundColor": {"color": {"rgbColor": _HIGHLIGHT_RGB}}
                        },
                        "fields": "backgroundColor",
                    }
                })
    return requests


def _unescape_quoted(value: str) -> str:
    """Restore ``quotedFileContent.value`` to plain text in the body's coordinate system.

    **Drive returns the quote HTML-escaped, while the body from ``documents.get`` is
    not.** The two are used as one coordinate system -- ``anchor_quote`` does a literal
    substring match, ``ctx_hash`` takes a window by code point -- so a selection
    containing a quote mark, ampersand or angle bracket is guaranteed to fail anchoring.

    Measured: a selection containing typographic quotation marks matched zero times as
    returned, and exactly once after unescaping. The failure surfaces as "cannot be
    located uniquely, please comment again" -- a quiet dead end no amount of retrying
    gets past.

    ``html.unescape`` is the identity on text without entities, so nothing else is
    affected.
    """
    return html.unescape(value)


class GoogleDocsProvider(DocProvider):
    def __init__(self, credentials_file: str) -> None:
        # The receipt sink (PR2c, D8): injected at wiring time so the provider stays
        # gateway-free. When present, every body write records a WAL receipt around
        # the platform call -- begin before, commit after, abort on a known refusal,
        # and an unknown outcome (exception) leaves it pending for the sweep.
        self.receipt_sink = None
        self._credentials_file = credentials_file
        # One client per thread. Sharing a Resource hangs: measured with 8 threads on a
        # single shared Resource, 5 of 8 timed out and the run took 60s; building one per
        # thread gave 0 failures in 0.38s.
        self._local = threading.local()
        # **The per-thread cache above is defeated by how _call works**, so this lock is
        # what actually keeps one SDK call at a time. See _call for the crash it fixes.
        # A threading.Lock, not asyncio.Lock: the watcher and the chat path may run on
        # different event loops, and an asyncio primitive bound to one of them would not
        # guard the other.
        self._call_lock = threading.Lock()
        self._identity: AgentIdentity | None = None
        self._revision_cache: dict[DocRef, str] = {}
        # One connection reaches documents, sheets and decks alike, so the format is
        # a property of the document and has to be looked up. Cached because a
        # document does not change type and this is on the read path.
        self._kind_cache: dict[DocRef, str] = {}

    # ---------------------------------------------------------------- clients

    def _clients(self) -> tuple[Any, Any]:
        got = getattr(self._local, "clients", None)
        if got is not None:
            return got
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - depends on the extras being installed
            raise ProviderError(
                "invalid",
                "clouddoc 需要 google extras：pip install 'jiuwenswarm[clouddoc]'",
            ) from exc
        cred = service_account.Credentials.from_service_account_file(
            self._credentials_file, scopes=list(_SCOPES)
        )
        clients = (
            build("docs", "v1", credentials=cred, cache_discovery=False),
            build("drive", "v3", credentials=cred, cache_discovery=False),
        )
        self._local.clients = clients
        return clients

    def _extra_client(self, name: str, version: str) -> Any:
        """A per-thread client for one of the format APIs.

        Built lazily and separately from the pair above: a deployment that only ever
        touches documents should not pay discovery for sheets and slides, and the
        thread-affinity reason for caching is the same one recorded in ``_clients``.
        """
        cache = getattr(self._local, "extra", None)
        if cache is None:
            cache = {}
            self._local.extra = cache
        got = cache.get(name)
        if got is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            cred = service_account.Credentials.from_service_account_file(
                self._credentials_file, scopes=list(_SCOPES)
            )
            got = build(name, version, credentials=cred, cache_discovery=False)
            cache[name] = got
        return got

    def _sheets_client(self) -> Any:
        return self._extra_client("sheets", "v4")

    def _slides_client(self) -> Any:
        return self._extra_client("slides", "v1")

    async def doc_kind(self, doc_ref: DocRef) -> str:
        """Which format this document is, cached for the life of the process.

        A document does not change type, so one metadata call each is enough. An
        unrecognised type answers ``""`` and every path that reads this refuses rather
        than guessing: sending a Docs request at a spreadsheet produces an error whose
        text explains nothing to the person who has to act on it.
        """
        got = self._kind_cache.get(doc_ref)
        if got is not None:
            return got
        _, drive = self._clients()
        meta = await self._call(
            drive.files().get(fileId=doc_ref, fields="mimeType,name").execute
        )
        kind = google_formats.kind_for(str(meta.get("mimeType") or ""))
        self._kind_cache[doc_ref] = kind
        return kind

    async def _require_kind(self, doc_ref: DocRef) -> str:
        """The document's format, refusing anything co-scribe does not serve.

        Two different refusals arrive here as one. A kind that never resolved is a
        file type the connection cannot open at all. A kind that resolved to a
        withdrawn format -- markdown, for a document adopted while it was still
        served -- is a row already in someone's panel, and the format cache is
        primed from that persisted row on every restart. Both must stop here: the
        reader below falls through to the *document* API for an unrecognised kind,
        so letting either past would read a file as a Google Doc and report the
        platform's confusion instead of ours.
        """
        kind = await self.doc_kind(doc_ref)
        if not kind or not is_supported_kind(kind):
            raise ProviderError("invalid", UNSUPPORTED_KIND_DETAIL)
        return kind

    async def _call(self, fn, *args, **kwargs):
        """Move a blocking SDK call off the event loop and normalize its exceptions into
        ProviderError."""

        def run():
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - classified first, then re-raised
                if type(exc).__name__ == "HttpError":
                    raise _classify(exc) from exc
                raise ProviderError("transport", str(exc)) from exc

        # **One SDK call at a time per provider.** ``_clients`` caches on a
        # ``threading.local``, which reads as thread-safe and is not enough here: the
        # client is fetched on the *calling* thread while ``to_thread`` runs the request
        # on a pool worker, so one httplib2/SSL connection ends up shared across workers.
        # google-api-python-client's http object is documented as not thread-safe, and
        # concurrent use corrupts OpenSSL's heap -- observed twice as a hard SIGTRAP
        # inside ``SSL_read_ex`` (crash reports Python-2026-08-13-1740/1745), each
        # preceded by "[SSL] record layer failure" from the watcher. Serializing here
        # costs latency only when the watcher poll and a chat turn overlap, which is
        # exactly the case that was crashing the process.
        def guarded():
            with self._call_lock:
                return run()

        return await asyncio.to_thread(guarded)

    # ---------------------------------------------------------------- basics

    @property
    def kind(self) -> str:
        return "google"

    def parse_doc_ref(self, url_or_id: str) -> DocRef:
        s = (url_or_id or "").strip()
        for pat in _DOC_ID_PATTERNS:
            m = pat.search(s)
            if m:
                return m.group(1)
        raise ProviderError("invalid", f"无法从 {url_or_id!r} 解析出文档 id")

    async def self_identity(self) -> AgentIdentity:
        if self._identity is None:
            with open(self._credentials_file, encoding="utf-8") as fh:
                info = json.load(fh)
            email = info.get("client_email") or ""
            # Drive's display name for a service account's comments happens to be the
            # full email. It still must not be used to identify anyone else.
            self._identity = AgentIdentity(display_name=email, address=email)
        return self._identity

    async def title(self, doc_ref: DocRef) -> str:
        _, drive = self._clients()
        meta = await self._call(drive.files().get(fileId=doc_ref, fields="name").execute)
        return str(meta.get("name") or "")

    async def capabilities(self, doc_ref: DocRef) -> DocCapabilities:
        _, drive = self._clients()
        meta = await self._call(
            drive.files()
            .get(fileId=doc_ref, fields="capabilities(canEdit,canComment,canModifyContent)")
            .execute
        )
        caps = meta.get("capabilities") or {}
        can_edit = bool(caps.get("canEdit") and caps.get("canModifyContent"))
        # revisionId is only populated with edit rights, and without it there is no
        # concurrency protection. Infer it from capabilities rather than the reverse: the
        # missing revisionId is the consequence, not the cause.
        #
        # Rights are only half the answer, though: a spreadsheet has no revision id
        # **at all**, whatever the rights -- its API carries no such field anywhere,
        # measured (§18.6). So the format's own trait can only take the flag away,
        # never grant it (C9: an absent capability is declared, not simulated).
        kind = await self.doc_kind(doc_ref)
        traits = google_formats.TRAITS.get(kind)
        has_revision = can_edit and (traits.has_revision_control if traits else True)
        return DocCapabilities(
            can_read=True,
            can_edit=can_edit,
            can_comment=bool(caps.get("canComment")),
            can_resolve=bool(caps.get("canComment")),
            has_revision_control=has_revision,
            max_quote_chars=418,
        )

    _URL_BY_KIND = {
        "document": "https://docs.google.com/document/d/{id}/edit",
        "spreadsheet": "https://docs.google.com/spreadsheets/d/{id}/edit",
        "presentation": "https://docs.google.com/presentation/d/{id}/edit",
        # A withdrawn format still has a row in someone's panel, and that row still
        # needs a link a person can open. This is the address Drive's own API returns
        # as ``webViewLink`` for such a file (verified against the API).
        "markdown": "https://drive.google.com/file/d/{id}/view",
    }

    def doc_url(self, doc_ref: DocRef, kind: str = "") -> str:
        """The right link per format. Falls back to the Drive file viewer, which
        opens every type, rather than to the document editor, which opens one.

        The fallback used to be ``/preview`` so that a kind which never resolved
        would still render inside the workbench's frame -- ``/view`` is served with
        ``X-Frame-Options: SAMEORIGIN`` (measured) and ``/preview`` is not. That
        traded one failure for a worse one: ``/preview`` is not an address Drive
        offers for every file. For a ``text/markdown`` file the API returns
        ``/view`` as its ``webViewLink``, and ``/preview`` answers with Google's
        "You need access" page **even to the file's owner** -- a link that accuses
        the reader of a permission problem they do not have.

        It is ``/view`` again, and the frame is no longer the reason to choose:
        every format the workbench embeds has an explicit entry above, so this
        fallback is only ever reached by a format co-scribe does not serve, and
        those are not embedded at all (the workbench offers the link instead).
        """
        template = self._URL_BY_KIND.get(
            kind or self._kind_cache.get(doc_ref, ""),
            "https://drive.google.com/file/d/{id}/view",
        )
        return template.format(id=doc_ref)

    async def text_domain_for(self, doc_ref: DocRef) -> str:
        """Per document, because a Drive holds several formats.

        All three Google formats are plain today, so this answers the same thing for
        each. It stays per-document rather than per-provider because the Feishu side
        genuinely differs -- a docx round-trips markdown, its sheets and decks do not
        -- and one connection there reaches both.
        """
        kind = await self.doc_kind(doc_ref)
        traits = google_formats.TRAITS.get(kind)
        return traits.text_domain if traits else self.text_domain

    async def create_document(self, title: str) -> DocRef:
        _, drive = self._clients()
        f = await self._call(
            drive.files()
            .create(
                body={"name": title, "mimeType": "application/vnd.google-apps.document"},
                fields="id",
            )
            .execute
        )
        return f["id"]

    async def share_document(
        self, doc_ref: DocRef, email: str, *, role: str = "writer"
    ) -> None:
        _, drive = self._clients()
        await self._call(
            drive.permissions()
            .create(
                fileId=doc_ref,
                body={"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail=False,
                fields="id",
            )
            .execute
        )

    async def trash_document(self, doc_ref: DocRef) -> None:
        _, drive = self._clients()
        await self._call(
            drive.files().update(fileId=doc_ref, body={"trashed": True}, fields="id").execute
        )

    _KIND_BY_MIME = {
        "application/vnd.google-apps.form": "form",
        "application/vnd.google-apps.drawing": "drawing",
        "application/vnd.google-apps.script": "script",
    }

    # Every format co-scribe can now open. The two lists below are complements of each
    # other by construction: a format added here leaves the unsupported list in the
    # same edit, so the panel cannot end up telling someone a document is unsupported
    # while the agent is already editing it.
    _SUPPORTED_MIMES = (
        google_formats.MIME_DOCUMENT,
        google_formats.MIME_SPREADSHEET,
        google_formats.MIME_PRESENTATION,
    )

    @staticmethod
    def _mime_clause(mimes: tuple[str, ...], *, negate: bool) -> str:
        op = "!=" if negate else "="
        join = " and " if negate else " or "
        return "(" + join.join(f"mimeType {op} '{m}'" for m in mimes) + ")"

    async def _drive_list(self, query: str, fields: str) -> list[dict]:
        _, drive = self._clients()
        out: list[dict] = []
        token = None
        while True:
            kw: dict[str, Any] = {
                "q": query,
                "fields": f"files({fields}),nextPageToken",
                "pageSize": 100,
            }
            if token:
                kw["pageToken"] = token
            page = await self._call(drive.files().list(**kw).execute)
            out.extend(page.get("files") or [])
            token = page.get("nextPageToken")
            if not token:
                return out

    async def list_shared_unsupported(self) -> list[dict]:
        query = (
            self._mime_clause(self._SUPPORTED_MIMES, negate=True)
            + " and mimeType != 'application/vnd.google-apps.folder' and trashed=false"
        )
        files = await self._drive_list(query, "id,name,mimeType")
        out: list[dict] = []
        for f in files:
            mime = f.get("mimeType") or ""
            out.append({
                "title": f.get("name") or "",
                "kind": self._KIND_BY_MIME.get(mime, "file"),
            })
        return out

    async def list_accessible_documents(self, known=()) -> list[DocSummary]:
        # Every format co-scribe can open, not documents alone. A service account's
        # Drive also holds whatever else was shared with it, and what is left out here
        # is exactly what ``list_shared_unsupported`` shows the person instead.
        query = (
            self._mime_clause(self._SUPPORTED_MIMES, negate=False) + " and trashed=false"
        )
        # Both capability flags, because admission reads both: a document that is
        # canEdit but not canModifyContent would be adopted and then refused, which is
        # exactly the silent half-working state this feature exists to prevent.
        files = await self._drive_list(
            query, "id,name,mimeType,capabilities(canEdit,canModifyContent)"
        )
        out: list[DocSummary] = []
        for f in files:
            kind = google_formats.kind_for(str(f.get("mimeType") or ""))
            if not kind:
                continue
            caps = f.get("capabilities") or {}
            self._kind_cache[f["id"]] = kind
            out.append(DocSummary(
                doc_id=f["id"],
                title=f.get("name") or "",
                can_edit=bool(caps.get("canEdit") and caps.get("canModifyContent")),
                kind=kind,
            ))
        return out

    async def sharing_posture(self, doc_ref: DocRef) -> list[tuple[str, str]]:
        _, drive = self._clients()
        out: list[tuple[str, str]] = []
        token = None
        while True:
            kw: dict[str, Any] = {
                "fileId": doc_ref,
                "fields": "permissions(type,role),nextPageToken",
                "pageSize": 100,
            }
            if token:
                kw["pageToken"] = token
            page = await self._call(drive.permissions().list(**kw).execute)
            for p in page.get("permissions") or []:
                out.append((p.get("type") or "", p.get("role") or ""))
            token = page.get("nextPageToken")
            if not token:
                return out

    # ---------------------------------------------------------------- read

    @staticmethod
    def _paragraph_text(paragraph: dict) -> tuple[str, list[tuple[int, int, int, int]]]:
        """A paragraph to (text, [(char_off, char_len, index_start, index_end)]).

        Non-text elements -- inlineObjectElement, footnoteReference and the rest --
        **contribute no characters at all**. Measured, they do not appear in
        quotedFileContent, so emitting a placeholder for them would put the quote out of
        alignment. They do **occupy document indices**, which is why each textRun becomes
        its own segment: the index gaps between segments are the skipped elements.
        """
        text_parts: list[str] = []
        spans: list[tuple[int, int, int, int]] = []
        cursor = 0
        for el in paragraph.get("elements", []) or []:
            run = el.get("textRun")
            if run is None:
                continue
            content = run.get("content") or ""
            if not content:
                continue
            spans.append((cursor, len(content), el["startIndex"], el["endIndex"]))
            text_parts.append(content)
            cursor += len(content)
        return "".join(text_parts), spans

    def _flatten(self, doc: dict) -> tuple[str, tuple[Segment, ...]]:
        """The one flattening that makes quotedFileContent a literal substring of the
        body, arrived at by measuring the alternatives.

        - Paragraphs take only textRuns; non-text elements emit no characters
        - Tables recurse, with **cells joined by newlines** -- a quote spanning a table
          comes back as TA\\nTB\\nTC\\nTD
        - Footnote bodies stay out of the stream
        """
        chunks: list[str] = []
        segments: list[Segment] = []
        base = 0

        def emit(paragraph: dict) -> None:
            nonlocal base
            text, spans = self._paragraph_text(paragraph)
            for char_off, char_len, i0, i1 in spans:
                segments.append(
                    Segment(
                        char_start=base + char_off,
                        char_end=base + char_off + char_len,
                        index_start=i0,
                        index_end=i1,
                    )
                )
            chunks.append(text)
            base += len(text)

        def walk(elements: list[dict]) -> None:
            for el in elements or []:
                if "paragraph" in el:
                    emit(el["paragraph"])
                elif "table" in el:
                    for row in el["table"].get("tableRows") or []:
                        for cell in row.get("tableCells") or []:
                            walk(cell.get("content") or [])

        walk((doc.get("body") or {}).get("content") or [])
        return "".join(chunks), tuple(segments)

    async def read(self, doc_ref: DocRef) -> DocSnapshot:
        kind = await self._require_kind(doc_ref)
        reader = google_formats.READERS.get(kind)
        if reader is not None:
            return await reader(self, doc_ref)
        docs, _ = self._clients()
        doc = await self._call(docs.documents().get(documentId=doc_ref).execute)
        text, segments = self._flatten(doc)
        revision = doc.get("revisionId")
        if revision:
            self._revision_cache[doc_ref] = revision
        return DocSnapshot(
            doc_id=doc_ref,
            kind="document",
            revision_id=revision,
            text=text,
            segments=segments,
        )

    # ---------------------------------------------------------------- edit

    def _locate(
        self, snap: DocSnapshot, needle: str, window: tuple[int, int] | None = None
    ) -> tuple[int, int]:
        """Locate needle uniquely in the flat text and return a document index range.

        ``window`` is the character range the caller's range rail already approved. Given
        it, the search runs **only inside that range**, judging by exactly the rail's
        rule. Without it the fallback is uniqueness across the whole body, which is all
        the chat path can do, having no rail.

        The two layers must share one rule. A proposal the rail passed as unique within
        its window, re-judged here as unique in the body, would be refused **after a
        person already approved it** -- and a phrase occurring twice is ordinary in a
        long document.
        """
        lo, hi = window if window else (0, len(snap.text))
        lo = max(0, lo)
        hi = min(len(snap.text), hi)
        hay = snap.text[lo:hi]
        first = hay.find(needle)
        if first < 0:
            where = "the allowed range" if window else "the body"
            raise ProviderError("invalid", f"{needle[:40]!r} not found in {where}")
        if hay.find(needle, first + 1) >= 0:
            where = "the allowed range" if window else "the body"
            raise ProviderError("invalid", f"{needle[:40]!r} is not unique within {where}")
        abs0 = lo + first
        return self._chars_to_indices(snap, abs0, abs0 + len(needle))

    @staticmethod
    def _chars_to_indices(snap: DocSnapshot, c0: int, c1: int) -> tuple[int, int]:
        """A character range to a document index range, converting through the first and
        last segments when it spans several."""
        start = end = None
        for seg in snap.segments:
            if seg.char_start <= c0 < seg.char_end and start is None:
                start = seg.index_start + _utf16_len(
                    # The prefix within a segment counts in UTF-16: a surrogate pair
                    # takes two indices
                    snap.text[seg.char_start : c0]
                )
            if seg.char_start < c1 <= seg.char_end:
                end = seg.index_start + _utf16_len(snap.text[seg.char_start : c1])
        if start is None or end is None:
            raise ProviderError("invalid", "字符区间无法映射到文档索引")
        return start, end

    async def edit(
        self,
        doc_ref: DocRef,
        old_string: str,
        new_string: str,
        *,
        revision_id: str | None = None,
    ) -> EditResult:
        snap = await self.read(doc_ref)
        rev = revision_id or snap.revision_id or self._revision_cache.get(doc_ref)
        return await self._submit(doc_ref, snap, [(old_string, new_string)], rev)

    async def edit_batch(
        self,
        doc_ref: DocRef,
        edits: list[tuple[str, str]],
        *,
        required_revision_id: str,
        window: tuple[int, int] | None = None,
        highlight: bool = False,
    ) -> EditResult:
        snap = await self.read(doc_ref)
        writer = google_formats.WRITERS.get(snap.kind)
        if writer is not None:
            # ``window`` is carried through, not dropped. It was dropped, on the
            # reasoning that the placement check bounds these formats more tightly --
            # true, but beside the point: without the window this layer re-judges
            # uniqueness across the whole body, and a quote of "42" matches every cell
            # showing 42. The proposal is then refused *after a person approved it*,
            # which is the exact failure the document path's ``_locate`` warns about.
            # Both layers judge by one rule.
            #
            # ``highlight`` is genuinely dropped: neither Sheets nor Slides has the
            # text-styling primitive the document path uses to mark what changed, and
            # simulating one with a coloured cell or shape would be a different,
            # permanent change to someone's document (C9).
            return await writer(self, doc_ref, snap, edits, required_revision_id, window)
        return await self._submit(
            doc_ref, snap, edits, required_revision_id, window, highlight=highlight
        )

    async def _submit(
        self,
        doc_ref: DocRef,
        snap: DocSnapshot,
        edits: list[tuple[str, str]],
        revision_id: str | None,
        window: tuple[int, int] | None = None,
        *,
        highlight: bool = False,
    ) -> EditResult:
        # Writing into an empty document is an insertion, not a replacement: there is no
        # anchor to locate and no segment table to map through, so ``_locate`` cannot
        # answer. It is accepted only when the body really is empty -- the caller checks
        # that too -- because "insert wherever" is a much wider capability than "replace
        # what you can uniquely locate".
        if len(edits) == 1 and not edits[0][0]:
            if snap.text.strip():
                return EditResult(
                    "invalid",
                    detail="an empty old_string is only accepted for an empty document",
                )
            return await self._insert_first(
                doc_ref, edits[0][1], revision_id, highlight=highlight
            )

        located = []
        for old, new in edits:
            try:
                i0, i1 = self._locate(snap, old, window)
            except ProviderError as exc:
                # Same shape as the API layer's invalid: a failed location is a
                # deterministic refusal, not an exceptional path. The startup sweep stops
                # and retires the proposal on it rather than retrying.
                return EditResult("invalid", detail=str(exc))

            # **The index span must equal the text's UTF-16 length exactly.**
            #
            # Flattening deliberately has non-text elements emit no characters, or
            # quotedFileContent would not line up, but they **do occupy indices**. So a
            # run of text spanning them carries those elements inside its index range,
            # and deleteContentRange deletes the whole range -- images, footnote
            # references and people chips would vanish with it, none of which the user
            # can even see in the quote.
            #
            # Measured on recorded data: a substring spanning one gap had a UTF-16 length
            # of 6 and an index span of 7.
            span = _utf16_len(old)
            if (i1 - i0) != span:
                return EditResult(
                    "invalid",
                    detail=(
                        f"the range of {old[:30]!r} contains non-text elements "
                        "(images, footnote references); editing across them would "
                        "delete them too — narrow the edit to exclude them"
                    ),
                )
            located.append((i0, i1, old, new))

        # **Overlap check.** The range rail guarantees each old_string is unique within
        # the window, not that two of them do not intersect. When they do, applying in
        # descending order puts the second deletion where the first edit already shifted
        # things, and unrelated text gets cut out.
        ordered = sorted(located, key=lambda x: x[0])
        for (a0, a1, _, _), (b0, _, _, _) in zip(ordered, ordered[1:]):
            if b0 < a1:
                return EditResult(
                    "invalid",
                    detail="two edits in the proposal overlap; they cannot be applied safely",
                )

        # Apply in descending order, or an earlier edit shifts a later one's indices.
        located.sort(key=lambda x: -x[0])

        body: dict[str, Any] = {"requests": _edit_requests(located, highlight=highlight)}
        if revision_id:
            body["writeControl"] = {"requiredRevisionId": revision_id}

        receipt_id = self._receipt_begin(doc_ref, located, highlight=highlight)
        docs, _ = self._clients()
        try:
            resp = await self._call(
                docs.documents().batchUpdate(documentId=doc_ref, body=body).execute
            )
        except ProviderError as exc:
            if exc.kind == "conflict":
                self._receipt_abort(receipt_id, "conflict")
                return EditResult("conflict", detail=str(exc))
            if exc.kind == "invalid":
                # Never a conflict: that would issue a false "applied" confirmation for
                # an edit that never landed.
                self._receipt_abort(receipt_id, "invalid")
                return EditResult("invalid", detail=str(exc))
            # Unknown outcome: leave the receipt pending for the sweep (IC-3) --
            # closing it here could hide a write that actually landed.
            raise
        new_rev = ((resp.get("writeControl") or {}).get("requiredRevisionId")) or None
        if new_rev:
            self._revision_cache[doc_ref] = new_rev
        self._receipt_commit(receipt_id, new_rev, highlighted=highlight)
        return EditResult("applied", new_revision_id=new_rev, highlighted=highlight,
                          receipt_id=receipt_id)

    async def add_page(self, doc_ref: DocRef, title: str = "") -> PageAdded:
        """Add a worksheet or slide, recorded like any other write.

        A page creation is a write, so it is written ahead like every other one: the
        receipt opens before the platform is asked and settles after. Crash in between
        and the sweep finds a pending entry and settles it as unknown, which is the
        honest answer -- exactly the discipline the shell bypass escaped.
        """
        kind = await self._require_kind(doc_ref)
        adder = google_formats.ADD_PAGE.get(kind)
        if adder is None:
            raise ProviderError(
                "unsupported",
                f"{kind} 没有「页」的概念，无法新增；文字内容请用 clouddoc_batch_edit。",
            )
        receipt_id = self._receipt_begin(
            doc_ref, [("", f"新增页：{title or '(未命名)'}")], highlight=False,
        )
        try:
            address = await adder(self, doc_ref, title)
        except ProviderError as exc:
            # Only a refusal the platform itself reported closes the receipt: the
            # request was judged before anything changed. A timeout or a dropped
            # connection may have created the page, and closing that receipt as
            # aborted would hide a landed write behind a "nothing happened" -- the
            # retry it invites is how one page becomes two. Those stay pending for
            # the sweep, as the write-ahead contract promises.
            if exc.kind in PRE_MUTATION_KINDS:
                self._receipt_abort(receipt_id, str(exc)[:200])
            raise
        except Exception as exc:  # noqa: BLE001 - outcome unknown: leave the receipt pending
            raise ProviderError("unknown", f"新增页的结果未知：{exc}") from exc
        self._receipt_commit(receipt_id, "")
        return PageAdded(address=str(address), receipt_id=receipt_id)

    async def write_regions(
        self,
        doc_ref: DocRef,
        regions: list[tuple[str, list[list[str]]]],
        *,
        required_revision_id: str = "",
    ) -> EditResult:
        """D15's addressed write, for the formats that are addressed by something other
        than position. A document has no region form and says so."""
        kind = await self._require_kind(doc_ref)
        writer = google_formats.REGION_WRITERS.get(kind)
        if writer is None:
            raise ProviderError(
                "unsupported",
                f"{kind} 没有区域写入形式；请用 clouddoc_batch_edit 做文本替换。",
            )
        return await writer(
            self, doc_ref, regions,
            required_revision_id=required_revision_id,
        )

    async def read_regions(self, doc_ref: DocRef, regions: list[str]) -> list[str]:
        """The read half of the addressed write, for the revert path's anchor check."""
        kind = await self._require_kind(doc_ref)
        reader = google_formats.REGION_READERS.get(kind)
        if reader is None:
            raise ProviderError(
                "unsupported",
                f"{kind} 没有区域读取形式；该文档不应持有区域回执。",
            )
        return await reader(self, doc_ref, regions)

    async def clear_highlight(self, doc_ref: DocRef, texts: list[str]) -> dict:
        """Reset the background color on each text span (D8.3: resolve removes that
        batch's highlights by receipt; a revert clears any inherited yellow).

        Fail-soft by design (the feasibility review's verdict): a span the current
        body no longer contains uniquely is skipped and reported, never guessed --
        style restoration cannot promise the *prior* background, only "none".
        """
        snap = await self.read(doc_ref)
        requests: list[dict] = []
        missed: list[str] = []
        for text in texts:
            if not text:
                continue
            try:
                i0, i1 = self._locate(snap, text, None)
            except ProviderError:
                missed.append(text[:40])
                continue
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": i0, "endIndex": i1},
                    "textStyle": {},
                    "fields": "backgroundColor",
                }
            })
        if requests:
            docs, _ = self._clients()
            await self._call(
                docs.documents().batchUpdate(
                    documentId=doc_ref, body={"requests": requests}
                ).execute
            )
        return {"cleared": len(requests), "missed": missed}

    # -------------------------------------------------------- receipt plumbing

    def _receipt_begin(self, doc_ref, located, *, highlight, regions=None, anchors=None):
        """Mechanical, in the write primitive: the model cannot skip it. A sink
        failure refuses nothing here -- recording is unconditional when wired, and
        the fail-closed duty for the direct-apply path sits with the caller that
        requires a sink (IC-2).

        ``regions`` names the addressed form of each entry, parallel to ``located``.
        A region receipt's old and new are the region's flattened content -- the pair
        the inverse is materialized from -- and the address is what the inverse is
        written back through; without it a revert would have to locate flattened grid
        content in a flattened grid body, which the segment-bounded locate rightly
        refuses."""
        sink = self.receipt_sink
        if sink is None:
            return None
        meta = getattr(self, "receipt_meta", None) or {}
        # The document path locates before it records, so its entries carry the index
        # range too; the formats that address a write by cell or shape have no such
        # range and pass the pair alone. Both are accepted here rather than making the
        # caller pad with a position it does not have -- a fake ``(0, 0)`` would read
        # back as a real location in the audit trail.
        pairs = [(row[-2], row[-1]) for row in located]
        # ``for_comment_ids_by_old`` keys attribution by the old text -- natural for
        # the replacement path, where the model names the old text. A region write's
        # old is computed here, not by the caller, so a comment-commissioned region
        # write attributes every edit instead, via the blanket ``for_comment_ids``.
        blanket = [str(x) for x in (meta.get("for_comment_ids") or [])]
        edits = [
            {"old": old, "new": new,
             "for_comment_ids": list(dict.fromkeys(
                 list((meta.get("for_comment_ids_by_old") or {}).get(old, [])) + blanket
             ))}
            for old, new in pairs
        ]
        # Each region spec is ``(address, old_grid)``. The flat old in the pair is for
        # reading the history; the grid is what a revert actually writes back -- kept
        # verbatim because flattening is lossy the moment a shape's text contains the
        # row separator.
        for i, spec in enumerate(regions or []):
            addr, old_grid = spec
            edits[i]["region"] = addr
            edits[i]["old_grid"] = old_grid
            # The platform URL fragment that lands a browser on this region (the
            # workbench's "locate"): a sheet's gid and range, a deck's slide id. Only
            # the writer knows the ids, so it hands them in; a document has none.
            anchor = (anchors or {}).get(addr)
            if anchor:
                edits[i]["anchor"] = anchor
        try:
            return sink.begin(str(doc_ref), edits, highlight=highlight,
                              source=str(meta.get("source") or ""),
                              executor=str(meta.get("executor") or ""))
        except Exception as exc:  # noqa: BLE001
            # IC-2: a write the ledger cannot record is refused. This runs before the
            # platform call, so nothing has landed; refusing here is the whole point
            # of the write-ahead order. Swallowing it (the old behaviour) let the
            # write go through untracked, and with an unreadable ledger file it also
            # replaced that file with a fresh one holding this receipt alone.
            logger.exception("[clouddoc] receipt begin failed; refusing the write")
            raise ProviderError(
                "ledger_unavailable",
                f"回执账本不可用，写入已拒绝（未触及文档）：{exc}",
            ) from exc

    def _receipt_commit(self, receipt_id, new_rev, *, highlighted: bool | None = None):
        """Close the receipt, correcting the highlight flag with what actually happened.

        ``begin`` recorded the request; only the platform knows the outcome, and the
        panel's unhighlight button reads this field to decide whether there is anything
        to undo.
        """
        if receipt_id is None or self.receipt_sink is None:
            return
        try:
            self.receipt_sink.commit(
                receipt_id, revision_after=new_rev, highlighted=highlighted
            )
        except Exception:  # noqa: BLE001
            logger.exception("[clouddoc] receipt commit failed")

    def _receipt_abort(self, receipt_id, reason):
        if receipt_id is None or self.receipt_sink is None:
            return
        try:
            self.receipt_sink.abort(receipt_id, reason=reason)
        except Exception:  # noqa: BLE001
            logger.exception("[clouddoc] receipt abort failed")

    async def _insert_first(
        self, doc_ref: DocRef, text: str, revision_id: str | None,
        *, highlight: bool = False,
    ) -> EditResult:
        """Write the first content into an empty document.

        Index 1 is where a Google document's body starts; index 0 is the document itself
        and rejects insertion.
        """
        body: dict[str, Any] = {
            "requests": [{"insertText": {"location": {"index": 1}, "text": text}}]
        }
        if highlight and text:
            body["requests"].append({
                "updateTextStyle": {
                    "range": {"startIndex": 1, "endIndex": 1 + _utf16_len(text)},
                    "textStyle": {"backgroundColor": {"color": {"rgbColor": _HIGHLIGHT_RGB}}},
                    "fields": "backgroundColor",
                }
            })
        if revision_id:
            body["writeControl"] = {"requiredRevisionId": revision_id}
        # **A first write is a write.** This path returns before the replacement path's
        # receipt plumbing, so the one edit that fills an empty document -- the very first
        # thing that happens to a fresh document, and the one a demo always performs --
        # left no pending receipt, no applied, no aborted. Measured 2026-08-14: 1,668
        # characters landed in a document and the ledger stayed empty, which makes
        # "recorded at the write primitive so the model can neither skip nor disable it"
        # false for exactly the case nobody would think to check.
        #
        # The pair is ``("", text)``: there was nothing there, and now there is this. That
        # is also what a revert needs -- replacing the new text with nothing empties the
        # document again, which is the state this write found.
        receipt_id = self._receipt_begin(doc_ref, [("", text)], highlight=highlight)
        docs, _ = self._clients()
        try:
            resp = await self._call(
                docs.documents().batchUpdate(documentId=doc_ref, body=body).execute
            )
        except ProviderError as exc:
            if exc.kind == "conflict":
                self._receipt_abort(receipt_id, "conflict")
                return EditResult("conflict", detail=str(exc))
            if exc.kind == "invalid":
                self._receipt_abort(receipt_id, "invalid")
                return EditResult("invalid", detail=str(exc))
            self._receipt_abort(receipt_id, "error")
            raise
        new_rev = ((resp.get("writeControl") or {}).get("requiredRevisionId")) or None
        if new_rev:
            self._revision_cache[doc_ref] = new_rev
        self._receipt_commit(receipt_id, new_rev, highlighted=bool(highlight and text))
        return EditResult("applied", new_revision_id=new_rev, receipt_id=receipt_id)

    # ---------------------------------------------------------------- comments

    # ``assigneeEmailAddress`` appears only on assigned comments and can be named in the
    # mask like any other field. ``replies(...mentionedEmailAddresses)`` is requested
    # because replies do carry server-computed mentions -- measured, contradicting an
    # earlier note in this codebase that said they do not.
    _COMMENT_FIELDS = (
        "comments(id,createdTime,anchor,author(displayName,me),content,"
        "quotedFileContent(value),resolved,mentionedEmailAddresses,"
        "assigneeEmailAddress,"
        "replies(id,createdTime,author(displayName,me),content,"
        "mentionedEmailAddresses)),"
        "nextPageToken"
    )

    # Drive returns a service account's full address as its comment display name
    # (measured). That is the only signal available for "this author is another agent",
    # and it is enough: an account cannot choose its own display name here, so an agent
    # cannot disguise itself as a person. A person who renames themselves to look like a
    # service account only stops themselves from triggering anything -- self-inflicted,
    # not an attack.
    _SERVICE_ACCOUNT_SUFFIX = ".iam.gserviceaccount.com"

    # Class-level default: tests build instances via __new__ (no credentials), and the
    # write primitive must read a sink attribute regardless of construction path.
    receipt_sink = None
    receipt_meta = None

    @classmethod
    def _is_service_account(cls, display_name: str) -> bool:
        return (display_name or "").strip().lower().endswith(cls._SERVICE_ACCOUNT_SUFFIX)

    @staticmethod
    def _anchor_regions(raw: str | None) -> tuple[str, ...]:
        """Decode a comment anchor into region addresses, where it decodes at all.

        Measured on live comments (2026-08-25): a comment left on a deck shape --
        clicked as a whole, no text selected -- carries
        ``{"type":"shape","page":<pageId>,"targets":[<objectId>...]}``, which is
        exactly the ``pageId/objectId`` address the region machinery writes through.
        A spreadsheet's ``workbook-range`` carries an opaque numeric id instead and
        decodes to nothing, so the earlier "anchors are opaque" verdict stands there;
        it was a per-format fact, not a platform one.

        Re-measured 2026-09-10 against a live sheet, because a batch edit across cells
        would need that id to resolve: it does not. The id appears in no public Sheets
        surface -- ``namedRanges``, ``developerMetadata`` and ``protectedRanges`` were
        all empty on the spreadsheet holding the comment. The same run settled what the
        quote pathway offers as a fallback: a comment on a **single cell** carries that
        cell's value in ``quotedFileContent``, while a comment on a **cell range**
        carries none at all. Between the two, a range selection leaves nothing in the
        platform's record that says which cells a person marked, so an unattended edit
        across them has no bound to compute and is refused rather than guessed --
        see ``_no_quote_detail`` for what the refusal tells the reader.

        Anything unreadable is an empty tuple, never an error: the anchor is an extra
        signal on top of the quote pathway, and a malformed one must not take down
        comment listing.
        """
        if not raw:
            return ()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ()
        if not isinstance(data, dict) or data.get("type") != "shape":
            return ()
        page = str(data.get("page") or "")
        targets = data.get("targets") or []
        if not page or not isinstance(targets, list):
            return ()
        return tuple(f"{page}/{t}" for t in targets if t)

    async def list_comments(
        self, doc_ref: DocRef, *, include_resolved: bool = False
    ) -> list[DocComment]:
        _, drive = self._clients()
        raw: list[dict] = []
        token = None
        while True:
            kw: dict[str, Any] = {
                "fileId": doc_ref,
                "fields": self._COMMENT_FIELDS,
                "pageSize": 100,  # the platform's hard cap; anything larger returns 400
            }
            if token:
                kw["pageToken"] = token
            page = await self._call(drive.comments().list(**kw).execute)
            raw.extend(page.get("comments") or [])
            token = page.get("nextPageToken")
            if not token:
                break

        out: list[DocComment] = []
        for c in raw:
            # comments.list has no resolved filter -- the platform offers five parameters
            # and none of them is one -- so it is filtered client-side.
            if not include_resolved and c.get("resolved"):
                continue
            author = c.get("author") or {}
            out.append(
                DocComment(
                    comment_id=c["id"],
                    # author.me is computed server-side and is the only trustworthy
                    # identity signal. A display name can be forged and must never decide
                    # whether the author is us.
                    author_is_self=bool(author.get("me")),
                    author_display_name=author.get("displayName") or "",
                    created_time=c.get("createdTime") or "",
                    content=c.get("content") or "",
                    quoted_text=_unescape_quoted(
                        (c.get("quotedFileContent") or {}).get("value") or ""
                    ),
                    resolved=bool(c.get("resolved")),
                    mentioned_addresses=tuple(c.get("mentionedEmailAddresses") or ()),
                    assignee_address=c.get("assigneeEmailAddress") or None,
                    author_is_service_account=self._is_service_account(
                        author.get("displayName") or ""
                    ),
                    anchor_regions=self._anchor_regions(c.get("anchor")),
                    replies=tuple(
                        DocReply(
                            reply_id=r["id"],
                            author_is_self=bool((r.get("author") or {}).get("me")),
                            author_display_name=(r.get("author") or {}).get("displayName") or "",
                            created_time=r.get("createdTime") or "",
                            content=r.get("content") or "",
                            mentioned_addresses=tuple(r.get("mentionedEmailAddresses") or ()),
                            author_is_service_account=self._is_service_account(
                                (r.get("author") or {}).get("displayName") or ""
                            ),
                        )
                        for r in (c.get("replies") or [])
                    ),
                )
            )
        return out

    async def reply_comment(
        self, doc_ref: DocRef, comment_id: str, content: str
    ) -> ReplyRef:
        _, drive = self._clients()
        r = await self._call(
            drive.replies()
            .create(fileId=doc_ref, commentId=comment_id, body={"content": content}, fields="id")
            .execute
        )
        return r["id"]

    async def update_reply(
        self, doc_ref: DocRef, comment_id: str, reply_id: ReplyRef, content: str
    ) -> None:
        _, drive = self._clients()
        await self._call(
            drive.replies()
            .update(
                fileId=doc_ref,
                commentId=comment_id,
                replyId=reply_id,
                body={"content": content},
                fields="id",
            )
            .execute
        )

    async def delete_reply(
        self, doc_ref: DocRef, comment_id: str, reply_id: ReplyRef
    ) -> None:
        _, drive = self._clients()
        await self._call(
            drive.replies()
            .delete(fileId=doc_ref, commentId=comment_id, replyId=reply_id)
            .execute
        )

    async def resolve_comment(
        self, doc_ref: DocRef, comment_id: str, content: str | None = None
    ) -> None:
        """Not offered, on principle rather than platform limitation.

        Invariant ③: the agent does not close a thread a person opened. Resolving is
        the reader's acknowledgement that the answer was the one they wanted, and an
        agent doing it removes the acknowledgement rather than earning it.

        **This used to resolve.** PR1 removed the tool, which took away the route an
        agent could reach it by, and left the capability sitting in the provider --
        dormant, not refused, and guarded by a contract test that only checked the
        method was a coroutine. The invariant was upheld by nothing except the absence
        of a caller, which is a property of today's code rather than a rule. It is a
        rule now.

        A test that needs a resolved thread should resolve it through the Drive API
        directly, the way the live suite already creates and deletes its fixtures: the
        invariant is about what the agent does, and a fixture is not the agent.
        """
        raise ProviderError("unsupported", "agent 不解决他人开启的评论线程。")
