"""What every cloud-document provider must supply, checked for all of them at once.

The second provider exposed two assumptions the first one had made invisible: the
loop prohibition decided "is this author another agent" from a Google-shaped display
name, and the receipt plumbing was read off attributes only Google happened to have.
Neither raised anything. A provider missing them answers wrong, or silently records
nothing, which is worse than a crash -- see §16.12.

So this file is the answer to "what must a provider fill in for itself", written as a
test rather than as prose, and parametrised over every provider there is. A third one
is added to ``ALL_PROVIDERS`` and immediately learns what it owes.

Two things are deliberately not here. Behaviour that needs a live platform belongs in
the integration suite, and behaviour that is one provider's own belongs in that
provider's file. What is left is the contract: the surface the machinery reaches for
without asking whether this particular provider has it.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    DocProvider,
    ProviderError,
)


def _google():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
        GoogleDocsProvider,
    )

    # Built without credentials: the contract is about shape, and touching the network
    # would make this a different kind of test.
    return object.__new__(GoogleDocsProvider)


def _feishu():
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.feishu_provider import (
        FeishuDocsProvider,
    )

    return FeishuDocsProvider(profile="p")


ALL_PROVIDERS = [
    pytest.param(_google, id="google"),
    pytest.param(_feishu, id="feishu"),
]

# Reached for by name, on any provider, by code that has no way to check first.
# Absence is an AttributeError at the worst moment -- mid-write.
REQUIRED_ATTRS = (
    # The registry assigns this to every provider it builds; a provider that does not
    # read it produces no receipts, and rings ⑤ and ⑥ stop working without a sound.
    "receipt_sink",
    "receipt_meta",
)

REQUIRED_METHODS = (
    "parse_doc_ref",
    "self_identity",
    "capabilities",
    "read",
    "edit_batch",
    "list_comments",
    "reply_comment",
    # Called by the panel's un-highlight act and by the resolve-driven clearing. A
    # provider whose platform cannot highlight still answers, reporting nothing cleared.
    "clear_highlight",
    # The panel and the create-document tool both hand a link to a person. They used to
    # build it from a Google-document-shaped template, which was wrong for every Feishu
    # document and for every Google spreadsheet and deck. Only the
    # provider knows the shape, so only the provider builds it.
    "doc_url",
    # The trash tool calls this by name; the base class answers ``unsupported``.
    "trash_document",
)


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_a_provider_carries_the_receipt_plumbing(make):
    """Recording is wired by assignment, not by asking. Both attributes must exist
    before anything assigns them, or the write primitive cannot read them back."""
    prov = make()
    for attr in REQUIRED_ATTRS:
        assert hasattr(prov, attr), f"{type(prov).__name__} 缺少 {attr}"


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_a_provider_implements_everything_the_machinery_calls(make):
    """Not the same as subclassing DocProvider: clear_highlight is called by name and
    is not on the abstract base, which is exactly how it went missing."""
    prov = make()
    for name in REQUIRED_METHODS:
        assert callable(getattr(prov, name, None)), (
            f"{type(prov).__name__} 缺少 {name}"
        )


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_a_provider_answers_whether_an_author_is_another_agent(make):
    """Invariant ⑤ needs this per platform (IC-6). Google reads a service-account
    suffix; a Feishu bot's name does not end that way, so a provider that inherited
    the test would call every bot a person. Each must answer for itself, by whatever
    the platform actually offers."""
    prov = make()
    answered = any(
        callable(getattr(prov, name, None))
        for name in ("_is_other_agent", "_is_service_account")
    )
    assert answered, (
        f"{type(prov).__name__} 未回答「他者是否 agent」——"
        "不变量⑤ 在该平台上没有判定基础"
    )


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_capabilities_reports_whether_a_batch_is_atomic(make):
    """C9: a capability that is absent is declared, never simulated. The default is
    true, so a provider that cannot commit several edits as one write has to say so --
    silence reads as "yes" and a batch would be half-applied to a shared document."""
    prov = make()
    sig = inspect.signature(type(prov).capabilities)
    assert "doc_ref" in sig.parameters, "capabilities 必须按文档回答"


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_resolving_a_thread_is_refused_everywhere(make):
    """Invariant ③ is a principle, not a platform limitation: resolving is the
    reader's acknowledgement that the answer was the one they wanted, and an agent
    doing it removes the acknowledgement rather than earning it. Both platforms offer
    the API and neither may use it.

    **This test asserted that the method was a coroutine.** Nothing else. A provider
    that resolved happily passed it, and one did -- Google's called the Drive API with
    ``action: "resolve"`` for the whole life of this file, under a test named for the
    guarantee it was not making. The name promised more than the assertion checked,
    which is worse than having no test: it is the reason nobody looked.

    So it now calls the method and requires a refusal."""
    prov = make()
    fn = getattr(prov, "resolve_comment", None)
    if fn is None:
        return
    assert inspect.iscoroutinefunction(fn)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(fn("doc", "comment"))
    assert exc.value.kind == "unsupported", (
        f"{type(prov).__name__}.resolve_comment 未按不变量③ 拒绝"
    )


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_a_provider_declares_its_text_domain(make):
    """Whether a markdown marker is content or a literal character decides whether the
    markup rail refuses it. Defaulting to plain fails toward refusing an extra
    proposal rather than writing asterisks into someone's document."""
    prov = make()
    assert prov.text_domain in ("plain", "markdown")


@pytest.mark.parametrize("make", ALL_PROVIDERS)
def test_an_unparseable_reference_is_refused_rather_than_guessed(make):
    """A reference that cannot be read is a configuration error someone must see.
    Returning something plausible would send a write at a document nobody named."""
    prov = make()
    for bad in ("", "   "):
        with pytest.raises(ProviderError):
            prov.parse_doc_ref(bad)


def test_every_provider_in_the_tree_is_covered_here():
    """The list above is the point of the file, so it must not fall behind the tree:
    a provider added without being listed would be exempt from all of it."""
    import pkgutil

    import jiuwenswarm.extensions.co_scribe.backend.toolkit.providers as pkg

    found = {
        name
        for _, name, _ in pkgutil.iter_modules(pkg.__path__)
        if name.endswith("_provider")
    }
    covered = {"google_provider", "feishu_provider"}
    assert found == covered, (
        f"provider 模块与本文件的覆盖清单不一致：{found ^ covered}。"
        "新增 provider 请加入 ALL_PROVIDERS。"
    )


def test_the_abstract_base_still_declares_what_it_used_to():
    """A method quietly dropped from DocProvider would take its implementations'
    obligation with it, and nothing would fail."""
    for name in ("read", "edit_batch", "list_comments", "reply_comment", "capabilities"):
        assert hasattr(DocProvider, name), f"DocProvider 少了 {name}"


def test_every_load_bearing_capability_still_has_a_reader():
    """DocCapabilities claims which of its flags are load-bearing. A flag that quietly
    stops being read is the failure this pins: it keeps being measured and declared and
    asserted, while nothing acts on it, and reasoning that leans on it leans on air.

    Four of seven were inert when this was written. Two were wired up; two are marked
    informational on purpose and are excluded here by name, not by accident."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[5] / "jiuwenswarm"
    # Co-scribe's production code lives in its own plugin now, where most module
    # paths no longer carry the word "clouddoc"; the plugin's backend directory
    # is what "co-scribe's sources" means.
    plugin = root / "extensions" / "co_scribe" / "backend"
    # The plugin's own tests live under jiuwenswarm/ too now, and their
    # paths carry the word "clouddoc"; a reader found in a test file is not
    # a reader in production code, so they are excluded by location.
    plugin_tests = root / "extensions" / "co_scribe" / "tests"
    sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in root.rglob("*.py")
        if ("clouddoc" in str(p) or plugin in p.parents)
        and plugin_tests not in p.parents
        and not p.name.endswith("provider.py")
    )
    # **The reader has to be reachable, not merely present.** This asserted that the
    # name appeared somewhere in the sources, and that is what let the chain break in
    # silence: ``max_quote_chars`` was read inside a watcher helper whose only caller was
    # the retired apply machinery, so deleting the dead code took the live reader with
    # it, the measurement went back to doing nothing, and this test stayed green because
    # the text was still there.
    #
    # So each reader is named, and the function holding it must itself be called.
    readers = {
        "can_edit": ("_admit",),
        "has_revision_control": ("_admit",),
        "max_quote_chars": ("_rail_cfg",),
    }
    for flag, holders in readers.items():
        assert f".{flag}" in sources or f'getattr(caps, "{flag}"' in sources, (
            f"{flag} 被声明为承重字段，但生产代码里没有读它的地方"
        )
        for holder in holders:
            calls = sources.count(f"self.{holder}(") + sources.count(f"await self.{holder}(")
            assert calls, (
                f"{flag} 的读者 {holder}() 没有任何调用方——"
                "字段有名义上的读者，而那个读者不可达"
            )


def test_resolve_comment_has_the_same_signature_on_every_provider():
    """The router passes three arguments; a provider declaring two would raise
    TypeError before it could refuse. Both refuse on principle -- the shape still
    has to match."""
    import inspect

    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.feishu_provider import FeishuDocsProvider
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import GoogleDocsProvider
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import DocProvider

    want = list(inspect.signature(DocProvider.resolve_comment).parameters)
    for cls in (GoogleDocsProvider, FeishuDocsProvider):
        assert list(inspect.signature(cls.resolve_comment).parameters) == want, cls.__name__
