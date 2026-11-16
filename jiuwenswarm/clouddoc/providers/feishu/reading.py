"""The reading half of the Feishu/Lark provider, driven through the official CLI.

Read the seam first: ``cli.LarkCli`` builds, runs and classifies every command, and
this file is what interprets the results as documents, metadata, capabilities and
comments. The write path, receipts and document lifecycle build on it in
``provider.py``.

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

The largest correction is a capability, and it went the other way. ``docs +update``
takes ``--revision-id`` as a base revision, which first read as Feishu's answer to
Google's WriteControl; the sibling command documents that pinning an older revision
rebuilds from that snapshot and discards newer edits, so it is not a lock and this
provider declares no revision control (see the write path). Edits are submitted as a
document-wide ``str_replace``, one command per edit, so the atomic batch answer is no
as well: several edits still mean several commands.

One finding from the CLI changes what was expected of this platform:

* **Document comments have no event.** ``event list`` offers eight domains --
  application, approval, board, card, im, minutes, task, vc -- and none carries a
  drive or comment event. The capability survey expected a push channel to replace
  polling here; there is none, so the watcher polls on Feishu exactly as it does on
  Google, and the "idle polling quota disappears structurally" note in §17.1 does not
  hold. What does hold is that event payloads carry ``sender_id`` as an ``ou_``-
  prefixed open_id, so were an event to exist, recognising the agent's own would be a
  platform fact rather than a guess.

One thing is decided rather than observed:

* **``--as bot``.** Enforced in the CLI wrapper. Acting as a user misattributes the
  write and, worse, breaks the loop prohibition: the agent recognises its own events
  by author, and an event it produced under a person's identity is indistinguishable
  from one that person produced.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from jiuwenswarm.clouddoc.providers.feishu.cli import LarkCli
from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats
from jiuwenswarm.clouddoc.providers.base import (
    is_trusted_doc_url,
    AgentIdentity,
    DocCapabilities,
    DocRef,
    DocSnapshot,
    ProviderError,
    Segment,
    TextDomain,
    UNSUPPORTED_KIND_DETAIL,
    is_supported_kind,
)

logger = logging.getLogger(__name__)


# The permission values a collaborator row is read by. Anything else -- empty, or a
# spelling the platform may use that this does not know -- is asked of the platform's
# own permission check instead.
_KNOWN_PERMS = frozenset({
    "edit", "full_access", "manage_collaborator", "owner",
    "comment", "view_and_comment", "view", "read",
})


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


class FeishuReader:
    """What the Feishu provider knows about a document without changing it.

    References and metadata, the app's capabilities on a document, and the body as
    markdown. ``FeishuCommenter`` (comments.py) adds comment threads and replies on
    top, and ``FeishuDocsProvider`` (provider.py) the writes; nothing here edits a
    body.
    """

    def __init__(
        self,
        *,
        profile: str = "",
        binary: str = "lark-cli",
        self_open_id: str = "",
        agent_roster: tuple[str, ...] = (),
        cli_env: Mapping[str, str] | None = None,
    ) -> None:
        # The app's secret is registered with the CLI once (``config init``) and lives
        # in its store; a connection names a profile, never a secret. ``cli_env`` is an
        # environment the host already prepared for the CLI (see the cli module).
        self.cli = LarkCli(binary=binary, profile=profile, env=cli_env)
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
        data = await self.cli.json(["whoami"])
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

    async def _metas_query(self, doc_ref: DocRef) -> Any:
        """The raw metadata answer for this token under every candidate type."""
        types = [feishu_formats.TYPE_DOCX, feishu_formats.TYPE_SHEET,
                 feishu_formats.TYPE_SLIDES, "wiki"]
        return await self.cli.json([
            "drive", "metas", "batch_query", "--data",
            json.dumps({"request_docs": [{"doc_token": str(doc_ref), "doc_type": t}
                                         for t in types],
                        "with_url": True}, ensure_ascii=False),
        ])

    async def _resolve_meta(self, doc_ref: DocRef) -> dict:
        """Ask the platform what this token is, in one call.

        Quiet by design: a failed call answers ``{}`` and the format lookups fall
        through to "document". A caller for whom "could not ask" and "asked, and
        the token names nothing" are opposite verdicts reads ``_metas_query``
        instead, which raises.

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
        try:
            data = await self._metas_query(doc_ref)
        except ProviderError:
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
        # Checked by host, as every other way into this cache is: it feeds the links
        # the panel shows and the origin later creates reuse, and one policy for all
        # entry points is the only one that holds. A deployment on its own domain
        # loses the canonical link here and keeps the pasted one.
        if is_trusted_doc_url(url, "feishu"):
            self._url_cache[str(doc_ref)] = url.split("?", 1)[0].split("#", 1)[0]
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

    async def resolved_object_token(self, doc_ref: DocRef) -> str:
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
            data = await self.cli.json([
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
        # Checked by host like a paste: this cache feeds the links the panel shows and
        # the origin later creates reuse, so a foreign or plain-http URL reaches neither.
        if is_trusted_doc_url(url, "feishu"):
            self._url_cache.setdefault(str(doc_ref), url.split("?", 1)[0].split("#", 1)[0])
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
        data = await self.cli.json(["docs", "+fetch", "--doc", doc_ref, "--detail", "simple"])
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
            if exc.kind in ("forbidden", "invalid"):
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
        # Only this app's own row decides. Another app's row, or the first
        # collaborator's, says nothing about what this identity may do; with no row
        # of its own (or no known app id) the probe asks the platform directly.
        perm: str | None = None
        for m in members:
            mid = str(_first(m, "member_id", "open_id", "id", default="") or "")
            if app_id and mid == app_id:
                perm = str(_first(m, "perm", "permission", "role", default="") or "").lower()
                break
        # A row of its own whose permission field is empty or spelled in a way this
        # does not know is as uninformative as no row: reading it as "no access"
        # would put a document the app fully owns into the comment-only bucket.
        if perm not in _KNOWN_PERMS:
            return await self._capabilities_by_probe(doc_ref)
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
        except ProviderError as exc:
            # Only a refusal is "no access". A document that is gone, a throttle or
            # a transport failure is not an answer about permissions: folded into an
            # all-False set it would keep a deleted document in the panel being told
            # to change its sharing, never retired, and call one 429 "unreadable".
            # Raised, as capabilities() raises them, so both paths agree.
            if exc.kind != "forbidden":
                raise
            return DocCapabilities(
                can_read=False, can_edit=False, can_comment=False, can_resolve=False,
                has_revision_control=False, max_quote_chars=None, atomic_batch=False,
            )
        can_edit = await self._auth_check(doc_ref, "edit")
        if can_edit is None:
            logger.warning(
                "[clouddoc] %s: neither the member list nor the permission check answered; "
                "edit is treated as absent (not adopted)",
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
        own answer; None when the check itself is unavailable.
        """
        try:
            data = await self._drive_json(doc_ref, lambda ftype: [
                "drive", "permission.members", "auth", "--token", doc_ref,
                "--type", ftype, "--action", action,
            ])
        except ProviderError:
            return None
        verdict = _first(data, "auth_result", "authResult", "result", default=None)
        # Only an answer that is plainly yes or no counts. ``bool()`` of a nested
        # object or of the string "false" is True, which would read a refusal as an
        # edit right; any other shape is "could not tell", which the probe declares
        # absent.
        if isinstance(verdict, bool):
            return verdict
        if isinstance(verdict, str) and verdict.strip().lower() in ("true", "false"):
            return verdict.strip().lower() == "true"
        return None

    # ---------------------------------------------------------------- reading

    def doc_url(self, doc_ref: DocRef, kind: str = "") -> str:
        """The link the platform gave for this document, or the token.

        Not built from a template: a Feishu document's URL begins with the tenant's own
        domain, and there is no way to derive it from a token. Returning the token when
        no URL was seen is the honest answer -- a person can paste a token into the
        panel, and a fabricated link would only look right.
        """
        return self._url_cache.get(str(doc_ref)) or str(doc_ref)

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

        Answered from the cache when the format is already known -- from discovery, a
        pasted link, or the panel's persisted metadata -- and otherwise asked of the
        platform with one metadata query (``_resolve_meta``), whose answer is cached
        from then on. Only a token the platform does not resolve falls back to
        "document".

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
        if kind != "document":
            # The fetch below serves a docx only; a format without a reader of its own
            # is refused here rather than read as one.
            raise ProviderError("invalid", UNSUPPORTED_KIND_DETAIL)
        data = await self.cli.json(
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

    # ---------------------------------------------------------------- comments

    def _app_id_hint(self) -> str:
        """The bound app id, used to find the app's own row in a member list.
        The lark-cli profile name is the app id in this deployment.
        """
        return getattr(self.cli, "_profile", "") or ""

    def _drive_type(self, doc_ref: DocRef) -> str:
        """The --type every drive comment verb now demands for a bare token
        (TENANT-VERIFY 2026-09-02, cli 1.0.89: it used to be inferred).
        """
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
        wiki; success teaches the flavor for every later call.
        """
        try:
            return await self.cli.json(make_args(self._drive_type(doc_ref)))
        except ProviderError as exc:
            token = str(doc_ref)
            if token not in self._wiki_tokens and (
                "not exist" in str(exc).lower() or "invalid parameter" in str(exc).lower()
            ):
                data = await self.cli.json(make_args("wiki"))
                self._wiki_tokens.add(token)
                return data
            raise
