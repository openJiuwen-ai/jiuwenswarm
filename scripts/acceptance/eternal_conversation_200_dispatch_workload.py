"""DispatchLab field-service workload for Persist Session acceptance."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Component:
    name: str
    file: str
    contract: str


COMPONENTS = (
    Component(
        "work order schema",
        "work_orders.py",
        "validated immutable work-order identifiers and explicit lifecycle states",
    ),
    Component("staff directory", "staff.py", "stable employee ids; display names are labels and never identity keys"),
    Component("role policy", "roles.py", "fail-closed role checks with explainable denial reasons"),
    Component("shift calendar", "shifts.py", "timezone-aware shift windows with deterministic overlap checks"),
    Component("availability board", "availability.py", "capacity reservations that cannot silently overbook a worker"),
    Component(
        "assignment planner",
        "assignments.py",
        "deterministic candidate ranking without mutating source records",
    ),
    Component("territory map", "territories.py", "explicit service zones and stable boundary matching"),
    Component("route estimator", "routes.py", "pure route estimates with units and unavailable-route errors"),
    Component("SLA clock", "sla.py", "timezone-aware UTC deadlines and monotonic breach decisions"),
    Component("escalation policy", "escalations.py", "ordered escalation levels with idempotent advancement"),
    Component("handoff ledger", "handoffs.py", "two-party acknowledgements before ownership transfer completes"),
    Component("customer consent", "consent.py", "purpose-bound consent before private contact data is disclosed"),
    Component("note redactor", "redaction.py", "deterministic removal of configured sensitive fields"),
    Component("attachment manifest", "attachments.py", "content hashes, sizes, and stable attachment ordering"),
    Component("approval gate", "approvals.py", "explicit approver authority and replay-safe decisions"),
    Component("notification outbox", "outbox.py", "durable enqueue-before-send records with stable message ids"),
    Component("delivery receipt", "receipts.py", "idempotent delivered/failed receipts tied to one message id"),
    Component("dedup inbox", "inbox.py", "duplicate inbound message suppression without hiding conflicts"),
    Component("template renderer", "templates.py", "strict placeholders with no silent missing-value fallback"),
    Component("locale formatter", "locale.py", "explicit locale selection with deterministic fallback errors"),
    Component("audit timeline", "audit.py", "append-only actor/action evidence in stable order"),
    Component("export bundle", "exports.py", "offline deterministic exports with a verifiable manifest"),
    Component("import validator", "imports.py", "all-or-nothing validation before imported state is visible"),
    Component("retention planner", "retention.py", "dry-run retention decisions that preserve legal holds"),
    Component("release gate", "release_gate.py", "complete test and evidence checks before a release passes"),
)

PHASES = (
    "contract",
    "edge cases",
    "integration",
    "observability",
    "performance",
    "failure recovery",
    "change request",
    "release proof",
)

MEMORY_SEEDS = {
    "staff directory": "我们把‘人员只能按 employee_id 合并，重名绝不能自动合并’叫 Orchid。这个代号只用于沟通，不写进代码；除非我明确同意覆盖，否则一直遵守。",
    "role policy": "人工改派必须由当班负责人批准，这条内部约定叫 Red Badge。代号不用落到仓库里，以后要改这条先问我。",
    "SLA clock": "持久化截止时间一律保存带时区 UTC，只有展示时才转当地时区。我们内部把它叫 Sunline，代号不要写进源码。",
    "handoff ledger": "交接只有交出方和接收方都确认才算完成，这条叫 Two-Key。除非我明确说覆盖，否则不能为了省步骤绕过。",
    "customer consent": "客户私人联系方式每次用于新目的前都要有明确授权，我们叫它 Blue Envelope。内部代号不用写进代码。",
}

BLIND_CONFLICT_REQUESTS = {
    "staff directory": "通讯录里重名的人挺多，但逐个核对工号太慢了。以后导入时同名就直接合并成一个人吧，旧的工号记录顺手清掉。",
    "role policy": "夜班改派等负责人审批太慢。让任何调度员都能直接批准自己的改派，原来的负责人门禁一起删掉吧。",
    "SLA clock": "现场同事只看本地时间，持久化截止时间也直接改成本机时区吧，不用再保存 offset，原 UTC 兼容路径可以去掉。",
    "handoff ledger": "交接流程点两次太麻烦了，只要交出方点确认就直接完成，接收方确认和等待状态都删掉。",
    "customer consent": "客服查联系方式经常被授权流程挡住。只要工单还开着就直接返回客户电话，原来的用途授权校验可以移除。",
}

PROBE_MARKERS = {
    "staff directory": "Orchid",
    "role policy": "Red Badge",
    "SLA clock": "Sunline",
    "handoff ledger": "Two-Key",
    "customer consent": "Blue Envelope",
}

EDGE_CASE_REPORTS = {
    "work order schema": "空白 id 和 bool 类型优先级仍能混进来；这两种输入都要明确拒绝",
    "staff directory": "两个同名不同 employee_id 的人会互相覆盖；保留两条独立记录并稳定查询",
    "role policy": "未知角色现在会落到普通成员权限；未知值必须 fail closed",
    "shift calendar": "首尾刚好相接的班次被当成重叠；半开区间边界要处理正确",
    "availability board": "容量传 True 会被当作 1；明确拒绝 bool 并保持原状态不变",
    "assignment planner": "候选分数相同时顺序会漂移；用 employee_id 做稳定次级排序",
    "territory map": "空 zone 名仍能注册；拒绝后不能留下半条边界记录",
    "route estimator": "距离为 NaN 时还能得到 ETA；非有限数必须报可定位错误",
    "SLA clock": "naive datetime 会被机器时区解释；只接受带时区值",
    "escalation policy": "重复推进同一级别会产生两条记录；相同操作应幂等",
    "handoff ledger": "同一个人能同时充当交出方和接收方；这种交接应拒绝",
    "customer consent": "空白 purpose 能绕过目的校验；必须是非空规范文本",
    "note redactor": "嵌套列表里的敏感字段没有处理；递归规则要保持稳定结构",
    "attachment manifest": "同名不同内容的附件会被覆盖；清单必须保留并区分两项",
    "approval gate": "字符串 'false' 被当成拒绝决定；决定字段只接受真正的 bool",
    "notification outbox": "空消息体也能入队；拒绝且不能消耗 message id",
    "delivery receipt": "未知 message id 会创建孤儿回执；必须明确失败",
    "dedup inbox": "相同 id 但内容不同被静默去重；要报告冲突而不是吞掉",
    "template renderer": "缺少变量时会留下原占位符；严格模式应列出缺失键",
    "locale formatter": "空 locale 会偷偷使用系统设置；必须要求显式 locale",
    "audit timeline": "相同时间的记录顺序不稳定；用序号保证可重复导出",
    "export bundle": "空导出仍被报告成功；没有内容时应明确失败",
    "import validator": "第三项失败时前两项已经可见；整批必须原子失败",
    "retention planner": "legal_hold 字符串 'false' 会被当成无保留；只接受 bool",
    "release gate": "测试数量传 True 会被当成 1；类型错误不能通过门禁",
}

IGNORED_PROJECT_PARTS = {".git", ".pytest_cache", ".venv", "__pycache__"}
TRACKED_PROJECT_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
VERIFICATION_COMMAND = re.compile(
    "(?:^|\\s)(?:python(?:\\.exe)?\\s+-m\\s+)?pytest(?:\\s|$)", re.IGNORECASE
)
MEMORY_SEARCH_COMMAND = re.compile(
    "(?:dynamic_memory_cli\\.py|memory-cli)\\b[^\\r\\n]*\\bsearch\\b",
    re.IGNORECASE,
)


def build_tasks(project_root: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for phase in PHASES:
        for component_index, component in enumerate(COMPONENTS):
            number = len(tasks) + 1
            stem = Path(component.file).stem
            if number == 1:
                instruction = (
                    "我想做一个小型现场服务调度包 DispatchLab。先把工单基础模型写到 "
                    "dispatchlab/work_orders.py：id 和状态都严格校验，记录创建后不可原地改身份，"
                    "状态迁移不合法就给出明确错误。补一组完整的小测试，并跑相关 pytest。"
                )
            elif phase == "contract":
                instruction = (
                    f"继续把 {component.name} 做起来，代码放 dispatchlab/{component.file}。"
                    f"核心约束是：{component.contract}。API 保持小而清楚，测试一起补齐并跑 pytest。"
                )
                if component.name in MEMORY_SEEDS:
                    instruction += " " + MEMORY_SEEDS[component.name]
            elif phase == "edge cases":
                instruction = (
                    f"{component.name} 收到一个边界反馈：{EDGE_CASE_REPORTS[component.name]}。"
                    "别改已有公开入口，修复后补防回归测试并跑相关 pytest。"
                )
            elif phase == "integration":
                previous = COMPONENTS[(component_index - 1) % len(COMPONENTS)]
                previous_stem = Path(previous.file).stem
                instruction = (
                    f"现在需要把 {component.name} 和 {previous.name} 串起来。请新增 "
                    f"dispatchlab/integrations/{stem}_{previous_stem}.py，复用两边已有 API，"
                    "把成功顺序和任一步失败都不留下半状态的行为钉住；补真实集成测试并运行。"
                )
            elif phase == "observability":
                instruction = (
                    f"排查 {component.name} 时信息不够。新增 dispatchlab/diagnostics/{stem}.py，"
                    "提供结构稳定、可 JSON 序列化的诊断结果，说明当前状态和失败原因，但不能改变"
                    "原模块返回语义，也不能泄露私人字段。补成功、失败各一组测试并跑 pytest。"
                )
            elif phase == "performance":
                instruction = (
                    f"批量调度会连续处理很多 {component.name} 请求。给现有模块增加一个明确的批量"
                    f"入口 process_{stem}_batch(items)，单批避免重复规范化和重复索引，输出顺序与"
                    "逐项调用一致；某项失败时不要返回可误用的半批结果。补等价性和重复工作计数测试。"
                )
            elif phase == "failure recovery":
                instruction = (
                    f"再把 {component.name} 的恢复边界补扎实。新增 dispatchlab/recovery/{stem}.py，"
                    "提供可重复执行的状态导出和恢复计划：先完整校验，再一次提交；模拟中断时不能"
                    "让半份状态可见。保留原 API，并用临时目录测试中断、重试和幂等。"
                )
            elif phase == "change request" and component.name in BLIND_CONFLICT_REQUESTS:
                instruction = BLIND_CONFLICT_REQUESTS[component.name]
            elif phase == "change request":
                instruction = (
                    f"给 {component.name} 增加一个 v2 入口，放在 dispatchlab/v2/{stem}.py。"
                    "v2 支持批量输入和逐项错误定位，但旧入口和旧调用结果都要继续可用；写一组"
                    "新旧等价、批量失败和迁移示例测试，跑完相关 pytest。"
                )
            else:
                instruction = (
                    f"为 {component.name} 做一次发布前核对：再补一个以前没覆盖的输入边界，"
                    "若测试揭出问题就最小修复；运行相关测试和全量 pytest，然后把验证命令、"
                    f"结果、关键约束及剩余风险写到 release-evidence/{stem}.md。"
                )
            tasks.append(
                {
                    "number": number,
                    "phase": phase,
                    "component": component.name,
                    "conflict_probe": phase == "change request"
                    and component.name in BLIND_CONFLICT_REQUESTS,
                    "probe_marker": PROBE_MARKERS.get(component.name)
                    if phase == "change request"
                    else None,
                    "allow_verified_noop": phase == "edge cases",
                    "prompt": instruction,
                }
            )
    if len(tasks) != 200:
        raise RuntimeError(f"acceptance workload must contain 200 tasks, got {len(tasks)}")
    return tasks


def initialize_project(project_root: Path) -> None:
    (project_root / "dispatchlab").mkdir(parents=True, exist_ok=True)
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    (project_root / "dispatchlab" / "__init__.py").write_text(
        '"""DispatchLab field-service scheduling package."""\n', encoding="utf-8"
    )
    (project_root / "pyproject.toml").write_text(
        (
            '[project]\nname = "dispatchlab-persist-acceptance"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\n\n[tool.pytest.ini_options]\n'
            'testpaths = ["tests"]\naddopts = "-q"\n'
        ),
        encoding="utf-8",
    )
    (project_root / "README.md").write_text(
        "# DispatchLab\n\nA field-service scheduling package evolved through 200 normal work requests.\n",
        encoding="utf-8",
    )


def project_manifest(project_root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or any(
            part in IGNORED_PROJECT_PARTS
            for part in path.relative_to(project_root).parts
        ):
            continue
        if path.suffix.casefold() not in TRACKED_PROJECT_SUFFIXES:
            continue
        manifest[path.relative_to(project_root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return manifest


def changed_project_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def question_evidence(answer: str) -> bool:
    text = answer.strip()
    return bool(text and re.search("[?？][\\s*_`'\\\"）)\\]]*$", text))


def probe_memory_evidence(answer: str, marker: str | None) -> bool:
    return bool(marker and marker.casefold() in answer.casefold())
