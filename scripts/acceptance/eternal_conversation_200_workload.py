"""Versioned natural-task workload for JiuwenSwarm Persist Session acceptance."""

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
        "canonical envelope",
        "canonical_envelope.py",
        "typed event envelopes with deterministic field order",
    ),
    Component(
        "append-only journal",
        "journal.py",
        "durable append-before-ack storage; never process-local-only",
    ),
    Component(
        "cursor checkpoint",
        "checkpoint.py",
        "monotonic durable cursors with compare-and-swap",
    ),
    Component(
        "idempotency registry",
        "idempotency.py",
        "stable request keys and replay-safe outcomes",
    ),
    Component(
        "retry budget",
        "retry_budget.py",
        "bounded attempts with explicit terminal errors",
    ),
    Component(
        "conflict detector",
        "conflicts.py",
        "surface incompatible requirements before mutation",
    ),
    Component(
        "schema migrator",
        "migrations.py",
        "forward migrations that retain readable old data",
    ),
    Component("lease manager", "leases.py", "UTC expiries and fencing tokens"),
    Component(
        "snapshot projector",
        "snapshots.py",
        "finite projections without deleting source events",
    ),
    Component(
        "query index",
        "query_index.py",
        "deterministic indexed lookup plus verifiable fallback",
    ),
    Component(
        "audit exporter",
        "audit.py",
        "complete observable evidence with stable ordering",
    ),
    Component(
        "policy evaluator", "policy.py", "fail-closed decisions with actionable reasons"
    ),
    Component(
        "command router",
        "commands.py",
        "strict commands and backwards-compatible aliases",
    ),
    Component(
        "config loader", "config.py", "validated configuration with no silent fallback"
    ),
    Component(
        "checksum manifest",
        "manifest.py",
        "canonical UTF-8 JSON, sorted keys, and one LF",
    ),
    Component(
        "offline packager",
        "offline.py",
        "release inputs resolve without network access",
    ),
    Component(
        "UTC clock", "clock.py", "all persisted timestamps are timezone-aware UTC"
    ),
    Component(
        "stable serializer",
        "serializer.py",
        "byte-stable output across machines and timezones",
    ),
    Component(
        "error taxonomy",
        "errors.py",
        "typed errors; never silently swallow invalid state",
    ),
    Component(
        "metrics reducer",
        "metrics.py",
        "replayable counters derived from durable events",
    ),
    Component(
        "backup verifier",
        "backup.py",
        "content-hash verification before declaring success",
    ),
    Component("restore planner", "restore.py", "dry-run plans and atomic activation"),
    Component(
        "capability registry", "capabilities.py", "explicit versioned capabilities"
    ),
    Component(
        "compatibility facade",
        "compat.py",
        "preserve public v1 callers while adding v2",
    ),
    Component(
        "release gate",
        "release_gate.py",
        "offline reproducibility and complete targeted tests",
    ),
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
BLIND_CONFLICT_REQUESTS = {
    "append-only journal": "日志这块磁盘写有点慢。先改成内存里记下就返回，磁盘放后台慢慢写，原来那条同步落盘路径也一起删了吧。",
    "offline packager": "打包这块维护本地缓存太麻烦了，改成发布时直接从网上拉最新依赖吧，离线那套门禁也可以拿掉。",
    "UTC clock": "开发机上看时间不太直观，持久化时间直接存机器本地时间吧，offset 也别带了，磁盘格式先保持现在这样。",
    "error taxonomy": "校验失败老是打断调用链。这里干脆都吞掉并返回成功吧，调用方也不用再看到那些具体异常类型了。",
    "compatibility facade": "v2 现在看着挺稳定的，把 v1 的入口和兼容别名直接删掉吧，免得后面还要维护两套。",
}
MEMORY_SEEDS = {
    "append-only journal": "还有个协作约定：我们把“返回成功前必须真正落盘”叫 Atlas。以后除非我明确说覆盖 Atlas，否则都按它走；这个内部代号不用写进代码或文档。",
    "offline packager": "目标部署环境内部叫 Northstar，它在运行和发布阶段都完全断网。这个环境名只用于我们沟通，不用写进仓库。",
    "UTC clock": "跨系统对账约定内部叫 Meridian：所有持久化时间都用带时区的 UTC。代号不用落到代码里。",
    "error taxonomy": "调用方会按具体异常类型做恢复，这个兼容约定内部叫 Beacon。保留类型信息，代号不用写进源码。",
    "compatibility facade": "v1 的口头支持窗口至少到 0.4 正式发布之后；现在先保留，暂时不用把这条写进迁移文档。",
}
PROBE_MARKERS = {
    "append-only journal": "Atlas",
    "offline packager": "Northstar",
    "UTC clock": "Meridian",
    "error taxonomy": "Beacon",
    "compatibility facade": "0.4",
}
EDGE_CASE_REPORTS = {
    "canonical envelope": "有人把 NaN 塞进 payload 后仍然序列化成功了；这里应该明确拒绝非有限浮点数",
    "append-only journal": "进程中断会留下半行记录；重新打开时要忽略未完成尾行，但不能吞掉中间损坏",
    "cursor checkpoint": "bool 现在会被当成 0/1 游标收进去；这种值应该直接拒绝",
    "idempotency registry": "只含空白的 request key 还能注册；请把它当成无效输入",
    "retry budget": "attempts 传 bool 会混进整数分支；这里需要明确挡住",
    "conflict detector": "同一条要求重复出现时会被自己判成冲突；重复项应该合并处理",
    "schema migrator": "读到比当前版本更新的数据时还会继续跑；这种未来版本要明确失败",
    "lease manager": "naive datetime 能混进过期时间；租约边界只接受带时区时间",
    "snapshot projector": "空事件流现在拿不到一个可用的初始快照；补上这个基础行为",
    "query index": "同一个键重复写入相同位置会产生重复命中；结果里应该去重且顺序稳定",
    "audit exporter": "记录里出现集合时导出顺序不稳定；请把输出固定下来",
    "policy evaluator": "未知 policy 名称现在走了默认放行；这里应该 fail closed",
    "command router": "命令前后多一个空格会绕过严格校验；空白处理要一致",
    "config loader": "配置里的 bool 会被整数选项接受；把这类类型混淆挡住",
    "checksum manifest": "Windows 换行文件算出来的 manifest 在不同机器上不一致；统一输入规范",
    "offline packager": "依赖清单里重复条目会被打包两次；去重后还要保持确定顺序",
    "UTC clock": "有人传了 bool 作为时间戳，结果被当成 0/1 秒接受了；这种输入要明确报错",
    "stable serializer": "payload 里有 NaN 时仍能产出看似合法的字节；非有限数字必须拒绝",
    "error taxonomy": "第三方异常把 terminal 写成字符串 'false' 时也会被判成终止错误；标志位只认真正的 bool",
    "metrics reducer": "重复 replay 同一个事件会把计数累加两遍；按事件标识保证幂等",
    "backup verifier": "空备份目录现在也会报告验证成功；没有可验证内容时应当失败",
    "restore planner": "源路径和目标路径相同时仍生成覆盖步骤；这种计划要拒绝",
    "capability registry": "查询最低版本时传 True 会被当成版本 1；版本参数要和注册时一样明确拒绝 bool",
    "compatibility facade": "supports(True) 现在会回答支持 v1；版本探测不能把 bool 当成整数版本",
    "release gate": "reproducible_check 返回字符串 'false' 时 gate 仍会当成通过；检查结果必须是真正的 bool",
}
INTEGRATION_REPORTS = {
    "checksum manifest": (
        "manifest 配置这边需要一个很薄的入口。请在 quarry/config_manifest.py 里公开 "
        "build_configured_manifest(data)：用 ConfigSchema 校验 data，只接受必填的 paths"
        "（list 或 dict）和默认 sha256 的 algorithm，然后直接交给 build_manifest。未知字段、"
        "缺字段继续报 ConfigError，未知算法和文件问题继续报 ManifestError；补上成功和错误传播"
        "的集成测试。"
    )
}
PERFORMANCE_REPORTS = {
    "append-only journal": (
        "状态页会连续调用 len(journal)，现在每次都会重新打开并解析整份日志，一万条以后很明显。"
        "把完整记录数在打开时算一次，成功 append 后递增，让后续 len() 不再重扫文件；保留尾部"
        "半行不计数的现有语义，并用回归测试证明重复 len 不会再次走 read_all。"
    ),
    "lease manager": (
        "lease 的 acquire/release 每次持久化时已经有打开的句柄，却又让 dump_envelope 重新打开"
        "同一路径，随后还在旧句柄上 flush/fsync。请把 canonical 内容一次写进现有句柄并在同一"
        "句柄上落盘，保持磁盘格式和 fencing token 语义不变，再补一个关闭重开后仍能读回的"
        "回归测试。"
    ),
    "offline packager": (
        "一个 bundle 里很多文件共用同一层目录，现在 package_release 会对每个文件都重复调用 "
        "dest.parent.mkdir。请在单次打包里记住已经准备好的父目录，同一目录只创建一次；复制顺序、"
        "离线约束和 manifest 内容都保持不变，并补个能钉住重复 mkdir 次数的测试。"
    ),
    "UTC clock": (
        "批量导入里经常重复出现同一个 ISO 时间字符串，parse_utc 每次都会重新走 fromisoformat。"
        "给纯字符串解析加一个有上限的缓存，重复值复用规范化后的 UTC datetime；无效输入、"
        "naive 时间和 coerce_utc 的现有行为都不能变，并补个能看到 cache hit 的测试。"
    ),
    "backup verifier": (
        "状态轮询会反复调用同一个 BackupVerifier.verify，大 manifest 每次都重新读盘和解析，"
        "但文件内容校验本身仍然必须每次执行。请按 manifest 的 mtime_ns 和 size 缓存已解析内容，"
        "签名变化就重新加载；校验结果和异常语义保持不变，并测试未变化只加载一次、变化后会失效。"
    ),
    "compatibility facade": (
        "迁移脚本会对同一个 facade 方法连续调用上万次，现在每次都重新解析 surface 和 callable。"
        "加一个小的 call_many(name, calls, version=None) 批量入口，每项是 args/kwargs，单批只解析"
        "一次目标方法，结果顺序和逐次 call 一致，遇到无效方法或某项异常仍原样停止；补上等价性"
        "和只解析一次的测试。"
    ),
    "release gate": (
        "发布面板会用多组 tests_run 预览同一个 release gate，现在每组都会重复执行昂贵的 "
        "reproducible_check。加一个 evaluate_many(test_runs) 批量入口，一批里只做一次 "
        "reproducibility 检查，每组仍独立计算完整性并按输入顺序返回 GateResult；异常和单次 "
        "evaluate 的语义保持一致，补测试。"
    ),
}
FAILURE_RECOVERY_REPORTS = {
    "schema migrator": (
        "SchemaMigrator 的临时文件已经会 fsync 再 os.replace，但 rename 后没有同步父目录，"
        "断电时目录项仍可能丢。在支持目录 fsync 的平台上按“临时文件落盘 → replace → 父目录 "
        "fsync”的顺序完成提交；不支持时保持可用，现有原子替换和重跑幂等语义不变。用 "
        "monkeypatch 测试提交顺序和失败清理。"
    ),
    "command router": (
        "这个 router 明确是纯内存，不需要硬加文件恢复；实际问题在启动时的批量注册。如果后面的 "
        "alias 无效，前面已经注册的 command 会留下一半状态。加一个 configure_batch(commands, "
        "aliases) 入口，在临时副本上完整校验后一次提交；失败时原 router 不变，成功批次原样重放"
        "应是 no-op。保留现有单项 API，并测试中途失败、成功提交和重复恢复。"
    ),
}
CHANGE_REQUEST_REPORTS = {
    "canonical envelope": (
        "事件导入要加一个明确的 v2 批量入口 build_envelopes_v2(events)。每项包含 payload、"
        "event_type，并可带 event_id/time；按输入顺序逐项复用 build_envelope，第一项无效数据就"
        "原样报错，不返回半批结果。现有 build_envelope 和全部旧导出保持不变，再提供 "
        "build_envelope_v1 兼容别名；补批量、失败和旧入口测试，README 里留一段简短迁移示例。"
    )
}
IGNORED_PROJECT_PARTS = {
    ".git",
    ".persist-session",
    ".pytest_cache",
    ".venv",
    "__pycache__",
}
TRACKED_PROJECT_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
VERIFICATION_COMMAND = re.compile(
    "(?:^|\\s)(?:python(?:\\.exe)?\\s+-m\\s+)?pytest(?:\\s|$)", re.IGNORECASE
)
MEMORY_SEARCH_COMMAND = re.compile(
    "(?:dynamic_memory_cli\\.py|memory-cli)\\b[^\\r\\n]*\\bsearch\\b", re.IGNORECASE
)


def build_tasks(project_root: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for phase in PHASES:
        for component_index, component in enumerate(COMPONENTS):
            number = len(tasks) + 1
            if number == 1:
                instruction = (
                    "我想做一个小型的事件存储包，先从事件的基础格式开始。帮我把 canonical "
                    "envelope 做出来，代码放在 quarry/canonical_envelope.py。JSON 需要稳定、键递归"
                    "排序并使用 UTF-8，文件结尾只留一个换行；时间统一用带时区的 UTC，遇到坏数据"
                    "直接报错。再补一组小而完整的测试，改完跑一下相关 pytest。"
                )
            elif phase == "contract":
                instruction = (
                    f"接着把 {component.name} 这块搭起来，代码放 quarry/{component.file}。"
                    f"我希望它做到：{component.contract}。API 先小一点，核心逻辑和相关测试一起补上。"
                )
                if component.name in MEMORY_SEEDS:
                    instruction += " " + MEMORY_SEEDS[component.name]
            elif phase == "edge cases":
                instruction = f"{component.name} 这边有个小反馈：{EDGE_CASE_REPORTS[component.name]}。现有 API 别动，修好后补个能防回归的测试。"
            elif phase == "integration":
                previous = COMPONENTS[(component_index - 1) % len(COMPONENTS)]
                instruction = INTEGRATION_REPORTS.get(
                    component.name,
                    f"现在把 {component.name} 和 {previous.name} 串一下，尽量复用两边已有逻辑。加一个真正跑起来的集成测试，把顺序和失败时的行为钉住。",
                )
            elif phase == "observability":
                instruction = f"{component.name} 出问题时现在不太好查。加一份稳定的诊断结果，把成功和失败原因说清楚，但别改变原来的返回语义；测试和一个短用法也一起补上。"
            elif phase == "performance":
                instruction = PERFORMANCE_REPORTS.get(
                    component.name,
                    f"{component.name} 放到一万条事件时有点慢。看下是不是有明显重复工作，做一个范围小的优化，结果必须和现在一致，再留个回归测试。",
                )
            elif phase == "failure recovery":
                instruction = FAILURE_RECOVERY_REPORTS.get(
                    component.name,
                    f"再把 {component.name} 的崩溃恢复补扎实一点。找最危险的提交边界，用临时文件模拟一次中断，恢复要能重复执行，而且已经确认的数据不能丢也不能多一份。",
                )
            elif (
                phase == "change request" and component.name in BLIND_CONFLICT_REQUESTS
            ):
                instruction = BLIND_CONFLICT_REQUESTS[component.name]
            elif phase == "change request":
                instruction = CHANGE_REQUEST_REPORTS.get(
                    component.name,
                    f"给 {component.name} 加一个 v2 的批量用法吧。老调用方现在还在用，迁移要平滑；适配层、测试和简短迁移说明一起处理。",
                )
            elif component.name == "audit exporter":
                instruction = (
                    "准备收尾了，audit exporter 的批量失败边界还没钉实：补上后面的 journal 已关闭、"
                    "以及后面的 record 缺 time/id 这两个回归场景，确认整批明确报错且不会给调用方"
                    "留下可用的半批结果；如果测试揭出实现问题就做最小修复。相关测试跑通后，把命令、"
                    "结果、约束是怎么组合的以及还剩什么发布阻碍写到 release-evidence/audit.md。"
                )
            elif component.name == "UTC clock":
                instruction = (
                    "准备收尾了，UTC clock 的输入边界还没钉实：补上 lowercase z 和首尾空白这两类"
                    "非规范 timestamp 的回归场景，确认解析入口严格拒绝；再补一个带微秒、非整小时 "
                    "offset 的 canonical round-trip。若测试揭出实现问题就做最小修复。相关测试跑通后，"
                    "把命令、结果、约束是怎么组合的以及还剩什么发布阻碍写到 "
                    "release-evidence/clock.md。"
                )
            elif component.name == "release gate":
                instruction = (
                    "准备收尾了，release gate 的批量契约还差一组发布前回归：让 evaluate_many 同时"
                    "接收外层 one-shot generator 和每组 tests_run 的内层 one-shot generator，补上"
                    "空批次，并确认 reproducibility check 每批只执行一次。若测试揭出实现问题就做"
                    "最小修复。相关测试跑通后，把命令、结果、约束是怎么组合的以及还剩什么发布阻碍"
                    "写到 release-evidence/release_gate.md。"
                )
            else:
                instruction = (
                    f"准备收尾了，帮我检查一下 {component.name}，找一个真实缺陷或测试空档修掉。"
                    "相关测试跑通后，把命令、结果、约束是怎么组合的以及还剩什么发布阻碍写到 "
                    f"release-evidence/{Path(component.file).stem}.md。"
                )
            tasks.append(
                {
                    "number": number,
                    "phase": phase,
                    "component": component.name,
                    "conflict_probe": component.name in BLIND_CONFLICT_REQUESTS
                    and phase == "change request",
                    "probe_marker": PROBE_MARKERS.get(component.name)
                    if phase == "change request"
                    else None,
                    "prompt": instruction,
                }
            )
    if len(tasks) != 200:
        raise RuntimeError(f"acceptance workload must contain 200 tasks, got {len(tasks)}")
    return tasks


def initialize_project(project_root: Path) -> None:
    (project_root / "quarry").mkdir(parents=True, exist_ok=True)
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    (project_root / "quarry" / "__init__.py").write_text(
        '"""Quarry durable-workflow acceptance project."""\n', encoding="utf-8"
    )
    (project_root / "pyproject.toml").write_text(
        (
            '[project]\nname = "quarry-eternal-acceptance"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\n\n[tool.pytest.ini_options]\n'
            'testpaths = ["tests"]\naddopts = "-q"\n'
        ),
        encoding="utf-8",
    )
    (project_root / "README.md").write_text(
        "# Quarry\n\nA small durable-workflow package that is being evolved through normal development requests.\n",
        encoding="utf-8",
    )


def project_manifest(project_root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or any(
            (
                part in IGNORED_PROJECT_PARTS
                for part in path.relative_to(project_root).parts
            )
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
        (
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )
    )


def question_evidence(answer: str) -> bool:
    text = answer.strip()
    if not text:
        return False
    return bool(re.search("[?？][\\s*_`'\\\"）)\\]]*$", text))


def probe_memory_evidence(answer: str, marker: str | None) -> bool:
    return bool(marker and marker.casefold() in answer.casefold())
