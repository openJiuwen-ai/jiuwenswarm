"""The Feishu/Lark provider: the write path and document lifecycle.

``comments.FeishuCommenter`` (on ``reading.FeishuReader``) answers what a document is,
what this app may do with it, its body and its comments; this module adds what changes a document -- one edit per
command, each written ahead as a receipt -- and creating, sharing, trashing and
discovering documents.

* **The write channel has no highlight.** ``docs +update`` exposes no background
  colour, so the edit is made without one and reported as ``highlighted=False``; the
  caller spells the change out in its reply instead (see the write path).
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

from jiuwenswarm.clouddoc.providers.feishu.cli import _classify
from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats
from jiuwenswarm.clouddoc.providers.feishu.comments import FeishuCommenter
from jiuwenswarm.clouddoc.providers.feishu.reading import _first
from jiuwenswarm.clouddoc.providers.base import (
    PRE_MUTATION_KINDS,
    PageAdded,
    AgentIdentity,
    DocProvider,
    DocRef,
    DocSnapshot,
    DocSummary,
    EditResult,
    ProviderError,
)

logger = logging.getLogger(__name__)


# The metadata query's code for a token that names no record: deleted, or never was.
_META_RECORD_NOT_FOUND = "970005"


def _is_about(failure: Any, token: str) -> bool:
    """Whether a ``failed_list`` entry concerns ``token``; an entry naming no token does."""
    if not isinstance(failure, dict):
        return False
    return str(_first(failure, "token", "doc_token", default=token) or token) == token


def _names_nothing_but_absence(data: Any, token: str) -> bool:
    """Whether a metadata answer positively says the token names nothing.

    Absence is a verdict only when the platform returned no record under any type and
    every failure it listed for this token is "record not found". An empty or
    unfamiliar answer, or a failure for another reason (no permission, unsupported
    type), is "could not tell", and the caller keeps the document.
    """
    if not isinstance(data, dict) or _first(data, "metas", default=None):
        return False
    listed = _first(data, "failed_list", default=[]) or []
    failed = [f for f in listed if _is_about(f, token)]
    codes = {str(_first(f, "code", default="") or "") for f in failed}
    return bool(codes) and codes == {_META_RECORD_NOT_FOUND}


class FeishuDocsProvider(FeishuCommenter, DocProvider):

    # The receipt plumbing is set on the instance by the connection registry, exactly
    # as it is for Google. Declared here as class defaults so the write primitive can
    # read them whatever path built the provider -- a test constructing one directly
    # must not have to know about them.
    receipt_sink = None
    receipt_meta = None

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
            return await writer(self, doc_ref, snap, edits, required_revision_id, window=window)
        refusal = self._locate_refusal(snap, old, window)
        if refusal is not None:
            return refusal

        args = self._update_args(doc_ref, old, new)
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
        receipt_id = self.receipt_begin(doc_ref, [(old, new)], highlight=highlighted)

        res = await self.cli.run(args)
        payload, failure = self._update_envelope(res)
        if failure:
            return self._settle_refused_update(receipt_id, res.code, failure)
        doc = ((payload.get("data") or {}).get("document") or {})
        new_rev = str(doc.get("revision_id") or "")
        self.receipt_commit(receipt_id, new_rev, highlighted=highlighted)
        return EditResult("applied", new_revision_id=new_rev, highlighted=highlighted,
                          receipt_id=receipt_id)

    def _locate_refusal(
        self, snap: DocSnapshot, old: str, window: tuple[int, int] | None
    ) -> EditResult | None:
        """Why ``old`` cannot be replaced by a docx update, or None when it can."""
        if not old:
            return None
        if not self._unique_in_window(snap, old, window):
            # With a reason: the toolkit prints ``detail`` to the model, and a refusal
            # that says only "locate_failed" invites the same payload again.
            return EditResult(
                "locate_failed", new_revision_id="",
                detail=f"{old[:40]!r} 在正文里找不到，或在允许范围内不唯一",
            )
        if window and snap.text.count(old) != 1:
            # ``str_replace`` carries no position: the platform searches the whole
            # document, so the command for the approved occurrence is the same as the
            # command for any other. Unique inside the window is therefore not enough;
            # the text must be unique in the body, which also places its one
            # occurrence inside the window.
            return EditResult(
                "locate_failed", new_revision_id="",
                detail=(
                    f"{old[:40]!r} 在允许范围内唯一，但正文别处还有相同文字；本平台的替换"
                    "作用于全文，无法只改批准的那一处。请扩大要替换的文字，使其在全文唯一。"
                ),
            )
        return None

    @staticmethod
    def _update_args(doc_ref: DocRef, old: str, new: str) -> list[str]:
        args = ["docs", "+update", "--doc", doc_ref]
        if old:
            args += ["--command", "str_replace", "--pattern", old, "--content", new]
        else:
            # An empty old_string means writing into an empty document, which append
            # expresses; overwrite is avoided because it can drop comments and blocks
            # the CLI does not model.
            args += ["--command", "append", "--content", new]
        return args + ["--doc-format", "markdown"]

    @staticmethod
    def _update_envelope(res: Any) -> tuple[dict, str]:
        """The update's envelope and its failure text ("" when it succeeded).

        A refusal can arrive as a failed exit or as ``ok: false`` on stdout with exit
        0; both are classified the way ``LarkCli.json`` classifies them.
        """
        if not res.ok:
            failure = res.stderr or res.stdout
            if not failure.strip():
                # A failed exit with no text at all -- a killed process -- says
                # nothing about whether the edit landed: pending, not settled.
                raise ProviderError(
                    "unknown", f"写入结果无法确认（CLI 退出码 {res.code}，没有任何输出）"
                )
            return {}, failure
        try:
            payload = json.loads(res.stdout)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            # Exit 0 without an envelope says nothing about whether the edit landed,
            # so the receipt stays pending for the sweep.
            raise ProviderError(
                "unknown", f"写入结果无法确认（输出不是 CLI 的 JSON）：{res.stdout[:200]}"
            )
        if "ok" in payload and not payload.get("ok"):
            return payload, res.stdout
        return payload, ""

    def _settle_refused_update(self, receipt_id: str, code: int, failure: str) -> EditResult:
        """Close or keep the receipt of a refused update; a conflict is returned."""
        err = _classify(code, failure)
        low = str(err).lower()
        if "revision" in low or "conflict" in low or "version" in low:
            self.receipt_abort(receipt_id, "conflict")
            return EditResult("conflict", new_revision_id="")
        # Closed only when the platform refused the request. A timeout or an
        # unrecognised failure may have landed the write, so the receipt stays
        # pending for the sweep, as add_page's does.
        if err.kind in PRE_MUTATION_KINDS:
            self.receipt_abort(receipt_id, err.kind)
        raise err

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

    def receipt_begin(self, doc_ref, edits, *, highlight, regions=None):
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

    def receipt_commit(self, receipt_id, new_rev, *, highlighted: bool | None = None):
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

    def receipt_abort(self, receipt_id, reason):
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
            data = await self._metas_query(doc_ref)
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
        return _names_nothing_but_absence(data, str(doc_ref))

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
            name = {"document": "文档"}.get(kind, kind)
            raise ProviderError(
                "unsupported",
                f"飞书的{name}不支持新增页；请在平台上手动加好再让我写入。",
            )
        receipt_id = self.receipt_begin(
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
                self.receipt_abort(receipt_id, str(exc)[:200])
            raise
        except Exception as exc:  # noqa: BLE001 - outcome unknown: leave the receipt pending
            raise ProviderError("unknown", f"新增页的结果未知：{exc}") from exc
        self.receipt_commit(receipt_id, "")
        return PageAdded(address=str(address), receipt_id=receipt_id)

    async def write_regions(
        self,
        doc_ref: DocRef,
        regions: list[tuple[str, list[list[str]]]],
        *,
        required_revision_id: str = "",
    ) -> EditResult:
        """D15's addressed write, for the formats that are addressed by something other
        than position. A docx has no region form and the base class says so.
        """
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

    # ---------------------------------------------------------------- documents

    async def create_document(self, title: str) -> DocRef:
        try:
            data = await self.cli.json(["docs", "+create", "--title", title])
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
            data = await self.cli.json([
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
        self, doc_ref: DocRef, email: str, *, role: str = "writer"
    ) -> None:
        # ``email`` keeps the contract's name so a keyword call reaches both
        # providers; here it may also be an open_id, a chat id or a union id.
        address = email
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

        spaces = await self._spaces_of(wiki_nodes)
        if not spaces:
            self._discovery_available = False
            self._discovery_reason = "无法从已纳管的 wiki 文档反查出所在空间（node-get 未返回 space_id）。"
            return []

        out: list[DocSummary] = []
        blocked: list[str] = []
        for sid in sorted(spaces):
            try:
                data = await self.cli.json(
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
                summary = self._adopt_node(it)
                if summary is not None:
                    out.append(summary)

        if blocked and not out:
            self._discovery_available = False
            self._discovery_reason = (
                "应用不是 wiki 空间 " + "、".join(blocked) + " 的成员，平台不允许它列出空间里的文档"
                "（131006）。在飞书里把这个应用加为该空间的成员（member 即可），刷新就能自动发现；"
                "在此之前请粘贴文档链接纳管。"
            )
            logger.info("[clouddoc] Feishu discovery degraded: %s", self._discovery_reason)
            return []
        if blocked:
            self._discovery_reason = "部分空间未开放给应用：" + "、".join(blocked)
        self._discovery_available = True
        return out

    async def _spaces_of(self, wiki_nodes: set[str]) -> dict[str, str]:
        """Space id -> one node in it, for every node ``wiki +node-get`` resolves."""
        spaces: dict[str, str] = {}
        for node in sorted(wiki_nodes):
            try:
                got = await self.cli.json(["wiki", "+node-get", "--node-token", node])
            except ProviderError:
                continue
            n = _first(got or {}, "node", default=None) or (got or {})
            sid = str(_first(n, "space_id", default="") or "")
            if sid:
                spaces.setdefault(sid, node)
        return spaces

    def _adopt_node(self, it: dict) -> DocSummary | None:
        """Learn one listed wiki node; None for a nameless or unsupported one."""
        node = str(_first(it, "node_token", default="") or "")
        if not node:
            return None
        obj = str(_first(it, "obj_token", default="") or "")
        otype = str(_first(it, "obj_type", default="") or "").lower()
        title = str(_first(it, "title", default="") or "")
        kind = feishu_formats.KIND_BY_TYPE.get(otype, "")
        if not kind:
            self._unsupported[node] = (title, otype)
            return None
        self._wiki_tokens.add(node)
        self._kind_cache[node] = kind
        if obj and obj != node:
            self._object_tokens[node] = obj
        tenant = self._tenant_host()
        if tenant:
            self._url_cache[node] = f"https://{tenant}/wiki/{node}"
        return DocSummary(doc_id=node, title=title, can_edit=True, kind=kind)

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
        """Return ``[(member, perm), ...]`` for the explicit collaborators only.

        **This differs from the base contract**, which returns ``(type, role)`` so a
        caller can spot an audience nobody curated (``type == "anyone"``). On this
        platform ``drive +member-list`` lists named members; a link-sharing setting
        ("anyone in the organisation / on the internet with the link") is not a
        member and never appears here. So the first element is a member id, and a
        document shared by link looks the same as a curated one.

        A caller must therefore not read an absent ``anyone`` row as "no link
        sharing" on Feishu. The setting itself is readable through ``drive
        +permission-get-setting`` (``link_share_entity``); mapping it into this list
        belongs with the PR that wires a consumer, once its answer is measured on a
        tenant.
        """
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
