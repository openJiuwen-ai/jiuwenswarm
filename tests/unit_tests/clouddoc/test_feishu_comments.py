"""Offline tests for the comment layer of the Feishu provider.

Decisions rather than observations: that authors are told apart by id and never by
display name, that a mention counts only when a person typed it, that a reply is read
from its elements, that every page of comments and of a long thread is read, and that
resolving a thread is refused. A stub stands in for the platform's shapes.
"""

from __future__ import annotations

import json

import pytest
from _feishu_fakes import DOC, _StubCli, provider_with

from jiuwenswarm.clouddoc.providers.base import ProviderError, summons
from jiuwenswarm.clouddoc.providers.feishu.comments import FeishuCommenter


def _provider(replies: dict | None = None) -> tuple[FeishuCommenter, _StubCli]:
    return provider_with(FeishuCommenter, replies)


@pytest.mark.asyncio
async def test_an_unknown_author_is_not_the_bot():
    """The failure that starts a loop is calling someone else's comment ours, so doubt
    resolves toward not-self.
    """
    p, _ = _provider()
    assert p._is_self("ou_bot") is True
    assert p._is_self("ou_someone") is False
    assert p._is_self("") is False
    assert p._is_self(None) is False


@pytest.mark.asyncio
async def test_identity_is_compared_by_id_never_by_display_name():
    """A person can set their display name to the bot's; the id is the fact."""
    p = FeishuCommenter(self_open_id="ou_bot")
    p.cli = _StubCli()
    assert p._is_self("bot") is False


# --------------------------------------------------------------------- principle


@pytest.mark.asyncio
async def test_resolving_a_thread_is_refused_on_principle():
    """The platform has the API; invariant ③ is why it is not offered. Resolving is the
    reader's acknowledgement, and an agent doing it removes the acknowledgement.
    """
    p, _ = _provider()
    with pytest.raises(ProviderError) as e:
        await p.resolve_comment(DOC, "c1")
    assert e.value.kind == "unsupported"


# ------------------------------------------------ shapes taken from the CLI's schema


@pytest.mark.asyncio
async def test_a_reply_is_read_from_its_elements_not_as_a_string():
    """The schema gives content.elements, a list of typed runs. Read as a string every
    reply looks empty -- including the agent's own, which is how a verdict is
    recognised, so the loop would answer itself forever.
    """
    p, _ = _provider({
        "drive +list-comments": {
            "items": [{
                "comment_id": "c1",
                "user_id": "ou_someone",
                "create_time": 1700000000,
                "quote": "被引用的原文",
                "is_solved": False,
                "reply_list": {"replies": [
                    {"reply_id": "r1", "user_id": "ou_someone",
                     "create_time": 1700000000,
                     "content": {"elements": [
                         {"type": "person", "person": {"user_id": "ou_bot"}},
                         {"type": "text_run", "text_run": {"text": " 请改这句"}},
                     ]}},
                ]},
            }]
        }
    })
    comments = await p.list_comments(DOC)
    assert len(comments) == 1
    c = comments[0]
    assert c.content == "@ou_bot 请改这句", "评论正文取自首条回复,按元素类型读取"
    assert c.quoted_text == "被引用的原文"
    assert c.replies == (), "首条回复是评论本身，不再算作回复"
    assert c.mentioned_addresses == ("ou_bot",)
    assert c.author_is_self is False


# ---------------------------------------- "is this author another agent" (IC-6)


@pytest.mark.asyncio
async def test_other_agent_detection_is_not_borrowed_from_google():
    """IC-6: the display-name suffix that identifies a Google service account says
    nothing here. A provider that borrowed it would call every Feishu bot a person and
    invariant ⑤ would fail exactly where two deployments share a document.
    """
    p, _ = _provider()
    for name in (
        "docs-agent@x.iam.gserviceaccount.com",  # Google's shape, meaningless here
        "ou_someone",
        "cli_app",
        "",
    ):
        assert p._is_other_agent(name) is False


@pytest.mark.asyncio
async def test_an_unrecognised_author_is_answered_rather_than_ignored():
    """Until a tenant shows what a bot's authorship looks like, the answer is False --
    the safe direction for what it feeds: a comment from someone unrecognised gets a
    reply instead of being silently dropped.
    """
    p, _ = _provider({
        "drive +list-comments": {
            "items": [{
                "comment_id": "c1", "user_id": "ou_unknown",
                "create_time": 1, "quote": "原文", "is_solved": False,
                "reply_list": {"replies": [
                    {"reply_id": "r1", "user_id": "ou_unknown", "create_time": 1,
                     "content": {"elements": [{"text_run": {"text": "看看这句"}}]}},
                ]},
            }]
        }
    })
    c = (await p.list_comments(DOC))[0]
    assert c.author_is_self is False
    assert c.author_is_service_account is False, "未识别的作者按人处理，评论会被回应"


@pytest.mark.asyncio
async def test_the_bots_own_comment_is_still_recognised():
    """Self-recognition works by open_id and is independent of the unresolved question
    about other agents -- the loop that matters most is answering oneself.
    """
    p, _ = _provider({
        "drive +list-comments": {
            "items": [{
                "comment_id": "c1", "user_id": "ou_bot",
                "create_time": 1, "quote": "原文", "is_solved": False,
                "reply_list": {"replies": []},
            }]
        }
    })
    c = (await p.list_comments(DOC))[0]
    assert c.author_is_self is True


@pytest.mark.asyncio
async def test_a_mention_in_the_thread_is_surfaced_as_an_address():
    """The mention-hint path compares addresses; a person element in any reply is the
    only mention signal this platform's payload carries, so it must surface or every
    @ goes silently unanswered.
    """
    p, _ = _provider({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_human",
        "reply_list": {"replies": [
            {"reply_id": "r1", "user_id": "ou_human", "content": {"elements": [
                {"type": "text_run", "text_run": {"text": "Please summarize"}},
            ]}},
            {"reply_id": "r2", "user_id": "ou_human", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
            ]}},
        ]},
    }]}})
    c = (await p.list_comments(DOC))[0]
    assert c.mentioned_addresses == ()
    assert [r.mentioned_addresses for r in c.replies] == [("ou_bot",)]
    assert summons(c, "ou_bot") is True


@pytest.mark.asyncio
async def test_a_reply_carries_its_own_mentions():
    """The follow-up gate reads each reply's mention set. A reply built without one
    can never continue a thread on this platform, whatever the comment-level set
    says -- and the re-mention every notice asks for is exactly such a reply.
    """
    p, _ = _provider({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_human",
        "reply_list": {"replies": [
            {"reply_id": "r1", "user_id": "ou_human", "content": {"elements": [
                {"type": "text_run", "text_run": {"text": "Please summarize"}},
            ]}},
            {"reply_id": "r2", "user_id": "ou_bot", "content": {"elements": [
                {"type": "text_run", "text_run": {"text": "Done."}},
            ]}},
            {"reply_id": "r3", "user_id": "ou_human", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
                {"type": "text_run", "text_run": {"text": " shorter please"}},
            ]}},
        ]},
    }]}})
    comments = await p.list_comments(DOC)
    replies = comments[0].replies
    assert [r.reply_id for r in replies] == ["r2", "r3"]
    assert replies[0].mentioned_addresses == ()
    assert replies[1].mentioned_addresses == ("ou_bot",)


@pytest.mark.asyncio
async def test_a_reply_an_agent_wrote_carries_no_mentions():
    """Same filter as the comment-level set: a mention an agent typed is not a
    signal, at either level of the thread.
    """
    p = FeishuCommenter(profile="p", self_open_id="ou_bot",
                        agent_roster=("ou_agentB",))
    p.cli = _StubCli({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_human",
        "reply_list": {"replies": [
            {"reply_id": "r1", "user_id": "ou_agentB", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
            ]}},
            {"reply_id": "r2", "user_id": "ou_bot", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
            ]}},
        ]},
    }]}})
    c = (await p.list_comments(DOC))[0]
    assert c.mentioned_addresses == ()
    assert [r.mentioned_addresses for r in c.replies] == [()]


# ------------------------------------------------------------ the agent roster (§16.14)

def test_a_rostered_open_id_is_recognised_as_another_agent():
    """The Feishu payload carries no identity type, so a bot cannot be told from a
    person by inspection; the deployer's roster supplies that fact.
    """
    p = FeishuCommenter(profile="p", self_open_id="ou_bot",
                        agent_roster=("ou_agentB", "ou_agentC"))
    assert p._is_other_agent("ou_agentB") is True
    assert p._is_other_agent("ou_human") is False
    assert p._is_other_agent("") is False


@pytest.mark.asyncio
async def test_a_mention_an_agent_wrote_is_not_a_trigger_signal():
    """Counting a mention an agent posted would let an agent summon an agent -- the
    recruitment the loop prohibition forbids. Only a person's mention surfaces.
    """
    p = FeishuCommenter(profile="p", self_open_id="ou_bot",
                        agent_roster=("ou_agentB",))
    p.cli = _StubCli({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_human",
        "reply_list": {"replies": [
            # A human reply, mentioning the bot: this is a real summons.
            {"reply_id": "r1", "user_id": "ou_human", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
            ]}},
            # Another agent's reply, also mentioning the bot: must NOT count.
            {"reply_id": "r2", "user_id": "ou_agentB", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
            ]}},
        ]},
    }]}})
    c = (await p.list_comments(DOC))[0]
    # The human's mention counts; the agent's reply carries none.
    assert c.mentioned_addresses == ("ou_bot",)
    assert [r.mentioned_addresses for r in c.replies] == [()]
    assert summons(c, "ou_bot") is True


@pytest.mark.asyncio
async def test_a_mention_only_an_agent_wrote_surfaces_nothing():
    p = FeishuCommenter(profile="p", self_open_id="ou_bot",
                        agent_roster=("ou_agentB",))
    p.cli = _StubCli({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_human",
        "reply_list": {"replies": [
            {"reply_id": "r1", "user_id": "ou_agentB", "content": {"elements": [
                {"type": "person", "person": {"user_id": "ou_bot"}},
            ]}},
        ]},
    }]}})
    comments = await p.list_comments(DOC)
    assert comments[0].mentioned_addresses == ()


class _PagedCli(_StubCli):
    """Answers listings page by page, keyed by --page-token."""

    def __init__(self, pages: dict) -> None:
        super().__init__()
        self.pages = pages

    async def json(self, args, *, timeout=None):
        self.calls.append(list(args))
        key = " ".join(args[:2])
        token = args[args.index("--page-token") + 1] if "--page-token" in args else ""
        return self.pages.get((key, token), {})


@pytest.mark.asyncio
async def test_every_page_of_comments_and_of_a_long_thread_is_read():
    """The CLI does not paginate: one call is one page of cards, and a card embeds
    only the first page of its replies. A later comment, or a mention in reply 51,
    must still be seen.
    """
    p, _ = _provider()

    def card(cid, replies, more=False):
        return {"comment_id": cid, "user_id": "ou_p", "has_more": more,
                "reply_list": {"replies": replies}}

    def reply(rid, text):
        return {"reply_id": rid, "user_id": "ou_p",
                "content": {"elements": [{"type": "text_run", "text_run": {"text": text}}]}}

    p.cli = _PagedCli({
        ("drive +list-comments", ""): {"items": [card("first", [reply("r1", "a")], more=True)],
                                       "has_more": True, "page_token": "t2"},
        ("drive +list-comments", "t2"): {"items": [card("second", [reply("r9", "z")])],
                                         "has_more": False},
        ("drive +list-replies", ""): {"items": [reply("r1", "a"), reply("r2", "b")],
                                      "has_more": True, "page_token": "rt2"},
        ("drive +list-replies", "rt2"): {"items": [reply("r3", "c")], "has_more": False},
    })
    p._kind_cache[DOC] = "document"
    comments = await p.list_comments(DOC)
    assert [c.comment_id for c in comments] == ["first", "second"]
    # The re-read thread repeats the opener r1, which is the comment, not a reply.
    assert comments[0].content == "a"
    assert [r.reply_id for r in comments[0].replies] == ["r2", "r3"]
    assert comments[1].content == "z"
    assert comments[1].replies == ()


# ------------------------------------------- review of #11730: paging, replies, self


class _EndlessCli(_StubCli):
    """A listing that always says there is more, with the token ``make`` returns."""

    def __init__(self, make_token) -> None:
        super().__init__()
        self.make_token = make_token

    async def json(self, args, *, timeout=None):
        self.calls.append(list(args))
        return {"items": [], "has_more": True, "page_token": self.make_token(len(self.calls))}


@pytest.mark.asyncio
@pytest.mark.parametrize("make_token,calls", [
    (lambda n: "", 1),
    (lambda n: "same", 2),
    (lambda n: f"t{n}", 50),
], ids=["more-without-token", "repeated-token", "never-ends"])
async def test_a_listing_that_cannot_finish_raises_rather_than_truncating(make_token, calls):
    """The contract is to page to the end or raise. A listing that says there is more
    but gives no new token, or never ends, raises after a bounded number of requests
    instead of handing back part of the list as if it were all of it.
    """
    p, _ = _provider()
    p.cli = _EndlessCli(make_token)
    p._kind_cache[DOC] = "document"
    with pytest.raises(ProviderError) as caught:
        await p.list_comments(DOC)
    assert caught.value.kind == "transport"
    assert len([c for c in p.cli.calls if "+list-comments" in c]) == calls


@pytest.mark.asyncio
async def test_the_reply_verbs_issue_the_documented_commands():
    """--content is a JSON element array (plain text is refused by the CLI), carried
    without escaping non-ASCII; each verb names the comment and, where it applies,
    the reply.
    """
    p, stub = _provider({"drive +add-reply": {"reply_id": "r9"}})
    p._kind_cache[DOC] = "document"
    assert await p.reply_comment(DOC, "c1", "已处理") == "r9"
    await p.update_reply(DOC, "c1", "r9", "改为：已完成")
    await p.delete_reply(DOC, "c1", "r9")
    add, update, delete = stub.calls[-3:]
    payload = add[add.index("--content") + 1]
    assert json.loads(payload) == [{"type": "text", "text": "已处理"}]
    assert "已处理" in payload
    assert add[:2] == ["drive", "+add-reply"] and add[add.index("--comment-id") + 1] == "c1"
    assert update[:2] == ["drive", "+update-reply"]
    assert update[update.index("--reply-id") + 1] == "r9"
    assert delete[:2] == ["drive", "+delete-reply"]
    assert delete[delete.index("--reply-id") + 1] == "r9"
    assert "--content" not in delete


@pytest.mark.asyncio
async def test_deleting_a_reply_learns_a_wiki_container_like_the_other_verbs():
    """A wiki-hosted document addressed as a docx answers "not exist". Deleting a
    reply retries once as wiki, as answering and editing do, instead of failing.
    """
    class _WikiOnlyCli(_StubCli):
        async def json(self, args, *, timeout=None):
            self.calls.append(list(args))
            if args[args.index("--type") + 1] != "wiki":
                raise ProviderError("not_found", "file does not exist")
            return {}

    p, _ = _provider()
    p.cli = _WikiOnlyCli()
    await p.delete_reply(DOC, "c1", "r1")
    assert [c[c.index("--type") + 1] for c in p.cli.calls] == ["docx", "wiki"]


@pytest.mark.asyncio
async def test_the_bots_own_id_is_known_before_authors_are_judged():
    """With no preset open id, the watcher's first call is the comment listing. The
    bot's own id is fetched before the walk, so its own comment is not read as a
    person's.
    """
    p, _ = _provider({
        "whoami": {"open_id": "ou_me"},
        "drive +list-comments": {"items": [{
            "comment_id": "c1", "user_id": "ou_me",
            "reply_list": {"replies": [
                {"reply_id": "r1", "user_id": "ou_me", "content": {"elements": []}},
                {"reply_id": "r2", "user_id": "ou_me", "content": {"elements": []}},
            ]},
        }]},
    })
    p._self_open_id = ""
    p._kind_cache[DOC] = "document"
    comments = await p.list_comments(DOC)
    assert comments[0].author_is_self is True
    assert comments[0].replies[0].author_is_self is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,raw,quoted", [
    ("spreadsheet", "D2 16oAxy-nB", "16oAxy-nB"),
    ("document", "D2 16oAxy-nB", "D2 16oAxy-nB"),
    ("spreadsheet", "D2 C3 total", "C3 total"),
    ("spreadsheet", "Total 5", "Total 5"),
    ("spreadsheet", "C3", "C3"),
    ("", "D2 16oAxy-nB", "D2 16oAxy-nB"),
], ids=[
    "sheet-strips-the-address", "prose-keeps-every-character",
    "only-the-leading-address-is-taken", "a-word-is-not-an-address",
    "an-address-without-text-is-kept", "unknown-format-keeps-every-character",
])
async def test_a_cell_address_is_stripped_from_a_quote_only_on_a_grid(kind, raw, quoted):
    """A spreadsheet quote arrives as "<A1 address> <text>"; only there is the address
    stripped, and only the leading one, so a cell whose own text opens with something
    address-shaped keeps it. A document's quote, or one whose format is unknown, keeps
    every character.
    """
    p, _ = _provider({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_p", "quote": raw,
        "reply_list": {"replies": []},
    }]}})
    p._kind_cache[DOC] = kind
    comments = await p.list_comments(DOC)
    assert comments[0].quoted_text == quoted


class _MetasFail(_StubCli):
    """The platform answers comments, but the metadata query fails."""

    async def json(self, args, *, timeout=None):
        if args[:2] == ["drive", "metas"]:
            self.calls.append(list(args))
            raise ProviderError("transport", "metadata unavailable")
        return await super().json(args, timeout=timeout)


@pytest.mark.asyncio
async def test_a_failed_format_lookup_is_logged_and_the_quote_kept(caplog):
    """Without the format, a spreadsheet quote keeps its cell prefix and anchors as
    text. That is degraded, so it is logged as a warning rather than passing unseen.

    Through the real path: the metadata CLI call fails, the reader swallows it and
    ``doc_kind`` answers its "document" default, so the warning must not depend on an
    exception reaching the comment layer.
    """
    p, _ = _provider()
    p.cli = _MetasFail({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_p", "quote": "D2 16oAxy-nB",
        "reply_list": {"replies": []},
    }]}})
    with caplog.at_level("WARNING"):
        comments = await p.list_comments(DOC)
    assert any(c[:2] == ["drive", "metas"] for c in p.cli.calls)
    assert comments[0].quoted_text == "D2 16oAxy-nB"
    assert any("not known" in r.getMessage() for r in caplog.records)
    assert DOC not in p._kind_cache, "a failed lookup is not cached"


@pytest.mark.asyncio
async def test_a_known_format_lists_without_a_warning(caplog):
    """A format already learned, or answered by the metadata query, logs nothing."""
    p, _ = _provider({"drive +list-comments": {"items": []}})
    p._kind_cache[DOC] = "document"
    with caplog.at_level("WARNING"):
        await p.list_comments(DOC)
    assert not any("not known" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_thread_that_cannot_be_read_whole_fails_the_listing():
    """A card whose thread cannot be re-read is not answered from its first page: the
    bot's own reply may sit further down, and missing it would answer the thread twice.
    """
    class _RepliesFail(_StubCli):
        async def json(self, args, *, timeout=None):
            self.calls.append(list(args))
            if args[:2] == ["drive", "+list-replies"]:
                raise ProviderError("permission", "no access to replies")
            return {"items": [{
                "comment_id": "c1", "user_id": "ou_p", "has_more": True,
                "reply_list": {"replies": [{"reply_id": "r1", "user_id": "ou_p"}]},
            }]}

    p, _ = _provider()
    p.cli = _RepliesFail()
    p._kind_cache[DOC] = "document"
    with pytest.raises(ProviderError) as e:
        await p.list_comments(DOC)
    assert e.value.kind == "permission"


@pytest.mark.asyncio
async def test_the_comment_is_not_counted_among_its_replies():
    """The opening message is the comment. A bot-opened comment with no reply under it
    must not look like a thread the bot has already answered.
    """
    p, _ = _provider({"drive +list-comments": {"items": [{
        "comment_id": "c1", "user_id": "ou_bot",
        "reply_list": {"replies": [
            {"reply_id": "r1", "user_id": "ou_bot", "content": {"elements": [
                {"type": "text_run", "text_run": {"text": "Please review"}},
            ]}},
        ]},
    }]}})
    p._kind_cache[DOC] = "document"
    c = (await p.list_comments(DOC))[0]
    assert c.content == "Please review"
    assert c.replies == ()
    assert not any(r.author_is_self for r in c.replies)


def _lark_binary():
    import shutil

    return shutil.which("lark-cli")


@pytest.mark.skipif(_lark_binary() is None, reason="lark-cli 未安装")
@pytest.mark.parametrize("args", [
    ["drive", "+update-reply", "--token", "T", "--type", "docx", "--comment-id", "c1",
     "--reply-id", "r1", "--content", '[{"type":"text","text":"hi"}]'],
    ["drive", "+delete-reply", "--token", "T", "--type", "docx", "--comment-id", "c1",
     "--reply-id", "r1"],
], ids=["update-reply", "delete-reply"])
def test_the_cli_accepts_the_reply_editing_commands(args):
    """Checked against the installed binary with --dry-run, which rejects an unknown
    verb or flag without calling the platform (see the cli tests).
    """
    import subprocess

    res = subprocess.run([_lark_binary(), *args, "--as", "bot", "--dry-run"],
                         capture_output=True, text=True, timeout=60)
    text = (res.stderr or res.stdout or "").strip()
    assert text, "CLI 没有输出"
    payload = json.loads(text)
    assert (payload.get("error") or {}).get("type") != "validation", payload.get("error")
