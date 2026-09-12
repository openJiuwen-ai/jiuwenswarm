# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""复现:不同 session 的并发请求在 AgentServer 内被串行阻塞.

生产现象:
  多条【不同 session】的并发 chat.send,AgentServer 日志从
  ``[AgentWebSocketServer] 收到请求``(agent_ws_server.py:504) 到
  ``[JiuWenClaw] 处理流式请求``(interface.py:969) 耗时 20+ 秒,
  而请求本身只处理 7 秒。同 session 串行队列(SessionManager)已排除。

串行化来源 —— 每个新 session 的首条请求都要执行:
  AgentManager.get_agent(缓存 miss) → _create_agent(agent_manager.py:282)
    → JiuWenClaw.create_instance(interface.py:387)
      → ToolManager.load_tools_from_disk(tool_manager.py:309)

该函数虽为 ``async def`` 但包含:
  (1) 同步段: tools_dir.mkdir / glob / 逐文件 open+json.load —— 无 await
      让出点, 直接阻塞整个 event loop, 所有并发协程停摆;
  (2) 逐工具 ``await _add_mcp_server_and_ability`` → 全局单例
      ``Runner.resource_mgr.add_mcp_server``(stdio MCP 需 spawn 子进程并
      握手, 生产单工具可达数百 ms), 且同一 AgentManager 下所有 session
      共享同一 user_workspace_dir → 同一 tools 目录 → 并发注册同一批工具。

N 个不同 session 在单 event loop 上, 上述两段近似串行累加:
  最后一个请求的 B3→B5 ≈ N × 单 agent 建立耗时。

用例分两层:
  * test_shadow_loader_serializes_distinct_sessions —— 零依赖复刻
    load_tools_from_disk 的代码结构, 本地必跑, 输出完成时刻阶梯;
  * test_get_agent_concurrent_new_sessions_staircase —— 拉真实
    AgentManager/tool_manager 的端到端链路, 因本仓库 .venv 存量坏包
    (pysbd / openjiuwen reasoning_bank 的 SyntaxError)导致 import 失败时
    自动 skip, 在生产镜像(python3.11 完整依赖)环境可跑。

运行(仓库根目录):
  PYTHONPATH="agent-runtime/foundation;agent-runtime/management" \
    python -m pytest tests/unit/test_agentserver_multi_session_create_serialization.py -v -s --no-cov

判读: 各 session 完成时刻呈等差阶梯(步长 ≈ 单 agent 建立耗时)即复现;
真并行应全部在同一时刻附近完成。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

# --- 模拟参数: 生产 stdio MCP 单工具 spawn+握手 数百 ms 量级 ---
_SYNC_SECONDS_PER_TOOL = 0.12  # add_mcp_server 内同步段(子进程 spawn/文件)
_ASYNC_SECONDS_PER_TOOL = 0.08  # 握手等真实异步段
_NUM_TOOLS = 4
_NUM_SESSIONS = 6


def _write_tool_configs(tools_dir: Path, count: int) -> None:
    """落盘 count 个工具配置(生产中由 tools.add 写入, 每新 session 全量加载)."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        record = {
            "name": f"mcp_tool_{i}",
            "description": f"tool {i}",
            "type": "stdio",
            "command": "python",
            "args": ["-m", f"fake_mcp_{i}"],
        }
        (tools_dir / f"mcp_tool_{i}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )


class _ShadowGlobalResourceMgr:
    """复刻全局单例 Runner.resource_mgr 的关键特征.

    add_mcp_server 在真实实现里对 stdio 工具要先 spawn 子进程/读写文件
    (同步段, 不让出 event loop), 再等待握手(异步段)。所有 agent 实例
    共享同一个全局单例 —— 与生产 resource_mgr 一致。
    """

    def __init__(self) -> None:
        self.registered: list[str] = []

    async def add_mcp_server(self, mcp_cfg: Any, *, tag: str) -> Any:
        time.sleep(_SYNC_SECONDS_PER_TOOL)
        await asyncio.sleep(_ASYNC_SECONDS_PER_TOOL)
        self.registered.append(str(getattr(mcp_cfg, "server_name", tag)))
        return None  # None 视为 ok(见 tool_manager._mcp_add_result_is_ok)


_SHADOW_RESOURCE_MGR = _ShadowGlobalResourceMgr()


class _ShadowAbilityManager:
    def add(self, card: Any) -> None:
        pass


class _ShadowToolManager:
    """复刻 tool_manager.ToolManager.load_tools_from_disk 的代码结构.

    与生产(tool_manager.py:309-359)逐段对应:
      tools_dir.mkdir                 -> :316
      sorted(tools_dir.glob(*.json))  -> :321 (同步目录扫描)
      open + json.load                -> :323-324 (同步读+解析)
      create_mcp_tool                 -> :340
      await _add_mcp_server_and_ability -> :341 (全局单例注册)
      agent.ability_manager.add       -> :80
    """

    def __init__(self, get_agent: Any, get_tools_dir: Any) -> None:
        self._get_agent = get_agent
        self._get_tools_dir = get_tools_dir

    async def _add_mcp_server_and_ability(
        self, agent: Any, mcp_cfg: Any, *, tag: str
    ) -> None:
        result = await _SHADOW_RESOURCE_MGR.add_mcp_server(mcp_cfg, tag=tag)
        if result is None:
            agent.ability_manager.add(mcp_cfg)
            return
        raise RuntimeError("add_mcp_server 失败")

    async def load_tools_from_disk(self) -> dict[str, Any]:
        agent = self._get_agent()
        if agent is None:
            raise RuntimeError("JiuWenClaw 未初始化，请先调用 create_instance()")

        tools_dir = self._get_tools_dir()
        tools_dir.mkdir(parents=True, exist_ok=True)
        registered: list[dict[str, str]] = []

        for path in sorted(tools_dir.glob("*.json")):  # 同步扫描
            with open(path, encoding="utf-8") as f:  # 同步读
                record = json.load(f)  # 同步解析
            name = str(record.get("name") or path.stem)
            mcp_cfg = types.SimpleNamespace(server_name=name, server_id=f"id_{name}")
            await self._add_mcp_server_and_ability(agent, mcp_cfg, tag=name)
            registered.append({"name": name, "id": mcp_cfg.server_id})

        return {"tools_dir": str(tools_dir), "registered_tools": registered}


@pytest.mark.unit
async def test_shadow_loader_serializes_distinct_sessions(tmp_path: Path) -> None:
    """机制层复现: 并发调用 load_tools_from_disk(不同 agent 实例) 被串行化.

    每次调用对应一个新 session 的 create_instance; agents 互不相干,
    若无串行化应全部并行完成(墙钟 ≈ 单次耗时)。
    """
    tools_dir = tmp_path / "tools"
    _write_tool_configs(tools_dir, _NUM_TOOLS)

    def _new_agent() -> Any:
        return types.SimpleNamespace(ability_manager=_ShadowAbilityManager())

    per_call = _NUM_TOOLS * (_SYNC_SECONDS_PER_TOOL + _ASYNC_SECONDS_PER_TOOL)
    serial_bound = _NUM_SESSIONS * per_call

    start = time.monotonic()
    done_at: list[float] = []

    async def one_new_session(index: int) -> None:
        tm = _ShadowToolManager(
            get_agent=_new_agent, get_tools_dir=lambda: tools_dir
        )
        await tm.load_tools_from_disk()  # 即 create_instance 内的那次调用
        done_at.append(time.monotonic() - start)

    await asyncio.gather(*(one_new_session(i) for i in range(_NUM_SESSIONS)))
    elapsed = time.monotonic() - start

    done_at.sort()
    for rank, t in enumerate(done_at, start=1):
        print(f"[shadow] 第 {rank} 个 session 的 agent 建立完成: +{t:.2f}s")

    print(
        f"[shadow] 并发 {_NUM_SESSIONS} session 总耗时={elapsed:.2f}s "
        f"单次理论={per_call:.2f}s 真并行理想≈{per_call:.2f}s 完全串行≈{serial_bound:.2f}s"
    )
    # 串行证据一: 墙钟远超单次耗时(完全串行时应接近 N 倍)
    assert elapsed > 2.5 * per_call, (
        f"elapsed={elapsed:.2f}s 未体现串行化(真并行理想={per_call:.2f}s)"
    )
    # 串行证据二: 墙钟达到完全串行界的一半以上(工作量被大量串行执行)
    assert elapsed > 0.5 * serial_bound, (
        f"elapsed={elapsed:.2f}s 距串行界{serial_bound:.2f}s 过远, 串行化不充分"
    )


@pytest.mark.unit
async def test_all_async_but_global_lock_still_serializes(tmp_path: Path) -> None:
    """变体: 注册全程 await(零同步 sleep), 但整段包在全局单例的锁内.

    复刻 ``Runner.resource_mgr`` 的锁语义: await 只让出 CPU, 不让出锁 ——
    同一时刻只有一个 session 的注册在临界区内推进。预期完全串行:
    墙钟 ≈ N × 单次, 且各 session 完成时刻呈等差阶梯(生产排队形态)。
    """
    tools_dir = tmp_path / "tools_lock"
    _write_tool_configs(tools_dir, _NUM_TOOLS)

    global_lock = asyncio.Lock()
    per_tool_wait = _SYNC_SECONDS_PER_TOOL + _ASYNC_SECONDS_PER_TOOL
    per_call = _NUM_TOOLS * per_tool_wait

    async def locked_add(agent: Any, mcp_cfg: Any, *, tag: str) -> None:
        async with global_lock:  # 全局单例临界区: 全 await 也无法并行
            await asyncio.sleep(per_tool_wait)
            agent.ability_manager.add(mcp_cfg)

    class _LockedLoader(_ShadowToolManager):
        async def _add_mcp_server_and_ability(
            self, agent: Any, mcp_cfg: Any, *, tag: str
        ) -> None:
            await locked_add(agent, mcp_cfg, tag=tag)

    def _new_agent() -> Any:
        return types.SimpleNamespace(ability_manager=_ShadowAbilityManager())

    start = time.monotonic()
    done_at: list[float] = []

    async def one_new_session(index: int) -> None:
        tm = _LockedLoader(get_agent=_new_agent, get_tools_dir=lambda: tools_dir)
        await tm.load_tools_from_disk()
        done_at.append(time.monotonic() - start)

    await asyncio.gather(*(one_new_session(i) for i in range(_NUM_SESSIONS)))
    elapsed = time.monotonic() - start

    done_at.sort()
    for rank, t in enumerate(done_at, start=1):
        print(f"[global-lock] 第 {rank} 个 session 的 agent 建立完成: +{t:.2f}s")
    steps = [round(b - a, 2) for a, b in zip(done_at, done_at[1:])]
    print(
        f"[global-lock] 并发 {_NUM_SESSIONS} session 总耗时={elapsed:.2f}s "
        f"单次理论={per_call:.2f}s 完全串行≈{_NUM_SESSIONS * per_call:.2f}s "
        f"相邻完成间隔={steps}"
    )
    # 全 await + 全局锁 ⇒ 接近完全串行(留 20% 余量)
    assert elapsed > 0.8 * _NUM_SESSIONS * per_call, (
        f"elapsed={elapsed:.2f}s 未体现全局锁串行(完全串行≈{_NUM_SESSIONS * per_call:.2f}s)"
    )
    # 阶梯证据: 相邻完成间隔 ≈ 单次锁持有时长(单工具注册), 而非 0(同刻完成)
    assert min(steps) > 0.5 * per_tool_wait


@pytest.mark.unit
async def test_sync_segment_plus_global_lock_combined(tmp_path: Path) -> None:
    """完整模型: 同步段 + 全局锁并存 —— 最贴近生产 ``add_mcp_server``.

    预测: 锁把 N×K 次注册排成一条队列(进度被排序 ⇒ 阶梯),
    同步段进一步冻结 loop 拉大每格耗时。
    """
    tools_dir = tmp_path / "tools_full"
    _write_tool_configs(tools_dir, _NUM_TOOLS)

    global_lock = asyncio.Lock()
    per_tool_wait = _SYNC_SECONDS_PER_TOOL + _ASYNC_SECONDS_PER_TOOL
    per_call = _NUM_TOOLS * per_tool_wait

    async def locked_add(agent: Any, mcp_cfg: Any, *, tag: str) -> None:
        async with global_lock:
            time.sleep(_SYNC_SECONDS_PER_TOOL)  # 临界区内的同步段
            await asyncio.sleep(_ASYNC_SECONDS_PER_TOOL)
            agent.ability_manager.add(mcp_cfg)

    class _FullLoader(_ShadowToolManager):
        async def _add_mcp_server_and_ability(
            self, agent: Any, mcp_cfg: Any, *, tag: str
        ) -> None:
            await locked_add(agent, mcp_cfg, tag=tag)

    def _new_agent() -> Any:
        return types.SimpleNamespace(ability_manager=_ShadowAbilityManager())

    start = time.monotonic()
    done_at: list[float] = []

    async def one_new_session(index: int) -> None:
        tm = _FullLoader(get_agent=_new_agent, get_tools_dir=lambda: tools_dir)
        await tm.load_tools_from_disk()
        done_at.append(time.monotonic() - start)

    await asyncio.gather(*(one_new_session(i) for i in range(_NUM_SESSIONS)))
    elapsed = time.monotonic() - start

    done_at.sort()
    for rank, t in enumerate(done_at, start=1):
        print(f"[sync+lock] 第 {rank} 个 session 的 agent 建立完成: +{t:.2f}s")
    steps = [round(b - a, 2) for a, b in zip(done_at, done_at[1:])]
    print(
        f"[sync+lock] 并发 {_NUM_SESSIONS} session 总耗时={elapsed:.2f}s "
        f"单次理论={per_call:.2f}s 完全串行≈{_NUM_SESSIONS * per_call:.2f}s "
        f"相邻完成间隔={steps}"
    )
    assert elapsed > 0.8 * _NUM_SESSIONS * per_call
    assert min(steps) > 0.5 * per_tool_wait  # 阶梯: 进度被锁排序


def _import_real_chain() -> Any:
    """尝试拉起真实 AgentManager / tool_manager 链路.

    本仓库 .venv 存量坏包(pysbd、openjiuwen context_evolver 的
    SyntaxError: invalid escape)会让 import 失败 —— 那是环境问题而非
    本用例问题, 此时 skip; 生产镜像内依赖完整可正常执行。
    """
    try:
        import pysbd  # noqa: F401
    except SyntaxError:
        # pysbd 仅被分句链路使用, stub 掉不影响本用例关注的路径
        fake_pkg = types.ModuleType("pysbd")
        fake_seg = types.ModuleType("pysbd.segmenter")

        class _Segmenter:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

        fake_seg.Segmenter = _Segmenter
        fake_pkg.Segmenter = _Segmenter
        fake_pkg.segmenter = fake_seg
        sys.modules.setdefault("pysbd", fake_pkg)
        sys.modules.setdefault("pysbd.segmenter", fake_seg)

    from jiuwenclaw.agentserver import tool_manager as tm_mod
    from jiuwenclaw.agentserver.agent_manager import AgentManager

    return tm_mod, AgentManager


@pytest.mark.unit
async def test_get_agent_concurrent_new_sessions_staircase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端: 并发 get_agent(不同 session) 的完成时刻呈阶梯.

    覆盖生产路径 AgentManager.get_agent → _create_agent → create_instance
    → load_tools_from_disk; 用 FakeJiuWenClaw 隔离 LLM/adapter, 只保留
    工具加载段(B3→B5 的主要耗时), 注册耗时注入与机制用例一致。
    """
    try:
        tm_mod, AgentManager = _import_real_chain()
    except (SyntaxError, ModuleNotFoundError) as exc:
        pytest.skip(
            "本地 .venv 依赖不完整/存量坏包, 无法 import 真实链路"
            "(生产镜像可跑; 本地需 PYTHONPATH=agent-runtime/foundation;"
            f"agent-runtime/management 且依赖完整): {exc}"
        )

    tools_dir = tmp_path / "tools"
    _write_tool_configs(tools_dir, _NUM_TOOLS)

    async def fake_add(agent: Any, mcp_cfg: Any, *, tag: str) -> None:
        time.sleep(_SYNC_SECONDS_PER_TOOL)
        await asyncio.sleep(_ASYNC_SECONDS_PER_TOOL)

    def fake_create_mcp_tool(single_json: str) -> Any:
        record = json.loads(single_json)
        return types.SimpleNamespace(
            server_name=str(record.get("name", "unknown")),
            server_id=f"id_{record.get('name', 'unknown')}",
        )

    monkeypatch.setattr(tm_mod, "_add_mcp_server_and_ability", fake_add)
    monkeypatch.setattr(tm_mod, "create_mcp_tool", fake_create_mcp_tool)

    import jiuwenclaw.agentserver.interface as interface_mod

    class FakeJiuWenClaw:
        def __init__(
            self,
            user_workspace_dir: str | None = None,
            agent_id: str | None = None,
            service_id: str | None = None,
            assembly_cache: Any = None,
        ) -> None:
            self._agent_name = "fake"
            self._tool_manager = tm_mod.ToolManager(
                get_agent=lambda: self,
                get_tools_dir=lambda: tools_dir,
            )

        async def create_instance(
            self, config: dict | None = None, *, mode: str = "agent"
        ) -> None:
            await self._tool_manager.load_tools_from_disk()

        async def reload_agent_config(self, *args: Any, **kwargs: Any) -> None:
            return None

    # _create_agent 内部是函数级 from ... import JiuWenClaw,
    # patch 模块命名空间即可生效
    monkeypatch.setattr(interface_mod, "JiuWenClaw", FakeJiuWenClaw)

    mgr = AgentManager(
        agent_id="ag",
        service_id="svc",
        user_workspace_dir=tmp_path / "workspace",
    )

    # 基准: 单 session 冷启动耗时
    t0 = time.monotonic()
    await mgr.get_agent(channel_id="web", mode="agent", session_id="warmup")
    single = time.monotonic() - t0
    print(f"[get_agent] 单 session 冷启动基准: {single:.2f}s")

    # 并发: N 个全新 session 同时首达(对应生产 N 条不同 session 的请求)
    start = time.monotonic()
    done_at: dict[str, float] = {}

    async def one_session(sid: str) -> None:
        await mgr.get_agent(channel_id="web", mode="agent", session_id=sid)
        done_at[sid] = time.monotonic() - start

    await asyncio.gather(*(one_session(f"session_{i}") for i in range(_NUM_SESSIONS)))
    elapsed = time.monotonic() - start

    for sid, t in sorted(done_at.items(), key=lambda kv: kv[1]):
        print(f"[get_agent] {sid} 完成: +{t:.2f}s")

    print(
        f"[get_agent] 并发 {_NUM_SESSIONS} 个 session 总耗时={elapsed:.2f}s "
        f"单session基准={single:.2f}s 真并行理想≈{single:.2f}s 完全串行≈{_NUM_SESSIONS * single:.2f}s"
    )
    assert elapsed > 2.5 * single, (
        f"elapsed={elapsed:.2f}s vs 单条基准={single:.2f}s, 未复现串行化"
    )
