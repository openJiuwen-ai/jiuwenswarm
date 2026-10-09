"""One provider surface over several connections, for the chat path.

A deployment holds one connection per platform account -- two Google service
accounts and a Feishu app, say -- and the watcher already routes each document to the
connection that adopted it. The chat path did not: it built one provider from the
first connection and reached nothing else. Measured 2026-09-03: asked to read a
Feishu document the panel listed as watched, the agent got ``not_found`` from the
Google provider, fetched the link over the web, met a login page, and advised the
person to make the document public.

This object presents the ``DocProvider`` surface and forwards every document-bound
call to the connection that owns the document. Ownership is the adopted-documents
list the panel persists, read live on every call so a document adopted mid-session is
found; a link whose token no connection lists is routed by the platform the link
names, and a bare token nobody lists goes to the first connection, as before.

Account-bound calls (identity, creation) go to the first connection; listings are
the union of all of them. The receipt sink and the per-turn receipt metadata are
propagated to every child, because a write may land on any of them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.factory import (
    credential_address,
)
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    ProviderError,
    list_accessible,
)

logger = logging.getLogger(__name__)


def _canonical(provider: Any, ref: str) -> str | None:
    """``ref`` as the provider names it, or None when it is not a reference it knows."""
    try:
        return str(provider.parse_doc_ref(ref) or "") or None
    except Exception:  # noqa: BLE001 - "not mine" is an answer, whatever the shape
        return None

# Host words that name a platform in a pasted link. A token alone says nothing about
# its platform (Google ids and Feishu tokens are both opaque base64-ish strings), so
# these only break ties for links no connection lists.
_HOST_WORDS: dict[str, tuple[str, ...]] = {
    "feishu": ("feishu.cn", "larksuite.com", "lark.", "/docx/", "/wiki/", "/sheets/", "/slides/"),
    "google": ("docs.google.com", "drive.google.com", "/document/d/", "/spreadsheets/d/",
               "/presentation/d/"),
}


class RoutingProvider:
    """``DocProvider``-shaped facade over ``[(credentials_file, provider), ...]``."""

    def __init__(
        self,
        connections: list[tuple[str, Any]],
        docs_of: Callable[[str], list[str]],
    ) -> None:
        if not connections:
            raise ValueError("RoutingProvider needs at least one connection")
        self._conns = list(connections)
        self._docs_of = docs_of
        self._learned: dict[str, Any] = {}
        self._receipt_meta: Any = None

    # ------------------------------------------------------------ resolution

    @property
    def default(self) -> Any:
        return self._conns[0][1]

    @property
    def providers(self) -> list[Any]:
        return [p for _, p in self._conns]

    def _by_docs(self, ref: str) -> Any | None:
        """The connection whose adopted list names ``ref`` -- by canonical id, exactly.

        Each candidate connection parses both sides with its own ``parse_doc_ref``,
        so a pasted link and a bare token compare as one document, while a token that
        merely contains -- or is contained in -- another does not: an id is opaque,
        and a prefix two ids share says nothing about who adopted which. A reference
        the platform cannot parse at all is simply not that connection's.
        """
        s = (ref or "").strip()
        if not s:
            return None
        for cf, p in self._conns:
            canon = _canonical(p, s)
            if canon is None:
                continue
            for d in self._docs_of(cf) or []:
                d = str(d or "").strip()
                if d and (d == s or _canonical(p, d) == canon):
                    return p
        return None

    def address_for(self, doc_ref: str) -> str:
        """The identity the connection owning ``doc_ref`` acts under, or empty.

        A mention names one account's address, and with several connections that
        address differs per document -- an email on one, a bot's open id on another.
        Read from the owning connection's credentials, the same source the host reads
        for a single connection, so both paths recognise the same mentions.
        """
        s = str(doc_ref or "").strip()
        for cf, p in self._conns:
            if p is self._by_docs(s):
                return credential_address(str(cf))
        return ""

    def _by_host(self, ref: str) -> Any | None:
        low = (ref or "").lower()
        for vendor, words in _HOST_WORDS.items():
            if any(w in low for w in words):
                for _, p in self._conns:
                    if getattr(p, "kind", "") == vendor:
                        return p
        return None

    def owner(self, doc_ref: str) -> Any:
        """The provider that owns ``doc_ref`` -- adopted list first, then what an
        earlier ``parse_doc_ref`` learned, then the first connection."""
        return self._by_docs(str(doc_ref)) or self._learned.get(str(doc_ref)) or self.default

    def for_platform(self, kind: str) -> Any | None:
        """The first connection on ``kind`` ("google", "feishu"), or None."""
        want = (kind or "").strip().lower()
        for _, p in self._conns:
            if str(getattr(p, "kind", "")).lower() == want:
                return p
        return None

    def learn(self, doc_ref: str, provider: Any) -> None:
        """Remember which connection a document belongs to -- for one just created,
        which no adopted list names yet."""
        self._learned[str(doc_ref)] = provider

    # ------------------------------------------------------------ DocProvider surface

    @property
    def kind(self) -> str:
        return str(getattr(self.default, "kind", ""))

    @property
    def text_domain(self):
        return self.default.text_domain

    async def text_domain_for(self, doc_ref):
        return await self.owner(doc_ref).text_domain_for(doc_ref)

    def doc_url(self, doc_ref, kind: str = "") -> str:
        return self.owner(doc_ref).doc_url(doc_ref, kind)

    def parse_doc_ref(self, url_or_id: str):
        s = (url_or_id or "").strip()
        p = self._by_docs(s) or self._by_host(s)
        if p is not None:
            ref = p.parse_doc_ref(s)
            self._learned[str(ref)] = p
            return ref
        last: ProviderError | None = None
        for _, q in self._conns:
            try:
                ref = q.parse_doc_ref(s)
            except ProviderError as exc:
                last = exc
                continue
            self._learned[str(ref)] = q
            return ref
        raise last or ProviderError("invalid", f"无法从 {url_or_id!r} 解析出文档 id")

    async def self_identity(self):
        return await self.default.self_identity()

    async def capabilities(self, doc_ref):
        return await self.owner(doc_ref).capabilities(doc_ref)

    async def title(self, doc_ref) -> str:
        return await self.owner(doc_ref).title(doc_ref)

    async def doc_kind(self, doc_ref) -> str:
        return await self.owner(doc_ref).doc_kind(doc_ref)

    async def read(self, doc_ref):
        return await self.owner(doc_ref).read(doc_ref)

    async def edit(self, doc_ref, old_string, new_string, *, revision_id=None):
        return await self.owner(doc_ref).edit(
            doc_ref, old_string, new_string, revision_id=revision_id
        )

    async def edit_batch(self, doc_ref, edits, *, required_revision_id, window=None,
                         highlight=False):
        return await self.owner(doc_ref).edit_batch(
            doc_ref, edits, required_revision_id=required_revision_id,
            window=window, highlight=highlight,
        )

    async def write_regions(self, doc_ref, regions, *, required_revision_id=""):
        return await self.owner(doc_ref).write_regions(
            doc_ref, regions, required_revision_id=required_revision_id
        )

    async def read_regions(self, doc_ref, regions):
        return await self.owner(doc_ref).read_regions(doc_ref, regions)

    # **Every per-document method is routed here by name.** The fallback below hands
    # anything unlisted to the first connection, which is right for a cache or a flag
    # and wrong for a document: ``add_page`` was missing from this list, so a request
    # for a Feishu deck went to the Google provider, which looked the token up on
    # Drive and answered not_found -- while ``read`` on the same deck, being listed,
    # worked. The model then reported "a platform-side limit". The audit in the test
    # suite compares this class against the provider contract so the next
    # per-document method cannot slip through to the fallback.
    async def add_page(self, doc_ref, title: str = ""):
        return await self.owner(doc_ref).add_page(doc_ref, title)

    async def trash_document(self, doc_ref) -> None:
        return await self.owner(doc_ref).trash_document(doc_ref)

    async def confirm_absent(self, doc_ref) -> bool:
        fn = getattr(self.owner(doc_ref), "confirm_absent", None)
        return bool(await fn(doc_ref)) if fn is not None else False

    def forget_kind(self, doc_ref) -> None:
        fn = getattr(self.owner(doc_ref), "forget_kind", None)
        if fn is not None:
            fn(doc_ref)

    async def list_comments(self, doc_ref, *, include_resolved: bool = False):
        return await self.owner(doc_ref).list_comments(
            doc_ref, include_resolved=include_resolved
        )

    async def reply_comment(self, doc_ref, comment_id, content):
        return await self.owner(doc_ref).reply_comment(doc_ref, comment_id, content)

    async def update_reply(self, doc_ref, comment_id, reply_id, content):
        return await self.owner(doc_ref).update_reply(doc_ref, comment_id, reply_id, content)

    async def delete_reply(self, doc_ref, comment_id, reply_id):
        return await self.owner(doc_ref).delete_reply(doc_ref, comment_id, reply_id)

    async def resolve_comment(self, doc_ref, comment_id, content=None):
        return await self.owner(doc_ref).resolve_comment(doc_ref, comment_id, content)

    async def create_document(self, title: str):
        ref = await self.default.create_document(title)
        self._learned[str(ref)] = self.default
        return ref

    async def share_document(self, doc_ref, email, *, role: str = "writer"):
        return await self.owner(doc_ref).share_document(doc_ref, email, role=role)

    async def list_shared_unsupported(self) -> list[dict]:
        out: list[dict] = []
        for _, p in self._conns:
            try:
                out.extend(await p.list_shared_unsupported())
            except ProviderError as exc:
                logger.warning("[clouddoc] %s 列不支持文件失败：%s", p.kind, exc)
        return out

    async def list_accessible_documents(self, known=()):
        """The union: a failure on one connection hides only that connection, and the
        error is logged rather than swallowed so the gap is visible.

        ``known`` is forwarded per child, defaulting to that connection's own adopted
        list: Feishu finds the wiki space to walk from the nodes it already manages,
        and a call that dropped the argument left the chat listing dependent on a
        warm process cache -- the cold-path hole the panel had already closed.
        """
        out = []
        for cf, p in self._conns:
            try:
                seed = list(known) or [str(d) for d in (self._docs_of(cf) or [])]
                out.extend(await list_accessible(p, seed))
            except ProviderError as exc:
                logger.warning("[clouddoc] %s 列可访问文档失败：%s", p.kind, exc)
        return out

    async def sharing_posture(self, doc_ref):
        return await self.owner(doc_ref).sharing_posture(doc_ref)

    # ------------------------------------------------------------ shared plumbing

    @property
    def receipt_sink(self):
        return getattr(self.default, "receipt_sink", None)

    @receipt_sink.setter
    def receipt_sink(self, sink) -> None:
        for _, p in self._conns:
            p.receipt_sink = sink

    @property
    def receipt_meta(self):
        return self._receipt_meta

    @receipt_meta.setter
    def receipt_meta(self, meta) -> None:
        self._receipt_meta = meta
        for _, p in self._conns:
            p.receipt_meta = meta

    def note_kind(self, doc_ref: str, kind: str) -> None:
        """Format priming, forwarded to the owner (and to any child that lacks a
        cache, harmlessly ignored)."""
        note = getattr(self.owner(doc_ref), "note_kind", None)
        if note is not None:
            note(doc_ref, kind)

    def note_url(self, doc_ref: str, url: str) -> None:
        """Link priming, forwarded to the owner (a child without a URL cache ignores
        it harmlessly)."""
        note = getattr(self.owner(doc_ref), "note_url", None)
        if note is not None:
            note(doc_ref, url)

    def __getattr__(self, name: str) -> Any:
        # Anything not routed above is a per-provider detail (a cache, a flag) read
        # by code that only ever had one provider; answer from the first connection.
        return getattr(self.default, name)


def build_routed_provider(
    specs: list[dict],
    *,
    build: Callable[..., Any],
    live_specs: Callable[[], list[dict]],
    agent_roster: tuple[str, ...] = (),
    log: Any = None,
) -> tuple[Any, str]:
    """The attended-path provider over every connection, built in one place.

    Every host that serves a person talking -- the chat turn, a team member -- reaches
    all connections' documents, routed by adoption. The chat path grew this first and
    the team path was left on the first connection alone: the same defect fixed on one
    host and not the next, which is why construction now lives here and hosts only
    call it. The unattended path does not use this: its provider is the one owning
    connection, and its confinement to one document and one account is the point.

    Returns ``(provider, first_credentials_file)``. The first connection must build --
    its failure is the caller's to handle -- because it is also the identity the toolkit
    reports for the turn. A later connection whose key cannot be read is skipped with
    a log line rather than taking the others down. With a single connection the plain
    provider is returned, so nothing changes for a one-account deployment.

    ``live_specs`` is read on every ownership question, so a document the panel adopts
    mid-session is routed correctly without rebuilding anything.
    """
    if not specs:
        raise ValueError("no clouddoc connections configured")
    first_cf = str(specs[0]["credentials_file"])
    first = build(first_cf, agent_roster=agent_roster)
    if len(specs) == 1:
        return first, first_cf
    children: list[tuple[str, Any]] = [(first_cf, first)]
    for spec in specs[1:]:
        cf = str(spec["credentials_file"])
        try:
            children.append((cf, build(cf, agent_roster=agent_roster)))
        except Exception:  # noqa: BLE001 - one bad key must not hide the other connections
            if log is not None:
                log.exception("[clouddoc] connection %s could not be built; routing skips it", cf)

    def docs_of(cf: str) -> list:
        return next(
            (sp["documents"] for sp in live_specs() if sp["credentials_file"] == cf), []
        )

    return RoutingProvider(children, docs_of), first_cf


def all_adopted_documents(live_specs: Callable[[], list[dict]]) -> list:
    """Every connection's adopted documents, in connection order -- what a routed
    toolkit may reach, for the ``watched_docs`` seam."""
    return [d for sp in live_specs() for d in sp["documents"]]
