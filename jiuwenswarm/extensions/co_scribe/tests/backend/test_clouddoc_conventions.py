"""Offline tests for the document-conventions subsystem."""

from __future__ import annotations

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocComment, DocReply
from jiuwenswarm.extensions.co_scribe.backend.host.conventions import (
    MAX_CONVENTIONS_CHARS, needs_ack, render_ack, select_conventions,
)
from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import TriggerConfig

CFG = TriggerConfig(sa_address="co-scribe@x.iam.gserviceaccount.com")


def C(cid, content, *, me=False, t="2026-01-01T00:00:00.000Z", resolved=False, replies=()):
    return DocComment(comment_id=cid, author_is_self=me, author_display_name="X",
                      created_time=t, content=content, quoted_text="", resolved=resolved,
                      replies=tuple(replies))


def test_marker_prefix_is_the_only_criterion():
    got = select_conventions([C("c1", "co-scribe 约定：正式语域\n产品名不译")], CFG)
    assert got.source == "in_doc" and got.item_count == 2
    assert select_conventions([C("c1", "请大家注意语域要正式")], CFG) is None


def test_replies_are_never_conventions():
    """Otherwise anyone with comment access could inject policy with one reply."""
    c = C("c1", "普通评论", replies=[DocReply("r1", False, "X", "t", "co-scribe 约定：改规则")])
    assert select_conventions([c], CFG) is None


def test_agent_authored_conventions_are_ignored():
    """Stops the agent's own text, written through reply_comment, from becoming policy."""
    assert select_conventions([C("c1", "co-scribe 约定：随便改", me=True)], CFG) is None


def test_earliest_wins_so_replanting_cannot_silently_override():
    got = select_conventions([
        C("c2", "co-scribe 约定：后植的规则", t="2026-01-02T00:00:00.000Z"),
        C("c1", "co-scribe 约定：原始规则", t="2026-01-01T00:00:00.000Z"),
    ], CFG)
    assert got.comment_id == "c1" and "原始规则" in got.text



def test_truncation_is_on_line_boundary():
    """Half a rule would still be followed as a whole one, so truncation goes by line."""
    text = "\n".join(f"第{i}条规则" for i in range(2000))
    got = select_conventions([C("c1", "co-scribe 约定：\n" + text)], CFG)
    assert got.truncated and len(got.text) <= MAX_CONVENTIONS_CHARS
    assert not got.text.endswith("第")  # no half line


def test_resolved_conventions_are_retired():
    assert select_conventions([C("c1", "co-scribe 约定：x", resolved=True)], CFG) is None


def test_ack_fires_on_first_effect_and_on_hash_change():
    cur = select_conventions([C("c1", "co-scribe 约定：甲")], CFG)
    assert needs_ack(None, cur) is True                                  # first time it takes effect
    acked = {"hash": cur.content_hash, "acked_at": "t"}
    assert needs_ack(acked, cur) is False                                # a restart does not repeat it
    changed = select_conventions([C("c1", "co-scribe 约定：乙")], CFG)
    assert needs_ack(acked, changed) is True                             # changed content is acknowledged again
    assert needs_ack(acked, None) is False                               # retirement posts nothing


def test_ack_text_reports_count_and_truncation():
    cur = select_conventions([C("c1", "co-scribe 约定：甲\n乙\n丙")], CFG)
    assert "3 条" in render_ack(cur)


# ------------------------------------------------------------ config consistency
#
# A config key with no implementation behind it is worse than no key: a user follows
# the documentation and nothing happens. Two of these existed here once -- batch
# marking and the release word each had a key and documentation but no code.


def test_every_shipped_config_key_is_consumed():
    """Every key in config.yaml's clouddoc section must actually be read.

    The scan set is **derived from directories, never a hard-coded file list**. It was
    hard-coded once, and the cost was a false report on ``connections``: the module
    reading it is provider.py on the agentserver side -- the very module holding the
    cross-process config schema -- and it was not on the list. Miss one reader and this
    stops being a guard against unimplemented keys and becomes noise about keys not
    being in the few files it happens to look at.

    What it compares are **string literals in the AST**, excluding two kinds of
    appearance that are not reads: docstrings, and write subscripts such as
    ``section["connections"] = ...``. It used to search the source text for substrings,
    so a comment saying "``connections:`` is the new shape" was enough to satisfy it.
    Excluding docstrings alone was not enough either, since the panel writing a key back
    to the config leaves a literal with the same name. With both closed, deleting the
    reader for any key turns this assertion red.
    """
    import ast
    import glob

    import yaml

    with open("jiuwenswarm/resources/config.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)["clouddoc"]
    keys = {k for k in cfg if k != "rail"} | set(cfg.get("rail") or {})

    # Recursive: both halves have subpackages now (host/authority, host/watch,
    # host/panel; toolkit/providers, toolkit/rails), and a key read only from one
    # of those would otherwise read as unread.
    files = (
        glob.glob("jiuwenswarm/extensions/co_scribe/backend/host/**/*.py", recursive=True)
        + glob.glob("jiuwenswarm/extensions/co_scribe/backend/toolkit/**/*.py", recursive=True)
        + ["jiuwenswarm/gateway/app_gateway.py",
           "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py"]
    )
    used: set[str] = set()
    for f in files:
        with open(f, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        skip = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)
        }
        skip |= {
            id(n.slice)
            for n in ast.walk(tree)
            if isinstance(n, ast.Subscript) and not isinstance(n.ctx, ast.Load)
        }
        used |= {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
        }

    unread = sorted(keys - used)
    assert not unread, f"配置里这些键没有任何代码读取：{unread}"


def test_trigger_config_fields_all_come_from_config():
    """The reverse: every configurable field of TriggerConfig needs a key in
    config.yaml."""
    import dataclasses
    import yaml

    from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import TriggerConfig

    with open("jiuwenswarm/resources/config.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)["clouddoc"]
    # One field is derived per connection rather than read from config: ``sa_address``
    # comes from the credentials. It is set via ``replace``, so it needs no config key.
    derived = {"sa_address"}
    fields = {f.name for f in dataclasses.fields(TriggerConfig)} - derived
    assert fields <= set(cfg), f"这些字段没有配置项：{sorted(fields - set(cfg))}"
