"""The Docs panel service layer -- panel.py and connections.py.

The panel holds no state, so everything here tests **whether the composition is
right**: whether an action lands in all three places -- the registry's memory, the
state file, config.yaml -- and whether classification agrees with the admission check.
"""

import asyncio
import json
from dataclasses import replace

import pytest

from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip
import yaml

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    AgentIdentity,
    DocComment,
    DocCapabilities,
    DocSummary,
    ProviderError,
)
from jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher import WatcherConfig
from jiuwenswarm.extensions.co_scribe.backend.host.authority.connections import (
    CloudDocConnections,
    read_connection_specs,
)
from jiuwenswarm.extensions.co_scribe.backend.host.cursor_store import CloudDocStore
from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import CloudDocPanel
from jiuwenswarm.extensions.co_scribe.backend.host.watch.triggers import TriggerConfig

SA = "co-scribe@x.iam.gserviceaccount.com"
SA2 = "scribe-b@y.iam.gserviceaccount.com"
DOC = "1AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKK"
DOC2 = "2BBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLL"


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def _comment(cid: str, body: str, *, assignee: str = "", resolved: bool = False):
    """One comment as the provider would report it."""
    return DocComment(
        comment_id=cid, author_is_self=False, author_display_name="someone",
        created_time="2026-01-01T00:00:00Z", content=body, quoted_text="",
        resolved=resolved, assignee_address=assignee or None,
    )


class PanelFakeProvider:
    """The panel uses a narrow slice of a provider: parse, capabilities, title,
    self_identity, doc_url.

    The address is derived from the credentials filename, since the registry builds a
    provider per key -- in these tests one filename is one identity.
    """

    def doc_url(self, doc_id, kind=""):
        # A link is the platform's to build, so the panel asks rather than templating
        # one. Present here because a provider missing it took the panel down.
        return f"https://example.test/{kind or 'doc'}/{doc_id}"

    def __init__(self, credentials_file: str):
        self.credentials_file = credentials_file
        self.address = {"k1.json": SA, "k2.json": SA2}.get(
            credentials_file.rsplit("/", 1)[-1], f"{credentials_file.rsplit('/', 1)[-1]}@fake"
        )
        self.caps = DocCapabilities(
            can_read=True, can_edit=True, can_comment=True, can_resolve=True,
            has_revision_control=True, max_quote_chars=418,
        )
        self.doc_title = "示例文档"
        self.cap_calls = 0
        self.accessible: list = []          # 共享给这个账号的文档

    @property
    def kind(self):
        return "fake"

    async def list_shared_unsupported(self):
        return list(getattr(self, "unsupported", []))

    async def list_accessible_documents(self):
        return list(self.accessible)

    def parse_doc_ref(self, url_or_id):
        if "docs.google.com" in url_or_id:
            return url_or_id.split("/d/")[1].split("/")[0]
        if len(url_or_id) > 20:
            return url_or_id
        raise ProviderError("invalid", f"无法解析 {url_or_id!r}")

    async def self_identity(self):
        return AgentIdentity(display_name=self.address, address=self.address)

    async def capabilities(self, doc_ref):
        self.cap_calls += 1
        if isinstance(self.caps, Exception):
            raise self.caps
        return self.caps

    async def title(self, doc_ref):
        return self.doc_title

    async def list_comments(self, doc_ref, *, include_resolved=False):
        # Nothing in the panel reads comments now that the backlog view is gone,
        # so the list stays empty unless a test sets it.
        return list(getattr(self, "comments", []))


@pytest.fixture
def kit(tmp_path):
    """A panel already holding one connection (k1.json to a service account). The key file
    genuinely exists under tmp."""
    store = CloudDocStore(tmp_path / "state.json", now_fn=Clock())
    providers: dict[str, PanelFakeProvider] = {}

    def factory(path: str) -> PanelFakeProvider:
        providers[path.rsplit("/", 1)[-1]] = p = PanelFakeProvider(path)
        return p

    async def dispatch(doc_id, comment_id, metadata):
        return "ok"

    reg = CloudDocConnections(
        store=store,
        dispatcher=dispatch,
        watcher_cfg=WatcherConfig(),
        base_trigger_cfg=TriggerConfig(sa_address=""),
        provider_factory=factory,
        now_fn=Clock(),
    )
    for name in ("k1.json", "k2.json"):
        (tmp_path / name).write_text(json.dumps({"client_email": name}))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("clouddoc:\n  enabled: true\n  documents: []\n")
    panel = CloudDocPanel(reg, config_path=cfg)

    class Kit:
        pass

    k = Kit()
    k.panel, k.reg, k.store, k.cfg, k.tmp = panel, reg, store, cfg, tmp_path
    k.providers = providers
    return k


async def _with_conn1(k):
    await k.reg.add(str(k.tmp / "k1.json"))
    return k.providers["k1.json"]


def _yaml_connections(cfg):
    return (yaml.safe_load(cfg.read_text()).get("clouddoc") or {}).get("connections")


# ------------------------------------------------------------------ document actions


@pytest.mark.asyncio
async def test_add_doc_lands_in_all_three_places(kit):
    """A successful add lands in all three: the watcher's memory, config.yaml, and the
    title cache."""
    await _with_conn1(kit)
    out = await kit.panel.add_doc(f"https://docs.google.com/document/d/{DOC}/edit?tab=t.0")
    assert out["result"] == "ok"
    assert DOC in kit.reg.list()[0].watcher._docs
    assert _yaml_connections(kit.cfg)[0]["documents"] == [DOC]
    rows = await kit.panel.list_docs()
    assert rows[0]["title"] == "示例文档"
    assert rows[0]["status"] == "ok"
    assert rows[0]["connection_id"] == kit.reg.list()[0].id


@pytest.mark.asyncio
async def test_comment_only_doc_is_refused_not_added(kit):
    """Comment-only access is refused, the same verdict admission reaches."""
    prov = await _with_conn1(kit)
    prov.caps = replace(prov.caps, can_edit=False, has_revision_control=False)
    out = await kit.panel.add_doc(DOC)
    assert out["result"] == "comment_only"
    assert kit.reg.all_docs() == []


@pytest.mark.asyncio
async def test_unshared_and_transient_are_distinguished(kit):
    prov = await _with_conn1(kit)
    prov.caps = ProviderError("forbidden", "not shared")
    assert (await kit.panel.add_doc(DOC))["result"] == "not_shared"
    prov.caps = ProviderError("rate_limited", "429")
    assert (await kit.panel.add_doc(DOC))["result"] == "unknown"


@pytest.mark.asyncio
async def test_add_doc_asks_every_connection_rather_than_the_first(kit):
    """Which identity can see a document is a fact, not a question for the person.

    The panel used to make you pick a connection before pasting a link, and
    defaulted to whichever came first. Picking the one the document was not shared
    with produced "not shared" -- a true statement about the wrong identity.
    """
    await kit.reg.add(str(kit.tmp / "k1.json"))
    await kit.reg.add(str(kit.tmp / "k2.json"))
    first, second = kit.providers["k1.json"], kit.providers["k2.json"]
    # The document is shared with the *second* connection only.
    first.caps = ProviderError("forbidden", "not shared")

    out = await kit.panel.add_doc(DOC)

    assert out["result"] == "ok"
    assert kit.reg.find_doc(DOC) is kit.reg.list()[1]
    assert second is not first


@pytest.mark.asyncio
async def test_add_doc_reports_the_most_informative_refusal(kit):
    """comment-only names a specific mistake with a specific fix, so it wins over
    not-shared, which is what every other identity would say anyway."""
    await kit.reg.add(str(kit.tmp / "k1.json"))
    await kit.reg.add(str(kit.tmp / "k2.json"))
    kit.providers["k1.json"].caps = ProviderError("forbidden", "not shared")
    kit.providers["k2.json"].caps = replace(
        kit.providers["k2.json"].caps, can_edit=False, has_revision_control=False
    )

    out = await kit.panel.add_doc(DOC)

    assert out["result"] == "comment_only"
    assert kit.reg.all_docs() == []


@pytest.mark.asyncio
async def test_there_is_no_remove_doc_only_the_watch_turned_off(kit):
    """Taking a document out of the list was a claim this deployment could not
    make. The share lives on the platform, so a local removal changed no fact
    anyone else could see, and the next discovery pass adopted the document again
    -- correctly, because it was still shared. What the panel can actually do is
    turn the watch off, which is the only thing this deployment can claim."""
    assert not hasattr(kit.panel, "remove_doc")
    assert hasattr(kit.panel, "watch_revoke") and hasattr(kit.panel, "watch_revoke_many")


@pytest.mark.asyncio
async def test_update_doc_thaws_a_fixed_document(kit):
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    await kit.store.note_permanent_failure(DOC, "comment_only_access")
    assert (await kit.panel.list_docs())[0]["status"] == "comment_only"
    assert (await kit.panel.update_doc(DOC))["result"] == "ok"
    assert (await kit.panel.list_docs())[0]["status"] == "ok"
    assert not await kit.store.is_frozen(DOC, 0.0)


@pytest.mark.asyncio
async def test_update_doc_does_not_thaw_on_transient_error(kit):
    prov = await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    await kit.store.note_permanent_failure(DOC, "comment_only_access")
    prov.caps = ProviderError("rate_limited", "429")
    assert (await kit.panel.update_doc(DOC))["result"] == "unknown"
    assert await kit.store.is_frozen(DOC, 0.0), "瞬态错误期间判决必须保持"


@pytest.mark.asyncio
async def test_list_docs_burns_no_quota(kit):
    prov = await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    before = prov.cap_calls
    for _ in range(5):
        await kit.panel.list_docs()
        await kit.panel.get_conf()
    assert prov.cap_calls == before, "list_docs/get_conf 打了实时 API"


@pytest.mark.asyncio
async def test_config_comments_survive_persistence(kit):
    """A round-trip write must preserve the comments in a user's config -- those are their
    own operational notes."""
    await _with_conn1(kit)
    kit.cfg.write_text(
        "clouddoc:\n"
        "  enabled: true\n"
        "  # 我的备忘：这里只放生产文档\n"
        "  documents: []\n"
    )
    await kit.panel.add_doc(DOC)
    assert "我的备忘" in kit.cfg.read_text()


# ------------------------------------------------------------------ connection actions


@pytest.mark.asyncio
async def test_add_connection_persists_and_hot_starts(kit):
    """Adding a connection lands in the registry and config.yaml together, and the watcher
    starts immediately."""
    await _with_conn1(kit)
    out = await kit.panel.add_connection(credentials_path=str(kit.tmp / "k2.json"))
    assert out["result"] == "ok"
    assert out["connection"]["agent_address"] == SA2
    assert out["connection"]["health"] == "idle"
    assert len(kit.reg.list()) == 2
    conns = _yaml_connections(kit.cfg)
    assert [c["credentials_file"].rsplit("/", 1)[-1] for c in conns] == ["k1.json", "k2.json"]
    # Hot start: the watcher task is already running
    assert kit.reg.list()[1].watcher._task is not None
    await kit.reg.stop_all()


@pytest.mark.asyncio
async def test_duplicate_address_is_refused(kit):
    """The same account twice means two watchers polling under one identity and every
    mention answered twice. Refused."""
    await _with_conn1(kit)
    out = await kit.panel.add_connection(credentials_path=str(kit.tmp / "k1.json"))
    assert out["result"] == "duplicate"
    assert len(kit.reg.list()) == 1


@pytest.mark.asyncio
async def test_missing_or_broken_key_is_refused(kit):
    await _with_conn1(kit)
    out = await kit.panel.add_connection(credentials_path=str(kit.tmp / "nope.json"))
    assert out["result"] == "invalid_key"
    out = await kit.panel.add_connection(credentials_json="not json at all")
    assert out["result"] == "invalid_key"
    out = await kit.panel.add_connection(credentials_json='{"no_email": 1}')
    assert out["result"] == "invalid_key"
    assert len(kit.reg.list()) == 1


@pytest.mark.asyncio
async def test_uploaded_key_lands_with_0600(kit):
    """An uploaded private key is written to its own file with owner-only permissions, and
    never into config.yaml."""
    await _with_conn1(kit)
    body = json.dumps({"client_email": "k2.json"})
    out = await kit.panel.add_connection(credentials_json=body, filename="team-b")
    assert out["result"] == "ok"
    path = kit.tmp / "clouddoc-keys" / "team-b.json"
    assert path.read_text() == body
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert body not in kit.cfg.read_text(), "私钥内容绝不能进 config.yaml"
    await kit.reg.stop_all()


@pytest.mark.asyncio
async def test_remove_connection_stops_watcher_and_persists(kit):
    await _with_conn1(kit)
    out = await kit.panel.add_connection(credentials_path=str(kit.tmp / "k2.json"))
    conn_id = out["connection"]["id"]
    await kit.panel.add_doc(DOC, connection_id=conn_id)

    removed = await kit.panel.remove_connection(conn_id)
    assert removed["result"] == "ok"
    assert len(kit.reg.list()) == 1
    assert kit.reg.all_docs() == []
    assert len(_yaml_connections(kit.cfg)) == 1
    # Its documents' state is collected
    assert not (await kit.store.doc_health(DOC))["failed"]


@pytest.mark.asyncio
async def test_doc_is_unique_across_connections(kit):
    """A document belongs to one connection. State and sessions are keyed by doc_id, so two
    identities on one document overwrite each other and every mention is answered
    twice."""
    await _with_conn1(kit)
    out2 = await kit.panel.add_connection(credentials_path=str(kit.tmp / "k2.json"))
    await kit.panel.add_doc(DOC)                       # 归第一个连接
    dup = await kit.panel.add_doc(DOC, connection_id=out2["connection"]["id"])
    assert dup["result"] == "exists"
    assert dup["connection_id"] == kit.reg.list()[0].id
    await kit.reg.stop_all()


# ------------------------------------------------------------------ config reading


def test_legacy_single_connection_config_still_reads():
    """The old top-level credentials_file and documents form folds into a one-element list,
    so a deployed config needs no edit."""
    legacy = {"credentials_file": "/k.json", "documents": ["a", "b"]}
    assert read_connection_specs(legacy) == [
        {"credentials_file": "/k.json", "documents": ["a", "b"]}
    ]
    new = {"connections": [{"credentials_file": "/k2.json", "documents": []}]}
    assert read_connection_specs(new) == [{"credentials_file": "/k2.json", "documents": []}]
    assert read_connection_specs({}) == []


@pytest.mark.asyncio
async def test_persist_upgrades_legacy_keys(kit):
    """The panel's first write upgrades the old keys into a connections list, leaving no
    second source of truth."""
    await _with_conn1(kit)
    kit.cfg.write_text(
        "clouddoc:\n  enabled: true\n  credentials_file: /old.json\n  documents: [x]\n"
    )
    await kit.panel.add_doc(DOC)
    data = yaml.safe_load(kit.cfg.read_text())["clouddoc"]
    assert "connections" in data
    assert "credentials_file" not in data
    assert "documents" not in data


# ------------------------------------------------------------------ connection health


@pytest.mark.asyncio
async def test_connection_health_aggregates_document_facts(kit):
    """A connection's light must be driven by facts. Four states: idle -- no documents is
    not health -- then ok, down and attention."""
    await _with_conn1(kit)
    conf = await kit.panel.get_conf()
    assert conf["connections"][0]["health"] == "idle"

    await kit.panel.add_doc(DOC)
    assert (await kit.panel.get_conf())["connections"][0]["health"] == "ok"

    await kit.store.note_permanent_failure(DOC, "comment_only_access")
    assert (await kit.panel.get_conf())["connections"][0]["health"] == "down"

    await kit.panel.add_doc(DOC2)   # 一好一坏
    assert (await kit.panel.get_conf())["connections"][0]["health"] == "attention"


# ------------------------------------------------------------------ cross-connection uniqueness


@pytest.mark.asyncio
async def test_startup_also_enforces_document_uniqueness(kit):
    """Uniqueness is enforced in **the registry**, not the panel.

    panel.add_doc is one of two entrances; the other is reading config at startup. A
    hand-edited or copied config that puts one document under two connections gets two
    watchers on it -- every mention answered twice, and with state keyed by doc_id the
    two overwrite each other. Putting the guard only on the UI path locks one door of
    two.
    """
    await kit.reg.add(str(kit.tmp / "k1.json"), [DOC, DOC2])
    await kit.reg.add(str(kit.tmp / "k2.json"), [DOC, "3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM"])

    first, second = kit.reg.list()
    assert DOC in first.watcher._docs
    assert DOC not in second.watcher._docs, "重复文档必须被第二个连接丢弃"
    assert "3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM" in second.watcher._docs, "非重复的不受影响"
    assert sorted(kit.reg.all_docs()) == sorted({DOC, DOC2, "3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM"})


@pytest.mark.asyncio
async def test_starting_up_does_not_delete_the_other_connections_state(kit):
    """State collection is absolute, so only the layer that knows every document may ask
    for it.

    ``gc`` keeps exactly the documents it is handed and deletes the rest. A watcher knows
    only its own, and calling it from there deleted every other connection's dedup keys,
    sessions and seeded flag on every start. The documents then
    re-seeded, marking everything currently outstanding as handled -- so a comment
    written between the last poll and a restart was swallowed without a trace.

    It read as ordinary housekeeping in the log: two connections, two lines of "collected
    1 document", each one deleting the other's.
    """
    await kit.reg.add(str(kit.tmp / "k2.json"), [DOC2])
    await kit.reg.add(str(kit.tmp / "k3.json"), ["3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM"])
    store = kit.reg.store
    await store.mark_triggered(DOC2, ["处理过的评论"])
    await store.mark_triggered("3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM", ["clouddoc:3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM:c9:-"])
    await store.mark_triggered("已从配置里移除的文档", ["旧键"])

    await kit.reg.start_all()

    snap = await store.snapshot()
    assert snap.get(DOC2, {}).get("triggered_ids"), "第二个连接的去重键被删了"
    assert (snap.get("3CCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKLLLMMM", {}).get("triggered_ids")), \
        "第三个连接的去重键被删了"
    assert "已从配置里移除的文档" not in snap, "不再纳管的文档应当被回收"


@pytest.mark.asyncio
async def test_a_document_added_before_startup_survives_startup(kit):
    """The document list has one owner: the watcher.

    ``start`` used to take the list and overwrite ``_docs`` with it, so anything the
    panel had added beforehand was dropped the moment the gateway started polling --
    and ``all_docs`` reported one source or the other depending on a flag.
    """
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    assert DOC in kit.reg.all_docs()

    await kit.reg.start_all()
    assert DOC in kit.reg.all_docs(), "启动把启动前加的文档丢了"


@pytest.mark.asyncio
async def test_sharing_a_document_is_what_puts_it_under_management(kit):
    """Sharing is the decision. The panel does not ask for a second one.

    What makes that safe is the trigger model: an adopted document costs a poll and
    nothing else until somebody @-mentions the agent in a comment. So adoption buys
    visibility, and the part that spends tokens stays a separate deliberate act.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSummary

    prov = await _with_conn1(kit)
    prov.accessible = [
        DocSummary(doc_id=DOC, title="已经在盯的", can_edit=True),
        DocSummary(doc_id=DOC2, title="刚共享进来的", can_edit=True),
    ]
    await kit.panel.add_doc(DOC)

    out = await kit.panel.sync_shared_docs()
    assert [d["doc_id"] for d in out["adopted"]] == [DOC2], "新共享的没被自动纳管"
    assert DOC2 in kit.reg.all_docs()
    # Persisted, or it lasts until the next restart and the user re-shares in confusion.
    assert DOC2 in _yaml_connections(kit.cfg)[0]["documents"]

    again = await kit.panel.sync_shared_docs()
    assert again["adopted"] == [], "第二次不该重复纳管"


@pytest.mark.asyncio
async def test_a_comment_only_share_is_reported_not_adopted(kit):
    """Admission refuses comment-only documents, so adopting one buys a broken entry.

    Reporting it is worth more than either adopting or hiding: the person made a
    specific mistake in the share dialog, and it has a specific fix.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocSummary

    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC2, title="只给了评论权的", can_edit=False)]

    out = await kit.panel.sync_shared_docs()
    assert out["adopted"] == []
    assert DOC2 not in kit.reg.all_docs(), "仅评论权的被纳管了，准入随后必然拒绝它"
    assert [d["doc_id"] for d in out["needs_editor"]] == [DOC2], "藏起来只会让人以为共享没生效"


@pytest.mark.asyncio
async def test_adoption_failing_does_not_break_the_panel(kit):
    """It runs on every panel open. A provider that cannot list must not take it down."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import ProviderError

    async def boom():
        raise ProviderError("forbidden", "Drive API has not been enabled")

    prov = await _with_conn1(kit)
    prov.list_accessible_documents = boom
    out = await kit.panel.sync_shared_docs()
    assert out["result"] == "unknown" and out["adopted"] == [] and out["needs_editor"] == []


@pytest.mark.asyncio
async def test_persist_goes_through_the_cross_process_mutex(kit, monkeypatch):
    """Writing config must go through update_config rather than a load and dump of its own.

    A single write is atomic, through a temporary file and a rename, but **the
    read-modify-write around it is not**: while the gateway saves a document list the
    agentserver may be writing permissions config, each process reads the old version
    and writes the whole file back, and whichever lands second erases the other. The
    product's update_config holds a threading lock and a portalocker file lock, built
    for exactly this pair.
    """
    from jiuwenswarm.extensions.co_scribe.backend.host.panel import panel as panel_mod

    seen: list[str] = []

    def _spy(mutator, **kw):
        """Record the call and apply the mutator **to the test's own file**.

        It must not delegate to the real ``update_config``: that function resolves the
        global config path itself, so calling it here rewrites the developer's live
        ``~/.jiuwenswarm/config/config.yaml`` with this test's fixture connection. That
        happened -- monkeypatching ``panel_mod.CONFIG_YAML_PATH`` only changes which
        branch the panel takes, not where update_config writes.

        The claim under test is "the panel goes through update_config", which a spy
        proves. Whether update_config itself locks correctly is the product's own
        contract, tested elsewhere.
        """
        seen.append("locked")
        data = load_yaml_round_trip(kit.cfg)
        dump_yaml_round_trip(kit.cfg, mutator(data if isinstance(data, dict) else {}))

    monkeypatch.setattr(panel_mod, "update_config", _spy)
    # Make the panel believe it is writing the global config path
    monkeypatch.setattr(panel_mod, "CONFIG_YAML_PATH", kit.cfg)
    kit.panel._config_path = kit.cfg

    await kit.reg.add(str(kit.tmp / "k1.json"))
    await kit.panel.add_doc(DOC)
    assert seen == ["locked"], "写配置绕过了跨进程互斥"


# ------------------------------------------------------------------ wiring integrity


def test_gateway_wiring_has_no_unbound_names():
    """The gateway's startup and shutdown paths must reference no unbound names.

    What happened: the multi-connection refactor renamed ``clouddoc_watcher`` to
    ``clouddoc_connections`` and missed one spot in the shutdown block --
    ``await clouddoc_watcher[0].stop()``. Being on the **shutdown path**, it never fires
    during normal operation and raises NameError only as the process exits, by which
    point nobody is usually reading the log.

    A missed rename of this kind cannot be caught by running tests, since the path is
    not covered; only reading the structure finds it.
    """
    import ast
    import pathlib

    src = pathlib.Path(
        __file__
    ).parents[5].joinpath("jiuwenswarm/gateway/app_gateway.py").read_text()
    tree = ast.parse(src)

    checked = 0
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = {
            t.id
            for n in ast.walk(func)
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.For, ast.AsyncFor))
            for t in ([n.target] if hasattr(n, "target") and n.target else getattr(n, "targets", []))
            if isinstance(t, ast.Name)
        }
        bound |= {a.arg for a in func.args.args + func.args.kwonlyargs}
        bound |= {
            n.name or n.asname
            for stmt in ast.walk(func)
            if isinstance(stmt, (ast.Import, ast.ImportFrom))
            for n in stmt.names
        }
        used = {
            n.id for n in ast.walk(func)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id.startswith("clouddoc")
        }
        if not used:
            continue
        checked += 1
        assert used <= bound, (
            f"{func.name} 引用了未绑定的名字 {sorted(used - bound)}——"
            "多半是一次没改干净的重命名，且只会在该分支被执行时才炸"
        )
    assert checked, "没有找到引用 clouddoc 名字的函数，这条检查需要重写"


# ------------------------------------------------------- getting off the ground


@pytest.mark.asyncio
async def test_adding_the_first_connection_turns_the_feature_on(kit):
    """The panel is the only switch. Nothing in the UI sets ``clouddoc.enabled``.

    Without this the whole configuration flow works right up until the next restart,
    and then goes quiet: the key is in config.yaml, the documents are listed, and the
    startup path skips all of it because the feature reads as off.
    """
    kit.cfg.write_text("clouddoc:\n  enabled: false\n")
    await _with_conn1(kit)
    await kit.panel.add_connection(credentials_path=str(kit.tmp / "k2.json"))

    section = yaml.safe_load(kit.cfg.read_text())["clouddoc"]
    assert section["enabled"] is True, "加了连接却没开启功能，重启后整套配置静默失效"


@pytest.mark.asyncio
async def test_a_registry_configured_off_answers_the_panel_but_does_not_poll(tmp_path):
    """Off must mean "do not poll", not "do not answer" -- those got conflated, and the
    conflation is what made a fresh install impossible to configure from the UI.

    The panel still has to work, because adding a connection is how it gets turned on.
    """
    store = CloudDocStore(tmp_path / "state.json", now_fn=Clock())
    (tmp_path / "k1.json").write_text(json.dumps({"client_email": "k1.json"}))

    async def dispatch(doc_id, comment_id, metadata):
        return "ok"

    reg = CloudDocConnections(
        store=store, dispatcher=dispatch, watcher_cfg=WatcherConfig(),
        base_trigger_cfg=TriggerConfig(sa_address=""),
        provider_factory=lambda p: PanelFakeProvider(p), now_fn=Clock(),
        enabled=False,
    )
    conn = await reg.add(str(tmp_path / "k1.json"), [DOC])
    await reg.start_all()
    assert conn.watcher._task is None, "功能已关闭却仍在轮询"

    cfg = tmp_path / "config.yaml"
    cfg.write_text("clouddoc:\n  enabled: false\n")
    panel = CloudDocPanel(reg, config_path=cfg)
    out = await panel.get_conf()
    assert len(out["connections"]) == 1, "关闭状态下面板答不出连接，就没法从界面开启"


@pytest.mark.asyncio
async def test_sync_reports_shared_but_unsupported_files(kit):
    """A shared spreadsheet must not vanish: the user cannot tell "unsupported" apart
    from "the share failed" unless the panel says so."""
    prov = await _with_conn1(kit)
    prov.unsupported = [{"title": "Budget 2026", "kind": "spreadsheet"}]
    out = await kit.panel.sync_shared_docs()
    assert out["unsupported"] == [{"title": "Budget 2026", "kind": "spreadsheet"}]


@pytest.mark.asyncio
async def test_unsupported_listing_failure_loses_the_notice_not_the_panel(kit):
    prov = await _with_conn1(kit)

    async def boom():
        raise RuntimeError("discovery down")
    prov.list_shared_unsupported = boom
    out = await kit.panel.sync_shared_docs()
    assert out["result"] == "ok"
    assert out["unsupported"] == []


@pytest.mark.asyncio
async def test_list_keys_reports_files_and_usage(kit):
    """Only files under clouddoc-keys/ are listed -- a path-mode connection pointing
    elsewhere is not a stored key. A stored key referenced by a connection is in_use."""
    keys = kit.cfg.parent / "clouddoc-keys"
    keys.mkdir(exist_ok=True)
    (keys / "old.json").write_text('{"client_email": "a@x.iam"}')
    (keys / "active.json").write_text('{"client_email": "b@x.iam"}')
    await kit.reg.add(str(keys / "active.json"))
    out = await kit.panel.list_keys()
    by_name = {k["filename"]: k for k in out["keys"]}
    assert by_name["old.json"]["client_email"] == "a@x.iam"
    assert by_name["old.json"]["in_use"] is False
    assert by_name["active.json"]["in_use"] is True


@pytest.mark.asyncio
async def test_a_key_names_the_connection_running_on_it(kit):
    """The panel draws one row per identity, joining a connection to its key file.
    ``in_use`` says a key is taken but not by whom, which cannot be joined on -- so
    every key row names its connection, and a key nobody is using names None."""
    keys = kit.cfg.parent / "clouddoc-keys"
    keys.mkdir(exist_ok=True)
    (keys / "orphan.json").write_text('{"client_email": "a@x.iam"}')
    (keys / "active.json").write_text('{"client_email": "b@x.iam"}')
    conn = await kit.reg.add(str(keys / "active.json"))
    rows = {k["filename"]: k for k in (await kit.panel.list_keys())["keys"]}
    assert rows["active.json"]["connection_id"] == conn.id
    # What remove_connection leaves behind on purpose: a key with no connection.
    assert rows["orphan.json"]["connection_id"] is None
    assert rows["orphan.json"]["provider"] == "google"

    # And the other way the halves come apart: a connection whose key is not in the
    # managed directory is in no key listing at all, so the connection carries it.
    outside = kit.cfg.parent / "elsewhere.json"
    outside.write_text('{"client_email": "c@x.iam"}')
    loose = await kit.reg.add(str(outside))
    by_id = {c["id"]: c for c in (await kit.panel.get_conf())["connections"]}
    assert by_id[conn.id]["key_filename"] == "active.json"
    assert by_id[conn.id]["key_managed"] is True
    assert by_id[loose.id]["key_filename"] == "elsewhere.json"
    assert by_id[loose.id]["key_managed"] is False
    assert "elsewhere.json" not in {k["filename"] for k in (await kit.panel.list_keys())["keys"]}


@pytest.mark.asyncio
async def test_delete_key_refuses_in_use_and_path_tricks(kit):
    keys = kit.cfg.parent / "clouddoc-keys"
    keys.mkdir(exist_ok=True)
    (keys / "spare.json").write_text("{}")
    (keys / "active.json").write_text('{"client_email": "b@x.iam"}')
    await kit.reg.add(str(keys / "active.json"))
    assert (await kit.panel.delete_key("active.json"))["result"] == "in_use"
    # A separator anywhere is refused outright -- otherwise this is arbitrary delete.
    assert (await kit.panel.delete_key("../config.yaml"))["result"] == "bad_name"
    assert (await kit.panel.delete_key("a/b.json"))["result"] == "bad_name"
    out = await kit.panel.delete_key("spare.json")
    assert out["result"] == "ok"
    assert not (keys / "spare.json").exists()
    assert (keys / "active.json").exists()


# ------------------------------------------------- periodic discovery of shared docs


class _Ticker:
    """A sleep that yields control, counts rounds, and stops the loop after N of them.

    The loop under test is infinite by design, so the test ends it the way the gateway
    does -- by cancelling -- rather than by giving the loop an exit condition it would
    not have in production.
    """

    def __init__(self, rounds: int) -> None:
        self.rounds = rounds
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) > self.rounds:
            raise asyncio.CancelledError
        await asyncio.sleep(0)


async def test_discovery_adopts_without_the_panel_being_opened(kit):
    """The point of the loop: a document shared with the account is adopted with nobody
    opening the Docs panel, which is what a chat-only deployment needs."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import discover_shared_periodically

    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC, title="共享进来的", can_edit=True)]
    assert kit.reg.all_docs() == []

    tick = _Ticker(rounds=1)
    with pytest.raises(asyncio.CancelledError):
        await discover_shared_periodically(kit.panel, interval_seconds=300, sleep_fn=tick)

    assert DOC in kit.reg.all_docs(), "定期发现应当在无人打开面板时纳管"
    assert _yaml_connections(kit.cfg)[0]["documents"] == [DOC], "纳管须落盘,重启后仍在"


async def test_discovery_leaves_the_tier_to_the_adoption_policy(kit):
    """Adoption is not a grant. With the default policy a discovered document is watched
    and nothing else -- no turn can be dispatched against it until someone grants one."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import discover_shared_periodically

    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC, title="共享进来的", can_edit=True)]

    tick = _Ticker(rounds=1)
    with pytest.raises(asyncio.CancelledError):
        await discover_shared_periodically(kit.panel, interval_seconds=300, sleep_fn=tick)

    registry = kit.reg._watch_registry
    assert registry is None or registry.get(DOC) is None, "默认策略下发现不得自动签发档位"


async def test_discovery_refuses_comment_only_like_admission_does(kit):
    """A comment-only share is reported, not adopted. Admission would refuse it anyway,
    and an entry that silently never works is worse than saying which document and why."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import discover_shared_periodically

    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC, title="只给了评论权", can_edit=False)]

    tick = _Ticker(rounds=1)
    with pytest.raises(asyncio.CancelledError):
        await discover_shared_periodically(kit.panel, interval_seconds=300, sleep_fn=tick)

    assert kit.reg.all_docs() == [], "仅评论权的文档不得被纳管"


async def test_discovery_survives_a_failing_round(kit):
    """Discovery is a convenience running in the process that also polls comments. A
    provider that fails must cost one round, not the loop."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import discover_shared_periodically

    prov = await _with_conn1(kit)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("unknown", "Drive 暂时不可用")
        return [DocSummary(doc_id=DOC, title="第二轮才拿到", can_edit=True)]

    prov.list_accessible_documents = flaky

    tick = _Ticker(rounds=2)
    with pytest.raises(asyncio.CancelledError):
        await discover_shared_periodically(kit.panel, interval_seconds=300, sleep_fn=tick)

    assert calls["n"] == 2, "第一轮失败后循环必须继续"
    assert DOC in kit.reg.all_docs()


async def test_discovery_waits_before_its_first_round(kit):
    """Startup already adopts through the registry; a round fired at t=0 would spend a
    full Drive listing per connection on work just done."""
    from jiuwenswarm.extensions.co_scribe.backend.host.panel.panel import discover_shared_periodically

    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC, title="共享进来的", can_edit=True)]

    tick = _Ticker(rounds=0)
    with pytest.raises(asyncio.CancelledError):
        await discover_shared_periodically(kit.panel, interval_seconds=300, sleep_fn=tick)

    assert tick.calls == [300], "必须先等一个间隔再发现"
    assert kit.reg.all_docs() == []




@pytest.mark.asyncio
async def test_list_docs_heals_a_missing_title_once(kit):
    """A document adopted before titles were recorded showed its id prefix
    forever; the panel now asks the provider once and caches the answer, the
    same self-heal the kind field already had."""
    prov = await _with_conn1(kit)
    prov.doc_title = ""  # adoption records nothing, the legacy state
    await kit.panel.add_doc(DOC)

    prov.doc_title = "治愈后的标题"
    rows = await kit.panel.list_docs()
    assert next(r for r in rows if r["doc_id"] == DOC)["title"] == "治愈后的标题"

    prov.doc_title = "不应再被读到"
    rows2 = await kit.panel.list_docs()
    assert next(r for r in rows2 if r["doc_id"] == DOC)["title"] == "治愈后的标题", (
        "标题应已缓存,不再询问 provider"
    )


@pytest.mark.asyncio
async def test_set_model_validates_like_cron_and_persists_deployment_wide(kit, monkeypatch):
    """§25.3: the model is wiring, set once for the deployment. The name goes through
    the cron validator so only a key the agentserver can resolve lands in the config,
    and an empty name restores the default."""
    import jiuwenswarm.gateway.cron.models as cron_models

    def fake_validate(raw):
        value = str(raw or "").strip()
        if not value:
            return None
        if value in ("gemma", "Gemma4-26B"):
            return "Gemma4-26B"
        raise ValueError(f"Unknown model {value!r}")

    monkeypatch.setattr(cron_models, "validate_cron_model", fake_validate)

    out = await kit.panel.set_model("gemma")
    assert out == {"ok": True, "model_name": "Gemma4-26B"}, "别名须落成规范名"
    assert load_yaml_round_trip(kit.cfg)["clouddoc"]["model_name"] == "Gemma4-26B"
    assert (await kit.panel.get_conf())["model_name"] == "Gemma4-26B"

    out = await kit.panel.set_model("nope")
    assert out["ok"] is False and "Unknown model" in out["detail"]
    assert load_yaml_round_trip(kit.cfg)["clouddoc"]["model_name"] == "Gemma4-26B", (
        "未知模型不得写入配置"
    )

    out = await kit.panel.set_model("")
    assert out == {"ok": True, "model_name": ""}
    assert (await kit.panel.get_conf())["model_name"] == ""


@pytest.mark.asyncio
async def test_set_doc_model_validates_then_persists_into_panel_meta(kit, monkeypatch):
    """The per-document pin follows ``set_model``'s shape exactly: validate, then
    persist. It lands in ``panel_meta`` -- operational data, beside title/kind/url --
    and never in the watch registry, which records authority rather than settings.
    An empty name clears the pin, so the document follows the deployment default."""
    import jiuwenswarm.gateway.cron.models as cron_models

    def fake_validate(raw):
        value = str(raw or "").strip()
        if not value:
            return None
        if value in ("gemma", "Gemma4-26B"):
            return "Gemma4-26B"
        raise ValueError(f"Unknown model {value!r}")

    monkeypatch.setattr(cron_models, "validate_cron_model", fake_validate)
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)

    out = await kit.panel.set_doc_model(DOC, "gemma")
    assert out == {"ok": True, "doc_id": DOC, "model_name": "Gemma4-26B"}, "别名须落成规范名"
    meta = (await kit.reg.store.doc_health(DOC))["panel_meta"]
    assert meta["model_name"] == "Gemma4-26B"
    row = next(r for r in await kit.panel.list_docs() if r["doc_id"] == DOC)
    assert row["model_name"] == "Gemma4-26B", "列表须带上文档自己的模型，记录弹窗才画得出"

    out = await kit.panel.set_doc_model(DOC, "nope")
    assert out["ok"] is False and "Unknown model" in out["detail"]
    meta = (await kit.reg.store.doc_health(DOC))["panel_meta"]
    assert meta["model_name"] == "Gemma4-26B", "未知模型不得落盘"

    out = await kit.panel.set_doc_model(DOC, "")
    assert out == {"ok": True, "doc_id": DOC, "model_name": ""}
    row = next(r for r in await kit.panel.list_docs() if r["doc_id"] == DOC)
    assert row["model_name"] == "", "清空即回到跟随部署默认"


@pytest.mark.asyncio
async def test_every_connection_takes_the_mention_as_its_summons(kit):
    """§16.14: the mention is a pointer edge and carries no authority, so the
    assignment field added no safety and is no longer read. Measured cost of keeping
    it on Google (2026-09-02): a person @-ed twice and read "assign it to me", then
    silence.

    Asserted through the gate rather than through a flag: the flag it used to check
    was the thing that got deleted, and a test on a flag would have gone green by
    disappearing with it.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocComment

    conn = await kit.reg.add(str(kit.tmp / "k1.json"))
    gate = conn.watcher._tcfg
    me = gate.sa_address
    assert me, "the connection must know its own address"

    def c(**kw):
        return DocComment(comment_id="c1", author_is_self=False, author_display_name="P",
                          created_time="", content="x", quoted_text="q", resolved=False, **kw)

    assert gate.is_for_me(c(mentioned_addresses=(me,))), "a mention summons"
    assert not gate.is_for_me(c(assignee_address=me)), "an assignment does not"


@pytest.mark.asyncio
async def test_add_connection_accepts_a_feishu_app_key(kit):
    """A Feishu app is an id and a secret, no client_email. The panel used to refuse
    it with "missing client_email", so the only way to connect Feishu was editing
    config.yaml by hand."""
    body = json.dumps({"app_id": "cli_abc123", "app_secret": "s3cr3t", "brand": "feishu"})
    out = await kit.panel.add_connection(credentials_json=body)
    assert out["result"] == "ok", out
    path = kit.tmp / "clouddoc-keys" / "cli_abc123.json"
    assert path.is_file() and json.loads(path.read_text())["app_id"] == "cli_abc123"

    # An id without a secret is not a key either.
    out = await kit.panel.add_connection(credentials_json=json.dumps({"app_id": "cli_x"}))
    assert out["result"] == "invalid_key"


@pytest.mark.asyncio
async def test_list_keys_names_a_feishu_key_by_its_app_id(kit):
    keys = kit.tmp / "clouddoc-keys"
    keys.mkdir()
    (keys / "fs.json").write_text('{"app_id": "cli_abc", "app_secret": "x"}')
    (keys / "g.json").write_text('{"client_email": "a@x.iam"}')
    out = await kit.panel.list_keys()
    by_name = {r["filename"]: r for r in out["keys"]}
    assert by_name["fs.json"]["address"] == "cli_abc"
    assert by_name["fs.json"]["client_email"] == ""
    assert by_name["g.json"]["address"] == "a@x.iam"


@pytest.mark.asyncio
async def test_removing_a_connection_revokes_the_mandates_signed_under_it(kit):
    """The connection is the mandator's authority for that account (D3). Before this,
    removing it only stopped the watcher: the mandate stayed live in the registry
    and a later re-adoption resumed unattended dispatch under an account the owner
    had disconnected. Now each mandate is revoked and journaled with the reason."""
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    await _with_conn1(kit)
    conn = kit.reg.list()[0]
    conn.watcher.watch(DOC)
    registry.issue(DOC, mode="apply_scoped")
    assert registry.get(DOC) is not None

    await kit.panel.remove_connection(conn.id)

    entry = registry.get(DOC)
    assert entry is not None and entry.get("revoked") is True, "值守随连接一起终止，条目留碑"
    assert registry.check(DOC).reason == "no_watch"
    assert not registry.is_write_live(DOC)
    tail = registry.audit_tail(10)
    assert any(r.get("event") == "revoke" and r.get("doc_id") == DOC
               and r.get("reason") == "connection_removed" for r in tail), tail
    assert registry.terminated_by_owner(DOC), "留碑：策略签发不得复活"


@pytest.mark.asyncio
async def test_hiding_a_document_stops_dispatch_but_keeps_it_adopted(kit):
    """Turning the watch off is a revocation, and nothing more.

    The document stays in the watcher's list and keeps being polled -- a
    collaborator @-ing the agent there still hears why nothing will happen. What
    stops is the standing delegation: no turn is dispatched, so no model call is
    made. That is the honest scope of a local decision about a document somebody
    else shared.
    """
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    await _with_conn1(kit)
    assert (await kit.panel.add_doc(DOC))["result"] == "ok"
    registry.issue(DOC, mode="apply_scoped")
    assert registry.is_write_live(DOC)

    assert (await kit.panel.watch_revoke(DOC))["ok"] is True

    entry = registry.get(DOC)
    assert entry is not None and entry.get("revoked") is True
    assert registry.check(DOC).reason == "no_watch"
    assert not registry.is_write_live(DOC), "关掉值守后在途写入必须被拦截"
    assert kit.reg.all_docs() == [DOC], "仍然纳管:平台上的共享没变,本地也不假装变了"
    assert _yaml_connections(kit.cfg)[0]["documents"] == [DOC]
    assert [a["event"] for a in registry.audit_for(DOC)][0] == "revoke"


@pytest.mark.asyncio
async def test_add_doc_issues_no_watch_under_the_default_policy(kit):
    """The policy is off unless the owner set it: a pasted link adopts -- the document
    is polled and persisted -- and the reply says the watch is off, so the person
    knows no write authority came with the paste. Same rule as discovery."""
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    assert kit.reg.auto_watch_policy in ("", "off")
    await _with_conn1(kit)
    out = await kit.panel.add_doc(DOC)
    assert out["result"] == "ok" and out["watch"] == "off"
    assert DOC in kit.reg.list()[0].watcher._docs, "纳管：进入轮询列表"
    assert _yaml_connections(kit.cfg)[0]["documents"] == [DOC], "纳管：写入配置"
    assert registry.get(DOC) is None, "默认策略下不签发任何 watch"
    assert not registry.check(DOC).dispatchable


@pytest.mark.asyncio
async def test_add_doc_applies_the_adoption_policy(kit):
    """The policy tier lands when the document is registered, not at the next
    restart: a link pasted into the panel gets the same treatment as a document
    listed in the config when the connection was built."""
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    kit.reg.auto_watch_policy = "apply_scoped"
    await _with_conn1(kit)
    out = await kit.panel.add_doc(DOC)
    assert out["result"] == "ok"
    assert out["watch"] == "apply_scoped", "策略开着时，回复要说明已签发"
    entry = registry.get(DOC)
    assert entry is not None and entry["mode"] == "apply_scoped"
    assert entry["issued_by"] == "policy"
    assert registry.check(DOC).dispatchable


@pytest.mark.asyncio
async def test_discovery_applies_the_adoption_policy(kit):
    """Same rule on the discovery path: a shared document adopted at run time gets
    the policy tier at adoption."""
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    kit.reg.auto_watch_policy = "apply_scoped"
    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC, title="共享进来的", can_edit=True)]
    out = await kit.panel.sync_shared_docs()
    assert [r["doc_id"] for r in out["adopted"]] == [DOC]
    entry = registry.get(DOC)
    assert entry is not None and entry["mode"] == "apply_scoped"


@pytest.mark.asyncio
async def test_discovery_does_not_turn_back_on_what_somebody_turned_off(kit):
    """The reason hiding needs no tombstone: the document never leaves the list,
    so discovery's own ``already watched`` skip passes over it, and the adoption
    policy meets the revoked entry and declines to dig it up. A document turned
    off stays off across a refresh, a re-share, and a restart."""
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    kit.reg.auto_watch_policy = "apply_scoped"
    prov = await _with_conn1(kit)
    prov.accessible = [DocSummary(doc_id=DOC, title="共享进来的", can_edit=True)]
    await kit.panel.sync_shared_docs()
    assert registry.check(DOC).dispatchable

    await kit.panel.watch_revoke(DOC)
    for _ in range(3):
        out = await kit.panel.sync_shared_docs()
        assert out["adopted"] == [], "已纳管的文档不会被再纳管一次"
        assert registry.check(DOC).reason == "no_watch", "发现流程不得把关掉的文档重新打开"
    assert DOC in kit.reg.all_docs(), "关掉值守≠移出纳管：仍在轮询"

    registry.issue(DOC, mode="apply_scoped", issued_by="manual")
    assert registry.check(DOC).dispatchable, "重新开启是人的动作"


@pytest.mark.asyncio
async def test_unhighlight_clears_the_document_and_stops_the_row_offering_it(kit, monkeypatch):
    """The panel's un-highlight is the manual half of D14, and it had no test.

    Two things have to hold together: the yellow really goes off the document (the
    provider is asked, with the exact strings the batch wrote), and the receipt stops
    advertising a highlight -- the row's label and the button both read ``highlight``,
    so a receipt that kept it True kept offering work already done. A second click is
    then refused rather than writing to the document again.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts import ReceiptStore

    ws = kit.tmp / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)

    prov = await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    cleared: list[tuple[str, list[str]]] = []

    async def clear_highlight(doc_id, texts):
        cleared.append((doc_id, list(texts)))
        return {"cleared": len(texts)}

    prov.clear_highlight = clear_highlight

    store = ReceiptStore()
    rid = store.begin(DOC, [{"old": "旧", "new": "新", "for_comment_ids": ["c1"]}], highlight=True)
    store.commit(rid, revision_after="r2")

    out = await kit.panel.unhighlight(rid)
    assert out["ok"] is True
    assert cleared == [(DOC, ["新"])], "要按回执写下的文本去撤，不是按整篇"
    r = store.get(rid)
    assert r["highlight"] is False and r["unhighlighted"] is True

    again = await kit.panel.unhighlight(rid)
    assert again["ok"] is False
    assert len(cleared) == 1, "已经撤过的回执不再动文档"


@pytest.mark.asyncio
async def test_unhighlight_refuses_a_receipt_that_never_highlighted(kit, monkeypatch):
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts import ReceiptStore

    ws = kit.tmp / "ws2"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)

    prov = await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    touched: list[str] = []

    async def clear_highlight(doc_id, texts):
        touched.append(doc_id)
        return {}

    prov.clear_highlight = clear_highlight

    store = ReceiptStore()
    plain = store.begin(DOC, [{"old": "旧", "new": "新", "for_comment_ids": []}], highlight=False)
    store.commit(plain, revision_after="r2")
    pending = store.begin(DOC, [{"old": "a", "new": "b", "for_comment_ids": []}], highlight=True)

    assert (await kit.panel.unhighlight(plain))["ok"] is False
    assert (await kit.panel.unhighlight(pending))["ok"] is False, "未落地的写入没有黄底可撤"
    assert (await kit.panel.unhighlight("nope"))["ok"] is False
    assert touched == []


@pytest.mark.asyncio
async def test_unhighlight_in_flight_survives_the_caller_being_cancelled(kit, monkeypatch):
    """The web channel cancels a request's task when the socket closes.

    Measured on the revert path before it was retired: a platform write cancelled
    after the platform took it and before the ledger did left the document changed
    and the ledger silent. ``unhighlight`` is the panel's remaining document write,
    and it is shielded for exactly this: once the platform has been asked, the
    receipt is marked whoever is still listening.
    """
    import asyncio

    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts import ReceiptStore

    ws = kit.tmp / "ws3"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)

    prov = await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    gate = asyncio.Event()

    async def slow_clear(doc_id, texts):
        await gate.wait()
        return {"cleared": len(texts)}

    prov.clear_highlight = slow_clear

    store = ReceiptStore()
    rid = store.begin(DOC, [{"old": "旧", "new": "新", "for_comment_ids": []}], highlight=True)
    store.commit(rid, revision_after="r2")

    task = asyncio.create_task(kit.panel.unhighlight(rid))
    for _ in range(20):  # let it reach the platform call
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()  # the platform answers after the caller left
    for _ in range(200):
        await asyncio.sleep(0.005)
        if store.get(rid)["highlight"] is False:
            break
    assert store.get(rid)["unhighlighted"] is True, "断连不能让账本漏掉已经发生的事"


@pytest.mark.asyncio
async def test_watch_set_refuses_a_document_nobody_adopted(kit):
    """A mandate names a document this deployment manages. Issuing one for an id
    nobody adopted left a registry entry no watcher would read and the panel could
    not show."""
    await _with_conn1(kit)
    out = await kit.panel.watch_set("doc-nobody-adopted", "apply_scoped")
    assert out["ok"] is False and "未纳管" in out["detail"]
    # The refusal happens before the registry is touched: this rig wires none, and
    # the call above returned instead of raising "watch registry not wired".


@pytest.mark.asyncio
async def test_get_conf_says_whether_a_question_channel_exists(kit, monkeypatch):
    """The always-ask floor refuses without a question channel, and the shipped
    config starts with permissions off; the panel has to say so or the first
    share simply fails."""
    import jiuwenswarm.common.config as config_mod

    await _with_conn1(kit)
    monkeypatch.setattr(config_mod, "get_config", lambda: {"permissions": {"enabled": False}})
    assert (await kit.panel.get_conf())["ask_channel"] is False
    monkeypatch.setattr(config_mod, "get_config", lambda: {"permissions": {"enabled": True}})
    assert (await kit.panel.get_conf())["ask_channel"] is True


# --------------------------------------------- bulk on/off over a selection


def _wire_registry(kit):
    from jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry import WatchRegistry

    registry = WatchRegistry(path=kit.cfg.parent / "watch.json")
    kit.reg._watch_registry = registry
    return registry


@pytest.mark.asyncio
async def test_bulk_enable_skips_the_ones_already_on(kit):
    """Re-issuing over a live grant is not a no-op: issuance sets a fresh term.

    "Turn on the ones I selected" would otherwise extend the thirty days on every
    watch already running -- the owner renewing grants they only meant to leave
    alone, with nothing on screen saying so. Renewal stays a per-document act in
    the usage audit.
    """
    registry = _wire_registry(kit)
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    await kit.panel.add_doc(DOC2)
    registry.issue(DOC, "apply_scoped", issued_by="manual")
    issued_at = registry.get(DOC)["issued_at"]
    expires_at = registry.get(DOC)["expires_at"]

    out = await kit.panel.watch_set_many([DOC, DOC2], "apply_scoped")
    assert out["issued"] == [DOC2]
    assert out["skipped"] == [{"doc_id": DOC, "reason": "already_on"}]
    assert registry.get(DOC)["issued_at"] == issued_at, "已开启的不得被重新签发"
    assert registry.get(DOC)["expires_at"] == expires_at, "期限不得被偷偷续上"
    assert registry.check(DOC2).dispatchable
    grants = [a for a in registry.audit_tail(50)
              if a["doc_id"] == DOC and a["event"] in ("grant", "modify")]
    assert len(grants) == 1, "跳过的文档不得多出一行签发审计"


@pytest.mark.asyncio
async def test_bulk_enable_reissues_an_expired_watch(kit):
    """Expired is not on. Skipping it would leave a selection where pressing 开启
    changes nothing and says nothing."""
    registry = _wire_registry(kit)
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    registry.issue(DOC, "apply_scoped", expires_at=1.0)
    assert registry.check(DOC).reason == "expired"

    out = await kit.panel.watch_set_many([DOC], "apply_scoped")
    assert out["issued"] == [DOC] and out["skipped"] == []
    assert registry.check(DOC).dispatchable


@pytest.mark.asyncio
async def test_bulk_enable_refuses_an_unmanaged_id_without_stopping(kit):
    registry = _wire_registry(kit)
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)

    out = await kit.panel.watch_set_many(["doc-nobody-adopted", DOC], "apply_scoped")
    assert out["issued"] == [DOC]
    assert out["skipped"][0]["doc_id"] == "doc-nobody-adopted"
    assert out["skipped"][0]["reason"] == "refused"
    assert registry.get("doc-nobody-adopted") is None


@pytest.mark.asyncio
async def test_bulk_off_lands_one_audit_line_per_document(kit):
    """Turning the watch off is a per-document revocation, and each is its own fact.

    A batch that wrote one line would leave every other document's history with a
    gap exactly where "who turned this off" should be. The documents themselves
    stay adopted -- hiding is a view and a delegation, not an unmanagement.
    """
    registry = _wire_registry(kit)
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    await kit.panel.add_doc(DOC2)
    registry.issue(DOC, "apply_scoped")
    registry.issue(DOC2, "apply_scoped")

    out = await kit.panel.watch_revoke_many([DOC, DOC2])
    assert sorted(out["revoked"]) == sorted([DOC, DOC2])
    assert sorted(kit.reg.all_docs()) == sorted([DOC, DOC2]), "关掉值守不移出纳管"
    for doc in (DOC, DOC2):
        assert registry.get(doc)["revoked"] is True, "每篇各自留碑"
        assert registry.check(doc).reason == "no_watch"
        assert len(registry.audit_for(doc)) >= 1
        assert [a["event"] for a in registry.audit_for(doc)][0] == "revoke"

    again = await kit.panel.watch_revoke_many([DOC, DOC2])
    assert again["revoked"] == []
    assert [s["reason"] for s in again["skipped"]] == ["already_off", "already_off"]


@pytest.mark.asyncio
async def test_watch_usage_carries_the_documents_lineage(kit):
    """The counts say how much of the grant was spent; the lineage says who
    granted it and why it is in the state it is in. One call, because the panel
    opens both at once."""
    registry = _wire_registry(kit)
    await _with_conn1(kit)
    await kit.panel.add_doc(DOC)
    await kit.panel.add_doc(DOC2)
    registry.issue(DOC, "apply_scoped", issued_by="manual")
    registry.issue(DOC2, "apply_scoped")
    registry.revoke(DOC, reason="document_removed")

    out = await kit.panel.watch_usage(DOC)
    events = [x["event"] for x in out["lineage"]]
    assert events == ["revoke", "grant"], "最新在前"
    assert all(x["doc_id"] == DOC for x in out["lineage"]), "别的文档不得混进来"
    assert out["lineage"][0]["reason"] == "document_removed"


# ------------------------------------------------- withdrawn formats are not adopted


@pytest.mark.asyncio
async def test_a_document_in_a_withdrawn_format_is_not_adopted_from_the_config(
    kit, monkeypatch, tmp_path
):
    """Markdown left the served formats, so a row for one stops being adopted.

    This is a claim the deployment could not make before. The note where
    ``remove_doc`` would be explains why a local removal was refused: the share
    lives on the platform, so dropping a row changed no fact anyone else could see
    and the next discovery pass adopted it back -- correctly, because it was still
    shared. A withdrawn format has left discovery, so nothing adopts it back.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)
    (ws / "config" / "clouddoc-state.json").write_text(json.dumps({"docs": {
        "MD_DOC": {"panel_meta": {"kind": "markdown", "title": "notes.md"}},
        "OK_DOC": {"panel_meta": {"kind": "spreadsheet", "title": "Q3"}},
        # Never probed, so no recorded format. Unknown admits: the format is learned
        # on the first read, and a startup guess must not evict a document the panel
        # simply never got round to probing.
        "NEW_DOC": {"panel_meta": {"title": "just added"}},
    }}), encoding="utf-8")

    conn = await kit.reg.add(str(kit.tmp / "k1.json"),
                             ["MD_DOC", "OK_DOC", "NEW_DOC"])

    assert sorted(conn.watcher._docs) == ["NEW_DOC", "OK_DOC"]
    assert "MD_DOC" not in kit.reg.all_docs()
    assert kit.reg.find_doc("MD_DOC") is None, "面板不再列出它，值守也不再轮询它"


@pytest.mark.asyncio
async def test_retiring_a_document_revokes_the_mandate_signed_for_it(
    kit, monkeypatch, tmp_path
):
    """A standing grant must not outlive the adoption it was signed under.

    The same rule as removing a connection: authority for a document nobody manages
    is a live mandate with nothing left to check it, and the panel's watch list
    reads the registry rather than the adopted set, so it would show the grant as a
    row for a document that is no longer there. Revoking leaves a tombstone, which
    is also what stops the adoption policy re-issuing it.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)
    (ws / "config" / "clouddoc-state.json").write_text(json.dumps({"docs": {
        "MD_DOC": {"panel_meta": {"kind": "markdown"}},
    }}), encoding="utf-8")

    revoked: list[tuple[str, str]] = []
    issued: list[str] = []

    class _Watches:
        def revoke(self, doc_id, reason=""):
            revoked.append((doc_id, reason))

        def issue(self, doc_id, mode, issued_by=""):
            issued.append(doc_id)

        def check(self, doc_id):
            return None

    kit.reg._watch_registry = _Watches()
    kit.reg.auto_watch_policy = "apply_scoped"
    await kit.reg.add(str(kit.tmp / "k1.json"), ["MD_DOC"])

    assert revoked == [("MD_DOC", "unsupported_format")]
    # And the policy is issued for what was adopted, not for what was asked for:
    # a watch issued here would be authority over a document this connection does
    # not watch.
    assert issued == []


@pytest.mark.asyncio
async def test_retiring_a_document_survives_the_next_startup(kit, monkeypatch, tmp_path):
    """The retirement must not undo itself, and it did.

    The drop reads the format the panel recorded, and ``start_all``'s collection then
    deletes the state of every document no longer adopted -- including that record.
    The next start saw no format, read it as unknown, admitted the document again,
    and the deployment oscillated between 19 documents and 17. Measured on a real
    workspace: two markdown files retired on one start and were back on the next.

    The record cannot simply be preserved either: ``kind_for`` no longer answers for
    a withdrawn format, so a re-probe stores "" and the document becomes
    indistinguishable from one whose format was never resolved. So the retirement is
    written where adoption is written -- the config's document list.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)
    (ws / "config" / "clouddoc-state.json").write_text(json.dumps({"docs": {
        "MD_DOC": {"panel_meta": {"kind": "markdown"}},
        "OK_DOC": {"panel_meta": {"kind": "document"}},
    }}), encoding="utf-8")

    kit.cfg.write_text(yaml.safe_dump({"clouddoc": {"enabled": True, "connections": [
        {"credentials_file": str(kit.tmp / "k1.json"), "documents": ["MD_DOC", "OK_DOC"]},
    ]}}, allow_unicode=True))

    await kit.reg.add(str(kit.tmp / "k1.json"), ["MD_DOC", "OK_DOC"])
    assert kit.reg.retired_docs == ["MD_DOC"]

    written = kit.panel.commit_retirements()
    assert written == ["MD_DOC"]

    # The adoption list on disk no longer carries it, so the next start never has to
    # re-derive the decision from a record that start is about to delete.
    (conn,) = _yaml_connections(kit.cfg)
    assert conn["documents"] == ["OK_DOC"]


@pytest.mark.asyncio
async def test_an_ordinary_start_does_not_rewrite_the_config(kit, monkeypatch, tmp_path):
    """Nothing retired means nothing written: a start that changes no adoption must
    not touch the file every other process shares."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)

    await kit.reg.add(str(kit.tmp / "k1.json"), ["OK_DOC"])
    before = kit.cfg.read_text()
    assert kit.panel.commit_retirements() == []
    assert kit.cfg.read_text() == before


@pytest.mark.asyncio
async def test_retiring_does_not_erase_a_connection_that_failed_to_start(
    kit, monkeypatch, tmp_path
):
    """The retirement removes documents; it must not rewrite the connection list.

    Startup skips a connection whose credentials cannot be read this minute --
    deliberately, so one bad connection does not block the rest -- which means the
    live registry is not always the whole truth. ``_persist`` rebuilds the list from
    that registry, so running it here would erase the skipped connection and every
    document under it: a network blip at boot turned into permanent config loss.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)
    (ws / "config" / "clouddoc-state.json").write_text(json.dumps({"docs": {
        "MD_DOC": {"panel_meta": {"kind": "markdown"}},
    }}), encoding="utf-8")

    # k2 is in the config but never built -- the shape of a connection whose key
    # could not be read at startup.
    kit.cfg.write_text(yaml.safe_dump({"clouddoc": {"enabled": True, "connections": [
        {"credentials_file": str(kit.tmp / "k1.json"), "documents": ["MD_DOC", "OK_DOC"]},
        {"credentials_file": str(kit.tmp / "k2.json"), "documents": ["FAR_DOC"]},
    ]}}, allow_unicode=True))

    await kit.reg.add(str(kit.tmp / "k1.json"), ["MD_DOC", "OK_DOC"])
    assert kit.panel.commit_retirements() == ["MD_DOC"]

    conns = _yaml_connections(kit.cfg)
    assert len(conns) == 2, "没起来的连接不得被抹掉"
    assert conns[0]["documents"] == ["OK_DOC"], "只删退管的那一篇"
    assert conns[1]["documents"] == ["FAR_DOC"], "别人的文档一个不动"


@pytest.mark.asyncio
async def test_retiring_works_on_a_config_nobody_has_upgraded_yet(
    kit, monkeypatch, tmp_path
):
    """The pre-connections shape is still accepted by the reader, so the retirement
    has to reach it too -- otherwise the write silently does nothing there and the
    oscillation this exists to stop comes straight back on exactly the deployments
    that have been running longest."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)
    (ws / "config" / "clouddoc-state.json").write_text(json.dumps({"docs": {
        "MD_DOC": {"panel_meta": {"kind": "markdown"}},
    }}), encoding="utf-8")

    kit.cfg.write_text(yaml.safe_dump({"clouddoc": {
        "enabled": True,
        "credentials_file": str(kit.tmp / "k1.json"),
        "documents": ["MD_DOC", "OK_DOC"],
    }}, allow_unicode=True))

    await kit.reg.add(str(kit.tmp / "k1.json"), ["MD_DOC", "OK_DOC"])
    assert kit.panel.commit_retirements() == ["MD_DOC"]

    section = yaml.safe_load(kit.cfg.read_text())["clouddoc"]
    assert section["documents"] == ["OK_DOC"]
    assert section["credentials_file"], "旧形态的其余字段不得被顺手改掉"


@pytest.mark.asyncio
async def test_a_retirement_is_written_once(kit, monkeypatch, tmp_path):
    """The hand-off is consumed. Adding a connection through the panel calls ``add``
    again, and a second commit must not rewrite the file for work already done."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit import deployment

    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    monkeypatch.setattr(deployment, "workspace_dir", lambda: ws)
    (ws / "config" / "clouddoc-state.json").write_text(json.dumps({"docs": {
        "MD_DOC": {"panel_meta": {"kind": "markdown"}},
    }}), encoding="utf-8")
    kit.cfg.write_text(yaml.safe_dump({"clouddoc": {"enabled": True, "connections": [
        {"credentials_file": str(kit.tmp / "k1.json"), "documents": ["MD_DOC", "OK_DOC"]},
    ]}}, allow_unicode=True))

    await kit.reg.add(str(kit.tmp / "k1.json"), ["MD_DOC", "OK_DOC"])
    assert kit.panel.commit_retirements() == ["MD_DOC"]

    after = kit.cfg.read_text()
    assert kit.panel.commit_retirements() == [], "第二次没有新的退管，不该再写"
    assert kit.cfg.read_text() == after


@pytest.mark.asyncio
async def test_a_document_is_not_retired_on_an_unconfirmed_not_found():
    """Retirement is destructive, so an ambiguous ``not_found`` must not reach it.

    Observed 2026-09-10: two live Feishu documents were retired as "no longer on the
    platform" -- one read back 3999 characters minutes later. On that platform a
    deleted file and a wiki-hosted one addressed with the wrong container answer with
    the same word, so the kind is not a fact. Retiring drops the document from the
    adoption list, voids its watch and makes the owner paste the link again, all from
    one INFO line, so the doubt has to resolve towards keeping it.
    """
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import ProviderError

    class _Prov:
        def __init__(self, absent):
            self._absent = absent

        async def capabilities(self, doc_id):
            raise ProviderError("not_found", "not exist")

        async def confirm_absent(self, doc_id):
            return self._absent

    class _Conn:
        def __init__(self, prov):
            self.provider = prov

    class _Reg:
        pass

    panel = CloudDocPanel(_Reg(), config_path=None)
    kept = await panel._probe(_Conn(_Prov(False)), "D")
    assert kept["result"] == "unknown", kept
    assert "保留纳管" in kept["detail"]

    gone = await panel._probe(_Conn(_Prov(True)), "D")
    assert gone["result"] == "not_shared", gone


@pytest.mark.asyncio
async def test_list_docs_probes_an_unresolvable_format_once_per_process(kit):
    """A row whose format cannot be resolved but whose title is known was re-asked on
    every panel open: the kind branch never added the id to the probed set, only the
    title branch did, and the per-load API cost the set exists to remove came back."""
    prov = await _with_conn1(kit)
    await kit.panel.add_doc(DOC)

    calls = []

    async def doc_kind(ref):
        calls.append(ref)
        raise ProviderError("transport", "down")

    prov.doc_kind = doc_kind
    await kit.panel.list_docs()
    await kit.panel.list_docs()
    assert calls == [DOC], f"格式探测应每进程一次，实际 {len(calls)} 次"


# ------------------------- review: unhighlight runs under the owning connection or not at all


@pytest.mark.asyncio
async def test_unhighlight_refuses_a_document_no_connection_owns(kit, monkeypatch):
    """A receipt whose document no connection lists has no account the write can be
    attributed to; falling back to the first connection would clear highlights --
    a document write -- under whichever identity happened to come first."""
    import jiuwenswarm.extensions.co_scribe.backend.toolkit.receipts as rc

    monkeypatch.setattr(rc, "get_receipts_path", lambda: kit.tmp / "r.json")
    prov = await _with_conn1(kit)
    cleared: list = []

    async def clear_highlight(doc_id, texts):
        cleared.append(doc_id)
        return {"cleared": len(texts)}

    prov.clear_highlight = clear_highlight
    orphan = "1ORPHANORPHANORPHANORPHANORPHANORPHAN"
    store = rc.ReceiptStore(kit.tmp / "r.json")
    rid = store.begin(orphan, [{"old": "旧", "new": "新", "for_comment_ids": []}],
                      highlight=True, executor="chat", source="batch_edit")
    store.commit(rid, revision_after="r2", highlighted=True)

    out = await kit.panel.unhighlight(rid)
    assert not out["ok"] and "不在任何连接" in out["detail"]
    assert cleared == [], "无主文档不得在任何连接下写入"

    kit.reg.list()[0].watcher._docs.append(orphan)
    out = await kit.panel.unhighlight(rid)
    assert out["ok"] and cleared == [orphan]


# ------------------------- review: a pasted origin is never persisted as the document's address


def _google_shaped(monkeypatch):
    """The panel fake, made to parse links the way the Google provider does."""

    def parse(self, url_or_id):
        if "/document/d/" in url_or_id:
            return url_or_id.split("/d/")[1].split("/")[0].split("?")[0]
        return PanelFakeProvider.parse_doc_ref(self, url_or_id)

    monkeypatch.setattr(PanelFakeProvider, "kind", property(lambda self: "google"))
    monkeypatch.setattr(PanelFakeProvider, "parse_doc_ref", parse)


@pytest.mark.asyncio
async def test_a_link_on_a_foreign_host_is_adopted_by_id_but_its_origin_is_dropped(kit, monkeypatch):
    _google_shaped(monkeypatch)
    await _with_conn1(kit)
    out = await kit.panel.add_doc(f"https://evil.example/document/d/{DOC}/edit")
    assert out["result"] == "ok" and out["doc_id"] == DOC
    rows = await kit.panel.list_docs()
    assert "evil.example" not in rows[0]["url"]
    assert rows[0]["url"].startswith("https://example.test/"), "地址来自平台侧，不来自粘贴"


@pytest.mark.asyncio
async def test_a_link_on_the_platforms_host_is_kept(kit, monkeypatch):
    _google_shaped(monkeypatch)
    await _with_conn1(kit)
    out = await kit.panel.add_doc(f"https://docs.google.com/document/d/{DOC}/edit?tab=t.0#h")
    assert out["result"] == "ok"
    rows = await kit.panel.list_docs()
    assert rows[0]["url"] == f"https://docs.google.com/document/d/{DOC}/edit"


@pytest.mark.asyncio
async def test_a_foreign_address_persisted_earlier_is_not_served(kit, monkeypatch):
    """Rows written before origins were checked are re-checked on the way out."""
    _google_shaped(monkeypatch)
    await _with_conn1(kit)
    await kit.panel.add_doc(f"https://docs.google.com/document/d/{DOC}/edit")
    await kit.store.set_panel_meta(DOC, url=f"https://evil.example/document/d/{DOC}/edit")
    rows = await kit.panel.list_docs()
    assert "evil.example" not in rows[0]["url"]
    assert rows[0]["url"].startswith("https://example.test/")


@pytest.mark.asyncio
async def test_a_canonical_url_off_the_platforms_hosts_is_shown_but_not_persisted(kit, monkeypatch):
    """The platform's own answer is shown as answered (a private deployment lives on
    its own domain and this is the one link it can show), but the store keeps only a
    link the same check would accept back -- so it never round-trips a link that
    ``list_docs`` would drop on the next read."""
    _google_shaped(monkeypatch)
    prov = await _with_conn1(kit)
    await kit.panel.add_doc(f"https://docs.google.com/document/d/{DOC}/edit")
    await kit.store.set_panel_meta(DOC, url="")

    async def canonical_url(doc_id):
        return f"https://evil.example/document/d/{doc_id}/edit"

    prov.canonical_url = canonical_url
    rows = await kit.panel.list_docs()
    assert rows[0]["url"] == f"https://evil.example/document/d/{DOC}/edit"
    health = await kit.store.doc_health(DOC)
    assert not (health.get("panel_meta") or {}).get("url"), "非平台域名的地址不落库"


# ------------------------- review: a verdict names the connection it is about


def _shape(prov, vendor):
    """Make one panel fake parse links the way that vendor's provider does."""
    def parse(url_or_id):
        s = url_or_id.strip()
        if vendor == "google":
            if "docs.google.com" in s:
                return s.split("/d/")[1].split("/")[0].split("?")[0]
            if "/" not in s and len(s) > 20:
                return s
            raise ProviderError("invalid", f"not a Google reference: {s}")
        for marker in ("/docx/", "/wiki/", "/sheets/"):
            if marker in s:
                return s.split(marker, 1)[1].split("?")[0].split("/")[0]
        if "/" not in s and len(s) > 20:
            return s
        raise ProviderError("invalid", f"not a Feishu reference: {s}")
    prov.parse_doc_ref = parse


@pytest.mark.asyncio
async def test_a_refusal_names_the_connection_that_produced_it(kit):
    """Google connection first (and "selected"), Feishu second; a Feishu link that
    is not yet shared. The probe runs through the Feishu connection, so the address
    to share the document with is the Feishu bot's -- and the verdict must say so,
    or the panel copies the Google service-account address."""
    await kit.reg.add(str(kit.tmp / "k1.json"))
    await kit.reg.add(str(kit.tmp / "k2.json"))
    google, feishu = kit.providers["k1.json"], kit.providers["k2.json"]
    _shape(google, "google")
    _shape(feishu, "feishu")
    feishu.caps = ProviderError("forbidden", "no permission")

    out = await kit.panel.add_doc("https://acme.feishu.cn/docx/FsTokNotSharedYet1?from=x")
    assert out["result"] == "not_shared"
    assert out["connection_id"] == kit.reg.list()[1].id
    assert out["agent_address"] == SA2, "要复制的是飞书连接的地址，不是被选中的 Google 连接的"
    assert google.cap_calls == 0, "解析不了这条链接的连接不会被问"


@pytest.mark.asyncio
async def test_the_best_refusal_carries_its_own_connection(kit):
    """Both connections can parse the link; the more informative refusal wins, and
    the identity attached is the one that gave it."""
    await kit.reg.add(str(kit.tmp / "k1.json"))
    await kit.reg.add(str(kit.tmp / "k2.json"))
    first, second = kit.providers["k1.json"], kit.providers["k2.json"]
    first.caps = ProviderError("forbidden", "not shared")
    second.caps = replace(second.caps, can_edit=False)          # comment-only: outranks not_shared
    out = await kit.panel.add_doc(DOC)
    assert out["result"] == "comment_only"
    assert out["connection_id"] == kit.reg.list()[1].id and out["agent_address"] == SA2


@pytest.mark.asyncio
async def test_an_adoption_and_an_existing_row_name_their_connection_too(kit):
    await kit.reg.add(str(kit.tmp / "k1.json"))
    out = await kit.panel.add_doc(DOC)
    assert out["result"] == "ok" and out["connection_id"] == kit.reg.list()[0].id
    assert out["agent_address"] == SA
    again = await kit.panel.add_doc(DOC)
    assert again["result"] == "exists" and again["agent_address"] == SA
