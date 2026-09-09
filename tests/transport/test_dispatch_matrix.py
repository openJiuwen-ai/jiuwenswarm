# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.dispatch import HANDLERS, dispatch_to_handler, supported_methods
from jiuwenswarm.server.handlers import session as session_handlers
from jiuwenswarm.server.transports.sink import (
    STREAM_DONE,
    ResponseSink,
    SSESink,
    UnaryHTTPSink,
    WSSink,
)


class _RecordingServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        async def _call(*args, **kwargs):
            # 前三个位置参数固定为 (ws, request, send_lock)，其后才是 spec.args
            self.calls.append((name, args[3:], kwargs))

        return _call


class _Req:
    def __init__(self, method: ReqMethod, *, is_stream: bool = False) -> None:
        self.req_method = method
        self.is_stream = is_stream


_DISPATCH_TABLE_METHODS = frozenset({
    "AGENTS_CREATE", "AGENTS_DELETE", "AGENTS_DISABLE", "AGENTS_ENABLE", "AGENTS_GET",
    "AGENTS_LIST", "AGENTS_TOOLS_LIST", "AGENTS_UPDATE", "AGENT_PREWARM_SYNC",
    "AGENT_RELOAD_CONFIG", "BROWSER_RUNTIME_RESTART", "CHAT_CANCEL", "COMMAND_ADD_DIR",
    "COMMAND_BTW", "COMMAND_CHROME", "COMMAND_COMPACT", "COMMAND_COMPACT_PARTIAL",
    "COMMAND_CONTEXT", "COMMAND_DIFF", "COMMAND_MCP", "COMMAND_MODEL", "COMMAND_RECAP",
    "COMMAND_RESUME", "COMMAND_SANDBOX", "COMMAND_SESSION", "COMMAND_SIMPLIFY",
    "COMMAND_STATUS", "COMMAND_WORKFLOWS", "CONFIG_CACHE_CLEAR", "EXTENSIONS_DELETE",
    "EXTENSIONS_IMPORT", "EXTENSIONS_LIST", "EXTENSIONS_TOGGLE",
    "FILE_TRANSFER_CHUNK", "FILE_TRANSFER_COMPLETE", "FILE_TRANSFER_START",
    "HARNESS_PACKAGES_ACTIVATE", "HARNESS_PACKAGES_DEACTIVATE",
    "HARNESS_PACKAGES_DELETE", "HARNESS_PACKAGES_GET", "HARNESS_PACKAGES_SCAN",
    "HISTORY_GET", "HOOKS_LIST", "ISSUE_DELETE", "ISSUE_MATRIX", "ISSUE_STATE_LIST",
    "ISSUE_WATCH_ONCE", "PROACTIVE_TICK", "SCHEDULE_CANCEL", "SCHEDULE_CHECK_CONFIG",
    "SCHEDULE_CREATE", "SCHEDULE_DELETE", "SCHEDULE_LIST", "SCHEDULE_LOGS",
    "SCHEDULE_RUN", "SCHEDULE_STATUS", "SCHEDULE_UPDATE_CONFIG", "SESSION_DELETE",
    "SESSION_LIST", "SESSION_RENAME", "SESSION_REWIND", "SESSION_REWIND_AND_RESTORE",
    "SESSION_REWIND_COMPACT", "SESSION_REWIND_CONTEXT", "SESSION_SWITCH",
    "SYNC_AGENTS_CONFIGS", "TEAM_BINDINGS_LIST", "TEAM_BINDING_CREATE",
    "TEAM_BINDING_GENERATE", "TEAM_DELETE", "TEAM_HISTORY_GET", "TEAM_MEMBERS_GET",
    "TEAM_MQ_PUBLISH", "TEAM_RUNTIME_DISSOLVE", "TEAM_SESSION_BIND", "TEAM_SESSION_RESET", "TEAM_SNAPSHOT",
    "TEAM_TEMPLATES_LIST",
})

_BOOTSTRAP_METHODS = frozenset({
    "INITIALIZE", "SESSION_CREATE", "SESSION_FORK", "ACP_TOOL_RESPONSE",
})


def _permissions_methods() -> set[str]:
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        get_permissions_config_req_methods,
    )

    return {m.name for m in get_permissions_config_req_methods()}


def test_table_covers_expected_method_set() -> None:
    expected = _DISPATCH_TABLE_METHODS | _permissions_methods() | _BOOTSTRAP_METHODS
    current = {m.name for m in supported_methods()}
    assert current == expected, (
        f"仅在预期集合: {sorted(expected - current)}; 仅在表内: {sorted(current - expected)}"
    )


@pytest.mark.parametrize("method", sorted(HANDLERS, key=lambda m: m.value), ids=lambda m: m.value)
def test_declared_handler_exists(method: ReqMethod) -> None:
    spec = HANDLERS[method]
    assert callable(spec.resolve_fn(False)), f"{method} 的 fn 不可调用"
    if spec.stream_fn:
        assert callable(spec.stream_fn), f"{method} 的 stream_fn 不可调用"


@pytest.mark.parametrize(
    "method",
    sorted((m for m, s in HANDLERS.items() if s.fn is not None), key=lambda m: m.value),
    ids=lambda m: m.value,
)
def test_fn_signature_is_transport_free(method: ReqMethod) -> None:
    import inspect

    spec = HANDLERS[method]
    for fn in filter(None, (spec.resolve_fn(False), spec.stream_fn)):
        params = list(inspect.signature(fn).parameters)
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为 async"
        assert "self" not in params, f"{fn.__name__} 是自由函数，不应有 self"
        assert "ws" not in params, f"{fn.__name__} 仍带 ws 参数"
        assert "send_lock" not in params, f"{fn.__name__} 仍带 send_lock 参数"
        assert params[:1] == ["ctx"], f"{fn.__name__} 签名应为 (ctx, ...)：{params}"


def test_fn_handlers_do_not_remain_on_server() -> None:
    leftovers = sorted(
        f"_{spec.resolve_fn(False).__name__}"
        for spec in HANDLERS.values()
        if spec.fn is not None
        and hasattr(AgentWebSocketServer, f"_{spec.resolve_fn(False).__name__}")
    )
    assert not leftovers, f"这些 handler 在 handlers/ 里，但 server 上仍有同名方法：{leftovers}"


def test_handler_spec_requires_callable_fn() -> None:
    from jiuwenswarm.server.dispatch import HandlerSpec as Spec

    with pytest.raises(ValueError):
        Spec()                                  # 未指定 fn
    with pytest.raises(ValueError):
        Spec(fn="_not_callable")                # 字符串（旧的 method 形态）
    with pytest.raises(ValueError):
        Spec(fn=lambda ctx: None, stream_fn="x")  # stream_fn 也要可调用


@pytest.mark.parametrize(
    "method",
    sorted(HANDLERS, key=lambda m: m.value),
    ids=lambda m: m.value,
)
def test_ctx_dispatch_invokes_with_context(method: ReqMethod) -> None:
    from jiuwenswarm.server.context import RequestContext

    seen: list[RequestContext] = []

    async def fake_handler(ctx, *args, **kwargs):
        seen.append(ctx)

    spec = HANDLERS[method]
    patched = type(spec)(fn=fake_handler, args=spec.args, kwargs=spec.kwargs)
    sink = UnaryHTTPSink()
    ctx = RequestContext(
        request=_Req(method), sink=sink, connection_id="c1", services=None  # type: ignore[arg-type]
    )
    HANDLERS[method] = patched
    try:
        handled = asyncio.run(
            dispatch_to_handler(None, None, _Req(method), None, context_factory=lambda: ctx)
        )
    finally:
        HANDLERS[method] = spec
    assert handled is True
    assert seen and seen[0].sink is sink


#: 不参与表分发的 handler —— 分发骨架与默认路径。
#: 新增例外必须在此显式登记，避免「迁了方法却忘了改表」悄悄溜过。
NON_DISPATCH_HANDLERS = frozenset(
    {
        "_handle_message",          # 分发入口本身：字节 -> AgentRequest，属传输层
        # sandbox.* 配置 E2A 的二级分发分支：_handle_message 尾部按
        # get_sandbox_config_req_methods() 命中后直接调用，不登记主表（无 session、
        # 非聊天语义）；set 后需触发 reload_agents_config，故留在 server 上。
        "_handle_sandbox_config",
    }
)


def test_no_orphan_handler_methods() -> None:
    import inspect

    on_server = {
        name
        for name, _ in inspect.getmembers(AgentWebSocketServer, inspect.isfunction)
        if name.startswith("_handle_")
    }
    orphans = sorted(on_server - NON_DISPATCH_HANDLERS)
    assert not orphans, (
        f"以下 handler 既不在分发表、也未登记为非分发：{orphans}\n"
        f"若确属默认路径/其内部分支，请加入 NON_DISPATCH_HANDLERS 并说明原因。"
    )

    # 反向：清单里的名字必须**还在** server 上。
    # 少了这半条，清单会悄悄腐烂 —— 例如 6 个 _handle_sandbox_* 移走后，
    # 它们在清单里又躺了一轮没人发现；残留的名字等于给未来的同名方法发了张免检票。
    stale = sorted(NON_DISPATCH_HANDLERS - on_server)
    assert not stale, (
        f"NON_DISPATCH_HANDLERS 里这些名字已不在 server 上（多半是迁走了）：{stale}"
        f" —— 请从清单删除，否则它对同名新方法就是一张静默免检票。"
    )


def test_default_path_methods_are_not_in_table() -> None:
    for method in [ReqMethod.CHAT_SEND, ReqMethod.CHAT_RESUME]:
        assert method not in HANDLERS, f"{method} 不应在表内（它走默认路径）"
        server = _RecordingServer()
        handled = asyncio.run(dispatch_to_handler(server, None, _Req(method), None))
        assert handled is False, f"{method} 应未命中以便落到默认路径"


def test_second_dispatch_point_is_merged() -> None:
    import inspect

    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    for method in [
        ReqMethod.INITIALIZE,
        ReqMethod.SESSION_CREATE,
        ReqMethod.SESSION_FORK,
        ReqMethod.ACP_TOOL_RESPONSE,
    ]:
        assert method in HANDLERS, f"{method} 应在主表内，不得另起分发"
        fn = HANDLERS[method].resolve_fn(False)
        src = inspect.getsource(fn)
        assert "bootstrap_preconditions" in src, (
            f"{fn.__name__} 未声明前置条件 —— 并表后 telemetry 绑定与 "
            f"ensure_persistent_checkpointer 不再由调用链提供，必须显式包住方法体"
        )

    # 内层分发必须已从默认路径移除，否则会出现「两处都在管」
    from jiuwenswarm.server.handlers import _default as default_path

    impl_src = inspect.getsource(default_path._handle_unary_impl)
    assert "ReqMethod.INITIALIZE" not in impl_src, (
        "_handle_unary_impl 里仍残留第二处分发，与主表重复"
    )


def test_history_get_stream_variant() -> None:
    spec = HANDLERS[ReqMethod.HISTORY_GET]
    assert spec.resolve_fn(True) is session_handlers.handle_history_get_stream
    assert spec.resolve_fn(False) is session_handlers.handle_history_get
    assert spec.resolve_fn(True) is not spec.resolve_fn(False)


def test_dispatch_raises_when_table_and_impl_diverge() -> None:

    class Empty:
        def __getattr__(self, name):  # 模拟"没有该方法"
            raise AttributeError(name)

    with pytest.raises(AttributeError):
        asyncio.run(dispatch_to_handler(Empty(), None, _Req(ReqMethod.SESSION_LIST), None))


def _resp() -> AgentResponse:
    return AgentResponse(request_id="r1", channel_id="web", ok=True, payload={"a": 1})


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def test_all_sinks_satisfy_protocol() -> None:
    sinks = [WSSink(_FakeWS(), asyncio.Lock()), UnaryHTTPSink(), SSESink()]
    assert all(isinstance(s, ResponseSink) for s in sinks)


_VOLATILE_TOP = ("timestamp",)
_VOLATILE_PROVENANCE = ("converted_at",)


def _stable(wire: dict) -> dict:
    out = {k: v for k, v in wire.items() if k not in _VOLATILE_TOP}
    prov = out.get("provenance")
    if isinstance(prov, dict):
        out["provenance"] = {k: v for k, v in prov.items() if k not in _VOLATILE_PROVENANCE}
    return out


def test_ws_sink_wire_matches_legacy_encoding() -> None:
    ws = _FakeWS()
    resp = _resp()
    asyncio.run(WSSink(ws, asyncio.Lock()).send_unary(resp))
    actual = json.loads(ws.sent[0])
    expected = encode_agent_response_for_wire(resp, response_id="r1")
    assert _stable(actual) == _stable(expected)
    assert actual.get("timestamp"), "timestamp 不应缺失"

    ws2 = _FakeWS()
    chunk = AgentResponseChunk(request_id="r1", channel_id="web", payload={"d": 9})
    asyncio.run(WSSink(ws2, asyncio.Lock()).send_chunk(chunk, sequence=5))
    actual2 = json.loads(ws2.sent[0])
    expected2 = encode_agent_chunk_for_wire(chunk, response_id="r1", sequence=5)
    assert _stable(actual2) == _stable(expected2)
    assert actual2.get("sequence") == 5


def test_unary_http_sink_keeps_object_without_serializing() -> None:
    sink = UnaryHTTPSink()
    resp = _resp()
    asyncio.run(sink.send_unary(resp))
    assert sink.response is resp  # 同一对象，未经序列化往返


def test_sse_sink_order_and_sentinel() -> None:
    sink = SSESink()

    async def run():
        for i in range(3):
            await sink.send_chunk(
                AgentResponseChunk(request_id="r1", channel_id="web", payload={"i": i}),
                sequence=i,
            )
        await sink.finish()
        out = []
        while True:
            item = await sink.queue.get()
            if item is STREAM_DONE:
                return out
            out.append(item)

    frames = asyncio.run(run())
    assert len(frames) == 3
    assert all(isinstance(f, dict) for f in frames)


def test_oversized_payload_returns_false() -> None:
    from jiuwenswarm.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES

    big = AgentResponse(
        request_id="r1",
        channel_id="web",
        ok=True,
        payload={"x": "A" * (AGENT_WS_SEND_BUDGET_BYTES + 1024)},
    )
    sent = asyncio.run(WSSink(_FakeWS(), asyncio.Lock()).send_unary(big))
    assert sent is False


def test_send_error_shape() -> None:
    sink = UnaryHTTPSink()
    asyncio.run(sink.send_error("r9", "boom", code="NOT_FOUND"))
    assert sink.response is not None
    assert sink.response.ok is False
    assert sink.response.payload["code"] == "NOT_FOUND"


def test_no_duplicate_method_names_in_server_modules() -> None:
    import ast
    from pathlib import Path

    server_dir = Path("jiuwenswarm/server")
    if not server_dir.exists():  # 打包安装场景下跳过
        pytest.skip("源码目录不可用")

    # 既有重名的豁免登记。**保持为空**是目标状态：发现新的重名应当去修，而不是往这里加。
    KNOWN_PREEXISTING: set[str] = set()  # 曾有的一处已清理，保持为空即可

    offenders: list[str] = []
    # Large modules (e.g. interface_deep) can trip CPython AST recursion limits.
    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous_limit, 10000))
    try:
        for path in sorted(server_dir.rglob("*.py")):
            if ".bak-" in path.name:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SystemError as exc:
                pytest.skip(f"ast.parse recursion limit on {path.as_posix()}: {exc}")
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                seen: dict[str, int] = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # @property / @x.setter 等合法重名：装饰器里带同名属性
                        decorated = any(
                            isinstance(d, ast.Attribute)
                            and d.attr in {"setter", "getter", "deleter"}
                            for d in item.decorator_list
                        )
                        key = f"{path.as_posix()}::{node.name}.{item.name}"
                        if item.name in seen and not decorated and key not in KNOWN_PREEXISTING:
                            offenders.append(
                                f"{path.as_posix()}:{item.lineno} {node.name}.{item.name} "
                                f"（先定义于第 {seen[item.name]} 行，将被覆盖）"
                            )
                        seen[item.name] = item.lineno
    finally:
        sys.setrecursionlimit(previous_limit)

    assert not offenders, "发现重名方法（后者会静默覆盖前者）：\n  " + "\n  ".join(offenders)


def test_service_members_matches_actual_handler_usage() -> None:
    import ast
    import pathlib

    from jiuwenswarm.server.context import SERVICE_MEMBERS

    from jiuwenswarm.server import handlers as handlers_pkg

    handlers_dir = pathlib.Path(handlers_pkg.__file__).parent
    # 扫描范围 = 全部 ctx.services 的消费者：handlers/<域>.py 与汇合点 pipeline.py。
    # pipeline 也经 ctx.services 访问服务端（如按 connection_id 读 ACP 能力），
    # 漏掉它会让清单里合法的成员被误判成「多余」。
    sources = [py for py in sorted(handlers_dir.glob("*.py")) if py.name != "__init__.py"]
    sources.append(handlers_dir.parent / "pipeline.py")
    def _is_ctx_services(node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id == "services":
            return True
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
            and node.attr == "services"
        )

    used: set[str] = set()
    for py in sources:
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 形态一：属性语法 ctx.services.xxx
            if isinstance(node, ast.Attribute) and _is_ctx_services(node.value):
                used.add(node.attr)
            # 形态二：动态访问 getattr/setattr/hasattr(ctx.services, "xxx", ...)
            #
            # 必须一并扫：``getattr(ctx.services, "x", None)`` 会把门面抛的
            # AttributeError 吞成默认值 —— 越界访问**不报错、只是拿到 None**，
            # 正是白名单最该拦住的那种静默失效。只看属性语法会漏掉它们。
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "setattr", "hasattr"}
                and len(node.args) >= 2
                and _is_ctx_services(node.args[0])
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                used.add(node.args[1].value)

    # SERVICE_MEMBERS 是「门面公有名 → server 实际属性名」的映射，比对用 key。
    missing = sorted(used - SERVICE_MEMBERS.keys())
    assert not missing, (
        f"handler 用了但未登记的成员：{missing}"
        f" —— 登记进 context.SERVICE_MEMBERS，或把它改为域内模块级函数。"
    )
    stale = sorted(SERVICE_MEMBERS.keys() - used)
    assert not stale, (
        f"SERVICE_MEMBERS 里已无人使用的成员：{stale}"
        f" —— 请删除，否则它对未来的越界访问就是一张免检票。"
    )

    # 门面对外只暴露公有名：下划线不得再穿透到业务层（G.CLS.11）。
    leaked = sorted(name for name in used if name.startswith("_"))
    assert not leaked, (
        f"业务层直接访问了受保护成员：{['ctx.services.' + n for n in leaked]}"
        f" —— 改用 SERVICE_MEMBERS 登记的公有名，私有名只应出现在 context.py 的映射里。"
    )


def test_server_has_no_dangling_self_attribute_calls() -> None:
    import ast
    import inspect

    from jiuwenswarm.server import agent_ws_server as mod

    tree = ast.parse(inspect.getsource(mod))
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "AgentWebSocketServer"
    )

    defined: set[str] = {
        m.name for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(cls):
        # self.x = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    defined.add(target.attr)
        # self.x: T = ...
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
        ):
            defined.add(node.target.attr)
    # 类属性（含 ClassVar）
    for m in cls.body:
        if isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
            defined.add(m.target.id)
        if isinstance(m, ast.Assign):
            for target in m.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)

    used = {
        node.attr: node.lineno
        for node in ast.walk(cls)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }

    dangling = sorted(
        (lineno, name) for name, lineno in used.items() if name not in defined
    )
    assert not dangling, (
        "以下 self.X 在类上找不到定义，运行到即 AttributeError（多半是 helper 搬走了、"
        "调用方留在原地）：\n  "
        + "\n  ".join(f"agent_ws_server.py:{ln}  self.{n}" for ln, n in dangling)
    )


def test_sandbox_bootstrap_helpers_callable_from_server() -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.context import AgentServerServices
    from jiuwenswarm.server.handlers.sandbox import (
        allocate_internal_jiuwenbox_port,
        parse_sandbox_host_port,
    )

    host, port = parse_sandbox_host_port("http://127.0.0.1:8321")
    assert (host, port) == ("127.0.0.1", 8321)

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)

    class _Runner:
        def is_owned_listener(self, host: str, port: int) -> bool:
            return True          # 已拥有该监听 → 应直接复用 preferred_port

    server._jiuwenbox_runner = _Runner()  # type: ignore[attr-defined]

    # 与 agent_ws_server._bootstrap_internal_jiuwenbox 里的调用形态完全一致
    got = allocate_internal_jiuwenbox_port(AgentServerServices(server), host, port)
    assert got == port, f"已拥有监听时应复用 preferred_port，实际 {got}"

    # 端口被占 → 走 pick_free_tcp_port；这条同时验证门面对方法的解析
    class _BusyRunner:
        def is_owned_listener(self, host: str, port: int) -> bool:
            return False

    server._jiuwenbox_runner = _BusyRunner()  # type: ignore[attr-defined]
    server._is_tcp_port_bindable = lambda h, p: False  # type: ignore[attr-defined]
    server._pick_free_tcp_port = lambda h: 54321       # type: ignore[attr-defined]

    got = allocate_internal_jiuwenbox_port(AgentServerServices(server), host, port)
    assert got == 54321, f"端口被占时应改用内核分配的空闲端口，实际 {got}"
