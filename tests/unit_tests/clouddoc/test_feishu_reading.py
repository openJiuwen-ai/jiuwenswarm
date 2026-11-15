"""Offline tests for the reading half of the Feishu provider.

What can be tested without a tenant is the part that is decisions rather than
observations: that the bot never acts as a user, that an unknown author is not treated
as the bot itself, and that capabilities come from this app's own access. The
platform's own shapes are assumptions marked in the provider and settled by the spikes
in §17.2; a stub stands in for them here, and passing these tests says nothing about
whether those assumptions hold.
"""

from __future__ import annotations

import json

import pytest
from _feishu_fakes import DOC, _RefusingCli, _StubCli, provider_with

from jiuwenswarm.clouddoc.providers.base import ProviderError
from jiuwenswarm.clouddoc.providers.feishu.cli import LarkCli
from jiuwenswarm.clouddoc.providers.feishu.reading import FeishuReader


def _provider(replies: dict | None = None) -> tuple[FeishuReader, _StubCli]:
    return provider_with(FeishuReader, replies)


# --------------------------------------------------------------------- identity


def test_every_command_is_issued_as_the_bot():
    """Acting as a user misattributes the write, and it breaks the loop prohibition:
    the agent recognises its own events by author.
    """
    cli = LarkCli(binary="lark-cli", profile="p")
    args = cli._base_args()
    assert "--as" in args and args[args.index("--as") + 1] == "bot"
    assert "user" not in args


# --------------------------------------------------------------------- capabilities


@pytest.mark.asyncio
async def test_the_platform_declares_it_cannot_batch_atomically():
    """C9: a missing capability is declared, not simulated. Upstream refuses the batch
    on the strength of this flag.
    """
    # The live shape (TENANT-VERIFY 2026-09-02): an items array, one row per
    # collaborator, the app's own row keyed by its appid member type.
    p, _ = _provider({"drive +member-list": {"items": [
        {"member_id": "ou_owner", "member_type": "openid", "perm": "full_access"},
        # The profile name is the app id in this deployment (``_app_id_hint``).
        {"member_id": "p", "member_type": "appid", "perm": "edit"},
    ]}})
    caps = await p.capabilities(DOC)
    assert caps.atomic_batch is False
    assert caps.can_edit is True


@pytest.mark.asyncio
async def test_no_optimistic_locking_is_claimed_because_the_flag_is_not_one():
    """This asserted the opposite, on the reading that ``--revision-id`` is Feishu's
    WriteControl. The CLI's own help text for the sibling command says what the flag
    actually does: pinning an older revision "rebuilds the page from that snapshot and
    **discards newer edits to it**". ``docs +update`` documents the same flag the same
    way, as a "base revision id".

    A flag that destroys a concurrent edit is not a lock, and claiming one here is
    worse than claiming nothing: admission would rely on protection that does not
    exist, and the person would be told "applied" about a write that removed someone
    else's work. Declared absent until a tenant proves otherwise (C9).
    """
    p, _ = _provider({"drive +member-list": {"perm": "edit"}})
    caps = await p.capabilities(DOC)
    assert caps.has_revision_control is False


@pytest.mark.asyncio
async def test_a_document_the_bot_cannot_reach_says_so_instead_of_reporting_no_rights():
    """Gone and ungranted are different answers and must not arrive as the same one.

    This used to return an all-False capability set, which reads as "shared with no
    rights" -- so the panel diagnosed comment-only and told the owner to change a
    permission on a document that had been deleted, and nothing ever retired it because
    nothing ever said it was not there. ``not_found`` is raised, and the panel's probe
    turns it into "not shared", which is the verdict retirement acts on.
    """
    p, stub = _provider()
    stub.fail_with = ProviderError("not_found", "not found")
    with pytest.raises(ProviderError) as e:
        await p.capabilities(DOC)
    assert e.value.kind == "not_found"


# --------------------------------------------------------------------- link parsing


@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://x.feishu.cn/docx/AbC123456789", "AbC123456789"),
        ("https://x.feishu.cn/docx/AbC123456789?from=space", "AbC123456789"),
        ("https://x.feishu.cn/wiki/W12345678", "W12345678"),
        ("BareToken12345", "BareToken12345"),
    ],
)
def test_document_links_and_tokens_are_accepted(given, expected):
    p = FeishuReader()
    assert p.parse_doc_ref(given) == expected


def test_an_unparseable_reference_is_refused():
    p = FeishuReader()
    for bad in ("", "   ", "not a token at all"):
        with pytest.raises(ProviderError):
            p.parse_doc_ref(bad)


@pytest.mark.asyncio
async def test_the_body_and_its_revision_come_from_the_document_object():
    """content and revision_id are both under data.document; the revision is what a
    later write pins itself to.
    """
    p, _ = _provider({
        "docs +fetch": {"document": {
            "document_id": DOC, "content": "正文内容。", "revision_id": 12,
        }}
    })
    snap = await p.read(DOC)
    assert snap.text == "正文内容。"
    assert snap.revision_id == "12"
    assert snap.segments[0].char_end == len(snap.text)


@pytest.mark.asyncio
async def test_capabilities_fall_back_to_reading_when_permissions_are_user_only():
    """Reading the collaborator list may itself be user-only. Answering "no access"
    would put a workable document into the comment-only bucket and admission would
    refuse it, so the questions that matter are asked directly: can it be read, and
    does the platform's own permission check grant edit. Read alone proves read.
    """
    p, _ = _provider()
    p.cli = _RefusingCli(
        "drive +member-list",
        replies={"docs +fetch": {"document": {"content": "正文", "revision_id": 3}}},
    )
    caps = await p.capabilities(DOC)
    assert caps.can_read is True and caps.can_edit is False, "读得到不等于改得了"
    assert caps.atomic_batch is False

    p.cli = _RefusingCli(
        "drive +member-list",
        replies={"docs +fetch": {"document": {"content": "正文", "revision_id": 3}},
                 "drive permission.members": {"auth_result": True}},
    )
    caps = await p.capabilities(DOC)
    assert caps.can_read is True and caps.can_edit is True


@pytest.mark.asyncio
async def test_a_document_that_cannot_be_read_reports_no_access():
    """The fallback is a probe, not an assumption: when the read fails too, the
    document really is unusable and admission should refuse it.
    """
    p, _ = _provider()
    p.cli = _RefusingCli("drive +member-list")  # and no docs +fetch reply configured
    p.cli._replies = {}

    async def fail_fetch(args, *, timeout=None):
        raise ProviderError("forbidden", "no")

    p.cli.json = fail_fetch
    caps = await p.capabilities(DOC)
    assert caps.can_read is False and caps.can_edit is False


def test_the_body_travels_as_markdown_so_markers_are_content():
    """Read and written with --doc-format markdown, so a marker round-trips instead of
    landing in someone's document as literal characters. The rail may allow them.
    """
    p, _ = _provider()
    assert p.text_domain == "markdown"


@pytest.mark.asyncio
async def test_a_spreadsheets_title_comes_from_drive_metadata():
    """``docs +fetch`` serves only a docx; the metas query answers any type, and the
    canonical URL it carries is learned so a bare token still links out.
    """
    p, _ = _provider({"drive metas": {"metas": [{
        "title": "区域实弹", "url": "https://x.feishu.cn/sheets/SheetTok12345",
        "owner_id": "ou_c004",
    }]}})
    ref = p.parse_doc_ref("SheetTok12345")
    p._kind_cache[ref] = "spreadsheet"
    assert await p.title(ref) == "区域实弹"
    assert p.doc_url(ref) == "https://x.feishu.cn/sheets/SheetTok12345"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://x.feishu.cn/sheets/SheetTok12345",
                                 "https://evil.example/sheets/SheetTok12345"])
async def test_a_metadata_url_off_the_platforms_hosts_is_not_learned(url):
    """The metas answer is checked like a paste: the cached URL becomes the panel's
    link and the origin later creates reuse, so neither may come from plain http or
    a foreign host.
    """
    p, _ = _provider({"drive metas": {"metas": [{"title": "t", "url": url}]}})
    ref = p.parse_doc_ref("SheetTok12345")
    p._kind_cache[ref] = "spreadsheet"
    assert await p.title(ref) == "t"
    assert p.doc_url(ref) != url
    assert str(ref) not in p._url_cache


@pytest.mark.asyncio
async def test_a_noted_format_survives_where_the_pasted_link_did_not():
    """The kind cache is process memory; the panel's store is what survives a restart.
    Fed back through note_kind, a known spreadsheet stays off the docx paths.
    """
    p, _ = _provider({"drive metas": {"metas": [{"title": "复原标题"}]}})
    p.note_kind("SheetTok12345", "spreadsheet")
    assert p._drive_type("SheetTok12345") == "sheet"
    assert await p.title("SheetTok12345") == "复原标题"


@pytest.mark.asyncio
async def test_the_preset_open_id_wins_when_whoami_carries_none():
    """sa_address anchors on the identity's address; degraded to the display name it
    matches no mention ever.
    """
    p, _ = _provider({"whoami": {"name": "co-scribe"}})
    ident = await p.self_identity()
    assert ident.address == "ou_bot"


@pytest.mark.asyncio
async def test_a_bot_created_documents_url_is_asked_of_the_platform():
    p, _ = _provider({"drive metas": {"metas": [{
        "title": "T", "url": "https://x.feishu.cn/docx/DocToken123456",
    }]}})
    assert await p.canonical_url(DOC) == "https://x.feishu.cn/docx/DocToken123456"
    # Cached: the panel persists it, and the provider answers from memory after.
    assert p.doc_url(DOC) == "https://x.feishu.cn/docx/DocToken123456"


@pytest.mark.asyncio
async def test_a_wiki_hosted_url_probe_retries_once_as_wiki():
    """The wiki flavor is process memory; after a restart the metas query goes out as
    docx and the platform answers the mismatch with a failed_list, not an error.
    """
    class _WikiCli(_StubCli):
        async def json(self, args, *, timeout=None):
            self.calls.append(list(args))
            if args[:2] == ["drive", "metas"]:
                body = json.loads(args[args.index("--data") + 1])
                if body["request_docs"][0]["doc_type"] == "wiki":
                    return {"metas": [{"title": "我的文档",
                                       "url": "https://x.feishu.cn/docx/UnderlyingTok"}]}
                return {"failed_list": [{"code": 970005}]}
            return {}

    p = FeishuReader(profile="p", self_open_id="ou_bot")
    p.cli = _WikiCli()
    assert await p.canonical_url("WikiTok123456") == "https://x.feishu.cn/docx/UnderlyingTok"
    # The flavor is learned: drive verbs go out as wiki from here on.
    assert p._drive_type("WikiTok123456") == "wiki"


# ------------------------------------------------------ markdown, withdrawn

@pytest.mark.asyncio
async def test_a_file_link_is_refused_where_it_is_pasted():
    """Drive files were served as markdown and are not served at all now.

    Refused at parse time rather than at the first read: the person is looking at
    the link they just pasted, which is the only moment the refusal can tell them
    anything useful. Adopting it and failing later would put a permanently broken
    row in the panel instead.
    """
    p, _ = _provider()
    with pytest.raises(ProviderError) as exc:
        p.parse_doc_ref("https://x.feishu.cn/file/MdTok1234567")
    assert "不支持" in str(exc.value)


@pytest.mark.asyncio
async def test_a_markdown_row_left_over_from_before_refuses_rather_than_reading_as_a_docx():
    """The panel's persisted metadata primes the format cache on every restart, so a
    document adopted while markdown was served comes back as kind "markdown" for as
    long as its row exists. The read path falls through to the docx fetch for any
    kind it has no reader for, so without this the file would be fetched as a docx
    and the person would see the platform's confusion instead of our answer.
    """
    p, stub = _provider()
    p.note_kind("MdTok1234567", "markdown")
    with pytest.raises(ProviderError) as exc:
        await p.read("MdTok1234567")
    assert "不支持" in str(exc.value)
    assert stub.calls == [], "被拒绝的格式不得向平台发出任何请求"


@pytest.mark.asyncio
async def test_a_format_without_a_reader_is_refused_rather_than_read_as_a_docx(monkeypatch):
    """The provider recognises a spreadsheet by its link before the format module has
    a reader for it. The read path's fallback is the docx fetch, so a recognised
    format with no reader is refused there instead of being fetched as a docx.
    """
    from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats

    monkeypatch.delitem(feishu_formats.READERS, "spreadsheet", raising=False)
    p, stub = _provider()
    p.note_kind("SheetTok12345", "spreadsheet")
    with pytest.raises(ProviderError) as exc:
        await p.read("SheetTok12345")
    assert "不支持" in str(exc.value)
    assert stub.calls == [], "被拒绝的格式不得向平台发出任何请求"


def test_a_markdown_file_is_reported_as_seen_but_unsupported():
    """It leaves the served list and lands on the "shared but not supported" one in
    the same edit -- the two are complements, so a format cannot be listed by one
    path and refused by the next.
    """
    from jiuwenswarm.clouddoc.providers.feishu import formats as feishu_formats

    assert "markdown" not in feishu_formats.KIND_BY_TYPE.values()
    assert feishu_formats.UNSUPPORTED_REASON[feishu_formats.TYPE_MARKDOWN]


@pytest.mark.asyncio
async def test_the_quiet_format_lookup_still_swallows_failures():
    """``doc_kind`` keeps the fail-soft form: an unanswered metadata call falls through
    to "document" instead of raising into every read path.
    """
    p, stub = _provider()
    stub.fail_with = ProviderError("transport", "down")
    assert await p.doc_kind(DOC) == "document"


# ------------------------- review: one vendor-aware reader for the connection's own address


# ------------------------- review: edit admission never rests on a read alone


class _MemberListDeniedCli(_StubCli):
    """The tenant that refuses the collaborator list but answers the permission check."""

    def __init__(self, auth) -> None:
        super().__init__()
        self.auth = auth

    async def json(self, args, *, timeout=None):
        self.calls.append(list(args))
        if "+member-list" in args:
            raise ProviderError("forbidden", "no scope for member list")
        if "permission.members" in args:
            if isinstance(self.auth, Exception):
                raise self.auth
            return {"auth_result": self.auth}
        return {}


async def _readable(doc_ref):
    from jiuwenswarm.clouddoc.providers.base import DocSnapshot

    return DocSnapshot(doc_id=doc_ref, kind="document", revision_id="1", text="正文")


@pytest.mark.asyncio
@pytest.mark.parametrize("auth,expected", [(True, True), (False, False)])
async def test_the_platforms_own_permission_check_decides_edit(auth, expected):
    p = FeishuReader(profile="p", self_open_id="ou_bot")
    p.cli = _MemberListDeniedCli(auth)
    p._kind_cache[DOC] = "document"
    p.read = _readable
    caps = await p.capabilities(DOC)
    assert caps.can_read is True
    assert caps.can_edit is expected
    assert ["drive", "permission.members", "auth", "--token", DOC, "--type", "docx",
            "--action", "edit"] in p.cli.calls


@pytest.mark.asyncio
async def test_an_unanswerable_permission_check_is_declared_absent():
    """Read proven, edit unknown: unknown is not admitted. The old answer -- edit
    assumed wherever read worked -- moved the failure to the write, after the person
    had delegated on a promise the panel could not keep.
    """
    p = FeishuReader(profile="p", self_open_id="ou_bot")
    p.cli = _MemberListDeniedCli(ProviderError("unsupported", "no such verb"))
    p._kind_cache[DOC] = "document"
    p.read = _readable
    caps = await p.capabilities(DOC)
    assert caps.can_read is True and caps.can_edit is False


@pytest.mark.asyncio
async def test_another_apps_edit_right_is_not_ours():
    """Only this app's own row decides. With only another app listed, the member
    list says nothing about this identity, and the platform's own check is asked.
    """
    class _OtherAppCli(_MemberListDeniedCli):
        async def json(self, args, *, timeout=None):
            if "+member-list" in args:
                self.calls.append(list(args))
                return {"items": [
                    {"member_id": "cli_other", "member_type": "appid", "perm": "edit"},
                    {"member_id": "ou_owner", "member_type": "openid", "perm": "full_access"},
                ]}
            return await super().json(args, timeout=timeout)

    p, _ = _provider()
    p.cli = _OtherAppCli(auth=False)
    p.read = _readable
    caps = await p.capabilities(DOC)
    assert caps.can_edit is False
    assert any("permission.members" in c for c in p.cli.calls)


@pytest.mark.asyncio
async def test_a_cold_metadata_lookup_learns_no_foreign_url():
    """The cold path through ``doc_kind`` applies the same host policy as a paste and
    ``_title_from_meta``: the cached URL becomes the panel's link and the origin
    later creates reuse.
    """
    p, _ = _provider({"drive metas": {"metas": [{
        "doc_type": "docx", "title": "t", "url": "https://evil.example/docx/DocToken123456",
    }]}})
    await p.doc_kind(DOC)
    assert DOC not in p._url_cache
    assert p.doc_url(DOC) != "https://evil.example/docx/DocToken123456"


@pytest.mark.asyncio
@pytest.mark.parametrize("perm", ["", "superuser"], ids=["empty", "unknown-spelling"])
async def test_an_unreadable_permission_on_our_own_row_asks_the_platform(perm):
    """A row of our own with an empty or unknown permission says nothing, like no row
    at all; it must not be read as "no access".
    """
    class _OwnRowCli(_MemberListDeniedCli):
        async def json(self, args, *, timeout=None):
            if "+member-list" in args:
                self.calls.append(list(args))
                return {"items": [{"member_id": "p", "member_type": "appid", "perm": perm}]}
            return await super().json(args, timeout=timeout)

    p, _ = _provider()
    p.cli = _OwnRowCli(auth=True)
    p.read = _readable
    caps = await p.capabilities(DOC)
    assert caps.can_edit is True
    assert any("permission.members" in c for c in p.cli.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,expected", [
    (True, True), (False, False), ("true", True), ("false", False),
    ("0", None), ({"auth_result": False}, None), (1, None),
], ids=["true", "false", "str-true", "str-false", "str-0", "nested", "int"])
async def test_only_a_plain_yes_or_no_counts_as_the_platforms_answer(answer, expected):
    """``bool()`` of a nested object or of "false" is True, which would read a refusal
    as an edit right; any shape that is not plainly yes or no is "could not tell".
    """
    p, _ = _provider()
    p.cli = _MemberListDeniedCli(auth=answer)
    assert await p._auth_check(DOC, "edit") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["not_found", "rate_limited", "transport", "unknown"])
async def test_a_probe_read_that_fails_for_another_reason_is_not_no_access(kind):
    """Through the probe, as through the member list, a document that is gone or a
    read that failed transiently raises; only a refusal reads as "no access".
    """
    p, _ = _provider()
    p.cli = _RefusingCli("drive +member-list")

    async def fail_read(doc_ref):
        raise ProviderError(kind, "read failed")

    p.read = fail_read
    with pytest.raises(ProviderError) as caught:
        await p.capabilities(DOC)
    assert caught.value.kind == kind


@pytest.mark.asyncio
async def test_the_wiki_retry_reads_the_platform_message_in_any_case():
    """The container retry matches "not exist" without regard to case, as the
    classifier's text rules do; "Doc Not Exist" still teaches the wiki flavor.
    """
    class _CaseCli(_StubCli):
        async def json(self, args, *, timeout=None):
            self.calls.append(list(args))
            if args[args.index("--type") + 1] != "wiki":
                raise ProviderError("not_found", "Doc Not Exist")
            return {"items": []}

    p, _ = _provider()
    p.cli = _CaseCli()
    await p._drive_json(DOC, lambda ftype: ["drive", "+member-list", "--token", DOC,
                                            "--type", ftype])
    assert [c[c.index("--type") + 1] for c in p.cli.calls] == ["docx", "wiki"]
    assert DOC in p._wiki_tokens
