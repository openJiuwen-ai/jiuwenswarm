"""Build the phase 2/3/4 scope review from preserved acceptance evidence."""
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "3fea8d1623a13646e935df9e96100ab7ad470aa4"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, encoding="utf-8").strip()


def read_log(name):
    path = OUT / name
    raw = path.read_bytes() if path.exists() else gzip.decompress((OUT / (name + ".gz")).read_bytes())
    return raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8", errors="replace")


def phase_reason(path):
    if path.startswith("artifacts/"):
        return "验收附件", "验收记录、原始数据或报告；不改变产品行为"
    if path.startswith("tests/"):
        return "回归测试", "验证阶段二至四的功能、隔离、恢复或兼容性"
    if path.startswith("scripts/benchmarks/"):
        return "验收工具", "阶段二至四的真实进程、执行入口或性能采样工具"
    if path.startswith("scripts/"):
        assert path in {"scripts/jiuwenswarm.spec", "scripts/jiuwenswarm_exe_entry.py"}, path
        return "阶段四", "冻结程序的 Worker 入口参数转发及延迟导入打包支持"
    rules = [
        ("jiuwenswarm/agents/harness/common/auto_harness/", "阶段四", "可选 Auto-Harness 延迟导入；轻量重置保持原有文件字段语义"),
        ("jiuwenswarm/agents/harness/common/plugins/rail_manager.py", "阶段三", "共享有版本指纹的 Rail 类配方；实例仍归具体 Agent"),
        ("jiuwenswarm/agents/harness/common/tools/__init__.py", "阶段三、四", "工具定义和工厂延迟解析，实际有状态工具保持独立"),
        ("jiuwenswarm/agents/swarm/__init__.py", "阶段四", "延迟加载既有工作模式的提供者，使用时保留注册行为"),
        ("jiuwenswarm/channels/desktop/", "阶段二、四", "重启保留 Worker 子树；最终退出使用显式生命周期控制权"),
        ("jiuwenswarm/channels/process_cli/machine.py", "阶段三", "单次调用 RunContext 与执行选项隔离；仍启动本地子进程 Runtime"),
        ("jiuwenswarm/channels/web/frontend/", "阶段二", "重连游标、事件去重、状态恢复、历史回退及对应错误文案"),
        ("jiuwenswarm/common/utils.py", "阶段四", "压缩 Front 日志配置解析路径，保留合法 YAML 的兼容语义"),
        ("jiuwenswarm/gateway/channel_manager/web/", "阶段二", "转发 AgentServer 的权威运行状态及事件游标，去掉本地 busy 覆盖"),
        ("jiuwenswarm/runtime/execution_telemetry.py", "阶段四", "观察完成必需准备后的真实执行入口，避免以 ACK 当执行时间"),
        ("jiuwenswarm/runtime/kernel.py", "阶段三", "资源共享键、租约、准备合并、失效及使用中资源保护"),
        ("jiuwenswarm/runtime/project_lsp.py", "阶段三、四", "项目范围的 SDK LSP 资源租约与关闭"),
        ("jiuwenswarm/runtime/run_context.py", "阶段三", "不可变的单次预算、权限、工具和凭据上下文"),
        ("jiuwenswarm/runtime/service.py", "阶段二", "读取既有 Coordinator 的权威执行快照"),
        ("jiuwenswarm/server/agent_ws_server.py", "阶段二、四", "实际交付确认、内部跨会话执行归属，以及可选服务后台准备"),
        ("jiuwenswarm/server/control/repositories/session_repository.py", "阶段二", "Front 的颜色更新改为受锁保护的字段补丁"),
        ("jiuwenswarm/server/front/", "阶段二", "调用方请求去重、对象身份清理、warming 取消和可选运行快照"),
        ("jiuwenswarm/server/ipc/", "阶段二、四", "ACK/订阅协议、进程租约、连接清理及 Windows 管道存活检查"),
        ("jiuwenswarm/server/runtime/agent_adapter/", "阶段三、四", "共享模型/MCP 资源，独立运行上下文，Kernel 准备和执行入口观测"),
        ("jiuwenswarm/server/runtime/agent_warm_pool.py", "阶段三", "后台预构建优先准备 Kernel，保留真实 session claim/pin 路径"),
        ("jiuwenswarm/server/runtime/session/", "阶段二、四", "metadata 字段合并、缓存与生命周期锁；缩短 Windows 锁竞争等待"),
        ("jiuwenswarm/server/runtime/skill/skill_manager.py", "阶段三", "共享 Skill 元数据索引；向调用方返回独立可变快照"),
        ("jiuwenswarm/server/supervisor/", "阶段二、四", "Worker 选主/身份/重连、独立 dispatch、交付 ACK 及统一关闭期限"),
        ("jiuwenswarm/server/worker/", "阶段二、四", "执行归属、事件日志、订阅恢复、排队取消、准备编排及最终关闭"),
    ]
    for prefix, phase, reason in rules:
        if path.startswith(prefix):
            return phase, reason
    raise AssertionError(f"Unclassified change: {path}")


def main():
    revision = git("log", "-1", "--format=%H", "--", "jiuwenswarm", "scripts", "tests")
    changed = git("diff", "--name-only", BASE, revision).splitlines()
    scope = [{"file": p, "phase": phase_reason(p)[0], "reason": phase_reason(p)[1]} for p in changed]
    product = [r for r in scope if r["file"].startswith("jiuwenswarm/") or r["file"] in {"scripts/jiuwenswarm.spec", "scripts/jiuwenswarm_exe_entry.py"}]
    support = [r for r in scope if r["phase"] in {"回归测试", "验收工具"}]
    with (OUT / "scope-by-file.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["file", "phase", "reason"])
        writer.writeheader()
        writer.writerows(product + support)

    selections = [
        ("Process CLI / Python SDK / RunContext", "process-cli-sdk-final.log", "tests/unit_tests/process_cli + sdks/python/tests + runtime/test_run_context.py"),
        ("AgentServer / Runtime / Server / Windows 锁", "runtime-front-session-final.log", "tests/unit_tests/agentserver + runtime + server + test_windows_resource_lock_latency.py"),
        ("Gateway / E2A / AgentOS / Desktop", "gateway-agentos-desktop-final.log", "gateway + e2a + test_agentos*.py + test_desktop*.py"),
        ("Agent / RSI / Auto-Harness / PersonalContext / Rail", "agents-harness-skills-final.log", "agents + rsi + auto_harness + personal_context + test_rail_manager_session_scope.py"),
        ("模型配置 / 登录凭据 / MCP 内置 / Skill 环境", "model-auth-mcp-final.log", "model/auth 配置回归和 Skill 环境用例，见保留日志中的 collected 列表"),
        ("本轮修正与相关 Worker/Front/Desktop", "scoped-fixes-final4.log", "Worker shutdown/residency/phase2、Front 模块边界、logging/harness state、Electron 契约"),
        ("外部真实模型 SDK 续聊 + 真实 SDK LSP", "real-model-sdk-lsp.log", "test_custom_agent_resume_binding_and_busy + test_issue4689_project_lsp.py"),
    ]
    runs = []
    failures = set()
    for label, filename, selection in selections:
        content = read_log(filename)
        summaries = [line.strip("= \r\n") for line in content.splitlines() if re.match(r"^=+ .*\d+ (?:passed|failed)", line)]
        if not summaries:
            raise RuntimeError(f"Test has not completed: {filename}")
        nodes = sorted(set(re.findall(r"^FAILED (tests/\S+)", content, re.M)))
        failures.update(nodes)
        runs.append({"suite": label, "log": filename, "selection": selection, "summary": summaries[-1], "failures": nodes})
    baseline_nodes = set(re.findall(r"^FAILED (tests/\S+)", read_log("baseline-all-current-failures.log"), re.M))
    candidate_only = sorted(failures - baseline_nodes)
    assert not candidate_only, candidate_only

    web_runs = []
    for name in ["web-client-runtime-ack", "session-event-gate", "ws-event-dedup", "chat-store-streaming", "history-pagination", "history-record-reassembler", "cross-session-message", "i18n-locales"]:
        filename = f"web-test-{name}.log"
        text = read_log(filename)
        passed = int(re.search(r"(?:#|ℹ) pass (\d+)", text)[1])
        failed = int(re.search(r"(?:#|ℹ) fail (\d+)", text)[1])
        assert failed == 0
        web_runs.append({"script": f"test:{name}", "log": filename, "passed": passed})
    build = read_log("web-build.log")
    assert "built in" in build
    lint = json.loads((OUT / "ruff-baseline-comparison.json").read_text(encoding="utf-8"))
    assert lint["candidate_only"] == []
    lock_paths = ["pyproject.toml", "uv.lock", "jiuwenswarm/channels/web/frontend/package.json", "jiuwenswarm/channels/web/frontend/package-lock.json"]
    assert not git("diff", "--name-only", BASE, revision, "--", *lock_paths)
    lock_hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in lock_paths}

    native_names = ["native-gateway-tool-final.json", "native-gateway-interaction-final.json", "native-offline.json", "native-cancel.json", "native-queue-caller-final.json", "native-steer-caller-final.json", "native-cross-session-accepted-final.json", "native-mcp.json", "native-failed-create.json", "native-warming-cancel-accepted-final.json", "native-crash.json", "native-agentos-directory-a.json", "native-agentos-directory-b.json", "native-metadata-concurrency.json"]
    native = []
    for filename in native_names:
        item = json.loads((OUT / filename).read_text(encoding="utf-8"))
        if item.get("scenario") != "crash" and item.get("scenario"):
            assert item["same_worker"] and item["same_instance"]
        if item.get("scenario") in {"tool", "mcp"}:
            assert item["side_effects"] == ["once"]
        if item.get("scenario"):
            assert item["shutdown"]["ok"] is True
        if item.get("scenario") == "cross-session":
            assert item["same_target_execution"] and item["target_history_complete"]
        native.append({"file": filename, **item})
    agentos = [i for i in native if i.get("profile") == "agentos"]
    assert len(agentos) == 2 and agentos[0]["data_dir"] != agentos[1]["data_dir"]
    assert agentos[0]["before"]["worker_instance"] != agentos[1]["before"]["worker_instance"]

    requirements = [
        ("二", "断连只解除订阅，Worker 持有原执行", "覆盖：retire_generation/detach 不取消已接收执行", "WorkerDispatcher、WorkerGatewayBridge；真实 offline/tool/interaction/cross-session"),
        ("二", "序号回放、状态快照与待答交互；不重发输入", "覆盖：有界 2048 条 / 8 MiB 事件日志，游标过期走历史", "event_store.py、runtimeResume.ts；回放边界单测、真实问答恢复及 once 工具计数"),
        ("二", "实际 Front→Gateway 交付后才提交", "覆盖：wire.ack；失败/超时 abort", "gateway_bridge.py、runtime_supervisor.py、session provision；真实 failed-create + 正常 SDK 建会话"),
        ("二", "独立 dispatch_id；调用方活动 request_id 去重", "本轮补齐：去重跨同一调用方的会话生效；清理校验对象身份", "request_registry.py、event_store.py；跨会话去重与旧对象清理测试"),
        ("二", "接收、处理返回和执行完成区分；warming 可取消", "本轮补齐：空 Coordinator 快照不覆盖已接收队列状态", "dispatcher.py；修正前复现 false，修正后测试通过；真实 warming-cancel 模型调用为零"),
        ("二", "metadata 字段合并、锁与事务/租约保留", "覆盖：写前持锁读新值，按字段合并并管理缓存", "session_metadata.py、lifecycle/project_store；真实两进程 64 轮并发 + session 回归"),
        ("二", "规范化数据目录/profile 选主，AgentOS user_id 不选目录", "覆盖：数据作用域、实例、协议、代码身份及 lifetime/startup locks", "identity.py、IPC server、supervisor；两 AgentOS 目录同 session/request ID 的独立进程证据"),
        ("二", "普通重启 detach；最终关闭共享 30 秒期限", "本轮补齐：Supervisor ACK、进程退出/强制回收、IPC 清理共用 deadline", "shutdown deadline 测试含真实子进程和 PID 重用拒绝；各真实进程场景 shutdown.done ok"),
        ("二", "对外契约保持；Web 自动恢复，Gateway 转发权威状态", "覆盖：既有 session.get_metadata 的可选参数/字段及内部订阅", "Gateway 转发 + Web 46 项测试和构建；真实 Gateway WS 接回；浏览器点击流程未实测"),
        ("三", "Kernel 管理可共享资源，租约保护生命周期", "覆盖：模型、工具工厂、Rail 配方、Skill 索引、MCP、项目 LSP", "kernel.py、适配器、Rail/Skill 管理器、project_lsp.py；资源与相关完整回归"),
        ("三", "SessionExecution 与 RunContext 独立", "按既有结构落地：复用 RuntimeSessionCoordinator/handles，不新建同名 SessionExecution 类", "history/checkpoint/活动执行仍归会话；RunContext 保存单次 turn/budget/tools/permissions/credential；Process CLI 805 项"),
        ("三", "共享键覆盖资源身份；凭据不明文进入键/日志", "覆盖：可信 data/profile、project、mode、definition/capabilities/config、资源 ID/路由及 HMAC 引用", "KernelIdentity/ResourceKey；ModelGroup 的有状态路由包装仍独立，单模型连接可租用"),
        ("三", "有状态 Rail/工具及 Skill/MCP 快照保持归属", "覆盖：共享类/工厂与索引；实际实例和会话选择独立", "Rail scope、Skill、Agent、MCP 回归；MCP 实际子进程复用，PowerShell/MCP 各只产生 once"),
        ("三", "配置失效保护执行；warm pool 转为 Kernel 准备", "覆盖：失效资源退休，有租约时延迟关闭；真实 claim/pin 路径保留", "ResourceKernel retire/release、adapter hot reload、agent_warm_pool；模型配置/凭据回归"),
        ("四", "Front/控制面不依赖 Runtime 重导入", "覆盖：真实 Front 子进程中运行控制查询并检查已加载模块", "test_front_import_boundary.py 的完整模块列表；Worker warming 下仍可查询"),
        ("四", "按配置迁移、模型身份/路由的依赖顺序准备", "覆盖：迁移在模型和会话构造前，AGENT_READY 后释放执行队列", "worker/lifecycle.py 和 dispatcher；真实首次执行、warming cancel、启动失败清理回归"),
        ("四", "可选能力后台/首次使用，调用等待对应准备", "覆盖主要代码路径；未逐一实测所有可选能力开启/异常组合", "MCP 首用、Skill/Auto-Harness 延迟导入、PersonalContext/Proactive/Sandbox 后台准备，图像/Zen 既有后台复用"),
        ("四", "先接现有 Worker，复用模型/MCP/认证/LSP/KVCache", "覆盖：优先 try_connect，Worker 不重新加载 Runtime", "同 PID/instance/execution 的真实重启；实际 MCP PID 相同；真实 LSP 共享/隔离；既有 KVCache owner 继续使用"),
        ("四", "重连、崩溃、升级区分；不自动重放工具", "覆盖重连/崩溃；升级身份/协议不符明确拒绝并要求显式生命周期处理", "crash 记录 interrupted、换实例；工具 once；老协议/安装包升级未做端到端实测"),
        ("四", "冻结参数和 Windows 存活检查兼容", "代码/契约回归覆盖；Windows 源码真实进程覆盖", "exe entry/spec、named pipe/owner lease；冻结 EXE 未实际构建验证"),
        ("边界", "不新增多 Worker 调度/WarmSpare；维持 CLI/TUI 通路", "保持：按数据目录隔离进程不等于新调度器", "Process CLI 仍本地子进程 Runtime，远程 TUI 的 Gateway /tui 通路未改动"),
    ]
    fixes = [
        ("阶段二", "Supervisor 关闭预算累加、kill 后存在无期限 wait", "改为单个绝对 deadline；超时记录未完成的 ACK/退出/IPC 清理步骤，PID 身份不符拒绝 kill"),
        ("阶段二", "同一调用方改换会话可以绕过活动 request_id 去重", "去重使用调用方作用域；事件日志和订阅仍按会话查询，旧清理不能移除新对象"),
        ("阶段二", "warming 的空执行快照会将已接收队列误报未处理", "同时保留 Worker 接收状态与 Coordinator 的真实执行状态；有修正前失败证据"),
        ("阶段四", "合法的行内 YAML logging 配置未被识别", "无独立 logging 段时安全解析整个 YAML；保留常规 block 配置的快速路径"),
        ("阶段四", "轻量 Auto-Harness 重置给未激活包新增 is_active 字段", "仅修改已有的活动标记，保持原文件结构及重置幂等性"),
        ("验收工具", "跨会话工具执行 ID 的首次采样过早", "由 Coordinator 的权威执行记录读取重启前 ID；保留误判日志/诊断 JSON，连续真实运行通过"),
    ]
    manifest = json.loads((ROOT / "artifacts/4689/acceptance-manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((ROOT / "artifacts/4689/acceptance-results.json").read_text(encoding="utf-8"))
    report = {"generated_at": datetime.now().astimezone().isoformat(), "baseline": BASE,
        "reviewed_code_commit": revision, "original_review_head": "9fd2d889f8283f44982f5a724100e43fb67c80aa",
        "scope_conclusion": "No out-of-plan business changes found; code confined to phases 2/3/4 plus their tests and evidence",
        "product_files": len(product), "test_and_benchmark_files": len(support), "scope": product + support,
        "repairs": fixes, "requirements": requirements, "test_runs": runs, "web_runs": web_runs,
        "current_unique_failures": len(failures), "baseline_selected_failure_count": len(baseline_nodes),
        "candidate_only_failures": candidate_only, "lint": lint, "dependency_files_unchanged": True,
        "dependency_sha256": lock_hashes, "native_evidence": [{k: i.get(k) for k in ("file", "scenario", "profile", "data_dir", "worker_pid", "same_worker", "same_instance", "same_target_execution", "model_calls", "main_model_calls", "side_effects", "shutdown", "shutdown_ms", "all_fields_preserved")} for i in native],
        "performance_provenance": {"code_commit": manifest["code_under_final_measurement"], "rerun_in_this_review": False, "metrics": metrics},
        "limitations": ["冷 Worker P95 约 8 秒，未达到 2.5 秒；依赖锁定未改", "性能正式采样属于此前 fc7c36c97；本轮为功能复核，没有重采 30/100 次 P95", "88 个初轮失败在 develop 逐项复现；最终回归失败为其中子集，未清理无关基线缺陷", "浏览器页面点击、冻结 EXE 及老协议安装升级未实测", "工具副作用和 MCP 恢复使用真实 SDK/子进程及本地模型协议 fixture；外部真实模型续聊单独报告"]}
    (OUT / "review-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    esc = html.escape

    def table(headers, rows):
        return '<div class="table"><table><thead><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'

    def link(filename, label=None):
        if filename.endswith(".log") and (OUT / (filename + ".gz")).exists():
            filename += ".gz"
        return f'<a href="{esc(filename, quote=True)}">{esc(label or filename)}</a>'

    requirements_table = table(["阶段", "计划要求", "复核结果", "实现与验证证据"], [[esc(c) for c in r] for r in requirements])
    test_table = table(["测试范围", "本轮最终结果", "证据"], [[esc(r["suite"]), esc(r["summary"]), link(r["log"], "完整日志")] for r in runs])
    web_table = table(["Web 测试", "通过", "证据"], [[esc(r["script"]), str(r["passed"]), link(r["log"], "日志")] for r in web_runs])
    fix_table = table(["范围", "发现的问题", "修正"], [[esc(c) for c in r] for r in fixes])
    file_table = table(["文件", "归属", "目的"], [[link("../../../" + r["file"], r["file"]), esc(r["phase"]), esc(r["reason"])] for r in product + support])
    native_rows = []
    for item in native:
        scenario = item.get("scenario") or "metadata 并发"
        if item.get("profile") == "agentos":
            scenario += " / AgentOS " + ("A" if "-a.json" in item["file"] else "B")
        result = "字段完整保留；两个真实进程各写 64 轮" if not item.get("scenario") else "Worker PID/实例保持，最终关闭成功"
        if scenario == "crash":
            result = "旧执行中断；新 Worker 实例；不自动重发原输入"
        if item.get("same_target_execution"):
            result += "；目标执行 ID 保持，历史完整"
        if item.get("scenario") in {"tool", "mcp"}:
            result += "；副作用严格一次"
        if item.get("scenario") == "warming-cancel":
            result += "；主模型调用为零"
        native_rows.append([esc(scenario), str(item.get("worker_pid") or "—"), esc(result), link(item["file"], "原始 JSON")])
    native_table = table(["真实进程场景", "Worker PID", "结果", "证据"], native_rows)
    performance_rows = []
    for mode, data in metrics.items():
        c = data["candidate"]
        control = max(c[name]["p95_ms"] for name in ("session.list", "project.list", "config.get", "history.get"))
        performance_rows.append([esc(mode), f'{c["listen"]["p95_ms"]:.2f} ms', f'{control:.2f} ms', f'{c["warm"]["p95_ms"]:.2f} ms', f'<strong class="fail">{c["cold"]["p95_ms"]:.2f} ms · 未达标</strong>'])
    performance_table = table(["模式", "Front ≤300 ms", "控制面 ≤500 ms", "已有 Worker ≤500 ms", "冷 Worker ≤2500 ms"], performance_rows)
    text = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Issue 4689 阶段二三四：范围审查与功能回归</title><style>
:root{{color-scheme:light;--ink:#172d43;--sub:#516377;--line:#d7e1ea;--blue:#155ca8}}
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fa;color:var(--ink);font:15px/1.7 "Microsoft YaHei","Segoe UI",sans-serif}}
header{{background:#163955;color:white;padding:40px max(24px,calc((100vw - 1220px)/2));}}h1{{font-size:28px;margin:4px 0 14px;line-height:1.4}}header p{{margin:4px 0;color:#d7e9f7}}main{{max-width:1220px;margin:28px auto;padding:0 20px}}section{{background:white;padding:26px 28px;margin:22px 0;border:1px solid var(--line);border-radius:12px}}h2{{font-size:21px;margin:0 0 18px}}h3{{font-size:17px;margin:22px 0 8px}}p{{margin:10px 0}}a{{color:var(--blue);text-underline-offset:3px}}code{{font:13px/1.6 Consolas,monospace;overflow-wrap:anywhere}}.intro{{border-left:5px solid #208766}}.muted{{color:var(--sub)}}.note{{background:#fff5df;border-left:4px solid #c98b17;padding:14px 18px}}.table{{overflow:auto;margin:14px 0}}table{{border-collapse:collapse;width:100%;min-width:680px;text-align:left}}th{{background:#edf3f8;font-weight:600}}th,td{{padding:12px;border:1px solid var(--line);vertical-align:top}}tbody tr:nth-child(even){{background:#fbfcfe}}td:first-child{{overflow-wrap:anywhere}}.fail{{color:#a52f2f}}.badge{{display:inline-block;background:#e4f5ed;color:#155e44;padding:4px 10px;border-radius:5px;font-weight:600}}ul{{padding-left:22px}}nav a{{display:inline-block;margin:6px 14px 0 0}}footer{{text-align:center;color:var(--sub);padding:20px}}@media(max-width:700px){{header{{padding:24px}}section{{padding:20px 16px}}h1{{font-size:23px}}main{{padding:0 12px}}}}
</style></head><body><header><p>JiuwenSwarm · Issue #4689 · Windows 源码形态</p><h1>阶段二、三、四：范围审查与功能回归</h1><p>基线 develop <code>{BASE}</code></p><p>本轮修正代码 <code>{revision}</code></p></header><main>
<section class="intro"><h2>复核结论</h2><span class="badge">未发现阶段二至四之外的业务改动</span><p>对原始 develop 与当前提交的文件差异、关键执行/资源/生命周期调用链以及受影响运行面进行复核。产品改动共 {len(product)} 个文件，均可对应已批准的阶段二至四要求；另有 {len(support)} 个测试与验收工具文件。逐文件目的见下方清单。</p><p>本轮发现并修正 5 处计划内实现/兼容缺口；另外修正了 1 处验收脚本的采样误判。依赖版本、锁文件和前端依赖均保持原样，未改 agent-core/OpenJiuwen，未增加新调度器或 WarmSpare。阶段一沿用既有实现。</p><p>最终回归没有候选版本独有失败；{len(failures)} 个当前失败均属于已在原始 develop 逐项复现的 88 个基线失败。该结论限于本报告列出的环境和验证范围，不等于整个项目所有用例均为绿色。</p><p class="note">冷 Worker 的此前正式 P95 约 8 秒，仍不满足 2.5 秒要求。遵照约定记录未达标，不扩展到依赖仓优化。本轮没有重新采集正式性能 P95。</p><nav><a href="#scope">逐项计划</a><a href="#fixes">本轮修正</a><a href="#tests">回归结果</a><a href="#native">真实进程</a><a href="#files">逐文件范围</a><a href="#limits">验收限制</a></nav></section>
<section id="scope"><h2>与阶段二、三、四计划逐项对应</h2><p class="muted">“覆盖”表示存在实现路径并有相应审查/测试证据；未实测的安装包、浏览器及可选能力组合单独标明。SessionExecution 采用现有 Coordinator/handles 的逻辑边界，未为对齐名称另造一套执行框架。</p>{requirements_table}</section>
<section id="fixes"><h2>本轮发现并收束的问题</h2>{fix_table}<p>关闭、去重和 warming 状态属于阶段二的可靠性要求；YAML 与字段结构兼容属于阶段四将启动逻辑变轻后必须保持的正常行为。修正没有延伸到与该方案无关的权限、市场发布、Shell Registry 或路径处理缺陷。</p><p>跨会话诊断保留了 {link('native-cross-session-diagnostic.json','首次空 journal ID 的原始快照')}：前后 Coordinator ID 一致。测试现在读取真实执行对象的 ID，不将尚未发出首条流事件的日志空字段当作执行更换。{link('warming-snapshot-before-fix.log','warming 状态修正前的失败证据')} 也予以保留。</p></section>
<section id="tests"><h2>本轮回归结果</h2>{test_table}<p>完整 Runtime/Gateway/Agent 回归在本轮修正提交上再次运行；Process CLI/SDK 在修正后也再次完整运行。专项复测与完整套件存在重复，表中次数不累加为“不同测试总数”。{link('baseline-comparison.json','初轮 88 个失败与 develop 对照')}、{link('baseline-all-current-failures.log','基线逐项复现日志')}。</p><p class="muted">日志链接提供保留原始编码和字节的 GZIP 压缩版；交付 ZIP 同时附有原样 .log 文件，解压后即可查阅。</p><p>Ruff 扫描受影响 Python 文件：候选 77 项、基线 77 项，按文件/规则/消息比对未新增问题；本轮直接修正的文件专项 Ruff 无问题。未清理无关基线告警。{link('ruff-baseline-comparison.json','静态检查对照证据')}。</p><h3>Web 正常事件与历史流程</h3>{web_table}<p>合计 {sum(r['passed'] for r in web_runs)} 项通过；TypeScript 检查及 Vite 生产构建通过。{link('web-build.log','构建完整日志')}。大型 chunk 告警仍为既有构建提示，未为消除告警改动其他页面或依赖。</p></section>
<section id="native"><h2>真实进程与实际资源验证</h2><p>以下场景启动真实 Python Front/Worker，两个 Gateway 场景还启动真实 Gateway，并从 WebSocket 进入实际 Runtime/SDK。取消、队列、steer、跨会话消息、历史与最终关闭均由真实组件执行。</p>{native_table}<p>工具场景触发实际 PowerShell 写文件以及实际 stdio MCP 子进程；两者副作用均为一次。外部真实模型通过本地已有配置完成 SDK 首轮、并发拒绝及另一子进程续聊；项目 LSP 测试启动实际 SDK 语言服务器，验证复用和项目隔离。</p><p class="muted">工具与恢复场景使用本地 OpenAI 协议模型 fixture 以稳定控制暂停点，不能当成外部真实模型与所有工具的联合验收。真实模型续聊和真实 LSP 的结果见独立日志。两个 AgentOS 目录分别运行完整恢复场景，使用相同 session/request ID，数据目录与 Worker 实例互不相同。</p></section>
<section><h2>六项目标的性能与功能验收状态</h2><p>以下性能数字引用此前正式采样，测量代码为 <code>{manifest['code_under_final_measurement']}</code>，本轮功能修正提交为 <code>{revision}</code>。两者不是同一提交，本轮不把旧数字包装为当前提交的重新实测。</p>{performance_table}<p>两种主要模式各版本各采集 30 次 Front 冷启动、30 次冷 Worker 和 100 次已有 Worker 请求；采用相同锁定依赖、模型配置及代表性工作区，父进程启动计时，chat 结束点是完成准备后的 SDK 实际执行入口。未清 OS 文件缓存，使用 nearest-rank P95。{link('../acceptance-manifest.json','环境与采样条件')}、{link('../issue4689-acceptance.html','此前完整验收报告')}。</p><p>“Gateway 不生成替代业务响应”“Front 不加载重依赖”“Worker 长驻并重启接回”三项目标，本轮分别通过 Gateway/错误链路回归、真实子进程模块检查和执行中真实重启/恢复验证。冷 Worker 仍未达标，保留原锁定依赖。</p></section>
<section id="files"><h2>逐文件范围清单</h2><p>该表列出相对基线的产品、测试及验收工具文件，不将 HTML/JSON/日志等交付附件视为业务功能。Web 客户端修改文件中包含按项目约定执行的格式化；未新增与重连无关的页面功能。Windows 文件锁短轮询服务于 metadata 的跨进程正确性及启动/请求时延要求。</p>{file_table}<p>{link('scope-by-file.csv','下载逐文件 CSV')} · {link('review-results.json','下载机器可读复核结果')}</p></section>
<section id="limits"><h2>剩余限制与复测边界</h2><ul>{''.join('<li>'+esc(v)+'</li>' for v in report['limitations'])}</ul><p>未发现计划外业务扩展，但不能声称“任何正常功能绝不会受影响”。当前可确认的是：本报告覆盖的完整回归未新增独有失败，受影响关键功能通过了真实进程、真实 SDK/资源和分开的真实模型测试。保留现有工作区及早期分析文档，修正和证据在独立分支提交；未推送或发布 PR。</p></section>
</main><footer>离线 HTML · {esc(report['generated_at'])} · 范围固定为 Issue #4689 阶段二至四</footer></body></html>'''
    (OUT / "issue4689-scope-and-regression-review.html").write_text(text, encoding="utf-8")
    print(json.dumps({"code_commit": revision, "product_files": len(product), "support_files": len(support), "current_failures": len(failures), "candidate_only": candidate_only, "web_passed": sum(r['passed'] for r in web_runs)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
