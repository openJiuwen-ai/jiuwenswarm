"""The chat path's routing provider: one surface, every connection's documents."""

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocSnapshot, DocSummary, EditResult, ProviderError,
)
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.routing import RoutingProvider


class _Fake:
    def __init__(self, kind, docs, *, address):
        self.kind = kind
        self.docs = docs
        self.address = address
        self.calls = []
        self.receipt_sink = None
        self.receipt_meta = None
        self.kinds = {}

    def parse_doc_ref(self, s):
        s = s.strip()
        if self.kind == "google":
            if "/document/d/" in s:
                return s.split("/document/d/")[1].split("/")[0]
            return s
        for marker in ("/docx/", "/sheets/"):
            if marker in s:
                return s.split(marker)[1].split("?")[0].split("/")[0]
        if "/" in s:
            raise ProviderError("invalid", "not mine")
        return s

    async def read(self, ref):
        self.calls.append(("read", ref))
        if ref not in self.docs:
            raise ProviderError("not_found", f"{self.kind} has no {ref}")
        return DocSnapshot(doc_id=ref, kind="document", revision_id="r", text=f"{self.kind}:{ref}")

    async def edit_batch(self, ref, edits, *, required_revision_id, window=None, highlight=False):
        self.calls.append(("edit", ref))
        return EditResult("applied", new_revision_id="r2", receipt_id=f"rcpt-{self.kind}")

    async def list_accessible_documents(self):
        return [DocSummary(doc_id=d, title=f"{self.kind}-{d}", can_edit=True, kind="document") for d in self.docs]

    async def create_document(self, title):
        return f"new-{self.kind}"

    def note_kind(self, ref, kind):
        self.kinds[ref] = kind


def _rig():
    g = _Fake("google", ["gdoc1", "gdoc2"], address="sa@x.iam")
    f = _Fake("feishu", ["FsTok1"], address="ou_1")
    conns = [("g.json", g), ("f.json", f)]
    docs = {"g.json": ["gdoc1", "gdoc2"], "f.json": ["FsTok1"]}
    return g, f, RoutingProvider(conns, lambda cf: docs.get(cf, []))


@pytest.mark.asyncio
async def test_a_document_is_read_through_the_connection_that_adopted_it():
    g, f, r = _rig()
    assert (await r.read("FsTok1")).text == "feishu:FsTok1"
    assert (await r.read("gdoc2")).text == "google:gdoc2"
    assert g.calls == [("read", "gdoc2")] and f.calls == [("read", "FsTok1")]


def test_a_pasted_link_routes_by_the_adopted_list_before_the_host():
    g, f, r = _rig()
    assert r.parse_doc_ref("https://x.feishu.cn/docx/FsTok1?from=chat") == "FsTok1"
    assert r.parse_doc_ref("https://docs.google.com/document/d/gdoc1/edit") == "gdoc1"
    # A link nobody lists routes by the platform it names, then is remembered.
    assert r.parse_doc_ref("https://x.feishu.cn/docx/Unknown9") == "Unknown9"
    assert r.owner("Unknown9") is f
    # A bare token nobody lists goes to the first connection, as before.
    assert r.owner("mystery") is g


@pytest.mark.asyncio
async def test_listings_are_the_union_and_creation_goes_to_the_first_connection():
    g, f, r = _rig()
    titles = sorted(s.title for s in await r.list_accessible_documents())
    assert titles == ["feishu-FsTok1", "google-gdoc1", "google-gdoc2"]
    assert await r.create_document("t") == "new-google"
    assert r.owner("new-google") is g


@pytest.mark.asyncio
async def test_receipt_plumbing_reaches_every_child():
    g, f, r = _rig()
    sink = object()
    r.receipt_sink = sink
    r.receipt_meta = {"executor": "chat"}
    assert g.receipt_sink is sink and f.receipt_sink is sink
    assert g.receipt_meta == {"executor": "chat"} and f.receipt_meta == {"executor": "chat"}
    r.receipt_meta = None
    assert g.receipt_meta is None and f.receipt_meta is None
    out = await r.edit_batch("FsTok1", [("a", "b")], required_revision_id="r")
    assert out.receipt_id == "rcpt-feishu"


def test_format_priming_lands_on_the_owner():
    g, f, r = _rig()
    r.note_kind("FsTok1", "spreadsheet")
    r.note_kind("gdoc1", "presentation")
    assert f.kinds == {"FsTok1": "spreadsheet"} and g.kinds == {"gdoc1": "presentation"}


def test_adoption_is_read_live():
    g, f, _ = _rig()
    docs = {"g.json": ["gdoc1"], "f.json": []}
    r = RoutingProvider([("g.json", g), ("f.json", f)], lambda cf: docs.get(cf, []))
    assert r.owner("LateTok") is g
    docs["f.json"] = ["LateTok"]           # the panel adopts it mid-session
    assert r.owner("LateTok") is f


@pytest.mark.asyncio
async def test_the_format_question_reaches_the_owner():
    g, f, r = _rig()
    g.doc_kind = None  # would raise if consulted for a Feishu token
    async def fk(ref): return "spreadsheet"
    f.doc_kind = fk
    assert await r.doc_kind("FsTok1") == "spreadsheet"


def test_a_platform_choice_names_the_creating_connection():
    g, f, r = _rig()
    assert r.for_platform("feishu") is f and r.for_platform("google") is g
    assert r.for_platform("wps") is None
    r.learn("NewTok", f)
    assert r.owner("NewTok") is f


def test_the_shared_builder_returns_the_plain_provider_for_one_connection():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.routing import build_routed_provider
    g = _Fake("google", ["gdoc1"], address="sa@x.iam")
    built = []
    def build(cf, *, agent_roster=()):
        built.append(cf)
        return g
    prov, first = build_routed_provider(
        [{"credentials_file": "g.json", "documents": ["gdoc1"]}],
        build=build, live_specs=lambda: [],
    )
    assert prov is g and first == "g.json" and built == ["g.json"]


def test_the_shared_builder_routes_several_connections_and_skips_a_bad_key():
    """The first connection must build (it is the turn's identity); a later one whose
    key cannot be read is skipped so the others stay reachable."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.routing import (
        RoutingProvider, all_adopted_documents, build_routed_provider,
    )
    g = _Fake("google", ["gdoc1"], address="sa@x.iam")
    f = _Fake("feishu", ["FsTok1"], address="ou_1")
    def build(cf, *, agent_roster=()):
        if cf == "bad.json":
            raise RuntimeError("unreadable")
        return {"g.json": g, "f.json": f}[cf]
    specs = [
        {"credentials_file": "g.json", "documents": ["gdoc1"]},
        {"credentials_file": "bad.json", "documents": ["x"]},
        {"credentials_file": "f.json", "documents": ["FsTok1"]},
    ]
    prov, first = build_routed_provider(specs, build=build, live_specs=lambda: specs)
    assert isinstance(prov, RoutingProvider) and first == "g.json"
    assert prov.owner("FsTok1") is f and prov.owner("gdoc1") is g
    assert all_adopted_documents(lambda: specs) == ["gdoc1", "x", "FsTok1"]


def test_the_shared_builder_fails_when_the_first_connection_cannot_build():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.routing import build_routed_provider
    def build(cf, *, agent_roster=()):
        raise RuntimeError("unreadable")
    with pytest.raises(RuntimeError):
        build_routed_provider([{"credentials_file": "g.json", "documents": []}],
                              build=build, live_specs=lambda: [])


@pytest.mark.asyncio
async def test_add_page_reaches_the_connection_that_owns_the_document():
    """A per-document call must route by owner, never fall through to the first connection.

    Observed live 2026-09-10: ``add_page`` was not in the routed list, so a request for
    a Feishu deck reached the Google provider, which looked the token up on Drive and
    answered not_found -- while ``read`` on the same deck, being routed, worked. The
    model reported "a platform-side limit" it could not work around.
    """
    g, f, r = _rig()
    async def add_page(self, ref, title=""):
        self.calls.append(("add_page", ref, title))
        return f"{self.kind}-page"
    _Fake.add_page = add_page
    try:
        assert await r.add_page("FsTok1", "第二页") == "feishu-page"
        assert ("add_page", "FsTok1", "第二页") in f.calls and not g.calls
    finally:
        del _Fake.add_page


def test_every_per_document_method_on_the_contract_is_routed_explicitly():
    """The structural version of the test above, so the next method cannot slip through.

    ``__getattr__`` answers from the first connection, which is right for a cache or a
    flag and wrong for anything that takes a document. Every coroutine on the provider
    contract whose first parameter is ``doc_ref`` must therefore be defined on the
    routing class itself.
    """
    import inspect
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocProvider

    per_doc = sorted(
        name for name, fn in inspect.getmembers(DocProvider, inspect.isfunction)
        if not name.startswith("_")
        and list(inspect.signature(fn).parameters)[:2] == ["self", "doc_ref"]
    )
    missing = [n for n in per_doc if n not in RoutingProvider.__dict__]
    assert not missing, f"这些按文档的方法没有显式路由，会落到第一个连接：{missing}"


@pytest.mark.asyncio
async def test_the_union_listing_seeds_each_child_with_its_own_adopted_documents():
    """Feishu finds the wiki space to walk from the nodes it already manages. The routed
    listing forwarded nothing, so the chat path's discovery on that platform depended
    on a warm process cache -- the cold-path hole the panel had already closed."""
    class _Seeded(_Fake):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.seen = None

        async def list_accessible_documents(self, known=()):
            self.seen = list(known)
            return [DocSummary(doc_id=d, title=f"{self.kind}-{d}", can_edit=True, kind="document")
                    for d in self.docs]

    g = _Fake("google", ["gdoc1"], address="sa@x.iam")      # the older one-argument contract
    f = _Seeded("feishu", ["FsTok1"], address="ou_1")
    docs = {"g.json": ["gdoc1"], "f.json": ["FsTok1", "FsTok2"]}
    r = RoutingProvider([("g.json", g), ("f.json", f)], lambda cf: docs.get(cf, []))

    titles = sorted(s.title for s in await r.list_accessible_documents())
    assert titles == ["feishu-FsTok1", "google-gdoc1"]
    assert f.seen == ["FsTok1", "FsTok2"], "子连接应收到自己的纳管清单"

    await r.list_accessible_documents(known=["X"])
    assert f.seen == ["X"], "显式传入的 known 优先"


# ------------------------- review: ownership is by exact canonical id


def test_a_prefix_or_substring_of_an_adopted_id_routes_nowhere():
    """An id is opaque: a token that contains another, or is contained in it, is a
    different document. Substring matching once routed by that accident."""
    g, f, r = _rig()
    assert r._by_docs("FsTok1") is f
    assert r._by_docs("FsTok10") is None, "多一位的 token 不是同一篇"
    assert r._by_docs("FsTok") is None, "前缀不是同一篇"
    assert r._by_docs("gdoc") is None
    assert r._by_docs("gdoc10") is None
    assert r._by_docs("see FsTok1 here") is None, "嵌在别的文字里的 id 不是引用"


def test_a_link_and_its_bare_token_route_to_the_same_connection():
    g, f, r = _rig()
    assert r._by_docs("https://x.feishu.cn/docx/FsTok1?from=space") is f
    assert r._by_docs("https://docs.google.com/document/d/gdoc2/edit") is g


def test_the_address_is_the_owning_connections(tmp_path):
    import json as _json

    gkey = tmp_path / "g.json"
    gkey.write_text(_json.dumps({"type": "service_account", "client_email": "sa@x.iam"}))
    fkey = tmp_path / "f.json"
    fkey.write_text(_json.dumps({"app_id": "cli_x", "app_secret": "s", "bot_open_id": "ou_1"}))
    g = _Fake("google", ["gdoc1"], address="sa@x.iam")
    f = _Fake("feishu", ["FsTok1"], address="ou_1")
    docs = {str(gkey): ["gdoc1"], str(fkey): ["FsTok1"]}
    r = RoutingProvider([(str(gkey), g), (str(fkey), f)], lambda cf: docs.get(cf, []))
    assert r.address_for("FsTok1") == "ou_1"
    assert r.address_for("gdoc1") == "sa@x.iam"
    assert r.address_for("nobody-lists-this") == ""
