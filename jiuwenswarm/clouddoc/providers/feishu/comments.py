"""Comment threads on a Feishu document: reading every page of them, and answering.

Builds on ``reading.FeishuReader``. Every card and every reply is read, following the
CLI's pagination, because the CLI does not paginate on its own; authors are told apart
by id (the bot itself, a rostered agent, a person), and only a mention a person typed
becomes a trigger signal. Resolving a thread is refused on principle.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jiuwenswarm.clouddoc.providers.feishu.reading import FeishuReader, _first
from jiuwenswarm.clouddoc.providers.base import (
    DocComment,
    DocRef,
    DocReply,
    ProviderError,
)

logger = logging.getLogger(__name__)

# The largest page the comment and reply listings accept, and how many pages one
# listing may follow before it stops and says so.
_COMMENT_PAGE_SIZE = 100
_MAX_PAGES = 50


def _reply_text(content: Any) -> str:
    """Flatten a reply's rich content into text.

    A reply is not a string: the contract gives ``content.elements``, a list of typed
    runs. Reading it as a string yields nothing, which would make every reply look
    empty -- including the agent's own, which is how a verdict is recognised.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    parts: list[str] = []
    for el in content.get("elements") or []:
        if isinstance(el, str):
            parts.append(el)
            continue
        if not isinstance(el, dict):
            continue
        # TENANT-VERIFY (2026-09-02): each element is {"type": <t>, <t>: {...}}
        # with every other type key present as null. Walking the values
        # indiscriminately appended the type tag itself, so a live comment read
        # back as "person 修改题目text_run".
        etype = el.get("type")
        payload = el.get(etype) if isinstance(etype, str) else None
        if etype == "text_run":
            t = (payload or {}).get("text")
            if isinstance(t, str):
                parts.append(t)
        elif etype == "person":
            uid = str((payload or {}).get("user_id") or "")
            parts.append(f"@{uid}" if uid else "@")
        elif etype == "docs_link":
            u = (payload or {}).get("url")
            if isinstance(u, str):
                parts.append(u)
        elif isinstance(payload, dict):
            t = payload.get("content") or payload.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


def _mentioned_ids(content: Any) -> list[str]:
    """Every person element's user_id in one rich-content payload.

    The mention-hint path compares these against the bot's own id: a person typing @
    in the editor produces a ``person`` element, which is the only mention signal
    this platform's comment payload carries.
    """
    if not isinstance(content, dict):
        return []
    ids: list[str] = []
    for el in content.get("elements") or []:
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        payload = el.get(etype) if isinstance(etype, str) else None
        if etype == "person":
            uid = str((payload or {}).get("user_id") or "").strip()
            if uid:
                ids.append(uid)
    return ids


def _split_thread(embedded: list, rows: list) -> tuple[dict, list]:
    """Split a card's thread into ``(opening message, the replies under it)``.

    Feishu models a comment as a thread whose opening message is reply one, so there
    is no separate body field. The base contract keeps the two apart, as Google does:
    ``DocComment.content`` is the comment and ``DocComment.replies`` is what came
    after it. Left in, the opening message reads as the first reply, so a consumer
    written against that contract counts it, shows it twice, and takes a comment the
    bot opened for a reply it has already posted.

    The opener is the card's first embedded reply, and is dropped from ``rows`` by id,
    so a full re-read of the thread (``+list-replies``) is split the same way whether
    or not it repeats the opener. An empty thread has an empty opener.
    """
    opener = (embedded or rows or [{}])[0]
    if not isinstance(opener, dict):
        opener = {}
    oid = str(_first(opener, "reply_id", default="") or "")
    if oid:
        rest = [r for r in rows if str(_first(r, "reply_id", default="") or "") != oid]
    else:
        rest = [r for r in rows if r is not opener]
    return opener, rest


_CELL_QUOTE = re.compile(r"^([A-Za-z]{1,3}[0-9]{1,7})[ \t]+(.*)$", re.DOTALL)


def _split_cell_quote(quote: str) -> tuple[str, str]:
    """Split a spreadsheet comment's quote into ``(cell address, cell text)``.

    Measured 2026-09-10 on a live sheet: this platform prefixes a cell comment's
    quoted text with the cell's own A1 address -- ``"D2 16oAxy…"``, ``"B2 google"``.
    Google does not, and the prefix went unnoticed until it broke anchoring: the rail
    looked for the whole string in the body, found it nowhere (the body holds the
    value, not the address), and refused an edit to a cell whose value was unique.

    The prefix is not noise to be stripped and forgotten. It is the one thing Google's
    spreadsheet comments do not carry -- **which cell the person marked** -- so it is
    returned as an address and used as the anchor, the same standing a deck comment's
    shape id has. A comment that carries it needs no text search at all, and a repeated
    value stops mattering here.

    Conservative by construction: only an address-shaped head followed by whitespace is
    taken, so a quote that merely starts with something letter-like keeps its full text
    and falls back to the search path.
    """
    m = _CELL_QUOTE.match(quote or "")
    if not m:
        return "", quote or ""
    return m.group(1).upper(), m.group(2)


class FeishuCommenter(FeishuReader):
    """The reader, plus the comment threads on a document and the replies to them."""

    async def list_comments(
        self, doc_ref: DocRef, *, include_resolved: bool = False
    ) -> list[DocComment]:
        # TENANT-VERIFY (2026-09-02, cli 1.0.89): a bare token now requires an
        # explicit --type; the spike-era CLI inferred it. The document's kind is
        # already cached on the read path, and docx is the right default for a
        # document never read (comments are polled after adoption, which reads).
        def make(ftype: str, page_token: str = "") -> list[str]:
            args = ["drive", "+list-comments", "--token", doc_ref, "--type", ftype, "--need-relation"]
            # The CLI defaults to unresolved only, so asking for everything is explicit.
            args += ["--solved-status", "all" if include_resolved else "false"]
            args += ["--page-size", str(_COMMENT_PAGE_SIZE)]
            if page_token:
                args += ["--page-token", page_token]
            return args

        # The format, resolved before the quotes are parsed rather than read out of a
        # cache that this call does not fill. The watcher lists comments before it
        # reads anything, so on its path the cache is empty and a spreadsheet looked
        # like a document -- which left the cell-address prefix in place and put the
        # anchoring bug back exactly where it hurts, on the unattended path. Cached
        # per token, so this costs at most one metadata call per document per process.
        try:
            kind = await self.doc_kind(doc_ref)
        except ProviderError:
            kind = ""
        if str(doc_ref) not in self._kind_cache:
            # The format was not learned. ``doc_kind`` answers "document" when the
            # metadata lookup fails or names nothing (``_resolve_meta`` is quiet by
            # design), so its answer alone cannot tell a docx from a lookup that never
            # happened; the cache can, because only a learned format is stored.
            # Fail-soft: the quote keeps every character and anchors as text, which
            # is degraded but not wrong. Logged as a warning, since the degraded
            # anchor is otherwise invisible; nothing is cached, so the next listing
            # asks again.
            logger.warning(
                "format of %s not known; quotes are read as prose this time", doc_ref
            )
            kind = ""
        # The bot's own id, before any author is judged. Left unknown, every comment
        # the bot wrote reads as a person's, which is the loop the self check exists
        # to prevent; the watcher lists comments before anything else asks.
        if not self._self_open_id:
            try:
                await self.self_identity()
            except ProviderError:
                logger.debug("self identity unavailable for %s; no author reads as self", doc_ref)

        # The CLI does not paginate on its own: one call is one page of cards, and a
        # card carries only the first page of its replies. Both are followed, or a
        # later comment -- or a mention in a long thread -- is never seen at all.
        raw = await self._all_pages(lambda token: self._drive_json(
            doc_ref, lambda ftype: make(ftype, token)
        ))
        out: list[DocComment] = []
        for c in raw:
            cid = str(_first(c, "comment_id", default="") or "")
            if not cid:
                continue
            author = _first(c, "user_id", default="") or ""
            embedded = (_first(c, "reply_list", default={}) or {}).get("replies") or []
            thread = embedded
            if _first(c, "has_more", default=False):
                # All or nothing: a failure here fails the listing. Falling back to
                # the embedded first page could hide the bot's own answer further
                # down, and the watcher would answer the thread a second time;
                # leaving the card out would make its real id look made up to every
                # check that looks a comment id up in this listing. The next poll
                # reads the thread again.
                thread = await self._all_replies(doc_ref, cid)
            opener, reply_rows = _split_thread(embedded, thread)
            # Each message carries the mentions a person typed in it: the comment its
            # own, each reply its own, as the base contract and ``summons`` read them.
            # A mention authored by this agent or another is not counted: where a
            # mention triggers, counting one an agent wrote would let an agent summon
            # an agent, the exact recruitment the loop prohibition forbids. The
            # follow-up gate reads each reply's own set, so a reply built without one
            # could never continue a thread on this platform.
            mentioned = self._person_mentions(opener)
            reply_mentions = [self._person_mentions(r) for r in reply_rows]
            replies = tuple(
                DocReply(
                    reply_id=str(_first(r, "reply_id", default="") or ""),
                    author_is_self=self._is_self(_first(r, "user_id", default="")),
                    author_is_service_account=self._is_other_agent(
                        _first(r, "user_id", default="")
                    ),
                    author_display_name=str(_first(r, "name", "user_name", default="") or ""),
                    created_time=str(_first(r, "create_time", default="") or ""),
                    content=_reply_text(_first(r, "content", default=None)),
                    mentioned_addresses=r_mentions,
                )
                for r, r_mentions in zip(reply_rows, reply_mentions)
            )
            raw_quote = str(_first(c, "quote", default="") or "")
            # Only on a grid: a document's quote is prose, and prose that happens to
            # open with something address-shaped must keep every character of itself.
            cell_addr, cell_text = (
                _split_cell_quote(raw_quote) if kind == "spreadsheet" else ("", raw_quote)
            )
            # Deliberately NOT fed to anchor_regions yet. The address arrives bare
            # ("D2"), while a region write is addressed sheet-qualified ("Sheet1!D2"),
            # and an unqualified entry would make the region branch refuse a correct
            # write. Qualifying it needs the snapshot's segment table, which belongs a
            # layer up; until then the stripped text anchors on its own -- which is
            # what was broken -- and the address is recorded here as the thing that
            # would end Google's ambiguity problem on this platform.
            _cell_addr_for_later = cell_addr
            out.append(
                DocComment(
                    comment_id=cid,
                    # Author identity is a platform fact here, unlike Google, which is
                    # what makes watch_grant.from implementable on this provider.
                    author_is_self=self._is_self(author),
                    author_is_service_account=self._is_other_agent(author),
                    author_display_name=str(_first(c, "name", "user_name", default="") or ""),
                    created_time=str(_first(c, "create_time", default="") or ""),
                    content=_reply_text(_first(opener, "content", default=None)),
                    # `quote` is in the response contract, which is what the rails
                    # anchor on. Without it a comment could not scope an edit.
                    quoted_text=cell_text,
                    resolved=bool(_first(c, "is_solved", default=False)),
                    mentioned_addresses=mentioned,
                    replies=replies,
                )
            )
        return out

    def _person_mentions(self, message: Any) -> tuple[str, ...]:
        """The ids a person mentioned in one message; none when an agent wrote it."""
        author = _first(message or {}, "user_id", default="")
        if self._is_self(author) or self._is_other_agent(author):
            return ()
        return tuple(dict.fromkeys(_mentioned_ids(_first(message or {}, "content", default=None))))

    def _is_self(self, author_id: Any) -> bool:
        """Whether a comment or reply is the bot's own.

        Compared by id, never by display name: a person can set their name to the
        bot's, and the self-filter is what stops the agent answering itself forever.
        An unknown id is not self -- the failure that lets a loop start is calling
        someone else's comment ours, so the doubt resolves the other way.
        """
        me = (self._self_open_id or "").strip()
        other = str(author_id or "").strip()
        return bool(me and other and me == other)

    def _is_other_agent(self, author_id: Any) -> bool:
        """Whether an author is some *other* agent, for the loop prohibition.

        IC-6 forbids deciding this from a display-name suffix: Google's providers end
        in .iam.gserviceaccount.com, a Feishu bot's name does not, and a provider that
        borrowed that test would classify every bot as a person -- invariant ⑤ would
        fail exactly where two deployments share a document.

        The platform fact that would answer it is not in the comment payload: the
        contract carries a bare ``user_id`` and no identity type. Until a tenant shows
        what a bot's authorship looks like (spike 9), this reports False, which is the
        safe direction for the one thing it feeds: an unrecognised author is treated as
        a person, so a comment is answered rather than silently ignored. The cost is
        that agent-to-agent loops across deployments are not yet cut here, which is the
        residue §16.8 already records and the content marker is meant to close.

        The roster closes that residue where it matters most -- mention-triggering,
        where an agent's post could otherwise become a summons. A declared open_id is a
        known other agent; nothing else is, so an unrecognised author is still treated
        as a person (the safe direction for answering). A deployment that shares
        documents with another agent must therefore declare that agent's open_id in
        ``agent_roster``; with no roster, the guard is the content marker alone.
        """
        aid = str(author_id or "").strip()
        return bool(aid) and aid in self._roster

    @staticmethod
    async def _all_pages(fetch) -> list:
        """Every ``items`` row of a paginated listing, following ``page_token``.

        The contract is to page to the end or raise, never to truncate in silence: a
        caller handed part of a listing cannot tell it from all of it. So a listing
        that says there is more but gives no token, repeats a token, or is still going
        after ``_MAX_PAGES`` pages raises instead of returning what it has -- and a
        platform that keeps answering ``has_more`` still cannot hold a poll forever.
        """
        rows: list = []
        token = ""
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            data = await fetch(token)
            rows += list(_first(data or {}, "items", default=[]) or [])
            if not _first(data or {}, "has_more", default=False):
                return rows
            token = str(_first(data or {}, "page_token", default="") or "")
            if not token or token in seen:
                raise ProviderError(
                    "transport", "评论列表分页中断：平台说还有下一页，但没有给出新的页码。"
                )
            seen.add(token)
        raise ProviderError(
            "transport", f"评论列表超过 {_MAX_PAGES} 页仍未结束，未能读完。"
        )

    async def _all_replies(self, doc_ref: DocRef, comment_id: str) -> list:
        """Every reply of one comment, for a card whose embedded list was cut short."""
        return await self._all_pages(lambda token: self._drive_json(doc_ref, lambda ftype: [
            "drive", "+list-replies", "--token", doc_ref, "--type", ftype,
            "--comment-id", comment_id, "--page-size", str(_COMMENT_PAGE_SIZE),
            *(["--page-token", token] if token else []),
        ]))

    @staticmethod
    def _reply_payload(content: str) -> str:
        # TENANT-VERIFY (2026-09-02, cli 1.0.89): --content takes a JSON element
        # array, not plain text -- plain text is rejected as invalid JSON, which
        # would silence every mechanical reply on this platform.
        return json.dumps([{"type": "text", "text": content}], ensure_ascii=False)

    async def reply_comment(self, doc_ref: DocRef, comment_id: str, content: str) -> str:
        data = await self._drive_json(doc_ref, lambda ftype: [
            "drive", "+add-reply", "--token", doc_ref, "--type", ftype,
            "--comment-id", comment_id, "--content", self._reply_payload(content),
        ])
        return str(_first(data, "reply_id", "id", default="") or "")

    async def update_reply(
        self, doc_ref: DocRef, comment_id: str, reply_id: str, content: str
    ) -> None:
        await self._drive_json(doc_ref, lambda ftype: [
            "drive", "+update-reply", "--token", doc_ref, "--type", ftype,
            "--comment-id", comment_id, "--reply-id", reply_id,
            "--content", self._reply_payload(content),
        ])

    async def delete_reply(self, doc_ref: DocRef, comment_id: str, reply_id: str) -> None:
        # Through _drive_json like the other reply verbs, so a wiki-hosted document
        # whose container is not yet known is retried as wiki rather than refused.
        await self._drive_json(doc_ref, lambda ftype: [
            "drive", "+delete-reply", "--token", doc_ref, "--type", ftype,
            "--comment-id", comment_id, "--reply-id", reply_id,
        ])

    async def resolve_comment(
        self, doc_ref: DocRef, comment_id: str, content: str | None = None
    ) -> None:
        """Not offered, on principle rather than platform limitation.

        The platform has the API. Invariant ③ says the agent does not close a thread a
        person opened: resolving is the reader's acknowledgement that the answer was
        the one they wanted, and an agent doing it removes the acknowledgement rather
        than earning it. PR1 removed this for Google and it does not come back here.
        """
        raise ProviderError("unsupported", "agent 不解决他人开启的评论线程。")
