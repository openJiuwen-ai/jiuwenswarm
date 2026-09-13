"""The Feishu/Lark provider, driven through the official CLI.

Read the seam first: ``lark_cli.LarkCli`` builds, runs and classifies every command,
and this file is what interprets the results as documents, comments and edits.

**Commands and response shapes come from the CLI itself.** Every command name and flag
was read from ``lark-cli 1.0.89``'s help, every field name from its ``schema``
outputSchema, and the whole set is checked against the installed binary in the tests.

What no local source can answer is what a *tenant* permits -- whether an app may
enumerate what was shared with it, own a document, or read a collaborator list. None
of those is left as an assumption to be checked later. Each is **asked at runtime and
remembered**, and a refusal degrades rather than fails:

* Enumeration refused → discovery returns nothing and adoption happens the way it
  already works, from a link pasted into the panel. Raising would take the panel down
  over a feature it does not need.
* Permission query refused → capabilities are settled by attempting the read the
  feature actually depends on, rather than reporting no access, which would put a
  workable document into the comment-only bucket and have admission refuse it.
* Ownership refused → the failure is translated into the sentence that helps ("share
  a document with this app instead") rather than relayed, the way Google's "storage
  quota exceeded" sends people to empty a trash folder that was never full.

The provider is therefore correct whichever way a tenant answers, rather than correct
only if a guess was right. What is still marked ``ASSUMPTION`` is narrow: field
spellings the schema did not cover, and limits such as where quoted text truncates --
each noted with the spike (§17.2) that measures it.

Reading the CLI corrected four guesses that would each have failed at runtime:
``--scope`` takes ``full`` and not ``all``; resolved comments are selected by
``--solved-status`` rather than an ``--include-resolved`` switch, and its default is
``false``, so the unresolved ones are what a caller gets unless it says otherwise;
collaborators are managed by ``+member-add`` / ``+member-list``, not
``+add-permission``; and identity comes from a top-level ``whoami``.

The largest correction is a capability. ``docs +update`` takes ``--revision-id`` as a
base revision, which is Feishu's answer to Google's WriteControl, so this provider
does have optimistic locking and says so. It also accepts comma-separated block ids
for a single ``block_replace``, which is worth knowing but does not change the atomic
batch answer: one command is one write, and several edits still mean several commands.

Two findings from the CLI change what was expected of this platform:

* **Document comments have no event.** ``event list`` offers eight domains --
  application, approval, board, card, im, minutes, task, vc -- and none carries a
  drive or comment event. The capability survey expected a push channel to replace
  polling here; there is none, so the watcher polls on Feishu exactly as it does on
  Google, and the "idle polling quota disappears structurally" note in §17.1 does not
  hold. What does hold is that event payloads carry ``sender_id`` as an ``ou_``-
  prefixed open_id, so were an event to exist, recognising the agent's own would be a
  platform fact rather than a guess.
* **The write channel has no highlight.** ``docs +update`` exposes no background
  colour, so the visible half of acceptance is unavailable and a caller asking for it
  is refused rather than quietly served a plain edit.

Three things are decided rather than observed:

* **``--as bot``.** Enforced in the CLI wrapper. Acting as a user misattributes the
  write and, worse, breaks the loop prohibition: the agent recognises its own events
  by author, and an event it produced under a person's identity is indistinguishable
  from one that person produced.
* **No atomic multi-edit writes.** The CLI applies edits one at a time and promises
  nothing about the batch, so ``capabilities`` reports ``atomic_batch=False`` and a
  multi-edit batch is refused upstream (C9). It is not simulated by editing and
  undoing: the document is shared, a half-written state is what readers see, and the
  undo would race whoever else is typing.
* **``+history-revert`` is not part of the edit path.** It restores a whole document
  to a point in time, taking other people's concurrent edits with it, which is the
  path D8-5 rejected. It stays available only as manual disaster recovery.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.lark_cli import LarkCli, _classify
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers import feishu_formats
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    is_trusted_doc_url,
    PRE_MUTATION_KINDS,
    PageAdded,
    AgentIdentity,
    DocCapabilities,
    DocComment,
    DocProvider,
    DocRef,
    DocReply,
    DocSnapshot,
    DocSummary,
    EditResult,
    ProviderError,
    Segment,
    TextDomain,
    UNSUPPORTED_KIND_DETAIL,
    is_supported_kind,
)

logger = logging.getLogger(__name__)


def _first(d: Any, *names: str, default: Any = None) -> Any:
    """Read the first key that is present.

    The CLI's field names are among the things no spike has confirmed, so each reader
    lists the plausible spellings rather than betting on one. This is scaffolding for
    an unverified integration, not a pattern to carry into settled code.
    """
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


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


def _comment_text(comment: Any) -> str:
    """A comment's own text, which the contract carries in its first reply.

    Feishu models a comment as a thread whose opening message is reply one, so there
    is no separate body field. Taking the first reply is what the platform means by
    "the comment", and an empty thread reads as empty rather than raising.
    """
    if not isinstance(comment, dict):
        return ""
    replies = (comment.get("reply_list") or {}).get("replies") or []
    if not replies:
        return ""
    return _reply_text((replies[0] or {}).get("content"))


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


class FeishuDocsProvider(DocProvider):
    def __init__(
        self,
        *,
        profile: str = "",
        binary: str = "lark-cli",
        self_open_id: str = "",
        agent_roster: tuple[str, ...] = (),
    ) -> None:
        # The app's secret is registered with the CLI once (``config init``) and lives
        # in its store; a connection names a profile, never a secret.
        self._cli = LarkCli(binary=binary, profile=profile)
        # The bot's own open_id, which is how its own comments are recognised. Left
        # empty it is fetched once on first use; see self_identity.
        self._self_open_id = self_open_id
        # Open_ids the deployer declares to be other agents. This platform's comment
        # payload carries a bare user_id with no identity type, so a bot cannot be told
        # from a person by inspection (see _is_other_agent); the roster supplies the
        # fact the payload withholds, which is what lets the loop-prohibition rail work
        # on Feishu once mentions -- rather than assignments -- trigger.
        self._roster: set[str] = {str(a).strip() for a in agent_roster if str(a).strip()}
        self._identity: AgentIdentity | None = None
        # Capabilities the tenant decides rather than the code. None means "not asked
        # yet"; the answer is remembered so a degraded deployment is told once instead
        # of on every poll.
        self._discovery_available: bool | None = None
        self._can_own_documents: bool | None = None
        # A connection reaches docx, spreadsheets and decks alike, so the
        # format belongs to the document. Cached because a document does not change
        # type and this sits on the read path.
        self._kind_cache: dict[str, str] = {}
        self._object_tokens: dict[str, str] = {}
        self._discovery_reason: str = ""
        # Tokens that came from /wiki/ links, or answered "not exist" as docx
        # and resolved as wiki. Drive verbs need --type wiki for these; docs
        # +fetch unwraps on its own, which is exactly how the gap hid: reading
        # worked while every comment and member verb reported "not exist"
        # (TENANT-VERIFY 2026-09-02, live).
        self._wiki_tokens: set[str] = set()
        # Shared files this provider cannot work on, learned during discovery and
        # reported to the person rather than dropped.
        self._unsupported: dict[str, tuple[str, str]] = {}
        # Links as the platform reported them. A Feishu URL carries the tenant's own
        # domain, which nothing here can know, so a URL is remembered when one is seen
        # and never constructed.
        self._url_cache: dict[str, str] = {}

    # The receipt plumbing is set on the instance by the connection registry, exactly
    # as it is for Google. Declared here as class defaults so the write primitive can
    # read them whatever path built the provider -- a test constructing one directly
    # must not have to know about them.
    receipt_sink = None
    receipt_meta = None

    @property
    def kind(self) -> str:
        return "feishu"

    @property
    def text_domain(self) -> TextDomain:
        # Not an assumption: this provider reads and writes with --doc-format markdown,
        # so markers in the body are content the platform round-trips rather than
        # literal characters that would land in someone's document. The rail may
        # therefore allow them, which is the correct behaviour for this transport.
        return "markdown"

    # ---------------------------------------------------------------- basics

    def parse_doc_ref(self, url_or_id: str) -> DocRef:
        """Accept a docx link, a wiki link, or a bare token.

        Wiki links address a node rather than the document, and the CLI resolves one
        to the other. That resolution needs a call, which a parser cannot make, so a
        wiki token is returned as-is and resolved at first use.
        """
        s = (url_or_id or "").strip()
        if not s:
            raise ProviderError("invalid", "空的文档引用。")
        for marker in ("/docx/", "/docs/", "/wiki/", "/sheets/", "/slides/", "/file/"):
            if marker in s:
                tail = s.split(marker, 1)[1]
                token = tail.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
                if token:
                    if marker == "/wiki/":
                        self._wiki_tokens.add(token)
                    if marker == "/sheets/":
                        # A pasted link is the only place a spreadsheet's format is
                        # ever visible up front: there is no listing to teach it, and
                        # without this the token would read as a docx and fail on the
                        # fetch with the platform's message instead of being served.
                        self._kind_cache[token] = "spreadsheet"
                    if marker == "/slides/":
                        self._kind_cache[token] = "presentation"
                    if marker == "/file/":
                        # A Drive file, and co-scribe serves none of them: markdown was
                        # the last one and has been withdrawn. Refused here, where the
                        # person is looking at the link they pasted, rather than
                        # adopted and failed on the first read.
                        raise ProviderError("invalid", UNSUPPORTED_KIND_DETAIL)
                    if is_trusted_doc_url(s, "feishu"):
                        # The pasted link is the only place the tenant's own
                        # domain ever appears; without keeping it, the panel
                        # could only ever show the bare token back. Kept only
                        # from the platform's own hosts: this cache feeds the
                        # links the panel shows and the origin later creates
                        # reuse, and a foreign origin must reach neither.
                        self._url_cache[token] = s.split("?", 1)[0].split("#", 1)[0]
                    return token
                break
        if "/" in s or " " in s:
            raise ProviderError("invalid", f"无法从 {url_or_id!r} 解析出文档 token。")
        return s

    async def self_identity(self) -> AgentIdentity:
        if self._identity is not None:
            return self._identity
        # `whoami` is a top-level command and reports the effective identity, which
        # under --as bot is the app. ASSUMPTION (spike 9): the open_id it returns is
        # the one that appears as the author of the bot's own comments. Falsified if
        # the two differ -- self-comment filtering would then have no basis and the
        # loop prohibition would need the content marker of §16.8 instead.
        data = await self._cli.json(["whoami"])
        open_id = str(_first(data, "open_id", "openId", "bot_open_id", default="") or "")
        name = str(_first(data, "name", "app_name", "display_name", default="") or "")
        self._self_open_id = self._self_open_id or open_id
        # The preset id wins: on this tenant whoami answers without an open_id, and
        # the credentials file's bot_open_id is then the only identity the trigger
        # config can anchor on. Ignoring it here made sa_address degrade to the
        # display name, which no mention ever matches.
        self._identity = AgentIdentity(
            display_name=name or "bot", address=self._self_open_id or None
        )
        return self._identity

    @property
    def discovery_available(self) -> bool:
        """False once the tenant has refused to enumerate; True before it has been asked.

        ``None`` means the question has not come up yet, and the panel should not put a
        limitation on screen that may not apply -- so it reads as available until the
        first listing settles it.
        """
        return self._discovery_available is not False

    def forget_kind(self, doc_ref: DocRef) -> None:
        """Forget the format and the container, so the next call re-asks the platform.

        Both are learned rather than given, and both were wrong for every document this
        platform ever adopted: discovery cannot enumerate here, so a pasted link arrived
        with no format and the old code assumed docx, which the panel then persisted and
        primed back on the next start -- a wrong answer that repaired itself into
        permanence.
        """
        self._kind_cache.pop(str(doc_ref), None)
        self._wiki_tokens.discard(str(doc_ref))

    async def _resolve_meta(
        self, doc_ref: DocRef, *, raise_on_error: bool = False
    ) -> dict:
        """Ask the platform what this token is, in one call.

        ``raise_on_error`` hands a failed call back to the caller instead of
        answering ``{}``. The format lookups want the quiet form -- an unanswered
        question falls through to "document". ``confirm_absent`` must not: for it
        "could not ask" and "asked, and the token names nothing" are opposite
        verdicts, and folding them into one empty dict retired a live document on
        a transport hiccup.

        ``metas batch_query`` takes a list of ``(token, doc_type)`` pairs, and a token
        offered under several types answers under exactly the one it really is: the
        others come back in neither ``metas`` nor ``failed_list`` (measured on a live
        tenant, 2026-09-10 -- one meta returned, ``failed_list`` empty). So the format is
        decided by the platform rather than assumed, and the title and canonical URL
        arrive with it.

        This exists because the old assumption stopped being safe. ``doc_kind`` guessed
        docx for anything discovery had not seen, which was correct while discovery was
        expected to see everything; on this platform discovery cannot enumerate at all,
        so every Feishu document arrives as a pasted link with its format unknown, and a
        spreadsheet or a deck was read as a docx and failed -- the panel showed a row
        with no name.
        """
        types = [feishu_formats.TYPE_DOCX, feishu_formats.TYPE_SHEET,
                 feishu_formats.TYPE_SLIDES, "wiki"]
        try:
            data = await self._cli.json([
                "drive", "metas", "batch_query", "--data",
                json.dumps({"request_docs": [{"doc_token": str(doc_ref), "doc_type": t}
                                             for t in types],
                            "with_url": True}, ensure_ascii=False),
            ])
        except ProviderError:
            if raise_on_error:
                raise
            return {}
        metas = _first(data or {}, "metas", default=[]) or []
        meta = metas[0] if isinstance(metas, list) and metas else {}
        if not meta:
            return {}
        dt = str(meta.get("doc_type") or "").lower()
        if dt == "wiki":
            self._wiki_tokens.add(str(doc_ref))
        kind = feishu_formats.KIND_BY_TYPE.get(dt, "")
        if kind:
            self._kind_cache[str(doc_ref)] = kind
        url = str(_first(meta, "url", default="") or "")
        # The platform's own answer, trusted by source rather than by host list: a
        # private deployment lives on its own domain, and this is the one link such a
        # tenant can ever show. Anything that is not https is not a link.
        if url.lower().startswith("https://"):
            self._url_cache[str(doc_ref)] = url
        # **The object's own token, which is not always the token we were given.** A
        # wiki node hosts a file, and the node's token addresses the node; the drive
        # verbs accept either, which is why this went unnoticed, but the slides API
        # accepts only the object -- a deck adopted by its /wiki/ link answered
        # ``3350002 not found`` to every read, and the panel showed a document that
        # could not be opened (measured 2026-09-10: the node token failed, the object
        # token returned 875 bytes of XML). The resolution was already in hand here
        # and simply thrown away.
        obj = str(meta.get("doc_token") or "")
        if obj and obj != str(doc_ref):
            self._object_tokens[str(doc_ref)] = obj
        return meta

    def _object_token(self, doc_ref: DocRef) -> str:
        """The platform object's token for a reference that may name a wiki node.

        Returns the reference itself when it already is the object, so callers can use
        it unconditionally. Only the APIs that refuse a node token need it.
        """
        return self._object_tokens.get(str(doc_ref), str(doc_ref))

    async def _resolved_object_token(self, doc_ref: DocRef) -> str:
        """``_object_token``, resolving first when this token has not been looked up.

        The sync form answers from the cache and silently hands back the reference when
        the cache is cold -- which is the wiki node again, and the slides API refuses
        it. That is not hypothetical: the kind can arrive from the panel's stored
        metadata without any metadata call ever running in this process, so the deck
        read as a deck and then failed to open. Callers on the slides path must use
        this one; the cache still means at most one metadata call per token.
        """
        key = str(doc_ref)
        if key not in self._object_tokens:
            try:
                await self._resolve_meta(doc_ref)
            except Exception:  # noqa: BLE001 - fall back to the reference itself
                pass
        return self._object_tokens.get(key, key)

    async def _title_from_meta(self, doc_ref: DocRef) -> str:
        """Title from drive metadata, for the formats ``docs +fetch`` cannot serve.

        TENANT-VERIFY (2026-09-02): a spreadsheet has no fetch of its own, and the
        metas query answers any type. It also carries the canonical URL, learned here
        so a token adopted bare still links to the real document.
        """
        async def ask(doc_type: str) -> dict:
            data = await self._cli.json([
                "drive", "metas", "batch_query", "--data",
                json.dumps({"request_docs": [{"doc_token": str(doc_ref),
                                              "doc_type": doc_type}],
                            "with_url": True}, ensure_ascii=False),
            ])
            metas = _first(data or {}, "metas", default=[]) or []
            return metas[0] if isinstance(metas, list) and metas else {}

        meta = await ask(self._drive_type(doc_ref))
        if not meta and str(doc_ref) not in self._wiki_tokens:
            # The wiki flavor is process memory too: a wiki-hosted token after a
            # restart reads as a docx here, and the platform answers the mismatch
            # with a failed_list rather than an error. Same posture as _drive_json:
            # retried once as wiki, and success teaches the flavor.
            meta = await ask("wiki")
            if meta:
                self._wiki_tokens.add(str(doc_ref))
        url = str(_first(meta, "url", default="") or "")
        if url.startswith("http"):
            self._url_cache.setdefault(str(doc_ref), url)
        return str(_first(meta, "title", default="") or "")

    async def canonical_url(self, doc_ref: DocRef) -> str:
        """The tenant-domain URL for a token nobody ever pasted.

        A bot-created document has no pasted link to remember, and the bare token
        renders as dead text in the panel. The metas query is the platform's own
        answer, cached the same way a paste would have been.
        """
        cached = self._url_cache.get(str(doc_ref))
        if cached:
            return cached
        await self._title_from_meta(doc_ref)
        return self._url_cache.get(str(doc_ref)) or str(doc_ref)

    async def title(self, doc_ref: DocRef) -> str:
        kind = await self.doc_kind(doc_ref)
        # The format decides, not the container. The wiki clause that used to sit here
        # was meant for a wiki-hosted docx, which ``docs +fetch`` unwraps -- but it also
        # caught a wiki-hosted spreadsheet or deck, and those have no fetch of their own,
        # so the panel showed them with no name at all. A knowledge base holds documents
        # of every format; which one it is is what picks the reader.
        if kind and kind != "document":
            return await self._title_from_meta(doc_ref)
        # TENANT-VERIFY (2026-09-02, cli 1.0.89): --detail meta does not exist
        # (allowed: simple/with-ids/full); simple carries the title as a tag in
        # the rendered content.
        data = await self._cli.json(["docs", "+fetch", "--doc", doc_ref, "--detail", "simple"])
        doc = _first(data, "document", default=data) or {}
        direct = str(_first(doc, "title", "name", default="") or "")
        if direct:
            return direct
        content = str(_first(doc, "content", default="") or "")
        m = re.search(r"<title>(.*?)</title>", content, re.S)
        return m.group(1).strip() if m else ""

    async def capabilities(self, doc_ref: DocRef) -> DocCapabilities:
        """What the platform lets the bot do with one document.

        ``atomic_batch`` is False and stays False: it is a property of the CLI's edit
        path, not of one document, and no spike can turn it on.
        """
        # The format first, because every drive verb below needs ``--type`` and the
        # helper that supplies it reads the kind cache. On a freshly pasted link that
        # cache is empty, so a spreadsheet was asked about as a docx, the platform
        # answered "not found", and the panel reported a document that is plainly there
        # as not shared. Resolving costs one metadata call, once per token.
        await self.doc_kind(doc_ref)
        # Reading the collaborator list may itself be a user-only operation on some
        # tenants. When it is, the honest answer is not "no access" -- that would put
        # a perfectly workable document into the comment-only bucket and admission
        # would refuse it. The document is read instead, which is the access the
        # feature actually needs, and the flags follow from whether that worked.
        try:
            data = await self._drive_json(doc_ref, lambda ftype: [
                "drive", "+member-list", "--token", doc_ref, "--type", ftype,
            ])
        except ProviderError as exc:
            if exc.kind == "not_found":
                # Gone is not the same as ungranted, and answering it with an all-False
                # capability set made them the same: the caller saw ``can_edit=False``
                # and diagnosed comment-only, so a deleted document sat in the panel
                # being told to change its permission to Editor, and never retired
                # because nothing ever said it was not there. Raising is what the
                # callers already expect -- the panel's probe maps ``not_found`` to
                # "not shared", which is the verdict retirement acts on.
                raise
            if exc.kind in ("forbidden", "unsupported", "invalid"):
                return await self._capabilities_by_probe(doc_ref)
            raise
        # member-list answers with an ``items`` array, one row per collaborator;
        # the app's own permission is the row whose member is this app id. Reading
        # ``perm`` off the top level (as this once did) always found nothing, so
        # every document -- including ones the bot fully owns -- read as
        # non-editable and fell to comment-only or backoff. (TENANT-VERIFY
        # 2026-09-02, live: the bot's own P1 doc showed full_access in the row.)
        members = _first(data, "items", "members", default=[]) or []
        app_id = str(_first(data, "app_id", default="") or "") or self._app_id_hint()
        perm = ""
        for m in members:
            mid = str(_first(m, "member_id", "open_id", "id", default="") or "")
            mtype = str(_first(m, "member_type", "type", default="") or "").lower()
            if mtype in ("appid", "app") or mid.startswith("cli_"):
                perm = str(_first(m, "perm", "permission", "role", default="") or "").lower()
                if not app_id or mid == app_id:
                    break
        if not perm and members:
            # A single-member list is the bot's own view of its own access.
            perm = str(_first(members[0], "perm", "permission", "role", default="") or "").lower()
        can_edit = perm in ("edit", "full_access", "manage_collaborator", "owner")
        return DocCapabilities(
            can_read=True,
            can_edit=can_edit,
            can_comment=can_edit or perm in ("comment", "view_and_comment"),
            can_resolve=can_edit,
            # **False, and the earlier True was the more dangerous error.** The
            # reading that `--revision-id` is Feishu's WriteControl does not survive
            # the sibling command's own help text: pinning an older revision "rebuilds
            # the page from that snapshot and discards newer edits to it". A flag that
            # destroys a concurrent edit is not a lock, and declaring one here would
            # let admission rely on protection that does not exist.
            #
            # Declared absent rather than simulated (C9). See the write path for the
            # tenant check that would settle it.
            has_revision_control=False,
            # Spike 7 closed (TENANT-VERIFY 2026-09-02): quotes truncate at exactly
            # 128 code points, hard, with no mark -- measured identically for an API
            # block anchor and for a live user selection (a 538-char paragraph came
            # back as precisely 128). Declared unmarked so the rail routes a
            # limit-length quote to QUOTE_TRUNCATED instead of anchoring the fragment.
            max_quote_chars=128,
            quote_truncates_unmarked=True,
            atomic_batch=False,
        )

    async def _capabilities_by_probe(self, doc_ref: DocRef) -> DocCapabilities:
        """Establish access without the collaborator list.

        Used when the permission query is unavailable. Read is proven by reading.
        Edit is asked of the platform's own permission check (``permission.members
        auth``, a read-risk call that answers for the calling identity alone), which
        needs no collaborator listing. When that check is unavailable too, edit is
        unknown, and **unknown is declared absent**: admission then refuses instead
        of assuming. An assumed edit right moved the failure to the write, after the
        person had delegated on the strength of a promise the panel could not keep.
        """
        try:
            await self.read(doc_ref)
        except ProviderError:
            return DocCapabilities(
                can_read=False, can_edit=False, can_comment=False, can_resolve=False,
                has_revision_control=False, max_quote_chars=None, atomic_batch=False,
            )
        can_edit = await self._auth_check(doc_ref, "edit")
        if can_edit is None:
            logger.warning(
                "[clouddoc] %s：成员列表与权限判定都不可用，编辑权按无处理（不纳管）",
                doc_ref,
            )
            can_edit = False
        return DocCapabilities(
            can_read=True, can_edit=can_edit, can_comment=can_edit, can_resolve=False,
            has_revision_control=False,
            # Same measured platform fact as the member-list path: the truncation
            # point belongs to the platform, not to how access was determined.
            max_quote_chars=128, quote_truncates_unmarked=True,
            atomic_batch=False,
        )

    async def _auth_check(self, doc_ref: DocRef, action: str) -> bool | None:
        """Whether this identity holds ``action`` on the document, by the platform's
        own answer; None when the check itself is unavailable."""
        try:
            data = await self._drive_json(doc_ref, lambda ftype: [
                "drive", "permission.members", "auth", "--token", doc_ref,
                "--type", ftype, "--action", action,
            ])
        except ProviderError:
            return None
        verdict = _first(data, "auth_result", "authResult", "result", default=None)
        return None if verdict is None else bool(verdict)

    # ---------------------------------------------------------------- reading

    def doc_url(self, doc_ref: DocRef, kind: str = "") -> str:
        """The link the platform gave for this document, or the token.

        Not built from a template: a Feishu document's URL begins with the tenant's own
        domain, and there is no way to derive it from a token. Returning the token when
        no URL was seen is the honest answer -- a person can paste a token into the
        panel, and a fabricated link would only look right.
        """
        return self._url_cache.get(doc_ref) or str(doc_ref)

    def note_kind(self, doc_ref: DocRef, kind: str) -> None:
        """Accept a format learned elsewhere -- the panel's persisted metadata.

        The kind cache is process memory and the pasted link that taught it does not
        come back after a restart; the panel's store is what survives. Without this
        seam a restarted process routes a known spreadsheet down the docx paths until
        someone pastes the link again.
        """
        if kind:
            self._kind_cache[str(doc_ref)] = str(kind)

    def note_url(self, doc_ref: DocRef, url: str) -> None:
        """Accept a document's link from the panel's persisted metadata.

        A Feishu link is not derivable from a token, so a provider that never saw one
        answers ``doc_url`` with the bare token -- and, worse, a document *created* in
        a fresh process had no tenant domain to build its own link from, so the person
        got a token instead of a link (measured 2026-09-03). One seeded link teaches
        the tenant origin (`_tenant_origin`), which every later create reuses. Same
        survives-restart contract as ``note_kind``: process memory, refilled from the
        store by priming.
        """
        u = str(url or "").strip()
        # Validated by host, like a paste: what the store holds was once a paste, and
        # a foreign origin that reached the store before origins were checked must
        # not re-enter this cache -- it feeds the links the panel shows, the tenant
        # origin later creates reuse, and ``canonical_url``, which would hand the
        # same foreign link straight back to be persisted again.
        if is_trusted_doc_url(u, "feishu"):
            self._url_cache.setdefault(str(doc_ref), u.split("?", 1)[0].split("#", 1)[0])

    async def doc_kind(self, doc_ref: DocRef) -> str:
        """Which format this document is.

        Answered from what discovery already saw, and otherwise assumed to be a docx.
        The assumption is the safe one: docx is the only format that was ever reachable
        before this, so a token arriving from somewhere else -- a link pasted into the
        panel -- behaves exactly as it did. A spreadsheet adopted that way reads as a
        docx and fails on the fetch, with the platform's own message, which is a better
        outcome than a metadata call on every read to serve a case the listing already
        covers.

        A withdrawn format can still come back from the panel's persisted metadata,
        which primes this cache on every restart, so ``read`` asks whether the answer
        is one co-scribe still serves.
        """
        got = self._kind_cache.get(str(doc_ref), "")
        if got:
            return got
        # Not seen before. The old code assumed docx here, which was safe only while
        # discovery filled this cache -- and on this platform discovery cannot list
        # anything, so the assumption applied to every pasted link and broke every
        # spreadsheet and deck. One metadata call settles it and is cached from then on.
        await self._resolve_meta(doc_ref)
        return self._kind_cache.get(str(doc_ref), "document")

    async def text_domain_for(self, doc_ref: DocRef) -> str:
        kind = await self.doc_kind(doc_ref)
        return feishu_formats.TEXT_DOMAIN.get(kind, self.text_domain)

    async def read(self, doc_ref: DocRef) -> DocSnapshot:
        """The body as flat text, plus how that text maps back onto the document.

        The CLI answers with ``document.content`` -- one string, XML by default and
        markdown on request -- rather than a block array, so the body arrives already
        flat and markdown is what this asks for: the rails work in character offsets
        over plain text, and XML tags would count as characters they must not.

        The segment map exists for Google, where document indices and characters
        diverge. Here the whole body is one run, which is what a single segment says.
        Block-level addressing is recovered at write time by fetching with ids, so
        reading stays cheap and the write pays for the precision it needs.
        """
        kind = await self.doc_kind(doc_ref)
        if not is_supported_kind(kind):
            # The fall-through below fetches as a docx, so an unserved format would be
            # read as one and report the platform's confusion instead of ours. A row
            # adopted while markdown was still served arrives here on every restart.
            raise ProviderError("invalid", UNSUPPORTED_KIND_DETAIL)
        reader = feishu_formats.READERS.get(kind)
        if reader is not None:
            return await reader(self, doc_ref)
        data = await self._cli.json(
            ["docs", "+fetch", "--doc", doc_ref, "--doc-format", "markdown"]
        )
        doc = _first(data, "document", default={}) or {}
        body = str(_first(doc, "content", default="") or "")
        return DocSnapshot(
            doc_id=str(_first(doc, "document_id", default=doc_ref) or doc_ref),
            kind="document",
            # Confirmed present in the response contract, and the same value
            # ``docs +update --revision-id`` pins a write to.
            revision_id=str(_first(doc, "revision_id", default="") or ""),
            text=body,
            segments=(
                Segment(char_start=0, char_end=len(body), index_start=0, index_end=1),
            ),
        )

    async def list_comments(
        self, doc_ref: DocRef, *, include_resolved: bool = False
    ) -> list[DocComment]:
        # TENANT-VERIFY (2026-09-02, cli 1.0.89): a bare token now requires an
        # explicit --type; the spike-era CLI inferred it. The document's kind is
        # already cached on the read path, and docx is the right default for a
        # document never read (comments are polled after adoption, which reads).
        def make(ftype: str) -> list[str]:
            args = ["drive", "+list-comments", "--token", doc_ref, "--type", ftype, "--need-relation"]
            # The CLI defaults to unresolved only, so asking for everything is explicit.
            args += ["--solved-status", "all" if include_resolved else "false"]
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

        data = await self._drive_json(doc_ref, make)
        # The schema names this list `items`, alongside has_more/page_token.
        raw = _first(data, "items", default=[]) or []
        out: list[DocComment] = []
        for c in raw:
            cid = str(_first(c, "comment_id", default="") or "")
            if not cid:
                continue
            author = _first(c, "user_id", default="") or ""
            reply_rows = (_first(c, "reply_list", default={}) or {}).get("replies") or []
            # A mention anywhere in the thread marks the thread: the comment's own
            # body is reply one on this platform, so the walk covers both at once.
            # A mention authored by this agent or another is not counted: where a
            # mention triggers, counting one an agent wrote would let an agent summon
            # an agent, the exact recruitment the loop prohibition forbids. Only a
            # mention a person typed becomes a trigger signal.
            mentioned: list[str] = []
            # Per reply, the same parse and the same author filter: the follow-up
            # gate reads each reply's own mention set, and a reply built without
            # one can never continue a thread on this platform.
            reply_mentions: list[tuple[str, ...]] = []
            for r in reply_rows:
                r_author = _first(r, "user_id", default="")
                if self._is_self(r_author) or self._is_other_agent(r_author):
                    reply_mentions.append(())
                    continue
                ids = _mentioned_ids(_first(r, "content", default=None))
                mentioned += ids
                reply_mentions.append(tuple(dict.fromkeys(ids)))
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
                    content=_comment_text(c),
                    # `quote` is in the response contract, which is what the rails
                    # anchor on. Without it a comment could not scope an edit.
                    quoted_text=cell_text,
                    resolved=bool(_first(c, "is_solved", default=False)),
                    mentioned_addresses=tuple(dict.fromkeys(mentioned)),
                    replies=replies,
                )
            )
        return out

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
        as a person (the safe direction for answering).
        """
        aid = str(author_id or "").strip()
        return bool(aid) and aid in self._roster

    # ---------------------------------------------------------------- writing

    async def edit(
        self,
        doc_ref: DocRef,
        old_string: str,
        new_string: str,
        *,
        revision_id: str | None = None,
    ) -> EditResult:
        return await self.edit_batch(
            doc_ref, [(old_string, new_string)], required_revision_id=revision_id or ""
        )

    async def edit_batch(
        self,
        doc_ref: DocRef,
        edits: list[tuple[str, str]],
        *,
        required_revision_id: str,
        window: tuple[int, int] | None = None,
        highlight: bool = False,
    ) -> EditResult:
        """One edit, submitted once.

        More than one is refused here as well as upstream. The upstream check reads
        capabilities and is the one a person sees a sentence from; this one is the
        guarantee, so a caller that reaches the provider directly cannot half-write a
        document by going around it.
        """
        if len(edits) > 1:
            raise ProviderError(
                "invalid",
                "本平台不支持一次提交多处修改；请逐条调用。",
            )
        if not edits:
            return EditResult("applied", new_revision_id="")
        old, new = edits[0]

        # Uniqueness is decided here, by the rail's rule, before the platform is asked
        # to substitute anything. str_replace matches on its own terms, and a pattern
        # that appears twice would otherwise be resolved by the platform's choice
        # rather than by the range a person approved.
        snap = await self.read(doc_ref)
        writer = feishu_formats.WRITERS.get(snap.kind)
        if writer is not None:
            # ``window`` is carried through so that this layer judges uniqueness by the
            # same rule the range rail did. Dropping it re-judges across the whole body,
            # and in a spreadsheet a quote of "42" matches every cell showing 42 -- the
            # proposal is refused after a person approved it.
            #
            # ``highlight`` is likewise not attempted -- no styling primitive exists on
            # this CLI's update surface for either format, and inventing one out of a
            # cell background would be a separate, permanent change to the document
            # (C9: declare, do not simulate).
            return await writer(
                self, doc_ref, snap, edits, required_revision_id, window
            )
        if old and not self._unique_in_window(snap, old, window):
            # With a reason: the toolkit prints ``detail`` to the model, and a refusal
            # that says only "locate_failed" invites the same payload again.
            return EditResult(
                "locate_failed", new_revision_id="",
                detail=f"{old[:40]!r} 在正文里找不到，或在允许范围内不唯一",
            )

        args = ["docs", "+update", "--doc", doc_ref]
        if old:
            args += ["--command", "str_replace", "--pattern", old, "--content", new]
        else:
            # An empty old_string means writing into an empty document, which append
            # expresses; overwrite is avoided because it can drop comments and blocks
            # the CLI does not model.
            args += ["--command", "append", "--content", new]
        args += ["--doc-format", "markdown"]
        # **The revision is deliberately not pinned.** It was, on the reading that
        # ``--revision-id`` is Feishu's WriteControl; the sibling command's help text
        # says otherwise, in words: for ``slides +update-slide``, "pinning an older
        # revision rebuilds the page from that snapshot and **discards newer edits to
        # it**". ``docs +update`` documents the same flag as a "base revision id" with
        # the same ``-1`` default, so the same reading applies until a tenant says
        # otherwise.
        #
        # If that is what it does, pinning is worse than having no lock at all: a
        # concurrent edit stops being a refused write and becomes a destroyed one, and
        # the caller is told "applied". Leaving it at latest and declaring no revision
        # control fails safe under either reading -- the content check below is what
        # actually guards the write.
        #
        # TENANT-VERIFY: send an update pinned to a stale revision against a document
        # edited in between, and see whether it errors or silently discards. If it
        # errors, this is a real lock and both this block and ``capabilities`` change
        # back together.
        # **Highlighting is unavailable here, and that is reported rather than raised.**
        #
        # It used to raise. Measured, that made ``apply_for_comment`` fail on every
        # single call against this platform -- the tool asks for highlighting
        # unconditionally -- so the entire apply_scoped watch level was unusable on
        # Feishu, and had been since PR3, with no test catching it because the fake
        # provider in the suite always accepts the flag.
        #
        # Refusing was the recorded decision (§17.6: degrade to refusal), and it is the
        # wrong one. What ring ⑥ needs is that the reader can see what changed; a
        # background colour is one way to show them, not the requirement itself. The
        # write happens, ``highlighted=False`` comes back, and the caller spells the
        # change out in its reply instead. That substitutes an honest mechanism rather
        # than simulating a missing one, which is what C9 forbids.
        highlighted = False

        # Written before the platform call and settled after it (IC-2's write-ahead
        # shape): a crash between the two leaves a pending entry for the sweep, which
        # is the whole point of recording intent first.
        receipt_id = self._receipt_begin(doc_ref, [(old, new)], highlight=highlighted)

        res = await self._cli.run(args)
        if not res.ok:
            err = _classify(res.code, res.stderr)
            low = f"{res.stderr}".lower()
            if "revision" in low or "conflict" in low or "version" in low:
                self._receipt_abort(receipt_id, "conflict")
                return EditResult("conflict", new_revision_id="")
            self._receipt_abort(receipt_id, err.kind)
            raise err
        try:
            payload = json.loads(res.stdout or "{}")
        except ValueError:
            payload = {}
        doc = ((payload.get("data") or {}).get("document") or {})
        new_rev = str(doc.get("revision_id") or "")
        self._receipt_commit(receipt_id, new_rev, highlighted=highlighted)
        return EditResult("applied", new_revision_id=new_rev, highlighted=highlighted,
                          receipt_id=receipt_id)

    @staticmethod
    def _unique_in_window(
        snap: DocSnapshot, old: str, window: tuple[int, int] | None
    ) -> bool:
        """Whether ``old`` occurs exactly once where the rail allowed it.

        The rail and the write must judge uniqueness by one rule: inside the approved
        window when there is one, across the body otherwise. Disagreement means an edit
        the rail passed gets refused after a person already approved it.
        """
        lo, hi = window if window else (0, len(snap.text))
        return snap.text[lo:hi].count(old) == 1

    # -------------------------------------------------------- receipt plumbing

    def _receipt_begin(self, doc_ref, edits, *, highlight, regions=None):
        """Record the intent before the platform is touched.

        Mechanical, inside the write primitive, so a model cannot skip it (IC-2). An
        absent sink records nothing and refuses nothing -- the fail-closed duty for the
        unattended path belongs to the caller that requires a sink. A sink that is
        wired but cannot record refuses the write (``ledger_unavailable``): this runs
        before the platform call, so nothing has landed, and a write the ledger cannot
        hold must not go through untracked.

        ``regions`` names the addressed form of each entry, parallel to ``edits``. A
        region receipt's old and new are the region's flattened content -- the pair
        the inverse is materialized from -- and the address is what the inverse is
        written back through. A region write's old is computed here rather than named
        by the caller, so a comment-commissioned one attributes every edit via the
        blanket ``for_comment_ids`` instead of the by-old map.
        """
        sink = self.receipt_sink
        if sink is None:
            return None
        meta = getattr(self, "receipt_meta", None) or {}
        by_old = meta.get("for_comment_ids_by_old") or {}
        blanket = [str(x) for x in (meta.get("for_comment_ids") or [])]
        rows = [
            {"old": old, "new": new,
             "for_comment_ids": list(dict.fromkeys(
                 list(by_old.get(old, [])) + blanket
             ))}
            for old, new in edits
        ]
        # Each region spec is ``(address, old_grid)``: the flat old in the pair reads
        # as history; the grid is what a revert actually writes back, kept verbatim
        # because flattening is lossy once a cell's text contains the row separator.
        for i, (addr, old_grid) in enumerate(regions or []):
            rows[i]["region"] = addr
            rows[i]["old_grid"] = old_grid
        try:
            return sink.begin(
                str(doc_ref), rows, highlight=highlight,
                source=str(meta.get("source") or ""),
                executor=str(meta.get("executor") or ""),
            )
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

    async def confirm_absent(self, doc_ref: DocRef) -> bool:
        """Whether this token is **really** gone, as opposed to asked about wrongly.

        ``not_found`` is not a fact on this platform. A deleted document and a
        wiki-hosted one addressed with the wrong container type answer with the same
        family of codes and the same word, "not exist", so the classifier cannot tell
        them apart and the caller must not treat the kind as settled. Measured
        2026-09-10, twice over: two reachable documents were retired as gone, one of
        which read back 3999 characters minutes later.

        The metadata query is the one call that separates them. It takes the token
        against candidate types at once; a live token resolves to its real object
        (a wiki node resolves to the file it hosts), and a deleted one answers
        ``1063005 Resource is deleted``. Anything else -- a transport failure, an
        unfamiliar shape -- returns False, because the only caller is about to do
        something it cannot undo and "I could not tell" must not read as "it is gone".
        """
        try:
            meta = await self._resolve_meta(doc_ref, raise_on_error=True)
        except ProviderError as exc:
            # Only the platform's own "deleted" answer is a verdict. A transport
            # failure, a missing binary, a timeout, a 20008 -- every other error is
            # "could not tell", and the caller keeps the document.
            text = str(exc)
            return exc.kind == "not_found" and (
                "deleted" in text.lower() or "1063005" in text
            )
        except Exception:  # noqa: BLE001 - could not tell; never retire on a maybe
            return False
        # The platform answered and named nothing under any candidate type.
        return not meta

    async def add_page(self, doc_ref: DocRef, title: str = "") -> PageAdded:
        """Add a worksheet, recorded like any other write.

        A page creation is a write, so it is written ahead like every other one: the
        receipt opens before the platform is asked and settles after. Crash in between
        and the sweep finds a pending entry and settles it as unknown, which is the
        honest answer -- exactly the discipline the shell bypass escaped.
        """
        kind = await self.doc_kind(doc_ref)
        adder = feishu_formats.ADD_PAGE.get(kind)
        if adder is None:
            raise ProviderError(
                "unsupported",
                f"飞书的{ {'document': '文档'}.get(kind, kind) }不支持新增页；"
                "请在平台上手动加好再让我写入。",
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
        than position. A docx has no region form and the base class says so."""
        kind = await self.doc_kind(doc_ref)
        writer = feishu_formats.REGION_WRITERS.get(kind)
        if writer is None:
            return await super().write_regions(
                doc_ref, regions, required_revision_id=required_revision_id
            )
        return await writer(
            self, doc_ref, regions, required_revision_id=required_revision_id
        )

    async def read_regions(self, doc_ref: DocRef, regions: list[str]) -> list[str]:
        """The read half of the addressed write, for the revert path's anchor check."""
        kind = await self.doc_kind(doc_ref)
        reader = feishu_formats.REGION_READERS.get(kind)
        if reader is None:
            return await super().read_regions(doc_ref, regions)
        return await reader(self, doc_ref, regions)

    async def clear_highlight(self, doc_ref: DocRef, texts: list[str]) -> dict:
        """Remove the highlight over the given text.

        The write channel has no highlight primitive here (see the module docstring),
        so nothing was ever painted and there is nothing to clear. Answering the shape
        callers expect keeps revert and the resolve-driven clearing working -- they
        call this unconditionally, and an AttributeError would turn a successful undo
        into a crash over a decoration that does not exist on this platform.
        """
        return {"cleared": 0, "missed": list(texts)}

    # ---------------------------------------------------------------- comments

    def _app_id_hint(self) -> str:
        """The bound app id, used to find the app's own row in a member list.
        The lark-cli profile name is the app id in this deployment."""
        return getattr(self._cli, "_profile", "") or ""

    def _drive_type(self, doc_ref: DocRef) -> str:
        """The --type every drive comment verb now demands for a bare token
        (TENANT-VERIFY 2026-09-02, cli 1.0.89: it used to be inferred)."""
        if str(doc_ref) in self._wiki_tokens:
            return "wiki"
        kind = self._kind_cache.get(str(doc_ref), "")
        return {
            "spreadsheet": "sheet",
            "presentation": "slides",
        }.get(kind, "docx")

    async def _drive_json(self, doc_ref: DocRef, make_args) -> Any:
        """Run a drive verb, learning a wiki-hosted document the honest way.

        Feishu masks both no-permission and wrong-container reads as "not
        exist". For a token adopted without its /wiki/ link there is no way to
        know the container up front, so the first failure is retried once as
        wiki; success teaches the flavor for every later call."""
        try:
            return await self._cli.json(make_args(self._drive_type(doc_ref)))
        except ProviderError as exc:
            token = str(doc_ref)
            if token not in self._wiki_tokens and (
                "not exist" in str(exc) or "invalid parameter" in str(exc).lower()
            ):
                data = await self._cli.json(make_args("wiki"))
                self._wiki_tokens.add(token)
                return data
            raise

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
        await self._cli.json(
            ["drive", "+delete-reply", "--token", doc_ref, "--type", self._drive_type(doc_ref),
             "--comment-id", comment_id,
             "--reply-id", reply_id]
        )

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

    # ---------------------------------------------------------------- documents

    async def create_document(self, title: str) -> DocRef:
        try:
            data = await self._cli.json(["docs", "+create", "--title", title])
        except ProviderError as exc:
            # Whether an app identity may own a file is the tenant's policy. Google
            # reports its answer as "storage quota exceeded", which sends people to
            # empty a trash folder that was never full; whatever Feishu calls it, the
            # useful sentence is the same one, so the failure is translated rather
            # than relayed.
            if exc.kind in ("forbidden", "invalid") or "quota" in str(exc).lower():
                self._can_own_documents = False
                raise ProviderError(
                    "forbidden",
                    "创建失败：此部署的应用身份不能在飞书名下持有文档。"
                    "请让用户自己新建文档并共享给本应用。",
                ) from exc
            raise
        doc = _first(data, "document", default=data) or {}
        token = str(
            _first(doc, "document_id", "token", "obj_token", default="") or ""
        )
        if not token:
            raise ProviderError("unknown", "创建文档未返回 token。")
        self._can_own_documents = True
        # The create call returns no link, and a Feishu link starts with the tenant's
        # own domain, which cannot be derived from a token. It can be learned from any
        # link this provider has already seen (a pasted document, the panel's rows):
        # the tenant is the same for every document the app reaches. With no link
        # ever seen the token stands, as doc_url documents.
        origin = self._tenant_origin()
        if origin:
            self._url_cache[token] = f"{origin}/docx/{token}"
        if not self._self_open_id:
            await self._learn_self_from_owned(token)
        return token

    def _tenant_origin(self) -> str:
        for url in self._url_cache.values():
            m = re.match(r"^(https?://[^/]+)/", str(url or ""))
            if m:
                return m.group(1)
        return ""

    async def _learn_self_from_owned(self, token: str) -> None:
        """Learn the bot's own open_id from a document it just created.

        Spike 9 fell the other way on this tenant: ``whoami`` answers without an
        open_id, so the bot's own comments could not be recognised by author and
        unattended dispatch had nothing to filter with. The owner of a document this
        very call created is this identity -- which makes the metas query a mechanical
        way to learn the id, with no guess and no configuration. Fail-soft: a create
        must not die on identity bookkeeping, and §16.8's content marker remains the
        fallback when no document was ever created from here.
        """
        try:
            data = await self._cli.json([
                "drive", "metas", "batch_query", "--data",
                json.dumps({"request_docs": [{"doc_token": token, "doc_type": "docx"}]}),
            ])
            metas = _first(data or {}, "metas", default=[]) or []
            meta = metas[0] if isinstance(metas, list) and metas else {}
            owner = str(_first(meta, "owner_id", default="") or "")
            if owner.startswith("ou_"):
                self._self_open_id = owner
                if self._identity is not None and not self._identity.address:
                    self._identity = AgentIdentity(
                        display_name=self._identity.display_name, address=owner,
                    )
        except Exception:  # noqa: BLE001 - the create must not fail on this
            logger.debug("[clouddoc] owner-based identity learning skipped", exc_info=True)

    async def share_document(
        self, doc_ref: DocRef, address: str, *, role: str = "writer"
    ) -> None:
        member_type = self._member_type_of(address)
        # ASSUMPTION (spike 4): a bot may grant access to a document it owns.
        # lark-cli rates adding a member as a high-risk write and refuses without
        # ``--yes`` (measured 1.0.93: "requires confirmation", surfaced here as
        # ``unknown`` and reported to the person as an unrecognised address). The
        # confirmation it asks for has already been given: sharing is part of a
        # creation the person asked for and approved through the tool's own gate.
        await self._drive_json(doc_ref, lambda ftype: [
            "drive", "+member-add", "--token", doc_ref, "--type", ftype,
            "--member-id", address, "--member-type", member_type,
            "--perm", "edit" if role == "writer" else "view", "--yes",
        ])

    @staticmethod
    def _member_type_of(address: str) -> str:
        # --member-type is required and the CLI rejects a mismatch with the id's own
        # prefix, so it is derived from the address rather than defaulted: an open_id
        # starts ou_, a chat oc_, and anything with an @ is an email.
        if address.startswith("ou_"):
            return "openid"
        if address.startswith("oc_"):
            return "openchat"
        if address.startswith("on_"):
            return "unionid"
        if "@" in address:
            return "email"
        raise ProviderError(
            "invalid",
            f"无法判断 {address!r} 的类型：需要 open_id（ou_ 开头）或邮箱。",
        )

    async def trash_document(self, doc_ref: DocRef) -> None:
        # Feishu's delete moves the file to the owner's recycle bin (the app's, for a
        # document the app created), where a person can bring it back from the UI.
        await self._drive_json(doc_ref, lambda ftype: [
            "drive", "+delete", "--file-token", doc_ref, "--type", ftype, "--yes",
        ])

    async def list_accessible_documents(self, known=()) -> list[DocSummary]:
        """Documents the bot can reach, for adoption.

        There is no "list what was shared with me" for a bot on this platform -- the
        ``drive +list-files`` verb this used to call does not exist in the CLI, and
        ``drive +search`` answers a bot with nothing under every filter (measured
        2026-09-10: empty query, wiki-only, title-only, time-windowed -- 0 hits each;
        ``--mine`` needs a logged-in user). What a bot **can** enumerate is a wiki
        space it is a member of, node by node, and on this tenant every document the
        agent works on is a wiki node. So discovery walks the spaces the managed
        documents already live in: each known wiki node names its space, and
        ``wiki +node-list`` walks the space. No configuration; the spaces are derived.

        The one thing that stops it is membership. Document-level access to three
        nodes does not make the bot a member of their space, and the listing answers
        ``131006 bot lacks permission``. That is the tenant's to fix in one step -- add
        the app to the space -- so the degrade says exactly that, with the space id,
        instead of the earlier "the API does not support it", which was true of the
        verb that no longer exists and sent people to paste links forever.
        """
        self._discovery_reason = ""
        # A wiki node is any reference that resolved to a *different* object token --
        # that is what hosting means -- as well as any token the drive retry learned
        # as wiki. ``_resolve_meta`` alone does not fill ``_wiki_tokens``: the metadata
        # answer names the hosted object's own type (sheet, docx, slides), not "wiki",
        # so keying on that set alone reported "no wiki documents" over three of them.
        # Start from the managed list, not from caches. On a cold process the caches
        # are filled by side doors -- the panel feeds a stored kind back through
        # ``note_kind`` without any metadata call -- so a boot-time pass saw "no wiki
        # documents" over three of them and returned nothing, silently, until the next
        # 300-second tick. ``+node-get`` on each managed token is the resolver: it names
        # the space, and a token that is not a wiki node simply answers not-found.
        wiki_nodes = set(str(k) for k in known) | set(self._wiki_tokens) | set(self._object_tokens)
        if not wiki_nodes:
            self._discovery_available = False
            self._discovery_reason = (
                "还没有任何已纳管的 wiki 文档可以推出空间；先粘一个 /wiki/ 链接纳管，"
                "之后同一空间里的文档就能自动发现。"
            )
            return []

        spaces: dict[str, str] = {}
        for node in sorted(wiki_nodes):
            try:
                got = await self._cli.json(["wiki", "+node-get", "--node-token", node])
            except ProviderError:
                continue
            n = _first(got or {}, "node", default=None) or (got or {})
            sid = str(_first(n, "space_id", default="") or "")
            if sid:
                spaces.setdefault(sid, node)
        if not spaces:
            self._discovery_available = False
            self._discovery_reason = "无法从已纳管的 wiki 文档反查出所在空间（node-get 未返回 space_id）。"
            return []

        out: list[DocSummary] = []
        blocked: list[str] = []
        for sid in sorted(spaces):
            try:
                data = await self._cli.json(
                    ["wiki", "+node-list", "--space-id", sid, "--page-all"]
                )
            except ProviderError as exc:
                if exc.kind in ("forbidden", "unsupported", "invalid"):
                    blocked.append(sid)
                    continue
                raise
            # The listing's key is ``nodes`` (measured on the live envelope: has_more +
            # nodes[]); ``items`` is kept as a fallback for a client that renames it.
            # Reading ``items`` alone showed 0 nodes over a space that had three, with
            # the bot a member -- an "empty space" that was a wrong dictionary key.
            for it in (_first(data or {}, "nodes", "items", default=[]) or []):
                node = str(_first(it, "node_token", default="") or "")
                obj = str(_first(it, "obj_token", default="") or "")
                if not node:
                    continue
                otype = str(_first(it, "obj_type", default="") or "").lower()
                title = str(_first(it, "title", default="") or "")
                kind = feishu_formats.KIND_BY_TYPE.get(otype, "")
                if not kind:
                    self._unsupported[node] = (title, otype)
                    continue
                self._wiki_tokens.add(node)
                self._kind_cache[node] = kind
                if obj and obj != node:
                    self._object_tokens[node] = obj
                tenant = self._tenant_host()
                if tenant:
                    self._url_cache[node] = f"https://{tenant}/wiki/{node}"
                out.append(DocSummary(doc_id=node, title=title, can_edit=True, kind=kind))

        if blocked and not out:
            self._discovery_available = False
            self._discovery_reason = (
                "应用不是 wiki 空间 " + "、".join(blocked) + " 的成员，平台不允许它列出空间里的文档"
                "（131006）。在飞书里把这个应用加为该空间的成员（member 即可），刷新就能自动发现；"
                "在此之前请粘贴文档链接纳管。"
            )
            logger.info("[clouddoc] 飞书发现降级：%s", self._discovery_reason)
            return []
        if blocked:
            self._discovery_reason = "部分空间未开放给应用：" + "、".join(blocked)
        self._discovery_available = True
        return out

    def _tenant_host(self) -> str:
        """The tenant's domain, learned from any URL already cached; "" if none."""
        for url in self._url_cache.values():
            m = re.match(r"https?://([^/]+)/", url or "")
            if m:
                return m.group(1)
        return ""

    @property
    def discovery_reason(self) -> str:
        """Why discovery is degraded, in words a person can act on; "" when it works."""
        return getattr(self, "_discovery_reason", "") or ""

    async def list_shared_unsupported(self) -> list[dict]:
        """Shared files this provider cannot co-edit.

        Reported rather than hidden: a person who shared a deck needs to know it was
        seen and why nothing happens, which is a different message from silence.

        Filled by the discovery pass above, which is the only place the file types are
        visible. Empty before discovery has run, and empty forever on a tenant that
        does not let the bot enumerate -- in which case the person is pasting links by
        hand anyway and finds out at adoption.
        """
        return [
            {
                "title": title,
                "kind": ftype or "file",
                "reason": feishu_formats.UNSUPPORTED_REASON.get(ftype, ""),
            }
            for title, ftype in self._unsupported.values()
        ]

    async def sharing_posture(self, doc_ref: DocRef) -> list[tuple[str, str]]:
        data = await self._drive_json(doc_ref, lambda ftype: [
            "drive", "+member-list", "--token", doc_ref, "--type", ftype,
        ])
        out: list[tuple[str, str]] = []
        for m in _first(data, "members", "items", default=[]) or []:
            who = str(_first(m, "member_id", "open_id", "name", default="") or "")
            perm = str(_first(m, "perm", "role", default="") or "")
            if who:
                out.append((who, perm))
        return out
