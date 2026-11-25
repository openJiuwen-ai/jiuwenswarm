# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 契约符合性检查（黑盒，仅依赖契约 §2 / §5，不含任何实现细节）。

用法：
    python api_contract_check.py --base http://127.0.0.1:52301 --sid <session_id>
    python api_contract_check.py --base http://127.0.0.1:52301 --sid <sid> --json-out report.json

退出码：0 = 全部通过；1 = 存在不通过项。
仅使用标准库，可在任意 Python 3.8+ 运行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RESULTS: list[dict] = []
PKG_ROOT = Path(__file__).resolve().parents[1]


def _source_mtime() -> str:
    """Latest mtime of the package's own sources（证据元数据，不是被测面）。"""
    stamp, name = _source_mtime_ms()
    if not stamp:
        return "unknown"
    return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp / 1000))} ({name})"


def _source_mtime_ms() -> tuple[int, str]:
    """(后端源码 mtime 毫秒, 文件名)。

    只统计 `team_collab_viz/**/*.py` —— 只有这些是进程内 import 的代码；
    `web/` 是请求时现读的静态文件，把它的 mtime 算进来会造成误判（前端改动 ≠ 实例变旧）。
    """
    newest, newest_name = 0, ""
    for path in sorted((PKG_ROOT / "team_collab_viz").rglob("*.py")):
        stamp = int(path.stat().st_mtime * 1000)
        if stamp > newest:
            newest, newest_name = stamp, str(path.relative_to(PKG_ROOT))
    return newest, newest_name


def _web_mtime_ms() -> str:
    """`web/` 最新改动时间（仅作为元数据记录，不参与新鲜度判据）。"""
    newest = 0
    for path in sorted((PKG_ROOT / "web").rglob("*")):
        if path.is_file():
            newest = max(newest, int(path.stat().st_mtime * 1000))
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest / 1000))
            if newest else "unknown")


def _source_fingerprint() -> dict[str, str]:
    """各后端源文件 sha256 前 16 位（与 backend-dev 公布的指纹可直接对照）。"""
    return {path.relative_to(PKG_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            for path in sorted((PKG_ROOT / "team_collab_viz").rglob("*.py"))}


def _web_fingerprint() -> dict[str, str]:
    """`web/**` 各文件 sha256 前 16 位：只记录、不参与新鲜度闸门（静态文件每请求现读）。"""
    out: dict[str, str] = {}
    for path in sorted((PKG_ROOT / "web").rglob("*")):
        if path.is_file():
            out[path.relative_to(PKG_ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return out


def _record(case: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"case": case, "ok": bool(ok), "detail": detail})
    return ok


def _check(case: str, cond: bool, detail: str = "") -> bool:
    return _record(case, cond, detail)


class Resp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, elapsed: float):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body
        self.elapsed = elapsed

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


def request(base: str, path: str, method: str = "GET", body: bytes | None = None,
            headers: dict[str, str] | None = None, timeout: float = 35.0) -> Resp:
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Resp(resp.status, dict(resp.headers), resp.read(), time.perf_counter() - started)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要能读到头与体
        return Resp(exc.code, dict(exc.headers or {}), exc.read(), time.perf_counter() - started)
    except urllib.error.URLError as exc:
        raise SystemExit(f"连接失败 {url}: {exc}") from exc


# ---------------------------------------------------------------- CORS 与通用头


def check_cors(base: str, sid: str) -> None:
    cases = [
        ("cors:GET /snapshot", request(base, f"/snapshot?sid={urllib.parse.quote(sid)}&wait=0")),
        ("cors:GET /health", request(base, "/health")),
        ("cors:GET /ui-state", request(base, f"/ui-state?sid={urllib.parse.quote(sid)}")),
        ("cors:GET /web/team-info.html", request(base, f"/web/team-info.html?sid={urllib.parse.quote(sid)}")),
        ("cors:GET /not-exist", request(base, "/definitely-not-a-route")),
    ]
    for name, resp in cases:
        detail = f"status={resp.status} ACAO={resp.header('access-control-allow-origin')!r} " \
                 f"ACAM={resp.header('access-control-allow-methods')!r} " \
                 f"ACAH={resp.header('access-control-allow-headers')!r} " \
                 f"ACAC={resp.header('access-control-allow-credentials')!r}"
        _check(name + " ACAO=*", resp.header("access-control-allow-origin") == "*", detail)
        _check(name + " 无 Allow-Credentials",
               "access-control-allow-credentials" not in resp.headers, detail)
        allow_methods = resp.header("access-control-allow-methods").upper().replace(" ", "")
        _check(name + " Allow-Methods 含 GET/PUT/PATCH/OPTIONS",
               all(m in allow_methods for m in ("GET", "PUT", "PATCH", "OPTIONS")), detail)
        allow_headers = resp.header("access-control-allow-headers").lower().replace(" ", "")
        _check(name + " Allow-Headers 含 content-type",
               "content-type" in allow_headers, detail)
        if "json" in resp.header("content-type"):
            _check(name + " Cache-Control: no-store",
                   "no-store" in resp.header("cache-control").lower(), detail)


def check_preflight(base: str) -> dict:
    resp = request(base, "/ui-state", method="OPTIONS", headers={
        "Origin": "null",
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "content-type",
    })
    detail = f"status={resp.status} headers={ {k: v for k, v in resp.headers.items() if k.startswith('access') or k == 'cache-control'} }"
    _check("preflight:OPTIONS /ui-state -> 204", resp.status == 204, detail)
    _check("preflight:ACAO=*", resp.header("access-control-allow-origin") == "*", detail)
    methods = resp.header("access-control-allow-methods").upper().replace(" ", "")
    _check("preflight:Allow-Methods 含 PATCH（契约 v1.3）", "PATCH" in methods, detail)
    _check("preflight:Allow-Methods 含 PUT", "PUT" in methods, detail)
    _check("preflight:Allow-Methods 含 GET", "GET" in methods, detail)
    _check("preflight:Allow-Headers 含 Content-Type",
           "content-type" in resp.header("access-control-allow-headers").lower().replace(" ", ""), detail)
    _check("preflight:无 Allow-Credentials",
           "access-control-allow-credentials" not in resp.headers, detail)
    return {"status": resp.status, "headers": resp.headers}


# ---------------------------------------------------------------- /health


def check_health(base: str) -> dict:
    resp = request(base, "/health")
    _check("health:200", resp.status == 200, f"status={resp.status}")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _check("health:JSON 可解析", False, str(exc))
        return {}
    _check("health:JSON 可解析", True, "")
    for field in ("ok", "pid", "port", "bind", "started_at", "snapshot", "events", "ui_state", "capabilities"):
        _check(f"health:含字段 {field}", field in data, f"keys={sorted(data)}")
    _check("health:bind=127.0.0.1", data.get("bind") == "127.0.0.1", f"bind={data.get('bind')!r}")
    expected_port = urllib.parse.urlsplit(base).port
    _check("health:port 与自身监听端口一致", data.get("port") == expected_port,
           f"health.port={data.get('port')} 实际={expected_port}")
    caps = data.get("capabilities") or []
    for cap in ("snapshot", "long_poll", "ui_state", "cors_star"):
        _check(f"health:capabilities 含 {cap}", cap in caps, f"capabilities={caps}")
    _check("health:draining_ports 存在且为 list（v1.2）",
           isinstance(data.get("draining_ports"), list), f"draining_ports={data.get('draining_ports')!r}")
    _check("health:draining_ports 正常为空（未误用模式 B）",
           data.get("draining_ports") in ([], None), f"draining_ports={data.get('draining_ports')!r}")
    snap = data.get("snapshot") or {}
    for field in ("revision", "generated_at", "stale", "session_map_source", "scopes"):
        _check(f"health.snapshot:含字段 {field}", field in snap, f"keys={sorted(snap)}")
    events = data.get("events") or {}
    _check("health.events:handler_registered=true", events.get("handler_registered") is True,
           f"events={events}")
    return data


# ---------------------------------------------------------------- /snapshot 矩阵


TOP_FIELDS = {
    "schema_version": str,
    "revision": int,
    "generated_at": int,
    "served_at": int,
    "stale": bool,
    "scope": dict,
    "availability": dict,
    "members": list,
    "tasks": list,
    "graph": dict,
    "limits": dict,
}

AVAILABILITY_FIELDS = ("session_map_source", "team_readable", "last_event_at", "last_success_at")
TEAM_FIELDS = ("team_name", "display_name", "leader_member_name", "desc", "created_at",
               "status", "status_label", "status_source", "progress")
PROGRESS_FIELDS = ("total", "completed", "cancelled", "in_review", "blocked", "pending",
                   "in_progress", "percent")
# 契约 §2.1 / §2.3 / §2.4 / §2.5 / §2.6 的逐键清单（FROZEN）。
# 断言用**键集合**（required ⊆ keys ⊆ required ∪ optional），不用计数：计数会被选填键坑。
TOP_REQUIRED = ("schema_version", "revision", "generated_at", "served_at", "stale", "stale_reason",
                "scope", "availability", "team", "members", "tasks", "graph", "limits")
MEMBER_FIELDS = ("member_name", "display_name", "desc", "role", "role_label", "role_group", "is_leader",
                 "status", "status_label", "execution_status", "execution_label", "mode",
                 "cli_agent", "task_progress", "active_task_count", "current_task")
MEMBER_OPTIONAL = ("unknown_enum",)   # 契约 §2.4：选填，缺省不出现（等价 false）
TASK_OPTIONAL = ("unknown_enum",)
GRAPH_FIELDS = ("edges", "layers", "node_order", "has_cycle")
ROLE_LABELS = {"leader": "队长", "teammate": "成员", "human_agent": "人类代理",
               "passive_human": "人类席位", "bridge_agent": "外部桥接成员",
               "worker": "执行工人", "external_cli": "外部 CLI 成员"}
SCHEMA_VERSION = "1.1"  # 契约 v1.3 起为 "1.1"（新增 members[].role_label）
TASK_FIELDS = ("task_id", "title", "content", "status", "status_label", "assignee",
               "blocked_by", "blocks", "unresolved_blocked_by", "updated_at", "depth", "flags")
TEAM_STATUS_LABELS = {"running": "运行中", "standby": "待机", "cleaned": "已结束",
                      "completed": "已完成", "history": "历史记录"}


def _keyset(tag: str, block: dict, required, optional=()) -> bool:
    """键集合断言：required 必须全在，且不得出现 required|optional 之外的键。"""
    keys = set(block)
    missing = [k for k in required if k not in keys]
    extra = sorted(keys - set(required) - set(optional))
    _check(f"{tag}:键集合 == 契约（{len(required)} 必填"
           f"{' + 选填 ' + str(len(optional)) if optional else ''}，无缺无多）",
           not missing and not extra,
           f"缺={missing} 多={extra} 实际keys={sorted(keys)}")
    return not missing and not extra


def check_snapshot_fields(snap: dict, tag: str = "snapshot") -> None:
    _keyset(f"{tag}:顶层（契约 §2.1，13 键）", snap, TOP_REQUIRED)
    for field, typ in TOP_FIELDS.items():
        present = field in snap
        _check(f"{tag}:顶层字段 {field}", present, f"keys={sorted(snap)}")
        if present:
            _check(f"{tag}:{field} 类型 {typ.__name__}", isinstance(snap[field], typ),
                   f"实际类型={type(snap[field]).__name__}")
    _check(f"{tag}:schema_version=='{SCHEMA_VERSION}'", snap.get("schema_version") == SCHEMA_VERSION,
           f"schema_version={snap.get('schema_version')!r}（契约 v1.3 要求 {SCHEMA_VERSION}）")
    rev = snap.get("revision")
    _check(f"{tag}:revision>=1", isinstance(rev, int) and rev >= 1, f"revision={rev!r}")
    stale = snap.get("stale")
    reason = snap.get("stale_reason")
    if stale is False:
        _check(f"{tag}:stale=false 时 stale_reason 为 null", reason is None, f"stale_reason={reason!r}")
    else:
        _check(f"{tag}:stale=true 时 stale_reason 合法",
               reason in ("ops_error", "no_session_map", "cold_start"), f"stale_reason={reason!r}")
    scope = snap.get("scope") or {}
    _check(f"{tag}:scope.kind ∈ session|global", scope.get("kind") in ("session", "global"), f"scope={scope}")
    _check(f"{tag}:scope.key 非空", bool(scope.get("key")), f"scope={scope}")
    _check(f"{tag}:stale_reason 键存在（§2.1）", "stale_reason" in snap, f"keys={sorted(snap)}")
    _check(f"{tag}:team 键存在（§2.1，可为 null）", "team" in snap, f"keys={sorted(snap)}")
    avail = snap.get("availability") or {}
    _keyset(f"{tag}:availability（契约 §2.2，4 键）", avail, AVAILABILITY_FIELDS)
    for field in AVAILABILITY_FIELDS:
        _check(f"{tag}:availability.{field} 存在", field in avail, f"keys={sorted(avail)}")
    _check(f"{tag}:session_map_source 合法",
           avail.get("session_map_source") in ("event", "default_name", "none"),
           f"={avail.get('session_map_source')!r}")
    limits = snap.get("limits") or {}
    _check(f"{tag}:limits.members_total", isinstance(limits.get("members_total"), int), f"limits={limits}")
    _check(f"{tag}:limits.tasks_total", isinstance(limits.get("tasks_total"), int), f"limits={limits}")
    _check(f"{tag}:limits 与数组长度一致",
           limits.get("members_total") == len(snap.get("members") or [])
           and limits.get("tasks_total") == len(snap.get("tasks") or []),
           f"limits={limits} members={len(snap.get('members') or [])} tasks={len(snap.get('tasks') or [])}")


def check_team_block(snap: dict, tag: str = "snapshot") -> None:
    team = snap.get("team")
    if team is None:
        _check(f"{tag}:team=null 时 members 为空", snap.get("members") == [], "")
        _check(f"{tag}:team=null 时 tasks 为空", snap.get("tasks") == [], "")
        _check(f"{tag}:team=null 时 availability.session_map_source=='none'",
               (snap.get("availability") or {}).get("session_map_source") == "none",
               f"={(snap.get('availability') or {}).get('session_map_source')!r}")
        graph = snap.get("graph") or {}
        _check(f"{tag}:team=null 时 graph 全空",
               graph.get("edges") == [] and graph.get("layers") == [] and graph.get("node_order") == [],
               f"graph={graph}")
        return
    for field in TEAM_FIELDS:
        _check(f"{tag}:team.{field} 存在", field in team, f"keys={sorted(team)}")
    _check(f"{tag}:team.status_label 非空（§2.3，属 4 个必须非空的 *_label 之一）",
           bool(str(team.get("status_label") or "").strip()), f"label={team.get('status_label')!r}")
    _check(f"{tag}:team.status 合法",
           team.get("status") in TEAM_STATUS_LABELS, f"status={team.get('status')!r}")
    expected_label = TEAM_STATUS_LABELS.get(team.get("status"))
    _check(f"{tag}:team.status_label 与 status 自洽（中文直出）",
           team.get("status_label") == expected_label,
           f"status={team.get('status')!r} label={team.get('status_label')!r} 期望={expected_label!r}")
    _check(f"{tag}:team.status_source ∈ event|derived|history",
           team.get("status_source") in ("event", "derived", "history"),
           f"status_source={team.get('status_source')!r}")
    if team.get("status") == "history":
        _check(f"{tag}:history 态 status_source=='history'", team.get("status_source") == "history",
               f"status_source={team.get('status_source')!r}")
    progress = team.get("progress") or {}
    _keyset(f"{tag}:team.progress（契约 §2.6，8 键）", progress, PROGRESS_FIELDS)
    for field in PROGRESS_FIELDS:
        _check(f"{tag}:team.progress.{field} 存在", field in progress, f"keys={sorted(progress)}")
    # 进度自洽：total == 各桶之和（in_progress 已合并 planning）
    buckets = ["completed", "cancelled", "in_review", "blocked", "pending", "in_progress"]
    if all(isinstance(progress.get(k), int) for k in buckets + ["total"]):
        _check(f"{tag}:team.progress 各桶之和 == total",
               sum(progress[k] for k in buckets) == progress["total"],
               f"buckets={ {k: progress[k] for k in buckets} } total={progress['total']}")
        denom = progress["total"] - progress["cancelled"]
        if denom > 0:
            expect = round(progress["completed"] / denom * 100, 1)
            _check(f"{tag}:team.progress.percent 公式正确",
                   progress.get("percent") is not None and abs(progress["percent"] - expect) <= 0.11,
                   f"percent={progress.get('percent')} 期望≈{expect}")
        else:
            _check(f"{tag}:分母为 0 时 percent 为 null（不得显示 0%）",
                   progress.get("percent") is None, f"percent={progress.get('percent')!r}")
    # 任务状态计数自洽
    tasks = snap.get("tasks") or []
    if tasks and all(isinstance(progress.get(k), int) for k in buckets + ["total"]):
        _check(f"{tag}:team.progress.total == len(tasks)",
               progress["total"] == len(tasks), f"{progress['total']} vs {len(tasks)}")
        expect_map = {}
        for t in tasks:
            st = t.get("status")
            if st in ("in_progress", "planning"):
                expect_map["in_progress"] = expect_map.get("in_progress", 0) + 1
            elif st in ("completed", "cancelled", "in_review", "blocked", "pending"):
                expect_map[st] = expect_map.get(st, 0) + 1
            else:
                expect_map.setdefault("__unknown__", 0)
                expect_map["__unknown__"] += 1
        mismatch = {k: (v, progress.get(k)) for k, v in expect_map.items() if progress.get(k) != v}
        _check(f"{tag}:team.progress 状态计数与 tasks 一致", not mismatch, f"不一致={mismatch}")


def check_members(snap: dict, tag: str = "snapshot") -> None:
    members = snap.get("members") or []
    for m in members:
        name = m.get("member_name")
        missing = [f for f in MEMBER_FIELDS if f not in m]
        if missing:
            _check(f"{tag}:member {name} 字段完整", False, f"缺={missing}")
            continue
        _keyset(f"{tag}:member {name}（契约 §2.4，16 必填 + unknown_enum 选填）",
                m, MEMBER_FIELDS, MEMBER_OPTIONAL)
        # unknown_enum 是选填的布尔，出现即必为 true（「未知枚举回填原值」那半句由后端单测覆盖）
        if "unknown_enum" in m:
            _check(f"{tag}:member {name} unknown_enum 出现时必为 true",
                   m["unknown_enum"] is True, f"unknown_enum={m['unknown_enum']!r}")
        # 4 个必须非空的 *_label 中，成员侧有 status_label / role_label
        _check(f"{tag}:member {name} status_label 非空",
               bool(str(m.get("status_label") or "").strip()), f"={m.get('status_label')!r}")
        _check(f"{tag}:member {name} role_label 非空",
               bool(str(m.get("role_label") or "").strip()), f"={m.get('role_label')!r}")
        _check(f"{tag}:member {name} role_label 与 role 自洽（中文直出）",
               m.get("role_label") == ROLE_LABELS.get(m.get("role"), m.get("role")),
               f"role={m.get('role')!r} role_label={m.get('role_label')!r}")
        _check(f"{tag}:member {name} role_group 与 is_leader 自洽",
               (m["role_group"] == "captain") == bool(m["is_leader"]), f"role={m['role']} group={m['role_group']}")
        _check(f"{tag}:member {name} display_name 非空",
               bool(str(m.get("display_name") or "").strip()), f"={m.get('display_name')!r}")
        if m.get("execution_status") is None:
            _check(f"{tag}:member {name} execution_status=null 时 execution_label 必须为 null"
                   f"（不是空串，也不是「未知」）",
                   m.get("execution_label") is None, f"label={m.get('execution_label')!r}")
        else:
            _check(f"{tag}:member {name} execution_label 非空",
                   bool(str(m.get("execution_label") or "").strip()),
                   f"execution_status={m.get('execution_status')!r} label={m.get('execution_label')!r}")
        prog = m.get("task_progress") or {}
        _keyset(f"{tag}:member {name}.task_progress（契约 §2.6，8 键）", prog, PROGRESS_FIELDS)
        cur = m.get("current_task")
        if cur is not None:
            _check(f"{tag}:member {name} current_task 键集合 == 契约 §2.4.2（task_id/title/status/status_label）",
                   set(cur) == {"task_id", "title", "status", "status_label"}, f"keys={sorted(cur)}")
    # 排序规则：captain 先，再 member_name 升序
    keys = [(0 if m.get("is_leader") else 1, m.get("member_name") or "") for m in members]
    _check(f"{tag}:members 排序符合契约（captain 先 → member_name 升序）", keys == sorted(keys), f"{keys}")
    # 每成员进度与 assignee 计数自洽
    tasks = snap.get("tasks") or []
    for m in members:
        name = m.get("member_name")
        mine = [t for t in tasks if (t.get("assignee") or {}).get("member_name") == name]
        prog = m.get("task_progress") or {}
        if "total" in prog:
            _check(f"{tag}:member {name} task_progress.total 与 assignee 任务数一致",
                   prog["total"] == len(mine), f"{prog.get('total')} vs {len(mine)}")
        nonterminal = [t for t in mine if t.get("status") not in ("completed", "cancelled")]
        _check(f"{tag}:member {name} active_task_count 与事实一致",
               m.get("active_task_count") == len(nonterminal),
               f"{m.get('active_task_count')} vs {len(nonterminal)}")


def check_tasks_and_graph(snap: dict, tag: str = "snapshot") -> None:
    tasks = snap.get("tasks") or []
    ids = [t.get("task_id") for t in tasks]
    _check(f"{tag}:task_id 唯一", len(ids) == len(set(ids)), f"{ids}")
    _check(f"{tag}:tasks 按 task_id 字典序", ids == sorted(ids), f"{ids}")
    for t in tasks:
        missing = [f for f in TASK_FIELDS if f not in t]
        if missing:
            _check(f"{tag}:task {t.get('task_id')} 字段完整", False, f"缺={missing}")
            continue
        _keyset(f"{tag}:task {t.get('task_id')}（契约 §2.5，12 必填 + unknown_enum 选填）",
                t, TASK_FIELDS, TASK_OPTIONAL)
        if "unknown_enum" in t:
            _check(f"{tag}:task {t.get('task_id')} unknown_enum 出现时必为 true",
                   t["unknown_enum"] is True, f"unknown_enum={t['unknown_enum']!r}")
        _check(f"{tag}:task {t.get('task_id')} status_label 非空",
               bool(str(t.get("status_label") or "").strip()), f"={t.get('status_label')!r}")
        asg = t.get("assignee")
        if asg is not None:
            _check(f"{tag}:task {t.get('task_id')} assignee 键集合 == {{member_name,display_name}}（§2.5）",
                   set(asg) == {"member_name", "display_name"}, f"keys={sorted(asg)}")
        for forbidden in ("created_at", "summary", "note", "reviewer"):
            _check(f"{tag}:task {t.get('task_id')} 不含缺口字段 {forbidden}",
                   forbidden not in t, f"发现 {forbidden}（契约 §2.5 明确不提供）")
        _check(f"{tag}:task {t.get('task_id')} blocked_by 去重升序",
               t["blocked_by"] == sorted(set(t["blocked_by"])), f"{t['blocked_by']}")
        _check(f"{tag}:task {t.get('task_id')} blocks 去重升序",
               t["blocks"] == sorted(set(t["blocks"])), f"{t['blocks']}")
        _check(f"{tag}:task {t.get('task_id')} unresolved_blocked_by ⊆ blocked_by",
               set(t["unresolved_blocked_by"]) <= set(t["blocked_by"]), f"{t['unresolved_blocked_by']} vs {t['blocked_by']}")
        flags = t.get("flags") or {}
        _check(f"{tag}:task {t.get('task_id')} flags 完整",
               all(k in flags for k in ("is_root", "is_join", "is_parallel_branch", "is_isolated")),
               f"flags={flags}")
        if not flags.get("is_root", True) or not flags.get("is_isolated", True):
            pass
        _check(f"{tag}:task {t.get('task_id')} is_root == (blocked_by 为空)",
               flags.get("is_root") == (len(t["blocked_by"]) == 0),
               f"is_root={flags.get('is_root')} blocked_by={t['blocked_by']}")
        _check(f"{tag}:task {t.get('task_id')} is_join == (blocked_by>=2)",
               flags.get("is_join") == (len(t["blocked_by"]) >= 2),
               f"is_join={flags.get('is_join')} blocked_by={t['blocked_by']}")
        _check(f"{tag}:task {t.get('task_id')} is_parallel_branch == (blocks>=2)",
               flags.get("is_parallel_branch") == (len(t["blocks"]) >= 2),
               f"is_parallel_branch={flags.get('is_parallel_branch')} blocks={t['blocks']}")
        _check(f"{tag}:task {t.get('task_id')} is_isolated == (无上游且无下游)",
               flags.get("is_isolated") == (len(t["blocked_by"]) == 0 and len(t["blocks"]) == 0),
               f"is_isolated={flags.get('is_isolated')}")
        _check(f"{tag}:task {t.get('task_id')} depth 非负整数",
               isinstance(t.get("depth"), int) and t["depth"] >= 0, f"depth={t.get('depth')!r}")
    # 边集与 blocked_by/blocks 互逆
    graph = snap.get("graph") or {}
    _keyset(f"{tag}:graph（契约 §2.5，4 键）", graph, GRAPH_FIELDS)
    edges = graph.get("edges") or []
    edge_pairs = [(e.get("from"), e.get("to")) for e in edges]
    _check(f"{tag}:graph.edges 按 (from,to) 升序", edge_pairs == sorted(edge_pairs), f"{edge_pairs}")
    for e in edges:
        _check(f"{tag}:edge 键集合 == {{from,to,resolved}}（§2.5）",
               set(e) == {"from", "to", "resolved"}, f"keys={sorted(e)}")
    declared_up = {(b, t.get("task_id")) for t in tasks for b in (t.get("blocked_by") or [])}
    _check(f"{tag}:graph.edges 与 tasks.blocked_by 完全一致",
           set(edge_pairs) == declared_up,
           f"仅边集={sorted(set(edge_pairs) - declared_up)} 仅字段={sorted(declared_up - set(edge_pairs))}")
    declared_down = {(t.get("task_id"), b) for t in tasks for b in (t.get("blocks") or [])}
    _check(f"{tag}:tasks.blocks 由边集反转得到（与 edges 互逆）",
           declared_down == set(edge_pairs),
           f"不一致={sorted(declared_down ^ set(edge_pairs))}")
    for e in edges:
        _check(f"{tag}:edge {e.get('from')}->{e.get('to')} resolved 为 bool",
               isinstance(e.get("resolved"), bool), f"resolved={e.get('resolved')!r}")
    # layers / node_order
    layers = graph.get("layers") or []
    by_depth: dict[int, list[str]] = {}
    for t in tasks:
        by_depth.setdefault(t.get("depth", 0), []).append(t.get("task_id"))
    expected_layers = [sorted(by_depth[i]) for i in sorted(by_depth)]
    _check(f"{tag}:graph.layers 与 depth 分组一致", layers == expected_layers,
           f"layers={layers} 期望={expected_layers}")
    order = graph.get("node_order") or []
    _check(f"{tag}:graph.node_order 覆盖全部任务且无重复",
           sorted(order) == sorted(ids), f"node_order={order}")
    pos = {tid: i for i, tid in enumerate(order)}
    violated = [e for e in edges if pos.get(e.get("from"), 1 << 30) >= pos.get(e.get("to"), -1)]
    _check(f"{tag}:graph.node_order 为合法拓扑序（上游在前）", not violated, f"违反边={violated}")
    _check(f"{tag}:graph.has_cycle 为 bool", isinstance(graph.get("has_cycle"), bool),
           f"has_cycle={graph.get('has_cycle')!r}")
    # depth 与最长上游链一致（有环时跳过）
    if not graph.get("has_cycle"):
        depth = {t.get("task_id"): t.get("depth") for t in tasks}
        expects = {}
        up = {t.get("task_id"): list(t.get("blocked_by") or []) for t in tasks}
        for tid in up:  # 迭代式求最长链，避免递归深度问题
            stack = [(tid, 0)]
            best = 0
            seen = set()
            while stack:
                node, d = stack.pop()
                best = max(best, d)
                if node in seen:
                    continue
                seen.add(node)
                for parent in up.get(node, []):
                    stack.append((parent, d + 1))
            expects[tid] = best
        mismatch = {k: (depth.get(k), v) for k, v in expects.items() if depth.get(k) != v}
        _check(f"{tag}:depth 等于最长上游链长度", not mismatch, f"不一致={mismatch}")


def check_snapshot(base: str, sid: str) -> dict:
    q = urllib.parse.quote(sid)
    resp = request(base, f"/snapshot?sid={q}&wait=0")
    _check("snapshot:200", resp.status == 200, f"status={resp.status}")
    _check("snapshot:Content-Type application/json",
           "application/json" in resp.header("content-type"), f"ct={resp.header('content-type')!r}")
    _check("snapshot:Cache-Control no-store",
           "no-store" in resp.header("cache-control").lower(), f"cc={resp.header('cache-control')!r}")
    try:
        snap = resp.json()
    except Exception as exc:  # noqa: BLE001
        _check("snapshot:JSON 可解析", False, f"{exc} body={resp.text[:200]}")
        return {}
    _check("snapshot:JSON 可解析", True, "")
    rev = snap.get("revision")
    _check("snapshot:ETag 头存在", resp.header("etag") != "", f"etag={resp.header('etag')!r}")
    _check("snapshot:ETag 反映 revision", str(rev) in resp.header("etag"), f"etag={resp.header('etag')!r} rev={rev}")
    check_snapshot_fields(snap, "snapshot")
    check_team_block(snap, "snapshot")
    check_members(snap, "snapshot")
    check_tasks_and_graph(snap, "snapshot")

    # since 矩阵
    r2 = request(base, f"/snapshot?sid={q}&since={rev}&wait=0")
    _check("snapshot:since==revision -> 200", r2.status == 200, f"status={r2.status}")
    if r2.status == 200:
        body = r2.json()
        unchanged = body.get("unchanged") is True if isinstance(body, dict) else False
        _check("snapshot:since==revision -> unchanged:true", unchanged, f"body={str(body)[:200]}")
        if unchanged:
            _check("snapshot:unchanged 带 revision", body.get("revision") == rev, f"body={body}")

    r3 = request(base, f"/snapshot?sid={q}&since=0&wait=0")
    _check("snapshot:since=0 -> 200 完整快照", r3.status == 200 and "schema_version" in r3.text,
           f"status={r3.status}")

    r4 = request(base, f"/snapshot?sid={q}&since={rev + 5000}&wait=0")
    _check("snapshot:since>revision -> revision_reset:true",
           r4.status == 200 and isinstance(r4.json(), dict) and r4.json().get("revision_reset") is True,
           f"status={r4.status} body={r4.text[:200]}")

    r5 = request(base, f"/snapshot?sid={q}&since=abc&wait=0")
    _check("snapshot:since 非法 -> 400 BAD_QUERY",
           r5.status == 400 and r5.json().get("code") == "BAD_QUERY",
           f"status={r5.status} body={r5.text[:200]}")
    r6 = request(base, f"/snapshot?sid={q}&wait=99")
    _check("snapshot:wait 越界 -> 400 BAD_QUERY",
           r6.status == 400 and r6.json().get("code") == "BAD_QUERY",
           f"status={r6.status} body={r6.text[:200]}")

    # 长轮询：since==rev & wait=2 应在 ~2s 内返回 unchanged
    t0 = time.perf_counter()
    r7 = request(base, f"/snapshot?sid={q}&since={rev}&wait=2", timeout=10)
    elapsed = time.perf_counter() - t0
    _check("snapshot:长轮询在 wait 上限内返回", r7.status == 200 and elapsed <= 4.0,
           f"status={r7.status} elapsed={elapsed:.2f}s")

    # 无 sid：应按 global 返回 200，不报 400
    r8 = request(base, "/snapshot?wait=0")
    _check("snapshot:缺 sid -> 200 且 scope.kind=='global'",
           r8.status == 200 and r8.json().get("scope", {}).get("kind") == "global",
           f"status={r8.status} scope={r8.json().get('scope') if r8.status == 200 else r8.text[:200]}")
    return snap


# ---------------------------------------------------------------- /ui-state


ALLOWED_KEYS = {"active_tab", "expanded_member_ids", "member_page", "selected_task_id",
                "detail_open", "search", "status_filter", "canvas", "theme"}


def put_state(base: str, sid: str, state) -> Resp:
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "sid": sid, "state": state}).encode("utf-8")
    return request(base, "/ui-state", method="PUT", body=payload,
                   headers={"Content-Type": "application/json", "Origin": "null"})


def patch_state(base: str, sid: str, state) -> Resp:
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "sid": sid, "state": state}).encode("utf-8")
    return request(base, "/ui-state", method="PATCH", body=payload,
                   headers={"Content-Type": "application/json", "Origin": "null"})


def get_state(base: str, sid: str) -> tuple[dict, dict]:
    r = request(base, f"/ui-state?sid={urllib.parse.quote(sid)}")
    if r.status != 200:
        return {}, {}
    body = r.json()
    return body, (body.get("state") or {})


def check_ui_state_patch(base: str, sid: str) -> None:
    """契约 v1.3/§5.3：PATCH 部分合并 + 按 sid 加锁串行化（两页并发不丢写）。"""
    # 基准：显式重置到已知状态
    base_state = {"active_tab": "team-info", "member_page": 1,
                  "search": {"team_info": "旧词", "task_graph": ""},
                  "canvas": {"layout_mode": "horizontal", "pan": {"x": 0.0, "y": 0.0}, "zoom": 1.0},
                  "theme": "system"}
    r = put_state(base, sid, base_state)
    _check("patch:前置 PUT 重置成功", r.status == 200, f"status={r.status} body={r.text[:200]}")

    # 1) 单键部分合并
    r = patch_state(base, sid, {"member_page": 3})
    _check("patch:单键 PATCH 200", r.status == 200, f"status={r.status} body={r.text[:300]}")
    if r.status == 200:
        body = r.json()
        _check("patch:PATCH 返回合并后完整 state", isinstance(body.get("state"), dict)
               and body["state"].get("member_page") == 3, f"body={str(body)[:300]}")
    _, st = get_state(base, sid)
    _check("patch:未出现的键保持不变（active_tab/theme 未丢）",
           st.get("active_tab") == "team-info" and st.get("theme") == "system", f"state={st}")
    _check("patch:active_tab/theme 与基准一致", st.get("member_page") == 3, f"state={st}")

    # 2) canvas 一级深合并（写 zoom 不清 layout_mode/pan）
    patch_state(base, sid, {"canvas": {"zoom": 2.25}})
    _, st = get_state(base, sid)
    canvas = st.get("canvas") or {}
    _check("patch:canvas 一级深合并保留 layout_mode",
           canvas.get("layout_mode") == "horizontal", f"canvas={canvas}")
    _check("patch:canvas 一级深合并保留 pan",
           isinstance(canvas.get("pan"), dict) and canvas["pan"].get("x") == 0.0, f"canvas={canvas}")
    _check("patch:canvas.zoom 已更新", canvas.get("zoom") == 2.25, f"canvas={canvas}")

    # 3) search 一级深合并（写 task_graph 不清 team_info）
    patch_state(base, sid, {"search": {"task_graph": "任务"}})
    _, st = get_state(base, sid)
    search = st.get("search") or {}
    _check("patch:search 一级深合并保留 team_info", search.get("team_info") == "旧词", f"search={search}")
    _check("patch:search.task_graph 已更新", search.get("task_graph") == "任务", f"search={search}")

    # 4) 数组整体替换（不做并集）
    patch_state(base, sid, {"status_filter": ["pending", "in_progress"]})
    patch_state(base, sid, {"status_filter": ["completed"]})
    _, st = get_state(base, sid)
    _check("patch:status_filter 为整体替换（非并集）",
           st.get("status_filter") == ["completed"], f"status_filter={st.get('status_filter')}")
    patch_state(base, sid, {"expanded_member_ids": ["a", "b"]})
    patch_state(base, sid, {"expanded_member_ids": ["c"]})
    _, st = get_state(base, sid)
    _check("patch:expanded_member_ids 为整体替换（非并集）",
           st.get("expanded_member_ids") == ["c"], f"expanded={st.get('expanded_member_ids')}")

    # 5) 显式 null = 恢复该键缺省值（契约 §5 第 5 条；用"先改成非缺省再 null"来观测，避免默认回填造成假绿）
    patch_state(base, sid, {"theme": "dark"})
    _, st = get_state(base, sid)
    _check("patch:theme 写入 dark 生效（null 用例的前置）", st.get("theme") == "dark",
           f"theme={st.get('theme')!r}")
    patch_state(base, sid, {"theme": None})
    _, st = get_state(base, sid)
    _check("patch:显式 null 恢复缺省值（theme: dark -> system）",
           st.get("theme") == "system", f"theme={st.get('theme')!r} state={st}")
    patch_state(base, sid, {"selected_task_id": "t-2"})
    patch_state(base, sid, {"selected_task_id": None})
    _, st = get_state(base, sid)
    _check("patch:显式 null 恢复缺省值（selected_task_id -> null）",
           st.get("selected_task_id") is None, f"selected={st.get('selected_task_id')!r}")

    # v1.9 U6：嵌套 null 非法（400 STATE_INVALID）；顶层 null 合法（回缺省对象）
    r_u6a = patch_state(base, sid, {"search": {"team_info": None}})
    _check("u6:PATCH 嵌套 null -> 400 STATE_INVALID",
           r_u6a.status == 400 and r_u6a.json().get("code") == "STATE_INVALID",
           f"status={r_u6a.status} body={r_u6a.text[:200]}")
    r_u6b = patch_state(base, sid, {"search": None})
    _check("u6:PATCH 顶层 null -> 200（不是 400）", r_u6b.status == 200,
           f"status={r_u6b.status} body={r_u6b.text[:200]}")
    _, st_u6 = get_state(base, sid)
    _check("u6:顶层 null 后 search 回缺省对象",
           st_u6.get("search") == {"team_info": "", "task_graph": ""}, f"search={st_u6.get('search')}")

    # v1.9 U7：未知子键 -> 400 STATE_KEY_NOT_ALLOWED，且整条请求不得产生部分写入
    _, before_u7 = get_state(base, sid)
    for u7_body, u7_name in (({"search": {"evil": "x"}}, "search.evil"),
                             ({"canvas": {"pan": {"z": 1}}}, "canvas.pan.z")):
        r_u7 = patch_state(base, sid, u7_body)
        _check(f"u7:未知子键 {u7_name} -> 400 STATE_KEY_NOT_ALLOWED",
               r_u7.status == 400 and r_u7.json().get("code") == "STATE_KEY_NOT_ALLOWED",
               f"status={r_u7.status} body={r_u7.text[:200]}")
        _, after_u7 = get_state(base, sid)
        _check(f"u7:{u7_name} 被拒后 search/canvas 与请求前逐键相等（无部分写入）",
               after_u7.get("search") == before_u7.get("search")
               and after_u7.get("canvas") == before_u7.get("canvas"),
               f"search={after_u7.get('search')} canvas={after_u7.get('canvas')}")

    # v1.9 U7 扩展：合法子键可写；非法子键在 PATCH 与 PUT 两个入口都必须 400 STATE_KEY_NOT_ALLOWED
    r_ok1 = patch_state(base, sid, {"search": {"team_info": "x"}})
    _check("u7:合法子键 search.team_info 可写（200）", r_ok1.status == 200, f"status={r_ok1.status}")
    _, st_ok1 = get_state(base, sid)
    _check("u7:search.team_info 读回 'x'", (st_ok1.get("search") or {}).get("team_info") == "x",
           f"search={st_ok1.get('search')}")
    r_ok2 = patch_state(base, sid, {"canvas": {"pan": {"x": 1, "y": 2}}})
    _check("u7:合法子键 canvas.pan{x,y} 可写（200）", r_ok2.status == 200, f"status={r_ok2.status}")
    _, st_ok2 = get_state(base, sid)
    _check("u7:canvas.pan 读回 {x:1.0,y:2.0}",
           (st_ok2.get("canvas") or {}).get("pan") == {"x": 1.0, "y": 2.0},
           f"pan={(st_ok2.get('canvas') or {}).get('pan')}")

    illegal_subkeys = (({"search": {"team": "secret"}}, "search.team"),
                       ({"canvas": {"nodes": []}}, "canvas.nodes"),
                       ({"canvas": {"pan": {"z": 1}}}, "canvas.pan.z"))
    _, before_illegal = get_state(base, sid)
    for method in ("PATCH", "PUT"):
        for illegal_body, illegal_name in illegal_subkeys:
            if method == "PATCH":
                r_il = patch_state(base, sid, illegal_body)
            else:
                r_il = put_state(base, sid, illegal_body)
            _check(f"u7:{method} 非法子键 {illegal_name} -> 400 STATE_KEY_NOT_ALLOWED",
                   r_il.status == 400 and r_il.json().get("code") == "STATE_KEY_NOT_ALLOWED",
                   f"status={r_il.status} body={r_il.text[:200]}")
            _, after_illegal = get_state(base, sid)
            _check(f"u7:{method} 非法子键 {illegal_name} 后 canvas/search 逐键相等（无部分写入）",
                   after_illegal.get("search") == before_illegal.get("search")
                   and after_illegal.get("canvas") == before_illegal.get("canvas"),
                   f"search={after_illegal.get('search')} canvas={after_illegal.get('canvas')}")

    # U6 扩展：子键 null 也是非法（null 只对顶层 9 键有重置语义）
    for null_body, null_name in (({"canvas": {"zoom": None}}, "canvas.zoom"),
                                 ({"canvas": {"pan": None}}, "canvas.pan"),
                                 ({"search": {"task_graph": None}}, "search.task_graph")):
        r_nullsub = patch_state(base, sid, null_body)
        _check(f"u6:子键 null {null_name} -> 400 STATE_INVALID",
               r_nullsub.status == 400 and r_nullsub.json().get("code") == "STATE_INVALID",
               f"status={r_nullsub.status} body={r_nullsub.text[:200]}")

    # U7 形状恒定：GET 的 search/canvas/pan 键集合固定（前端可据此做确定性回填）
    _, st_shape = get_state(base, sid)
    _check("u7:GET 的 search 键集合恒为 {team_info,task_graph}",
           set((st_shape.get("search") or {}).keys()) == {"team_info", "task_graph"},
           f"search={st_shape.get('search')}")
    _check("u7:GET 的 canvas 键集合恒为 {layout_mode,pan,zoom}",
           set((st_shape.get("canvas") or {}).keys()) == {"layout_mode", "pan", "zoom"},
           f"canvas={st_shape.get('canvas')}")
    _check("u7:GET 的 canvas.pan 键集合恒为 {x,y}",
           set(((st_shape.get("canvas") or {}).get("pan") or {}).keys()) == {"x", "y"},
           f"pan={((st_shape.get('canvas') or {}).get('pan'))}")

    # 6) 白名单在 PATCH 上同样生效
    for bad_key in ("tasks", "team", "members", "graph"):
        r = patch_state(base, sid, {bad_key: []})
        _check(f"patch:白名单拒绝 {bad_key} -> 400 STATE_KEY_NOT_ALLOWED",
               r.status == 400 and r.json().get("code") == "STATE_KEY_NOT_ALLOWED",
               f"status={r.status} body={r.text[:200]}")

    # 7) PATCH 的 16KiB 上限
    r = patch_state(base, sid, {"search": {"team_info": "y" * 17000}})
    _check("patch:超过 16KiB -> 400 STATE_TOO_LARGE",
           r.status == 400 and r.json().get("code") == "STATE_TOO_LARGE",
           f"status={r.status} body={r.text[:200]}")

    # 8) 并发合并：两个客户端交替 PATCH 不同键，最终两者都在（加锁串行化）
    import threading
    errors: list[str] = []

    def worker(key: str, value, times: int) -> None:
        try:
            for _ in range(times):
                resp = patch_state(base, sid, {key: value})
                if resp.status != 200:
                    errors.append(f"{key} -> {resp.status} {resp.text[:120]}")
                    return
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key} 异常 {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=("member_page", 7, 20)),
        threading.Thread(target=worker, args=("canvas", {"zoom": 3.5}, 20)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    _check("patch:并发 PATCH 全部 200", not errors, f"errors={errors[:4]}")
    _, st = get_state(base, sid)
    _check("patch:并发后两个键都在（不丢写）",
           st.get("member_page") == 7 and (st.get("canvas") or {}).get("zoom") == 3.5,
           f"state={st}")

    # 还原到干净状态给后续 DOM 用例
    put_state(base, sid, {"active_tab": "team-info", "theme": "system", "detail_open": False,
                          "selected_task_id": None, "expanded_member_ids": [], "member_page": 1,
                          "search": {"team_info": "", "task_graph": ""}, "status_filter": [],
                          "canvas": {"layout_mode": "horizontal", "pan": {"x": 0, "y": 0}, "zoom": 1.0}})


def check_ui_state(base: str, sid: str, snap: dict) -> None:
    q = urllib.parse.quote(sid)
    r = request(base, f"/ui-state?sid={q}")
    _check("ui-state:GET 200", r.status == 200, f"status={r.status}")
    body = r.json() if r.status == 200 else {}
    _check("ui-state:GET 含 schema_version/scope/state/updated_at/empty",
           all(k in body for k in ("schema_version", "scope", "state", "updated_at", "empty")),
           f"keys={sorted(body)}")
    _check("ui-state:GET scope.key 回显 sid", (body.get("scope") or {}).get("key") == sid,
           f"scope={body.get('scope')}")

    # v1.7 §5.3.1 U1–U4：GET 的 state 永不为 null，恒回完整 9 键缺省（缺省回填）
    cold_sid = f"{sid}-never-written-{int(time.time() * 1000)}"
    r_cold = request(base, f"/ui-state?sid={urllib.parse.quote(cold_sid)}")
    cold_body = r_cold.json() if r_cold.status == 200 else {}
    cold_state = cold_body.get("state")
    _check("u1:从未写过的 sid GET 200（不 404）", r_cold.status == 200, f"status={r_cold.status}")
    _check("u1:state 不为 null（恒回完整缺省对象）", isinstance(cold_state, dict), f"state={cold_state!r}")
    whitelist_keys = {"active_tab", "expanded_member_ids", "member_page", "selected_task_id",
                      "detail_open", "search", "status_filter", "canvas", "theme"}
    _check("u1:缺省 state 恰含 9 个白名单键",
           set((cold_state or {}).keys()) == whitelist_keys,
           f"keys={sorted((cold_state or {}).keys())}")
    defaults = {"active_tab": "team-info", "expanded_member_ids": [], "member_page": 1,
                "selected_task_id": None, "detail_open": False, "status_filter": [],
                "theme": "system"}
    for key, want in defaults.items():
        _check(f"u1:缺省 {key} == {want!r}", (cold_state or {}).get(key) == want,
               f"actual={(cold_state or {}).get(key)!r}")
    cold_search = (cold_state or {}).get("search") or {}
    _check("u1:缺省 search 为 {team_info:'', task_graph:''}",
           cold_search.get("team_info") == "" and cold_search.get("task_graph") == "",
           f"search={cold_search}")
    cold_canvas = (cold_state or {}).get("canvas") or {}
    _check("u1:缺省 canvas 是具体对象（含 layout_mode/pan/zoom）",
           isinstance(cold_canvas, dict) and cold_canvas.get("layout_mode") == "horizontal"
           and cold_canvas.get("pan") == {"x": 0, "y": 0}
           and isinstance(cold_canvas.get("zoom"), (int, float)),
           f"canvas={cold_canvas}")
    _check("u2:empty=true 表示从未写过", cold_body.get("empty") is True, f"empty={cold_body.get('empty')!r}")
    _check("u2:从未写过时 updated_at 为 null", cold_body.get("updated_at") is None,
           f"updated_at={cold_body.get('updated_at')!r}")
    # 写入后 empty 必须翻 false
    put_state(base, cold_sid, dict(defaults, **{"theme": "dark"}))
    r_after = request(base, f"/ui-state?sid={urllib.parse.quote(cold_sid)}")
    after_body = r_after.json() if r_after.status == 200 else {}
    _check("u2:写过之后 empty=false 且 updated_at 为 int",
           after_body.get("empty") is False and isinstance(after_body.get("updated_at"), int),
           f"empty={after_body.get('empty')!r} updated_at={after_body.get('updated_at')!r}")
    _check("u2:写过之后 theme 读回 dark",
           (after_body.get("state") or {}).get("theme") == "dark",
           f"theme={(after_body.get('state') or {}).get('theme')!r}")
    # U5：canvas 缺省是具体对象——只写 zoom 也应得到完整 canvas
    patch_state(base, sid, {"canvas": {"zoom": 2.0}})
    _, st_canvas = get_state(base, sid)
    canvas_now = st_canvas.get("canvas") or {}
    _check("u5:PATCH canvas.zoom 后得到完整 canvas（layout_mode/pan 不被清）",
           canvas_now.get("layout_mode") == "horizontal" and canvas_now.get("pan") == {"x": 0, "y": 0}
           and canvas_now.get("zoom") == 2.0, f"canvas={canvas_now}")

    valid = {"active_tab": "task-graph", "expanded_member_ids": ["backend-dev"],
             "member_page": 2, "selected_task_id": "impl-backend", "detail_open": True,
             "search": {"team_info": "后端", "task_graph": "契约"},
             "status_filter": ["in_progress", "completed"],
             "canvas": {"layout_mode": "vertical", "pan": {"x": 12.5, "y": -3.0}, "zoom": 1.5},
             "theme": "dark"}
    w = put_state(base, sid, valid)
    _check("ui-state:PUT 合法状态 200", w.status == 200, f"status={w.status} body={w.text[:300]}")
    if w.status == 200:
        wb = w.json()
        _check("ui-state:PUT 回 ok=true", wb.get("ok") is True, f"body={wb}")
        _check("ui-state:PUT 回 bytes 为 int", isinstance(wb.get("bytes"), int), f"body={wb}")
        _check("ui-state:PUT 回 scope.key", (wb.get("scope") or {}).get("key") == sid, f"body={wb}")
    r2 = request(base, f"/ui-state?sid={q}")
    back = (r2.json().get("state") or {}) if r2.status == 200 else {}
    _check("ui-state:写入后可读回（往返一致）", back == valid, f"读回={back}")

    # v1.8 U5：PUT 里的 null = 该键取缺省（与 PATCH 的 null 语义同义）
    r_u5 = put_state(base, sid, {"canvas": None, "active_tab": None})
    _check("u5:PUT 中的 null 被视为取缺省（200，而非 400 STATE_INVALID）",
           r_u5.status == 200, f"status={r_u5.status} body={r_u5.text[:200]}")
    _, st_u5 = get_state(base, sid)
    u5_canvas = st_u5.get("canvas") or {}
    _check("u5:PUT null 后 canvas 回到缺省对象",
           u5_canvas.get("layout_mode") == "horizontal" and u5_canvas.get("pan") == {"x": 0, "y": 0}
           and isinstance(u5_canvas.get("zoom"), (int, float)), f"canvas={u5_canvas}")
    _check("u5:PUT null 后 active_tab 回到缺省", st_u5.get("active_tab") == "team-info",
           f"active_tab={st_u5.get('active_tab')!r}")
    r_all_null = put_state(base, sid, {key: None for key in whitelist_keys})
    _check("u5:9 键全部传 null 也 200（等价重置为缺省）", r_all_null.status == 200,
           f"status={r_all_null.status} body={r_all_null.text[:200]}")
    _, st_null = get_state(base, sid)
    _check("u5:全 null 后 9 键齐全且 theme 回 system",
           set(st_null.keys()) == whitelist_keys and st_null.get("theme") == "system",
           f"state={str(st_null)[:200]}")

    # ⑥ v1.8 回环：GET 到的完整 state 原样 PUT 回去必须 200 且逐键相等
    put_state(base, sid, valid)
    _, st_get = get_state(base, sid)
    r_rt = put_state(base, sid, st_get)
    _check("⑥ 回环:GET 到的完整 9 键 state 原样 PUT 返回 200", r_rt.status == 200,
           f"status={r_rt.status} body={r_rt.text[:200]}")
    _, st_rt = get_state(base, sid)
    _check("⑥ 回环:回环后逐键相等", st_rt == st_get,
           f"diff={ {k: (st_get.get(k), st_rt.get(k)) for k in set(st_get) | set(st_rt) if st_get.get(k) != st_rt.get(k)} }")
    # ⑥-b 回环变体：GET 结果里 selected_task_id=null + canvas 为完整对象，原样 PUT 也必须 200
    variant = dict(valid, selected_task_id=None)
    put_state(base, sid, variant)
    _, st_v = get_state(base, sid)
    _check("⑥ 回环变体:selected_task_id 存的是 null", st_v.get("selected_task_id") is None,
           f"selected={st_v.get('selected_task_id')!r}")
    r_rt2 = put_state(base, sid, st_v)
    _check("⑥ 回环变体:含 null 值的完整 state 原样 PUT 200", r_rt2.status == 200,
           f"status={r_rt2.status} body={r_rt2.text[:200]}")
    _, st_v2 = get_state(base, sid)
    _check("⑥ 回环变体:回环后逐键相等（含 null 与 canvas 对象）", st_v2 == st_v,
           f"diff={ {k: (st_v.get(k), st_v2.get(k)) for k in set(st_v) | set(st_v2) if st_v.get(k) != st_v2.get(k)} }")
    put_state(base, sid, valid)  # 复位，供后续白名单用例的"未污染"断言使用

    # 白名单：塞团队数据必须 400 STATE_KEY_NOT_ALLOWED
    for bad_key in ("tasks", "team", "members", "graph", "snapshot"):
        r3 = put_state(base, sid, {bad_key: [{"x": 1}]})
        _check(f"ui-state:白名单拒绝 {bad_key} -> 400 STATE_KEY_NOT_ALLOWED",
               r3.status == 400 and r3.json().get("code") == "STATE_KEY_NOT_ALLOWED",
               f"status={r3.status} body={r3.text[:200]}")

    # 混合合法键 + 非法键也必须整体拒绝
    r4 = put_state(base, sid, {"active_tab": "team-info", "tasks": []})
    _check("ui-state:合法键+非法键混合仍 400 STATE_KEY_NOT_ALLOWED",
           r4.status == 400 and r4.json().get("code") == "STATE_KEY_NOT_ALLOWED",
           f"status={r4.status} body={r4.text[:200]}")
    r5 = request(base, f"/ui-state?sid={q}")
    _check("ui-state:被拒绝的写入未污染已存状态",
           (r5.json().get("state") or {}) == valid, f"读回={r5.json().get('state') if r5.status == 200 else r5.text[:200]}")

    # 大小上限 16KiB
    big = {"search": {"team_info": "x" * 17000, "task_graph": ""}}
    r6 = put_state(base, sid, big)
    _check("ui-state:超过 16KiB -> 400 STATE_TOO_LARGE",
           r6.status == 400 and r6.json().get("code") == "STATE_TOO_LARGE",
           f"status={r6.status} body={r6.text[:200]}")

    # status_filter 只允许 7 值
    r7 = put_state(base, sid, {"status_filter": ["not_a_status"]})
    _check("ui-state:status_filter 非法值被拒", r7.status == 400, f"status={r7.status} body={r7.text[:200]}")

    # 恢复上一次的合法状态，便于后续 DOM 检查
    put_state(base, sid, {"active_tab": "team-info", "theme": "system", "detail_open": False,
                          "selected_task_id": None, "expanded_member_ids": [], "member_page": 1,
                          "search": {"team_info": "", "task_graph": ""}, "status_filter": [],
                          "canvas": {"layout_mode": "horizontal", "pan": {"x": 0, "y": 0}, "zoom": 1.0}})


# ---------------------------------------------------------------- 静态资源与安全


def _strip_volatile(snap: dict) -> dict:
    """去掉每次响应都会变的 served_at，用于「逐字段相等」比较。"""
    return {k: v for k, v in (snap or {}).items() if k != "served_at"}


def _get_snapshot_now(base: str, sid: str) -> dict:
    q = urllib.parse.quote(sid)
    resp = request(base, f"/snapshot?sid={q}&wait=0")
    return (resp.json().get("snapshot") or {}) if resp.status == 200 else {}


def check_no_write_endpoints(base: str, sid: str) -> None:
    """§6：不存在任何写团队数据的端点（黑盒矩阵 + 最强反例 + 交叉隔离 + 路径穿越）。"""
    q = urllib.parse.quote(sid)
    METHOD_CODES: dict[str, int] = {}
    before = _get_snapshot_now(base, sid)
    known = ["/snapshot", "/health", "/ui-state", "/web/team-info.html", "/web/task-graph.html"]
    write_methods = ["POST", "PUT", "PATCH", "DELETE"]
    for path in known:
        for method in write_methods:
            body = b"{}"
            resp = request(base, f"{path}?sid={q}", method=method, body=body,
                           headers={"Content-Type": "application/json"})
            # 契约 §5.1 只登记了各端点允许的方法，未强制 405；硬要求是「写不进去」：
            # 不得 2xx/3xx，且 CORS 头必须照常存在（§5.0 要求含 4xx）。
            _check(f"写作面:{method} {path} 不成功（非 2xx/3xx）", resp.status >= 400,
                   f"status={resp.status}")
            _check(f"写作面:{method} {path} 的 4xx/5xx 仍带 ACAO=*（§5.0）",
                   resp.header("access-control-allow-origin") == "*",
                   f"status={resp.status} ACAO={resp.header('access-control-allow-origin')!r}")
            METHOD_CODES[f"{method} {path}"] = resp.status
    # 记录项：实际状态码分布（契约未规定必须 405，仅作实现观察，不计失败）
    _record("写作面:方法码分布（记录项，契约 §5.1 未强制 405）", True,
            json.dumps(METHOD_CODES, ensure_ascii=False, sort_keys=True))
    for path in ("/nope", "/team", "/tasks", "/members", "/snapshot/write", "/api/snapshot"):
        resp = request(base, f"{path}?sid={q}", method="PUT", body=b"{}",
                       headers={"Content-Type": "application/json"})
        _check(f"写作面:PUT {path} -> 404（未知路径一律不存在）", resp.status == 404,
               f"status={resp.status} body={resp.text[:120]!r}")
    for method in ("GET", "HEAD"):
        resp = request(base, "/favicon.ico", method=method)
        _check(f"写作面:{method} /favicon.ico 不 5xx（只读静态）", resp.status < 500, f"status={resp.status}")

    # 最强反例：唯一的写端点 /ui-state 也塞不进团队数据（白名单挡死），且快照逐字段不变
    for key in ("team", "members", "tasks", "graph", "snapshot", "revision"):
        resp = put_state(base, sid, {key: {"hack": 1}})
        code = (resp.json() or {}).get("code") if resp.status == 400 else None
        _check(f"写作面最强反例:PUT /ui-state 带 {key} -> 400 STATE_KEY_NOT_ALLOWED",
               resp.status == 400 and code == "STATE_KEY_NOT_ALLOWED",
               f"status={resp.status} code={code!r} body={resp.text[:160]!r}")
    after_hack = _get_snapshot_now(base, sid)
    _check("写作面最强反例:塞团队数据被拒后 /snapshot 逐字段不变（除 served_at）",
           _strip_volatile(after_hack) == _strip_volatile(before),
           "差异=" + str({k: (before.get(k), after_hack.get(k)) for k in set(before) | set(after_hack)
                         if before.get(k) != after_hack.get(k)})[:400])
    _check("写作面最强反例:被拒后 revision 不递增",
           after_hack.get("revision") == before.get("revision"),
           f"{before.get('revision')} -> {after_hack.get('revision')}")

    # 交叉隔离：合法写满 9 键视图状态后，快照仍逐字段相等、revision 不递增
    full_state = {
        "active_tab": "task-graph",
        "expanded_member_ids": ["m1", "m2"],
        "member_page": 2,
        "status_filter": ["pending", "in_progress"],
        "selected_task_id": "task-1",
        "detail_open": True,
        "search": {"team_info": "a", "task_graph": "b"},
        "canvas": {"layout_mode": "vertical", "pan": {"x": 12.5, "y": -3.0}, "zoom": 1.75},
        "theme": "dark",
    }
    resp = patch_state(base, sid, full_state)
    _check("写作面交叉隔离:PATCH /ui-state 九键写入 -> 200", resp.status == 200,
           f"status={resp.status} body={resp.text[:160]!r}")
    after_state = _get_snapshot_now(base, sid)
    _check("写作面交叉隔离:写满视图状态后 /snapshot 逐字段相等（视图状态与快照彻底隔离）",
           _strip_volatile(after_state) == _strip_volatile(before),
           "差异=" + str({k: (before.get(k), after_state.get(k)) for k in set(before) | set(after_state)
                         if before.get(k) != after_state.get(k)})[:400])
    _check("写作面交叉隔离:写视图状态不推进快照 revision",
           after_state.get("revision") == before.get("revision"),
           f"{before.get('revision')} -> {after_state.get('revision')}")
    # 还原，避免影响后续用例对视图状态的期望
    patch_state(base, sid, {"active_tab": "team-info", "expanded_member_ids": [], "member_page": 1,
                            "status_filter": None, "selected_task_id": None, "detail_open": False,
                            "search": {"team_info": "", "task_graph": ""},
                            "canvas": {"layout_mode": "horizontal", "pan": {"x": 0, "y": 0}, "zoom": 1.0},
                            "theme": "system"})

    # 路径穿越：绕过 /web 前缀也不得读到包内其它文件
    for path in ("/web/../manifest.yaml", "/web/..%2fmanifest.yaml",
                 f"/web/team-info.html?sid={q}"):
        resp = request(base, path)
        if path.startswith("/web/team-info.html"):
            _check("写作面:对照用例 GET /web/team-info.html -> 200（不是路径穿越）", resp.status == 200,
                   f"status={resp.status}")
        else:
            _check(f"写作面:{path} -> 404（不得越出 web/ 目录）", resp.status == 404,
                   f"status={resp.status} 前 80 字节={resp.body[:80]!r}")


def check_static(base: str, sid: str) -> None:
    q = urllib.parse.quote(sid)
    for page in ("team-info.html", "task-graph.html"):
        r = request(base, f"/web/{page}?sid={q}")
        _check(f"static:/web/{page} 200", r.status == 200, f"status={r.status}")
        _check(f"static:/web/{page} 是 HTML",
               "text/html" in r.header("content-type"), f"ct={r.header('content-type')!r}")
        _check(f"static:/web/{page} 内容非空且含 <html",
               len(r.body) > 200 and "<html" in r.text.lower(), f"len={len(r.body)}")
        _check(f"static:/web/{page} Cache-Control no-store",
               "no-store" in r.header("cache-control").lower(), f"cc={r.header('cache-control')!r}")

    fav = request(base, "/favicon.ico")
    _check("static:/favicon.ico -> 204", fav.status == 204, f"status={fav.status}")

    for bad in ("/web/../manifest.yaml", "/web/assets/../../manifest.yaml",
                "/web/%2e%2e/manifest.yaml", "/web//etc/passwd", "/../manifest.yaml"):
        r = request(base, bad)
        _check(f"security:路径穿越 {bad} 被拒（非 200）", r.status != 200,
               f"status={r.status} body={r.text[:120]!r}")

    r = request(base, "/definitely-not-a-route")
    _check("security:未知路由 -> 404 JSON 含 code", r.status == 404,
           f"status={r.status} body={r.text[:200]}")
    if r.status == 404:
        try:
            _check("security:错误体含 code/message", "code" in r.json() and "message" in r.json(),
                   f"body={r.json()}")
        except Exception:  # noqa: BLE001
            _check("security:错误体为 JSON", False, f"body={r.text[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="如 http://127.0.0.1:52301")
    ap.add_argument("--sid", required=True, help="会话 id（iframe src 里的 sid）")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--skip-ui-state", action="store_true")
    ap.add_argument("--allow-stale", action="store_true",
                    help="跳过「实例启动时刻必须晚于源码 mtime」的新鲜度闸门（仅排障用）")
    args = ap.parse_args()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    source_mtime = _source_mtime()
    src_ms, src_name = _source_mtime_ms()

    health = check_health(args.base)
    # 新鲜度闸门：被测服务进程必须在源码最后一次改动之后才 import（否则跑的是旧代码）
    server_started = health.get("started_at")
    fresh = isinstance(server_started, int) and server_started >= src_ms
    if not fresh and not args.allow_stale:
        print(f"REFUSE 实例可能导入的是旧代码：server_started_at={server_started} "
              f"< source_mtime_ms={src_ms}({src_name})。请重启隔离实例后重跑"
              f"（排障可用 --allow-stale）。", flush=True)
        return 2
    check_cors(args.base, args.sid)
    check_preflight(args.base)
    snap = check_snapshot(args.base, args.sid)
    if not args.skip_ui_state:
        check_ui_state(args.base, args.sid, snap)
        check_ui_state_patch(args.base, args.sid)
    check_static(args.base, args.sid)
    check_no_write_endpoints(args.base, args.sid)

    failed = [r for r in RESULTS if not r["ok"]]
    report = {
        "base": args.base,
        "sid": args.sid,
        "evidence_meta": {
            "started_at": started_at,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "source_mtime": source_mtime,
            "source_mtime_ms": src_ms,
            "source_fingerprint_sha256_16": _source_fingerprint(),
            "web_mtime": _web_mtime_ms(),
            "web_fingerprint_sha256_16": _web_fingerprint(),
            "gate_scope": "py only（py 是 import 进内存的；web/** 每请求现读，改前端不需要重启实例）",
            "server_pid": health.get("pid"),
            "server_started_at_ms": server_started,
            "server_started_at": (time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(server_started / 1000))
                                  if isinstance(server_started, int) else None),
            "fresh_instance": fresh,
            "note": "判据不是跑测结果本身，而是结果与被测源码的时间关系："
                    "server_started_at_ms >= source_mtime_ms 才代表这轮结果对应当前源码",
        },
        "health": health,
        "total": len(RESULTS),
        "passed": len(RESULTS) - len(failed),
        "failed": len(failed),
        "failures": failed,
        "cases": RESULTS,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"RESULT total={report['total']} passed={report['passed']} failed={report['failed']}")
    for f in failed:
        print(f"  [FAIL] {f['case']} | {f['detail']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
