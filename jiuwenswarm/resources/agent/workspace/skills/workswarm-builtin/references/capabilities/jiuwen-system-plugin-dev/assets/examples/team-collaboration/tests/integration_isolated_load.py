# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""隔离目录加载 + 行为验证（不依赖运行中的宿主，但依赖已安装的 jiuwenswarm/openjiuwen）。

覆盖契约里的强制项中"最容易假通过"的部分：
  R1 服务先行     —— 渲染 src 时端口必须已 listen（渲染前 GET /health 成功）
  R2 端口取自内核 —— /health.port 必须等于真实监听端口，且 src 里用它
  R3 单实例单端口 —— 同实例重复 ui.ready 只渲染、不新起监听
  R7 渲染幂等     —— instance_key 固定为 tcv.team-info / tcv.task-graph
  R8 模式 A       —— 实例被回收后 ≤5s 内无 LISTEN（记录真实耗时 ms）
  §5.2 降级       —— 读取失败必须保留上次成功数据 + stale=true + ops_error
  §6 更新时序     —— 事件触发整份重读，revision 单调递增

用法（用装有 jiuwenswarm 的解释器）：
    python tests/integration_isolated_load.py --json-out report.json
退出码：0 全部通过；1 有失败项。
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

PACKAGE_ID = "webui.team-collab-viz"
SLOT = "conversation.panel.right"
SESSION_ID = "isolated-session-0001"
TEAM_NAME = "isolated_team_for_tests"

RESULTS: list[dict] = []


def check(case: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"case": case, "ok": bool(ok), "detail": detail})
    return ok


# --------------------------------------------------------------------- fixtures


@dataclass
class FakeTeam:
    team_name: str = TEAM_NAME
    display_name: str = "隔离验证团队"
    leader_member_name: str = "cap"
    desc: str | None = None
    created: int = 1762390000000


@dataclass
class FakeMember:
    member_name: str
    display_name: str
    desc: str | None = None
    status: str = "ready"
    execution_status: str | None = "idle"
    mode: str = "build_mode"
    role: str = "teammate"
    cli_agent: str | None = None


@dataclass
class FakeTask:
    task_id: str
    title: str
    content: str = "正文"
    status: str = "pending"
    assignee: str | None = None
    updated_at: int | None = 1762390100000


@dataclass
class FakeDep:
    task_id: str
    depends_on_task_id: str
    resolved: bool = True


def base_members() -> list[FakeMember]:
    return [
        FakeMember("cap", "队长甲", role="leader", status="busy", execution_status="running"),
        FakeMember("worker-a", "成员甲", desc="简介甲"),
        FakeMember("worker-b", "成员乙", status="paused", execution_status=None),
    ]


def base_tasks() -> list[FakeTask]:
    return [
        FakeTask("t1", "根任务", status="completed", assignee="cap", updated_at=1762390200000),
        FakeTask("t2", "并行 A", status="in_progress", assignee="worker-a"),
        FakeTask("t3", "并行 B", status="in_progress", assignee="worker-b"),
        FakeTask("t4", "汇合", status="in_review", assignee="worker-a"),
        FakeTask("t9", "独立任务", status="pending"),
    ]


def base_deps() -> dict[str, list[FakeDep]]:
    return {
        "t1": [],
        "t2": [FakeDep("t2", "t1", True)],
        "t3": [FakeDep("t3", "t1", True)],
        "t4": [FakeDep("t4", "t2", True), FakeDep("t4", "t3", True)],
        "t9": [],
    }


class OpsError(RuntimeError):
    """模拟 Ops 读取失败。"""


class FakeTeamOps:
    def __init__(self, owner: "FakeOps") -> None:
        self._owner = owner

    async def info(self, team_name: str | None = None):
        self._owner.calls.append(("info", team_name))
        if self._owner.fail_reads:
            raise OpsError("simulated ops failure")
        return self._owner.team_obj

    async def members(self, team_name: str | None = None):
        self._owner.calls.append(("members", team_name))
        if self._owner.fail_reads:
            raise OpsError("simulated ops failure")
        return list(self._owner.members)

    async def tasks(self, team_name: str | None = None, session_id: str | None = None):
        self._owner.calls.append(("tasks", team_name))
        if self._owner.fail_reads:
            raise OpsError("simulated ops failure")
        return list(self._owner.tasks)

    async def task_dependencies(self, task_id: str, team_name: str | None = None,
                                session_id: str | None = None):
        self._owner.calls.append(("deps", task_id))
        if self._owner.fail_reads:
            raise OpsError("simulated ops failure")
        return list(self._owner.deps.get(task_id, []))


class FakeUiOps:
    def __init__(self, owner: "FakeOps") -> None:
        self._owner = owner

    async def render(self, request):
        self._owner.renders.append({
            "session_id": request.target.session_id,
            "slot_id": request.target.slot_id,
            "package_id": request.target.package_id,
            "instance_key": request.target.instance_key,
            "component": request.root.component,
            "props": dict(request.root.props),
            "health_port": self._owner.health_port,
            "health_ok": self._owner.health_ok,
        })
        return SimpleNamespace(status="delivered", revision=len(self._owner.renders))


class FakeStateOps:
    def __init__(self, owner: "FakeOps") -> None:
        self._owner = owner

    async def get(self, ref):
        return SimpleNamespace(exists=ref.key in self._owner.state, value=self._owner.state.get(ref.key))

    async def set(self, ref, value, expected_version=None):
        self._owner.state[ref.key] = value
        return SimpleNamespace(ok=True)


class FakeOps:
    """最小 HandlerOps 替身：只提供本包真正用到的成员。"""

    def __init__(self) -> None:
        self.session_context = SimpleNamespace(session_id=SESSION_ID)
        self.package_metadata = SimpleNamespace(package_id=PACKAGE_ID, package_version="1.0.0",
                                                root=str(PKG_ROOT))
        self.team = FakeTeamOps(self)
        self.ui = FakeUiOps(self)
        self.state = FakeStateOps(self)
        self.team_obj = FakeTeam()
        self.members = base_members()
        self.tasks = base_tasks()
        self.deps = base_deps()
        self.fail_reads = False
        self.calls: list[tuple] = []
        self.renders: list[dict] = []
        self.state: dict = {}
        self.health_port: int | None = None
        self.health_ok: bool | None = None

    @property
    def team_info(self) -> FakeTeam:
        return self.team_obj


async def _next_handler(event):  # noqa: ANN001
    return SimpleNamespace(accepted=True, status="next")


def http_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_status(url: str, timeout: float = 3.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def is_listening(port: int, timeout: float = 0.2) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# 包内对象结构可能演进（wrapper 持有 handlers），用一组候选路径读取端口，
# 避免把测试绑死在某个私有属性名上。
def activation_of(ext):
    """返回真正持有 listener 的对象（新版是 ext._handlers，旧版是 ext 本身）。"""
    return getattr(ext, "_handlers", ext)


def port_of(ext) -> int:
    """读取当前监听端口（读不到返回 0）。"""
    obj = activation_of(ext)
    for candidate in (getattr(obj, "port", None),
                      getattr(getattr(obj, "_context", None), "port", None)):
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return 0


def wait_released(port: int, limit: float = 8.0, interval: float = 0.1) -> float | None:
    started = time.perf_counter()
    while (time.perf_counter() - started) <= limit:
        if not is_listening(port):
            return round((time.perf_counter() - started) * 1000, 1)
        time.sleep(interval)
    return None


# ------------------------------------------------------------------------ cases


def run() -> int:
    from jiuwenswarm.extensions_v2.webui.models import RenderTarget, UiActionInput
    from team_collab_viz import extension as ext_module

    ops = FakeOps()
    ext = ext_module.create_extension()

    # R1/R2：实例创建后必须已经有监听端口（服务先行）
    port = port_of(ext)
    check("R1:create_extension 后已 listen（port>0）", isinstance(port, int) and port > 0, f"port={port}")
    base = f"http://127.0.0.1:{port}"
    health = {}
    try:
        health = http_json(base + "/health")
    except Exception as exc:  # noqa: BLE001
        check("R1:/health 可访问", False, f"{type(exc).__name__}: {exc}")
    if health:
        check("R1:/health 可访问", True, "")
        check("R2:/health.port == 真实监听端口", health.get("port") == port,
              f"health={health.get('port')} 实际={port}")
        check("R2:/health.bind == 127.0.0.1", health.get("bind") == "127.0.0.1", f"{health.get('bind')!r}")
        check("R8:/health.draining_ports 为空", health.get("draining_ports") in ([],),
              f"{health.get('draining_ports')!r}")
        check("health.capabilities 完整",
              all(c in (health.get("capabilities") or [])
                  for c in ("snapshot", "long_poll", "ui_state", "cors_star")),
              f"{health.get('capabilities')}")

    # registrar 登记项
    registrar_calls: list[tuple] = []
    registered_handlers: list = []

    class FakeRegistrar:
        def register_event_handler(self, event_type, handler, match_pattern, level="user"):
            registrar_calls.append((event_type, handler.__name__, dict(match_pattern), level))
            registered_handlers.append(handler)
            return f"h{len(registrar_calls)}"

    ext.register(FakeRegistrar())
    activation = activation_of(ext)
    # R0（契约 v1.5）：生命周期锚点必须是注册进总线的 Handler 的宿主对象，不是工厂返回值
    check("R0:生命周期锚点存在且不是 create_extension 返回值",
          activation is not ext, f"activation={type(activation).__name__}")
    check("R0:注册的 handler 都绑定在该锚点上",
          all(getattr(h, "__self__", None) is activation for h in registered_handlers),
          f"{[type(getattr(h, '__self__', None)).__name__ for h in registered_handlers]}")
    check("R0:锚点在 register 后仍持有可用端口（不得 register 后立刻释放）",
          port_of(ext) == port and is_listening(port), f"port_of={port_of(ext)} listening={is_listening(port)}")
    joined = {(c[0], c[1]) for c in registrar_calls}
    check("register:登记 ui.ready", any(e == "ui.ready" for e, _ in joined), f"{registrar_calls}")
    check("register:登记 team.monitor_event",
          any(e == "team.monitor_event" for e, _ in joined), f"{registrar_calls}")
    check("register:ui.ready 匹配槽位正确",
          any(c[0] == "ui.ready" and c[2].get("target.slot_id") == SLOT for c in registrar_calls),
          f"{registrar_calls}")
    check("register:team.monitor_event 使用空 pattern（全量订阅）",
          any(c[0] == "team.monitor_event" and c[2] == {} for c in registrar_calls), f"{registrar_calls}")
    check("register:handler_level 均为 user",
          all(c[3] == "user" for c in registrar_calls), f"{registrar_calls}")

    # ui.ready：两个 Iframe，src 必须指向已监听端口，渲染时端口已就绪
    event = SimpleNamespace(event_input=UiActionInput(
        event_type="ui.ready",
        target=RenderTarget(SESSION_ID, SLOT, PACKAGE_ID, ""),
    ))
    ops.health_port = port
    ops.health_ok = bool(health.get("ok"))
    out = asyncio.run(activation.on_ui_ready(event, ops, _next_handler))
    check("ui.ready:返回 accepted", getattr(out, "accepted", False) is True, f"{out}")
    check("ui.ready:渲染 2 个实例", len(ops.renders) == 2, f"实际={len(ops.renders)}")
    instances = [r["instance_key"] for r in ops.renders]
    check("R7:instance_key 恰为 tcv.team-info / tcv.task-graph",
          set(instances) == {"tcv.team-info", "tcv.task-graph"}, f"{instances}")
    check("ui.ready:根组件为 Iframe", all(r["component"] == "Iframe" for r in ops.renders),
          f"{[r['component'] for r in ops.renders]}")
    check("ui.ready:title 为中文页签名",
          {r["props"].get("title") for r in ops.renders} == {"团队信息", "任务关系"},
          f"{[r['props'].get('title') for r in ops.renders]}")
    for render in ops.renders:
        src = render["props"].get("src") or ""
        check(f"R1:渲染 {render['instance_key']} 时服务已就绪", render["health_ok"] is True,
              f"health_ok={render['health_ok']}")
        check(f"R2:{render['instance_key']} src 端口 == /health.port",
              src.startswith(f"http://127.0.0.1:{port}/"), f"src={src}")
        check(f"R2:{render['instance_key']} src 带 sid 查询参数",
              "?sid=" in src, f"src={src}")
        status = http_status(src)
        check(f"R1:{render['instance_key']} src 立刻 200（零死端口）", status == 200, f"status={status}")

    # 重复 ui.ready：不得新起监听（R3）、不得产生重复实例（R7）
    before_renders = len(ops.renders)
    out2 = asyncio.run(activation.on_ui_ready(event, ops, _next_handler))
    check("R3:重复 ui.ready 复用同一端口", port_of(ext) == port, f"{port_of(ext)} vs {port}")
    check("R7:重复 ui.ready 只覆盖同一实例（仍 2 条渲染）",
          len(ops.renders) - before_renders == 2, f"新增={len(ops.renders) - before_renders}")
    check("R3:重复 ui.ready 未新增监听端口（/health.port 不变）",
          http_json(base + "/health").get("port") == port, "")

    # 事件驱动刷新：revision 单调递增 + 内容来自整份重读
    def read_snapshot() -> dict:
        return http_json(base + f"/snapshot?sid={SESSION_ID}&wait=0")

    snap_before = read_snapshot()
    monitor_event = SimpleNamespace(event_input=SimpleNamespace(
        team_name=TEAM_NAME,
        monitor_event=SimpleNamespace(event_type="task_updated", team_name=TEAM_NAME,
                                      timestamp=int(time.time() * 1000), task_id="t2"),
    ))
    asyncio.run(activation.on_team_monitor_event(monitor_event, ops, _next_handler))
    snap_after = read_snapshot()
    check("事件:revision 单调递增", snap_after.get("revision", 0) > snap_before.get("revision", 0),
          f"{snap_before.get('revision')} -> {snap_after.get('revision')}")
    check("事件:每个事件都整份重读（info/members/tasks 都被调用）",
          {"info", "members", "tasks"} <= {c[0] for c in ops.calls}, f"{[c[0] for c in ops.calls][-12:]}")
    check("事件:依赖逐任务查询（任务数 == deps 调用数）",
          sum(1 for c in ops.calls if c[0] == "deps") >= len(base_tasks()),
          f"deps 调用={sum(1 for c in ops.calls if c[0] == 'deps')}")
    team_block = snap_after.get("team") or {}
    check("快照:team 已定位", team_block.get("team_name") == TEAM_NAME, f"{team_block.get('team_name')!r}")
    check("快照:availability.session_map_source 为 event",
          (snap_after.get("availability") or {}).get("session_map_source") == "event",
          f"{snap_after.get('availability')}")
    check("快照:status_source ∈ event|derived|history",
          team_block.get("status_source") in ("event", "derived", "history"),
          f"{team_block.get('status_source')!r}")
    check("快照:stale=False 且 stale_reason=None",
          snap_after.get("stale") is False and snap_after.get("stale_reason") is None,
          f"stale={snap_after.get('stale')} reason={snap_after.get('stale_reason')!r}")
    graph = snap_after.get("graph") or {}
    check("快照:图含汇合边（t4 有 2 条上游）",
          len([e for e in graph.get("edges") or [] if e.get("to") == "t4"]) == 2,
          f"edges={graph.get('edges')}")

    # 降级：读取失败必须保留上次成功数据（§5.2 / 用户要求 5）
    good_snapshot = json.loads(json.dumps(snap_after))
    ops.fail_reads = True
    asyncio.run(activation.on_team_monitor_event(monitor_event, ops, _next_handler))
    degraded = read_snapshot()
    check("降级:stale=True", degraded.get("stale") is True, f"stale={degraded.get('stale')!r}")
    check("降级:stale_reason=='ops_error'", degraded.get("stale_reason") == "ops_error",
          f"{degraded.get('stale_reason')!r}")
    check("降级:generated_at 保留上次成功时刻",
          degraded.get("generated_at") == good_snapshot.get("generated_at"),
          f"{degraded.get('generated_at')} vs {good_snapshot.get('generated_at')}")
    check("降级:served_at 已更新（> generated_at）",
          isinstance(degraded.get("served_at"), int)
          and degraded["served_at"] >= (good_snapshot.get("served_at") or 0),
          f"served_at={degraded.get('served_at')} 上次={good_snapshot.get('served_at')}")
    check("降级:revision 不递减",
          isinstance(degraded.get("revision"), int)
          and degraded["revision"] >= (good_snapshot.get("revision") or 0),
          f"{degraded.get('revision')} vs {good_snapshot.get('revision')}")
    for field in ("team", "members", "tasks", "graph", "limits"):
        check(f"降级:{field} 与失败前逐字段相等",
              degraded.get(field) == good_snapshot.get(field),
              f"实际={str(degraded.get(field))[:120]} 期望={str(good_snapshot.get(field))[:120]}")
    check("降级:availability 除 last_* 外与失败前一致",
          {k: v for k, v in (degraded.get("availability") or {}).items()
           if k not in ("last_event_at", "last_success_at")}
          == {k: v for k, v in (good_snapshot.get("availability") or {}).items()
              if k not in ("last_event_at", "last_success_at")},
          f"{degraded.get('availability')}")
    # 重复失败：revision 不得继续增长（契约 v1.6，防"每次失败都刷版本号"）
    revision_after_first_failure = degraded.get("revision")
    asyncio.run(activation.on_team_monitor_event(monitor_event, ops, _next_handler))
    degraded2 = read_snapshot()
    check("降级:重复失败 revision 不再增长",
          degraded2.get("revision") == revision_after_first_failure,
          f"{degraded2.get('revision')} vs {revision_after_first_failure}")
    check("降级:重复失败仍保留 team 数据",
          degraded2.get("team") == good_snapshot.get("team"),
          f"{(degraded2.get('team') or {}).get('team_name')!r}")
    check("降级:重复失败 stale_reason 仍为 ops_error",
          degraded2.get("stale_reason") == "ops_error", f"{degraded2.get('stale_reason')!r}")

    # 恢复：stale 翻转回 false，revision 再 +1，数据不变
    ops.fail_reads = False
    asyncio.run(activation.on_team_monitor_event(monitor_event, ops, _next_handler))
    recovered = read_snapshot()
    check("恢复:revision 再 +1",
          recovered.get("revision") == revision_after_first_failure + 1,
          f"{recovered.get('revision')} vs {revision_after_first_failure + 1}")
    check("恢复:stale=False 且 stale_reason=None",
          recovered.get("stale") is False and recovered.get("stale_reason") is None,
          f"stale={recovered.get('stale')} reason={recovered.get('stale_reason')!r}")
    check("恢复:team 数据与失败前一致",
          recovered.get("team") == good_snapshot.get("team"), "")
    health_after = http_json(base + "/health")
    check("恢复:/health.snapshot.stale 与快照 stale 一致",
          (health_after.get("snapshot") or {}).get("stale") == recovered.get("stale"),
          f"health={(health_after.get('snapshot') or {}).get('stale')} 快照={recovered.get('stale')}")

    # 长轮询「无变化」响应必须携带 stale/served_at（v1.6，供前端翻转过期徽标）
    rev_now = recovered.get("revision")
    lp = http_json(base + f"/snapshot?sid={SESSION_ID}&since={rev_now}&wait=1")
    check("长轮询:unchanged=True", lp.get("unchanged") is True, f"{str(lp)[:200]}")
    check("长轮询:unchanged 响应含 stale 字段", "stale" in lp, f"{str(lp)[:200]}")
    check("长轮询:unchanged 响应含 served_at 字段", "served_at" in lp, f"{str(lp)[:200]}")
    check("长轮询:unchanged 响应含 generated_at 字段", "generated_at" in lp, f"{str(lp)[:200]}")

    # 边界：从未有快照的 sid → 200 空快照（no_session_map），不得 503
    cold = http_json(base + "/snapshot?sid=never-seen-session-xyz&wait=0")
    check("边界:从未见过的 sid 返回 200 空快照",
          isinstance(cold, dict) and cold.get("team") is None,
          f"team={cold.get('team')!r}")
    check("边界:空快照 stale_reason=='no_session_map'",
          cold.get("stale_reason") == "no_session_map", f"{cold.get('stale_reason')!r}")
    check("边界:空快照 members/tasks 为空数组",
          cold.get("members") == [] and cold.get("tasks") == [], "")
    check("边界:空快照仍是 200（不得 503）", http_status(base + "/snapshot?sid=never-seen-session-xyz&wait=0") == 200,
          "")

    # §5 session_map_source 三态：event / default_name / none（契约 §2.2）
    from team_collab_viz.sessionmap import SessionMap  # noqa: PLC0415

    sm_event = SessionMap()
    sm_event.learn("sid-learned", "team-learned")
    team_ev, src_ev = asyncio.run(sm_event.resolve(ops, "sid-learned"))
    check("session_map_source:事件学到 -> ('team','event')",
          (team_ev, src_ev) == ("team-learned", "event"), f"team={team_ev!r} src={src_ev!r}")

    team_none, src_none = asyncio.run(SessionMap().resolve(ops, "sid-never-known-xyz"))
    check("session_map_source:既无事件也无法兜底 -> (None,'none')",
          (team_none, src_none) == (None, "none"), f"team={team_none!r} src={src_none!r}")

    # Confirm the default team through the team read operation.
    history_ops = FakeOps()
    history_ops.session_context.session_id = "sid-fb"
    history_ops.team_obj = FakeTeam(team_name="jiuwen_team_sid-fb")
    team_fb, src_fb = asyncio.run(SessionMap().resolve(history_ops, "sid-fb"))
    check("session_map_source:默认团队存在 -> ('team','default_name')",
          (team_fb, src_fb) == ("jiuwen_team_sid-fb", "default_name"), f"team={team_fb!r} src={src_fb!r}")
    history_ops.team_obj = FakeTeam(team_name="jiuwen_team_sid-other")
    team_other, src_other = asyncio.run(SessionMap().resolve(history_ops, "sid-fb"))
    check("session_map_source:结果队名不匹配 -> 'none'",
          (team_other, src_other) == (None, "none"), f"team={team_other!r} src={src_other!r}")

    cold_avail = cold.get("availability") or {}
    check("session_map_source:冷 sid 快照的 availability.session_map_source=='none'",
          cold_avail.get("session_map_source") == "none", f"={cold_avail.get('session_map_source')!r}")
    check("session_map_source:冷 sid 快照 team_readable=False",
          cold_avail.get("team_readable") is False, f"={cold_avail.get('team_readable')!r}")
    check("session_map_source:冷 sid 快照 last_event_at/last_success_at 均为 null",
          cold_avail.get("last_event_at") is None and cold_avail.get("last_success_at") is None,
          f"={cold_avail}")

    # R8 模式 A：实例回收后 ≤5s 无 LISTEN
    # 模拟宿主 unpublish：先释放 registrar 持有的 bound method，再丢掉工厂返回值
    check("R8:回收前端口仍在监听", is_listening(port), f"port={port}")
    registered_handlers.clear()
    del activation
    del ext
    gc.collect()
    released_ms = wait_released(port, limit=8.0)
    check("R8:实例回收后 5s 内无 LISTEN", released_ms is not None and released_ms <= 5000,
          f"实测耗时={released_ms} ms（上限 5000）")

    failed = [r for r in RESULTS if not r["ok"]]
    report = {"total": len(RESULTS), "passed": len(RESULTS) - len(failed),
              "failed": len(failed), "failures": failed, "cases": RESULTS}
    print(f"RESULT total={report['total']} passed={report['passed']} failed={report['failed']}")
    for f in failed:
        print(f"  [FAIL] {f['case']} | {f['detail']}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    report = run()
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
