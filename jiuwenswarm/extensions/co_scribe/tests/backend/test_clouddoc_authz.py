"""Offline tests for the cross-process authorization payload and default-deny."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as idp
from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import (
    CLOUDDOC_CHANNEL_ID,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    get_clouddoc_turn_doc_id,
    get_runtime_tool_channel_id,
    get_runtime_tool_metadata,
    is_unattended_clouddoc_turn,
)

DOC = "1AAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def bind():
    """Bind and restore the context variables on demand, simulating a real request's binding
    window."""
    tokens = []

    def _bind(*, channel_id=None, metadata=None, bound=True):
        tokens.append(idp._CRON_TOOL_BOUND.set(bound))
        if channel_id is not None:
            tokens.append(idp._CRON_TOOL_CHANNEL_ID.set(channel_id))
        if metadata is not None:
            tokens.append(idp._CRON_TOOL_METADATA.set(metadata))

    yield _bind
    for t in reversed(tokens):
        t.var.reset(t)


def test_unbound_channel_is_none_not_web():
    """Reading the contextvar directly yields the default 'web', making "nothing bound" and
    "genuinely on the web channel" indistinguishable by value. The reader has to consult
    _CRON_TOOL_BOUND first."""
    assert idp._CRON_TOOL_CHANNEL_ID.get() == "web"   # 底层默认值
    assert get_runtime_tool_channel_id() is None       # 对外契约
    assert get_runtime_tool_metadata() is None
    assert is_unattended_clouddoc_turn() is False


def test_bound_chat_turn_imposes_no_constraint(bind):
    bind(channel_id="web", metadata={})
    assert get_runtime_tool_channel_id() == "web"
    assert is_unattended_clouddoc_turn() is False
    # Chat path: no authorization scope, and the tool layer imposes no doc_id constraint
    assert get_clouddoc_turn_doc_id() is None


def test_clouddoc_turn_exposes_doc_id(bind):
    bind(channel_id=CLOUDDOC_CHANNEL_ID, metadata={"clouddoc": {"doc_id": DOC}})
    assert is_unattended_clouddoc_turn() is True
    assert get_clouddoc_turn_doc_id() == DOC


def test_clouddoc_turn_with_missing_payload_fails_closed(bind):
    """A clouddoc session with the payload missing. This is the only cross-process
    authorization field, and its absence means no authorization.

    It returns an empty string rather than None: None means "chat path, no constraint",
    which is the opposite meaning.
    """
    bind(channel_id=CLOUDDOC_CHANNEL_ID, metadata={})
    assert get_clouddoc_turn_doc_id() == ""
    assert get_clouddoc_turn_doc_id() != None  # noqa: E711 - 刻意区分空串与 None


def test_discriminator_is_channel_not_metadata_presence(bind):
    """The discriminator has to be channel_id.

    Were it "does metadata exist", it would be circular with the fail-closed rule: with
    metadata missing you could not tell this was a clouddoc session, so "missing means
    refuse" would be unreachable by construction.
    """
    bind(channel_id=CLOUDDOC_CHANNEL_ID, metadata=None)
    assert is_unattended_clouddoc_turn() is True, "没有 metadata 也必须认得出是 clouddoc 轮"


def test_metadata_accessor_does_not_use_fallback_proxy(bind):
    """_RuntimeCronToolContext.metadata must not be reused.

    When unbound, that property falls back to the previous turn's snapshot, which would
    have a tool authorizing an operation on document B while holding document A's id.
    """
    bind(channel_id=CLOUDDOC_CHANNEL_ID, metadata={"clouddoc": {"doc_id": DOC}})
    assert get_runtime_tool_metadata() == {"clouddoc": {"doc_id": DOC}}
    # Unbinding must yield None at once, not the previous turn's value
    tok = idp._CRON_TOOL_BOUND.set(False)
    try:
        assert get_runtime_tool_metadata() is None
    finally:
        idp._CRON_TOOL_BOUND.reset(tok)


def test_allowlist_covers_exactly_the_apply_scoped_family():
    # The IC-1 final state (D1 retirement done): an apply_scoped turn holds exactly
    # read / list / the bounded direct-apply / reply. propose_edit is gone with its
    # machinery -- approve was the acceptance link's stand-in, and D8 replaced it.
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        UNATTENDED_ALLOWLIST,
    )

    assert UNATTENDED_ALLOWLIST == {
        "clouddoc_read",
        "clouddoc_list_comments",
        "clouddoc_apply_for_comment",
        "clouddoc_reply_comment",
    }
    assert "clouddoc_propose_edit" not in UNATTENDED_ALLOWLIST
    # These two carry the whole argument that the agent cannot change the body, and must
    # never appear in the allowlist
    assert "clouddoc_edit" not in UNATTENDED_ALLOWLIST
    assert "clouddoc_resolve_comment" not in UNATTENDED_ALLOWLIST


def test_default_deny_branch_precedes_ask_user_bypass():
    """A source-level assertion about where the branch is inserted.

    ask_user is let through unconditionally outside avatar scenarios, and calling it in
    an unattended session hangs until the turn times out; the `perm_ctx is None` early
    return would likewise leave the clouddoc branch unreachable.
    """
    import inspect

    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers

    src = inspect.getsource(interrupt_helpers)
    i_clouddoc = src.index("is_unattended_clouddoc_turn()")
    i_askuser = src.index('inp.normalized_tool_name == "ask_user"')
    i_permctx = src.index("if perm_ctx is None:")
    assert i_clouddoc < i_askuser, "clouddoc 分支必须在 ask_user 旁路之前"
    assert i_clouddoc < i_permctx, "clouddoc 分支必须在 perm_ctx 早退之前"


# ------------------------------------------------------------ the authorization snapshot
#
# What happened in a live environment: reading the contextvar inside the binding window
# is correct -- the closed-set strip from 49 tools to 4 proved it -- but by the time a
# tool actually runs the context has changed, and reading it then yields nothing. The
# symptom was the model receiving "missing doc_id" and abandoning the turn: the feature
# failed silently while every offline test stayed green.


class _StubAbilityManager:
    def __init__(self): self._items = []
    def list(self): return list(self._items)
    def add(self, card): self._items.append(card)
    def remove(self, name): self._items = [a for a in self._items if getattr(a, "name", "") != name]


class _StubInstance:
    def __init__(self): self.ability_manager = _StubAbilityManager()


def _scene_hook_input(normalized_tool_name, user_input=None):
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    return PermissionSceneHookInput(
        ctx=SimpleNamespace(session=None),
        tool_call=SimpleNamespace(id="call_1", name=normalized_tool_name, arguments={}),
        user_input=user_input,
        normalized_tool_name=normalized_tool_name,
        tool_args={},
        engine=None,
    )


def _adapter_with_toolkit(toolkit):
    """Assembles only the attributes _update_clouddoc_tools actually touches."""
    a = object.__new__(idp.JiuWenSwarmDeepAdapter)
    a._instance = _StubInstance()
    a._clouddoc_toolkit = toolkit
    a._clouddoc_turn = {}
    a._register_agent_owned_tool = lambda tool, owner: None
    a._tool_owner_id = lambda: "owner"
    return a


def test_authorization_survives_leaving_the_binding_window(bind, monkeypatch):
    """The snapshot is taken inside the binding window, and a tool outside that window can
    still resolve this turn's document.

    This is the minimal reproduction of the incident: bind, refresh the snapshot,
    **leave the binding window**, call the tool. An implementation that reads the
    contextvar gets an empty value at the last step.
    """
    monkeypatch.setattr(idp, "get_config", lambda: {"clouddoc": {"enabled": True}})

    captured: dict = {}

    class _Toolkit:
        def get_tools(self): return []

    adapter = _adapter_with_toolkit(_Toolkit())

    # Inside the binding window: refresh the snapshot
    bind(channel_id=CLOUDDOC_CHANNEL_ID,
         metadata={"clouddoc": {"doc_id": DOC, "comment_id": "c1"}})
    assert is_unattended_clouddoc_turn() is True
    adapter._update_clouddoc_tools()
    captured = dict(adapter._clouddoc_turn)

    assert captured == {"doc_id": DOC, "comment_id": "c1", "mode": None}, \
        "绑定窗口内没有把授权取成快照"

    # Leave the binding window, simulating the context a tool actually runs in
    idp._CRON_TOOL_BOUND.set(False)
    assert get_clouddoc_turn_doc_id() is None, "前提：此刻直接读 contextvar 已经取不到"
    assert adapter._clouddoc_turn["doc_id"] == DOC, \
        "快照必须与 contextvar 解耦——否则工具在执行时拿到空 doc_id"


def test_permission_decision_survives_leaving_the_binding_window(bind, monkeypatch):
    """The same window that hid the doc_id from the tools hid the *turn* from the
    permission rail.

    ``clouddoc_apply_for_comment`` sits at tier ``ask``. Deciding the scene from the
    contextvar meant the scene hook fell through to the tiered engine, which raised an
    approval interrupt on a turn with nobody to answer it: the round ended with no text
    and the document got "this turn didn't complete". The rail therefore takes the same
    snapshot the tools take, and this asserts the decision **after** the window closes.
    """
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(idp, "get_config", lambda: {"clouddoc": {"enabled": True}})

    class _Toolkit:
        def get_tools(self): return []

    adapter = _adapter_with_toolkit(_Toolkit())
    rail = build_permission_rail(
        {"permissions": {"enabled": True}},
        unattended_clouddoc=adapter._unattended_clouddoc_turn,
    )
    hook = rail._host.permission_scene_hook

    bind(channel_id=CLOUDDOC_CHANNEL_ID,
         metadata={"clouddoc": {"doc_id": DOC, "comment_id": "c1",
                                "mode": "apply_scoped"}})
    adapter._update_clouddoc_tools()

    idp._CRON_TOOL_BOUND.set(False)
    assert is_unattended_clouddoc_turn() is False, \
        "前提：工具执行时 contextvar 已经解绑"

    outcome = asyncio.run(hook(_scene_hook_input("clouddoc_apply_for_comment")))
    assert outcome == ("approve",), "直改工具被挡在无人能答的审批中断上"

    # And the shape of the bug, so this test is known to be able to fail: a rail with
    # no snapshot has only the contextvars, which read False here -- it falls through to
    # the tiered engine, where clouddoc_apply_for_comment is `ask`.
    blind = build_permission_rail({"permissions": {"enabled": True}})
    assert asyncio.run(
        blind._host.permission_scene_hook(
            _scene_hook_input("clouddoc_apply_for_comment")
        )
    ) is None


def test_chat_turn_snapshot_stays_empty(bind, monkeypatch):
    """The chat path binds no authorization scope, so the snapshot must be empty and the
    tools impose no constraint."""
    monkeypatch.setattr(idp, "get_config", lambda: {"clouddoc": {"enabled": True}})

    class _Toolkit:
        def get_tools(self): return []

    adapter = _adapter_with_toolkit(_Toolkit())
    bind(channel_id="web", metadata={})
    adapter._update_clouddoc_tools()
    # An empty dict means unbound, and _resolve on the tool side imposes nothing.
    # Expressing unbound as {"doc_id": None} would make it indistinguishable from bound
    # with an empty value.
    assert adapter._clouddoc_turn == {}


def test_run_span_cleanup_is_bound_outside_the_try_block():
    """A name used in ``finally`` must be bound in the same function, **before** the ``try``.

    A production incident: ``close_agent_run_span`` was imported inside the try alongside
    ``open_agent_run_span``, while the finally called it. Anything raising earlier in the
    try made the finally itself fail with UnboundLocalError and **swallow the real
    exception whole** -- the user saw "cannot access local variable" and the actual
    failure was unknowable.

    The check runs on the AST rather than on text. This defect shows up only on
    exceptional paths, which are hard to construct reliably in a unit test, so structure
    is the only available criterion. An earlier version matched the call's shape in the
    source and reported falsely as soon as upstream reflowed the call across lines --
    **formatting is not the invariant, binding order is**.
    """
    import ast
    import inspect

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    NAME = "close_agent_run_span"

    def binds(node) -> bool:
        return isinstance(node, ast.ImportFrom) and any(a.name == NAME for a in node.names)

    def calls(stmts) -> bool:
        return any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == NAME
            for stmt in stmts
            for n in ast.walk(stmt)
        )

    tree = ast.parse(inspect.getsource(interface_deep))
    checked = 0
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bind_lines = [n.lineno for n in ast.walk(func) if binds(n)]
        for node in ast.walk(func):
            if not isinstance(node, ast.Try) or not calls(node.finalbody):
                continue
            checked += 1
            assert any(line < node.lineno for line in bind_lines), (
                f"{func.name} 的 finally 调用了 {NAME}，但该函数在 try 之前没有绑定它"
                f"（绑定于 {bind_lines}，try 在第 {node.lineno} 行）——"
                "异常路径上会以 UnboundLocalError 掩盖真实错误"
            )
    assert checked, f"没有找到任何在 finally 里调用 {NAME} 的 try，这条检查需要重写"


def test_closed_set_survives_every_early_return(bind, monkeypatch):
    """The ability boundary must precede every early return.

    All four paths -- feature disabled, credentials missing, extras absent, provider
    construction failing -- used to return before stripping, so an unattended turn would
    run with the full default tool set, bash included. That hung the ability boundary on
    a feature switch, when it should hang only on whether anyone is present this turn.
    """
    class _Card:
        def __init__(self, name): self.name = name

    for cfg in (
        {},                                              # clouddoc 段缺失
        {"clouddoc": {"enabled": False}},                # 功能未启用
        {"clouddoc": {"enabled": True, "credentials_file": ""}},   # 凭证缺失
    ):
        monkeypatch.setattr(idp, "get_config", lambda c=cfg: c)
        adapter = _adapter_with_toolkit(None)
        for n in ("bash", "read_file", "write_file", "code"):
            adapter._instance.ability_manager.add(_Card(n))

        bind(channel_id=CLOUDDOC_CHANNEL_ID,
             metadata={"clouddoc": {"doc_id": DOC, "comment_id": "c1"}})
        adapter._update_clouddoc_tools()

        left = [getattr(a, "name", "") for a in adapter._instance.ability_manager.list()]
        assert left == [], f"配置 {cfg} 下残留了危险工具: {left}"


def test_stale_authorization_is_not_reused_when_construction_fails(bind, monkeypatch):
    """A failed construction must still refresh the authorization snapshot rather than leave
    the previous turn's doc_id in place."""
    monkeypatch.setattr(idp, "get_config", lambda: {"clouddoc": {"enabled": False}})
    adapter = _adapter_with_toolkit(None)
    adapter._clouddoc_turn = {"doc_id": "上一轮的文档", "comment_id": "旧评论"}

    bind(channel_id=CLOUDDOC_CHANNEL_ID,
         metadata={"clouddoc": {"doc_id": DOC, "comment_id": "c1"}})
    adapter._update_clouddoc_tools()
    assert adapter._clouddoc_turn["doc_id"] == DOC, "沿用了上一轮的授权"


def test_the_ambiguity_rail_reads_the_session_of_the_current_turn(monkeypatch):
    """The toolkit is built once and reused; the session it reads must not be.

    Observed live: the ``user_text`` reader closed over the session id of the first
    construction, and that first session had said nothing. Every later session then
    hit the ambiguity rail as "the user has not named a document" across seven managed
    documents -- while its own message named the deck in full -- and the model, refused
    on a valid id, began inventing id shapes (a URL, then the title). Two halves keep
    this from recurring: the current session is recorded on **every** call, and the
    reader resolves it at call time rather than at construction.
    """
    import inspect

    monkeypatch.setattr(idp, "get_config", lambda: {"clouddoc": {"enabled": False}})

    class _Toolkit:
        def get_tools(self): return []

    adapter = _adapter_with_toolkit(_Toolkit())
    adapter._update_clouddoc_tools("session-A")
    adapter._update_clouddoc_tools("session-B")
    assert adapter._clouddoc_session_id == "session-B", "每一轮都必须刷新当前会话"

    src = inspect.getsource(idp.JiuWenSwarmDeepAdapter._update_clouddoc_tools)
    assert "user_text=lambda: _clouddoc_user_text(self._clouddoc_session_id)" in src, \
        "读取器必须在调用时解析会话，而不是构造时闭包捕获"
    assert "user_text=lambda: _clouddoc_user_text(session_id)" not in src
